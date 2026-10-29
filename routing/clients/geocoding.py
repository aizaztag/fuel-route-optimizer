import hashlib
import logging
import threading
import time
from time import perf_counter
from collections.abc import Callable
from typing import Any

import httpx
from django.conf import settings
from django.core.cache import cache

from .exceptions import AddressNotFound, LocationNotInUSA, UpstreamError, UpstreamTimeout

_nominatim_lock = threading.Lock()
_last_nominatim_request_at = 0.0
logger = logging.getLogger(__name__)


class NominatimClient:
    """Nominatim client with successful-result caching and serialized request pacing."""

    def __init__(
        self,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or time.sleep
        self.lookup_events: list[dict[str, Any]] = []
        self._active_cache_status = 'miss'
        self._last_http_status: int | None = None
        self._last_http_attempted = False
        self._last_external_duration_ms: float | None = None
        self._active_failure_category: str | None = None
        self._active_failure_source: str | None = None

    def _classify_failure(self, exc: Exception) -> tuple[str, str]:
        if self._active_failure_category:
            return self._active_failure_category, self._active_failure_source or 'response'
        if isinstance(exc, AddressNotFound):
            return 'empty_result', 'lookup_result'
        if isinstance(exc, LocationNotInUSA):
            return 'non_us', 'lookup_result'
        if isinstance(exc, UpstreamTimeout):
            return 'timeout', 'transport'
        if isinstance(exc, UpstreamError):
            return exc.failure_category, exc.failure_source
        return 'upstream_error', 'response'

    def _track_lookup(self, operation: str, callback: Callable[..., Any], *args: Any) -> Any:
        started = perf_counter()
        self._active_cache_status = 'miss'
        self._last_http_status = None
        self._last_http_attempted = False
        self._last_external_duration_ms = None
        self._active_failure_category = None
        self._active_failure_source = None
        failed = False
        failure_category = None
        failure_source = None
        try:
            return callback(*args)
        except Exception as exc:
            failed = True
            failure_category, failure_source = self._classify_failure(exc)
            raise
        finally:
            event = {
                'operation': operation,
                'cache_status': self._active_cache_status,
                'duration_ms': round((perf_counter() - started) * 1000, 3),
                'http_status': self._last_http_status,
                'outbound_http_request': self._last_http_attempted,
                'external_duration_ms': self._last_external_duration_ms,
                'failed': failed,
                'failure_category': failure_category,
                'failure_source': failure_source,
            }
            self.lookup_events.append(event)
            if failed:
                logger.warning(
                    'Geocoding lookup failed: provider=nominatim operation=%s cache=%s '
                    'duration_ms=%.3f http_status=%s category=%s source=%s',
                    operation, event['cache_status'], event['duration_ms'], event['http_status'],
                    failure_category, failure_source,
                )

    def _request_json(self, url: str, params: dict[str, Any], operation: str) -> Any:
        global _last_nominatim_request_at

        http_started = None
        status_code = None
        try:
            # The lock spans pacing and the bounded HTTP call, preventing concurrent requests.
            with _nominatim_lock:
                now = self._monotonic()
                minimum_interval = max(1.0, settings.NOMINATIM_MIN_INTERVAL_SECONDS)
                wait_seconds = minimum_interval - (
                    now - _last_nominatim_request_at
                )
                if _last_nominatim_request_at and wait_seconds > 0:
                    self._sleep(wait_seconds)
                _last_nominatim_request_at = self._monotonic()
                http_started = perf_counter()
                self._last_http_attempted = True
                response = httpx.get(
                    url,
                    params=params,
                    headers={'User-Agent': settings.NOMINATIM_USER_AGENT},
                    timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
                )
                status_code = response.status_code
                self._last_http_status = status_code
                response.raise_for_status()
                payload = response.json()
            duration_ms = (perf_counter() - http_started) * 1000
            self._last_external_duration_ms = round(duration_ms, 3)
            logger.info(
                'External geocoding request completed: provider=nominatim operation=%s '
                'duration_ms=%.3f http_status=%s failed=false',
                operation, duration_ms, status_code,
            )
            return payload
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            category = 'rate_limited' if status_code == 429 else 'http_error'
            self._active_failure_category = category
            self._active_failure_source = 'http'
            duration_ms = (perf_counter() - http_started) * 1000 if http_started else 0.0
            self._last_external_duration_ms = round(duration_ms, 3) if http_started else None
            logger.warning(
                'External geocoding request failed: provider=nominatim operation=%s '
                'duration_ms=%.3f http_status=%s failure=%s',
                operation, duration_ms, status_code, category,
            )
            raise UpstreamError(
                'The address verification service is unavailable.',
                failure_category=category,
                failure_source='http',
                http_status=status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            self._active_failure_category = 'timeout'
            self._active_failure_source = 'transport'
            duration_ms = (perf_counter() - http_started) * 1000 if http_started else 0.0
            self._last_external_duration_ms = round(duration_ms, 3) if http_started else None
            logger.warning(
                'External geocoding request failed: provider=nominatim operation=%s '
                'duration_ms=%.3f http_status=%s failure=timeout',
                operation, duration_ms, status_code,
            )
            raise UpstreamTimeout(
                'The address verification service timed out.',
                failure_category='timeout',
                failure_source='transport',
            ) from exc
        except httpx.ConnectError as exc:
            self._active_failure_category = 'connection_error'
            self._active_failure_source = 'transport'
            duration_ms = (perf_counter() - http_started) * 1000 if http_started else 0.0
            self._last_external_duration_ms = round(duration_ms, 3) if http_started else None
            logger.warning(
                'External geocoding request failed: provider=nominatim operation=%s '
                'duration_ms=%.3f http_status=%s failure=transport_error',
                operation, duration_ms, status_code,
            )
            raise UpstreamError(
                'The address verification service is unavailable.',
                failure_category='connection_error',
                failure_source='transport',
            ) from exc
        except ValueError as exc:
            self._active_failure_category = 'invalid_response'
            self._active_failure_source = 'response'
            duration_ms = (perf_counter() - http_started) * 1000 if http_started else 0.0
            self._last_external_duration_ms = round(duration_ms, 3) if http_started else None
            logger.warning(
                'External geocoding request failed: provider=nominatim operation=%s '
                'duration_ms=%.3f http_status=%s failure=invalid_response',
                operation, duration_ms, status_code,
            )
            raise UpstreamError(
                'The address verification service returned an invalid response.',
                failure_category='invalid_response',
                failure_source='response',
            ) from exc
        except httpx.HTTPError as exc:
            self._active_failure_category = 'transport_error'
            self._active_failure_source = 'transport'
            duration_ms = (perf_counter() - http_started) * 1000 if http_started else 0.0
            self._last_external_duration_ms = round(duration_ms, 3) if http_started else None
            logger.warning(
                'External geocoding request failed: provider=nominatim operation=%s '
                'duration_ms=%.3f http_status=%s failure=connection_error',
                operation, duration_ms, status_code,
            )
            raise UpstreamError(
                'The address verification service is unavailable.',
                failure_category='transport_error',
                failure_source='transport',
            ) from exc

    @staticmethod
    def _key(prefix: str, value: str) -> str:
        digest = hashlib.sha256(value.strip().casefold().encode('utf-8')).hexdigest()
        return f'fuel-route:{prefix}:{digest}'

    @staticmethod
    def _service_key(prefix: str, endpoint: str, value: str) -> str:
        material = f'{endpoint}\0{value.strip().casefold()}'
        digest = hashlib.sha256(material.encode('utf-8')).hexdigest()
        return f'fuel-route:{prefix}:{digest}'

    @staticmethod
    def _raise_cached_failure(cached: Any) -> None:
        if not isinstance(cached, dict):
            return
        failure = cached.get('_geocoding_failure')
        if failure == 'not_found':
            raise AddressNotFound('No matching US location was found for this address.')
        if failure == 'outside_us':
            raise LocationNotInUSA('The location could not be verified as being in the United States.')

    @staticmethod
    def _state_code(address: dict[str, Any]) -> str | None:
        iso_code = address.get('ISO3166-2-lvl4') or address.get('ISO3166-2-lvl6')
        if isinstance(iso_code, str) and iso_code.upper().startswith('US-'):
            return iso_code.rsplit('-', 1)[-1].upper()
        state = address.get('state')
        if isinstance(state, str):
            normalized = ' '.join(state.strip().casefold().split())
            if len(normalized) == 2 and normalized.isalpha():
                return normalized.upper()
            return US_STATE_CODES.get(normalized)
        return None

    @classmethod
    def _place_from_result(
        cls,
        result: dict[str, Any],
        coordinates: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        address = result.get('address')
        if not isinstance(address, dict) or not isinstance(address.get('country_code'), str):
            raise UpstreamError(
                'The address verification service returned an invalid response.',
                failure_category='invalid_response', failure_source='response',
            )
        if address['country_code'].lower() != 'us':
            raise LocationNotInUSA('The location could not be verified as being in the United States.')
        if coordinates is None:
            try:
                latitude = float(result['lat'])
                longitude = float(result['lon'])
            except (KeyError, TypeError, ValueError) as exc:
                raise UpstreamError(
                    'The address verification service returned invalid coordinates.',
                    failure_category='invalid_response', failure_source='response',
                ) from exc
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise UpstreamError(
                    'The address verification service returned invalid coordinates.',
                    failure_category='invalid_response', failure_source='response',
                )
            coordinates = {'latitude': latitude, 'longitude': longitude}

        city = next((address.get(key) for key in ('city', 'town', 'village', 'hamlet', 'municipality') if address.get(key)), None)
        state = address.get('state')
        return {
            'coordinates': coordinates,
            'city': city.strip() if isinstance(city, str) else None,
            'state': state.strip() if isinstance(state, str) else None,
            'state_code': cls._state_code(address),
        }

    def geocode_us_place(self, address: str) -> dict[str, Any]:
        return self._track_lookup('forward_geocoding', self._geocode_us_place, address)

    def _geocode_us_place(self, address: str) -> dict[str, Any]:
        key = self._service_key('forward-geocode', settings.NOMINATIM_SEARCH_URL, address)
        cached = cache.get(key)
        if cached is not None:
            self._active_cache_status = 'hit'
            self._raise_cached_failure(cached)
            return cached

        results = self._request_json(
            settings.NOMINATIM_SEARCH_URL,
            {
                'q': address,
                'format': 'jsonv2',
                'addressdetails': 1,
                'countrycodes': 'us',
                'limit': 1,
            },
            operation='forward_geocoding',
        )
        if not isinstance(results, list):
            raise UpstreamError(
                'The address verification service returned an invalid result.',
                failure_category='invalid_response', failure_source='response',
            )
        if not results:
            cache.set(
                key,
                {'_geocoding_failure': 'not_found'},
                settings.GEOCODING_FAILURE_CACHE_TTL,
            )
            raise AddressNotFound('No matching US location was found for this address.')
        result = results[0]
        if not isinstance(result, dict):
            raise UpstreamError(
                'The address verification service returned an invalid result.',
                failure_category='invalid_response', failure_source='response',
            )
        try:
            place = self._place_from_result(result)
        except LocationNotInUSA:
            cache.set(
                key,
                {'_geocoding_failure': 'outside_us'},
                settings.GEOCODING_FAILURE_CACHE_TTL,
            )
            raise
        cache.set(key, place, settings.GEOCODING_CACHE_TTL)
        return place

    def geocode_us_address(self, address: str) -> dict[str, float]:
        """Backward-compatible helper returning only coordinates for Stage 2 callers."""
        return self.geocode_us_place(address)['coordinates']

    def verify_us_coordinates(self, point: dict[str, float]) -> dict[str, Any]:
        """Reverse geocode coordinates; numeric bounds alone cannot verify country."""
        return self._track_lookup('reverse_geocoding', self._verify_us_coordinates, point)

    def _verify_us_coordinates(self, point: dict[str, float]) -> dict[str, Any]:
        latitude = point['latitude']
        longitude = point['longitude']
        query_value = f'{latitude!r},{longitude!r}'
        key = self._service_key('us-coordinate-check', settings.NOMINATIM_REVERSE_URL, query_value)
        cached = cache.get(key)
        if cached is not None:
            self._active_cache_status = 'hit'
            self._raise_cached_failure(cached)
            return cached

        result = self._request_json(
            settings.NOMINATIM_REVERSE_URL,
            {
                'lat': latitude,
                'lon': longitude,
                'format': 'jsonv2',
                'addressdetails': 1,
                'zoom': 10,
            },
            operation='reverse_geocoding',
        )
        if not isinstance(result, dict):
            cache.set(
                key,
                {'_geocoding_failure': 'outside_us'},
                settings.GEOCODING_FAILURE_CACHE_TTL,
            )
            self._active_failure_category = 'invalid_response'
            self._active_failure_source = 'response'
            raise LocationNotInUSA('The coordinates could not be verified as a US location.')
        if 'error' in result:
            cache.set(
                key,
                {'_geocoding_failure': 'outside_us'},
                settings.GEOCODING_FAILURE_CACHE_TTL,
            )
            self._active_failure_category = 'empty_result'
            self._active_failure_source = 'lookup_result'
            raise LocationNotInUSA('The coordinates could not be verified as a US location.')
        try:
            place = self._place_from_result(result, coordinates=point)
        except LocationNotInUSA:
            cache.set(
                key,
                {'_geocoding_failure': 'outside_us'},
                settings.GEOCODING_FAILURE_CACHE_TTL,
            )
            raise
        cache.set(key, place, settings.GEOCODING_CACHE_TTL)
        return place


US_STATE_CODES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'district of columbia': 'DC', 'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI',
    'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY',
}
