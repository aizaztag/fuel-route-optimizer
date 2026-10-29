"""Reproducible mocked cold/warm request benchmark (no live HTTP calls)."""

import json
from decimal import Decimal
from time import perf_counter

import httpx
from django.conf import settings
from django.core.cache import cache
import pytest
from rest_framework.test import APIClient

from routing.models import StationCoordinate
from routing.station_data import FuelStation, FuelStationRepository, StationDataset, StationLoadStats


@pytest.mark.django_db
def test_mocked_cold_and_repeated_route_benchmark(monkeypatch, capsys):
    cache.clear()
    start = {'latitude': 41.8, 'longitude': -87.6}
    finish = {'latitude': 41.8, 'longitude': -80.0}
    longitude = -83.8
    stations = tuple(
        FuelStation(
            f'{index:04}', f'Station {index}', f'{100 + index} Main St',
            'Chicago', 'IL', Decimal('3.50'), 41.8, longitude,
        ) for index in range(25)
    )
    dataset = StationDataset(stations, StationLoadStats(25, 0, 0, 0, 0))
    for item in stations:
        StationCoordinate.objects.create(
            station_id=item.station_id,
            input_hash=item.coordinate_input_hash,
            latitude=item.latitude,
            longitude=item.longitude,
            status=StationCoordinate.MATCHED,
        )
    monkeypatch.setattr(
        FuelStationRepository, 'load',
        lambda self: self._attach_stored_coordinates(dataset),
    )
    calls = {'nominatim': 0, 'osrm': 0}

    def fake_get(url, **_kwargs):
        if url.startswith(settings.OSRM_ROUTE_URL):
            calls['osrm'] += 1
            payload = {
                'code': 'Ok',
                'routes': [{
                    'distance': 600 / 0.000621371,
                    'duration': 36000,
                    'geometry': {'type': 'LineString', 'coordinates': [
                        [start['longitude'], start['latitude']],
                        [finish['longitude'], finish['latitude']],
                    ]},
                }],
            }
        else:
            calls['nominatim'] += 1
            payload = {
                'address': {
                    'country_code': 'us', 'state': 'Illinois', 'ISO3166-2-lvl4': 'US-IL',
                },
            }
        return httpx.Response(200, json=payload, request=httpx.Request('GET', url))

    monkeypatch.setattr(httpx, 'get', fake_get)
    monkeypatch.setattr('routing.clients.geocoding.time.sleep', lambda _seconds: None)
    client = APIClient()
    body = {'start': start, 'finish': finish}
    with_caps = {'STATION_GEOCODING_CANDIDATE_LIMIT': 8}
    from django.test import override_settings

    with override_settings(**with_caps):
        started = perf_counter()
        cold = client.post('/api/v1/route-plan/', body, format='json')
        cold_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        repeated = client.post('/api/v1/route-plan/', body, format='json')
        repeated_ms = (perf_counter() - started) * 1000

    assert cold.status_code == repeated.status_code == 200
    assert cold.data['optimization_status'] == repeated.data['optimization_status'] == 'optimized'
    assert [leg['within_maximum_range'] for leg in cold.data['fuel_optimization']['route_legs']] == [True, True]
    assert cold.data['total_stop_purchase_cost'] == 105.0
    assert cold.data['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'] == 2
    assert cold.data['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'] == 1
    assert cold.data['diagnostics']['station_geocoding']['requests'] == []
    assert cold.data['diagnostics']['station_coordinate_lookup']['outbound_http_requests'] == 0
    assert repeated.data['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'] == 0
    assert repeated.data['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'] == 0
    assert calls == {'nominatim': 2, 'osrm': 1}

    report = {
        'mode': 'mocked_no_live_http',
        'cold': {
            'total_latency_ms': round(cold_ms, 3),
            'nominatim_requests': cold.data['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'],
            'osrm_requests': cold.data['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'],
            'station_geocoder_requests': 0,
            'candidates_available': cold.data['diagnostics']['candidate_search']['candidates_available_in_corridor'],
            'candidates_selected': cold.data['diagnostics']['candidate_search']['candidates_shortlisted'],
            'candidates_evaluated': cold.data['diagnostics']['candidate_search']['candidates_evaluated'],
            'optimization_status': cold.data['optimization_status'],
        },
        'repeated': {
            'total_latency_ms': round(repeated_ms, 3),
            'nominatim_requests': repeated.data['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'],
            'osrm_requests': repeated.data['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'],
            'station_geocoder_requests': 0,
            'candidates_available': repeated.data['diagnostics']['candidate_search']['candidates_available_in_corridor'],
            'candidates_selected': repeated.data['diagnostics']['candidate_search']['candidates_shortlisted'],
            'candidates_evaluated': repeated.data['diagnostics']['candidate_search']['candidates_evaluated'],
            'optimization_status': repeated.data['optimization_status'],
        },
    }
    print('ROUTE_PLAN_BENCHMARK=' + json.dumps(report, sort_keys=True))
