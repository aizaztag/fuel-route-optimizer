"""Opt-in batch enrichment for station coordinates using the US Census Geocoder."""

import csv
import io
import logging
import math
from dataclasses import dataclass
from time import perf_counter

import httpx
from django.conf import settings

from .models import StationCoordinate
from .station_data import FuelStation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnrichmentResult:
    station_id: str
    status: str
    latitude: float | None = None
    longitude: float | None = None
    failure_category: str = ''


def census_batch_csv(stations: list[FuelStation]) -> bytes:
    """Create the Census batch format: ID, street, city, state, blank ZIP."""
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    for station in stations:
        writer.writerow((station.station_id, station.address, station.city, station.state, ''))
    return stream.getvalue().encode('utf-8')


def parse_census_batch_csv(content: str | bytes, requested_ids: set[str]) -> dict[str, EnrichmentResult]:
    """Parse Census addressbatch output without accepting malformed coordinates."""
    if isinstance(content, bytes):
        content = content.decode('utf-8-sig')
    results: dict[str, EnrichmentResult] = {}
    for row in csv.reader(io.StringIO(content, newline='')):
        if len(row) < 3:
            continue
        station_id = row[0].strip().casefold()
        if station_id not in requested_ids:
            continue
        if row[2].strip().casefold() != 'match':
            results[station_id] = EnrichmentResult(station_id, StationCoordinate.NO_MATCH)
            continue
        try:
            longitude_text, latitude_text = row[5].split(',', maxsplit=1)
            longitude, latitude = float(longitude_text), float(latitude_text)
        except (IndexError, ValueError, TypeError):
            results[station_id] = EnrichmentResult(
                station_id, StationCoordinate.FAILED, failure_category='invalid_response'
            )
            continue
        if not (math.isfinite(latitude) and math.isfinite(longitude)
                and -90 <= latitude <= 90 and -180 <= longitude <= 180):
            results[station_id] = EnrichmentResult(
                station_id, StationCoordinate.FAILED, failure_category='invalid_response'
            )
            continue
        results[station_id] = EnrichmentResult(
            station_id, StationCoordinate.MATCHED, latitude=latitude, longitude=longitude
        )
    for station_id in requested_ids - results.keys():
        results[station_id] = EnrichmentResult(
            station_id, StationCoordinate.FAILED, failure_category='invalid_response'
        )
    return results


class CensusBatchGeocoder:
    """Single sequential batch submission; no per-address calls or retries."""

    def __init__(self, post=None):
        self._post = post or httpx.post
        self.last_diagnostics: dict = {}

    def geocode(self, stations: list[FuelStation]) -> dict[str, EnrichmentResult]:
        payload = census_batch_csv(stations)
        started = perf_counter()
        status_code = None
        try:
            response = self._post(
                settings.CENSUS_GEOCODER_BATCH_URL,
                data={'benchmark': settings.CENSUS_GEOCODER_BENCHMARK},
                files={'addressFile': ('stations.csv', payload, 'text/csv')},
                timeout=settings.CENSUS_GEOCODER_TIMEOUT_SECONDS,
            )
            status_code = response.status_code
            response.raise_for_status()
            result = parse_census_batch_csv(response.content, {s.station_id for s in stations})
        except httpx.TimeoutException:
            result = {
                station.station_id: EnrichmentResult(
                    station.station_id, StationCoordinate.FAILED, failure_category='timeout'
                ) for station in stations
            }
        except httpx.HTTPStatusError:
            result = {
                station.station_id: EnrichmentResult(
                    station.station_id, StationCoordinate.FAILED, failure_category=(
                        'rate_limited' if status_code == 429 else 'http_error'
                    )
                ) for station in stations
            }
        except httpx.HTTPError:
            result = {
                station.station_id: EnrichmentResult(
                    station.station_id, StationCoordinate.FAILED, failure_category='connection_error'
                ) for station in stations
            }
        except (UnicodeDecodeError, csv.Error, ValueError):
            result = {
                station.station_id: EnrichmentResult(
                    station.station_id, StationCoordinate.FAILED, failure_category='invalid_response'
                ) for station in stations
            }
        elapsed_ms = round((perf_counter() - started) * 1000, 3)
        self.last_diagnostics = {
            'duration_ms': elapsed_ms,
            'http_status': status_code,
            'failed': any(item.status == StationCoordinate.FAILED for item in result.values()),
            'failure_categories': {},
            'records_submitted': len(stations),
            'outbound_http_requests': 1,
        }
        for item in result.values():
            if item.failure_category:
                categories = self.last_diagnostics['failure_categories']
                categories[item.failure_category] = categories.get(item.failure_category, 0) + 1
        logger.info(
            'Station enrichment batch: provider=us_census_geocoder records=%d duration_ms=%.3f '
            'http_status=%s failed=%s failure_categories=%s',
            len(stations), elapsed_ms, status_code, self.last_diagnostics['failed'],
            self.last_diagnostics['failure_categories'],
        )
        return result
