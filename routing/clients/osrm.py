import hashlib
import logging
import math
from time import perf_counter
from typing import Any

import httpx
from django.conf import settings
from django.core.cache import cache

from .exceptions import NoRouteFound, UpstreamError, UpstreamTimeout

METERS_TO_MILES = 0.000621371
SECONDS_PER_MINUTE = 60
logger = logging.getLogger(__name__)


def convert_route_units(distance_meters: float, duration_seconds: float) -> tuple[float, float]:
    """Return distance in miles and duration in minutes, rounded for API output."""
    return round(distance_meters * METERS_TO_MILES, 2), round(duration_seconds / SECONDS_PER_MINUTE, 2)


class OSRMClient:
    def __init__(self) -> None:
        self.last_request_diagnostics: dict[str, Any] = {}

    def route(self, start: dict[str, float], finish: dict[str, float]) -> dict[str, Any]:
        # OSRM accepts longitude,latitude order; the API accepts latitude,longitude.
        coordinate_path = (
            f"{start['longitude']},{start['latitude']};"
            f"{finish['longitude']},{finish['latitude']}"
        )
        cache_material = f'{settings.OSRM_ROUTE_URL}|{coordinate_path}|driving|geojson'
        cache_key = 'fuel-route:osrm:' + hashlib.sha256(cache_material.encode()).hexdigest()
        self.last_request_diagnostics = {
            'cache_status': 'miss',
            'outbound_http_request': False,
            'http_status': None,
            'failed': False,
            'external_duration_ms': None,
        }
        cached = cache.get(cache_key)
        if cached is not None:
            self.last_request_diagnostics['cache_status'] = 'hit'
            logger.info('Routing cache hit: provider=osrm')
            return cached

        logger.info('Routing cache miss: provider=osrm')
        http_started = None
        status_code = None
        try:
            http_started = perf_counter()
            self.last_request_diagnostics['outbound_http_request'] = True
            response = httpx.get(
                f'{settings.OSRM_ROUTE_URL.rstrip("/")}/{coordinate_path}',
                params={'overview': 'full', 'geometries': 'geojson', 'steps': 'false'},
                headers={'User-Agent': settings.NOMINATIM_USER_AGENT},
                timeout=settings.OSRM_TIMEOUT_SECONDS,
            )
            status_code = response.status_code
            self.last_request_diagnostics['http_status'] = status_code
            response.raise_for_status()
            payload = response.json()
            external_duration_ms = round((perf_counter() - http_started) * 1000, 3)
            self.last_request_diagnostics['external_duration_ms'] = external_duration_ms
            logger.info(
                'External routing request completed: provider=osrm duration_ms=%.3f '
                'http_status=%s failed=false',
                external_duration_ms, status_code,
            )
        except httpx.TimeoutException as exc:
            elapsed = (perf_counter() - http_started) * 1000 if http_started else 0.0
            self.last_request_diagnostics['external_duration_ms'] = round(elapsed, 3)
            self.last_request_diagnostics['failed'] = True
            logger.warning(
                'External routing request failed: provider=osrm duration_ms=%.3f '
                'http_status=%s failure=timeout', elapsed, status_code,
            )
            raise UpstreamTimeout('The routing service timed out.') from exc
        except (httpx.HTTPError, ValueError) as exc:
            elapsed = (perf_counter() - http_started) * 1000 if http_started else 0.0
            self.last_request_diagnostics['external_duration_ms'] = round(elapsed, 3)
            self.last_request_diagnostics['failed'] = True
            logger.warning(
                'External routing request failed: provider=osrm duration_ms=%.3f '
                'http_status=%s failure=%s', elapsed, status_code, type(exc).__name__,
            )
            raise UpstreamError('The routing service is unavailable.') from exc

        if not isinstance(payload, dict):
            self.last_request_diagnostics['failed'] = True
            logger.warning('Routing provider returned an invalid payload: provider=osrm http_status=%s', status_code)
            raise UpstreamError('The routing service returned an invalid response.')
        if payload.get('code') == 'NoRoute':
            self.last_request_diagnostics['failed'] = True
            logger.warning('Routing provider returned no route: provider=osrm http_status=%s', status_code)
            raise NoRouteFound('No driving route was found between these locations.')
        if payload.get('code') != 'Ok':
            self.last_request_diagnostics['failed'] = True
            logger.warning('Routing provider returned an error: provider=osrm http_status=%s', status_code)
            raise UpstreamError('The routing service returned an error.')
        routes = payload.get('routes')
        if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
            self.last_request_diagnostics['failed'] = True
            logger.warning('Routing provider returned no route: provider=osrm http_status=%s', status_code)
            raise NoRouteFound('No driving route was found between these locations.')
        route = routes[0]
        geometry = route.get('geometry')
        if (
            not isinstance(geometry, dict)
            or geometry.get('type') != 'LineString'
            or not isinstance(geometry.get('coordinates'), list)
            or not geometry['coordinates']
        ):
            self.last_request_diagnostics['failed'] = True
            logger.warning('Routing provider returned invalid geometry: provider=osrm http_status=%s', status_code)
            raise UpstreamError('The routing service returned invalid route geometry.')
        for coordinate in geometry['coordinates']:
            if (
                not isinstance(coordinate, (list, tuple))
                or len(coordinate) < 2
                or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in coordinate[:2])
                or not -180 <= coordinate[0] <= 180
                or not -90 <= coordinate[1] <= 90
            ):
                self.last_request_diagnostics['failed'] = True
                logger.warning('Routing provider returned invalid coordinates: provider=osrm http_status=%s', status_code)
                raise UpstreamError('The routing service returned invalid route geometry.')
        try:
            distance_meters = float(route['distance'])
            duration_seconds = float(route['duration'])
            if (
                not math.isfinite(distance_meters)
                or not math.isfinite(duration_seconds)
                or distance_meters < 0
                or duration_seconds < 0
            ):
                self.last_request_diagnostics['failed'] = True
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            self.last_request_diagnostics['failed'] = True
            logger.warning('Routing provider returned invalid metrics: provider=osrm http_status=%s', status_code)
            raise UpstreamError('The routing service returned invalid route metrics.') from exc

        distance_miles, duration_minutes = convert_route_units(distance_meters, duration_seconds)
        result = {
            'distance_miles': distance_miles,
            'duration_minutes': duration_minutes,
            'geometry': geometry,
        }
        cache.set(cache_key, result, settings.ROUTING_CACHE_TTL)
        return result
