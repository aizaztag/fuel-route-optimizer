from unittest.mock import Mock

import httpx
import pytest
from django.core.cache import cache
from django.conf import settings
from rest_framework.test import APIClient

from routing.clients.osrm import convert_route_units
from routing.station_data import FuelStation, FuelStationRepository, StationDataset, StationLoadStats
from decimal import Decimal

pytestmark = pytest.mark.django_db

ROUTE_URL = 'https://router.project-osrm.org/route/v1/driving'
SEARCH_URL = 'https://nominatim.openstreetmap.org/search'
REVERSE_URL = 'https://nominatim.openstreetmap.org/reverse'
GEOMETRY = {'type': 'LineString', 'coordinates': [[-74.0, 40.7], [-73.9, 40.8]]}


@pytest.fixture(autouse=True)
def clear_django_cache():
    cache.clear()


def make_response(payload, status_code=200):
    response = httpx.Response(status_code, json=payload, request=httpx.Request('GET', 'https://test.invalid'))
    return response


def osrm_success():
    return {'code': 'Ok', 'routes': [{'distance': 100_000, 'duration': 3_600, 'geometry': GEOMETRY}]}


def reverse_success(country='us'):
    return {'address': {'country_code': country}}


def search_success(latitude, longitude, country='us'):
    return [{
        'lat': str(latitude),
        'lon': str(longitude),
        'address': {'country_code': country},
    }]


def test_valid_coordinate_request_routes_once_and_returns_leaflet_geojson(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == REVERSE_URL:
            return make_response(reverse_success())
        return make_response(osrm_success())

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': 40.7, 'longitude': -74.0},
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }, format='json')

    assert response.status_code == 200
    assert response.data['start'] == {'latitude': 40.7, 'longitude': -74.0}
    assert response.data['finish'] == {'latitude': 40.8, 'longitude': -73.9}
    assert response.data['distance_miles'] == 62.14
    assert response.data['estimated_duration_minutes'] == 60.0
    assert response.data['geometry'] == GEOMETRY
    assert response.data['fuel_optimization']['implemented'] is True
    assert response.data['fuel_stops'] == []
    assert response.data['total_fuel_cost'] == 0.0
    assert response.data['total_stop_purchase_cost'] == 0.0
    assert response.data['fuel_cost_basis']['initial_tank_cost_included'] is False
    assert response.data['total_estimated_fuel_cost'] == 0.0
    assert response.data['optimization_status'] == 'no_stops_needed'
    assert response.data['fuel_optimization']['route_legs'] == [{
        'from_station_id': None,
        'to_station_id': None,
        'estimated_distance_miles': 62.14,
        'within_maximum_range': True,
    }]
    assert sum(url.startswith(ROUTE_URL) for url, _ in calls) == 1
    assert sum(url == REVERSE_URL for url, _ in calls) == 2
    assert all(kwargs['timeout'] for _, kwargs in calls)
    diagnostics = response.data['diagnostics']
    assert set(diagnostics['timings_ms']) == {
        'start_geocoding', 'finish_geocoding', 'osrm_route',
        'candidate_station_selection', 'station_geocoding_total',
        'station_route_matching', 'fuel_optimization',
        'route_planning_service_total', 'total_request_processing',
    }
    assert diagnostics['endpoint_geocoding']['start']['http_status'] == 200
    assert diagnostics['endpoint_geocoding']['finish']['http_status'] == 200
    assert diagnostics['timings_ms']['total_request_processing'] >= 0
    assert diagnostics['upstream_requests']['nominatim']['outbound_http_requests'] == 2
    assert diagnostics['upstream_requests']['osrm']['outbound_http_requests'] == 1
    assert response.data['station_coverage']['stations_considered'] == 0
    assert diagnostics['timings_ms']['candidate_station_selection'] == 0.0
    assert diagnostics['station_geocoding']['requests'] == []
    assert not any(url == SEARCH_URL for url, _ in calls)


def test_endpoint_rate_limit_is_reported_without_retry_or_stack_details(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return make_response({'error': 'rate limited'}, status_code=429)

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': 40.7, 'longitude': -74.0},
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }, format='json')

    assert response.status_code == 502
    assert response.data['error_code'] == 'rate_limited'
    assert 'traceback' not in str(response.data).lower()
    assert calls == [REVERSE_URL]


def test_valid_address_request_uses_us_filter_and_user_agent(monkeypatch):
    calls = []
    searches = iter([search_success(40.7, -74.0), search_success(40.8, -73.9)])

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == SEARCH_URL:
            return make_response(next(searches))
        return make_response(osrm_success())

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start_address': 'New York, NY',
        'finish_address': 'Brooklyn, NY',
    }, format='json')

    assert response.status_code == 200
    search_calls = [kwargs for url, kwargs in calls if url == SEARCH_URL]
    assert len(search_calls) == 2
    assert all(kwargs['params']['countrycodes'] == 'us' for kwargs in search_calls)
    assert all(kwargs['headers']['User-Agent'].startswith('FuelRouteOptimizer/') for kwargs in search_calls)
    assert sum(url.startswith(ROUTE_URL) for url, _ in calls) == 1


@pytest.mark.parametrize('payload', [
    {},
    {'start_address': 'New York'},
    {'start': {'latitude': 40.7, 'longitude': -74.0}},
    {'start_address': 'New York', 'finish': {'latitude': 40.8, 'longitude': -73.9}},
    {'start_address': 'New York', 'finish_address': 'Boston', 'unexpected': True},
    {'start': {'latitude': 40.7, 'longitude': -74.0, 'unexpected': True}, 'finish': {'latitude': 40.8, 'longitude': -73.9}},
    {'start': {'latitude': 'north', 'longitude': -74.0}, 'finish': {'latitude': 40.8, 'longitude': -73.9}},
])
def test_missing_or_malformed_input_returns_helpful_400_without_http(payload, monkeypatch):
    request = Mock(side_effect=AssertionError('External HTTP request should not occur'))
    monkeypatch.setattr(httpx, 'get', request)
    response = APIClient().post('/api/v1/route-plan/', payload, format='json')

    assert response.status_code == 400
    assert response.data
    request.assert_not_called()


@pytest.mark.parametrize('point', [
    {'latitude': 91, 'longitude': -74},
    {'latitude': 40, 'longitude': -181},
    {'latitude': -91, 'longitude': 0},
])
def test_out_of_range_coordinates_are_rejected_before_external_calls(point, monkeypatch):
    request = Mock(side_effect=AssertionError('External HTTP request should not occur'))
    monkeypatch.setattr(httpx, 'get', request)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': point,
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }, format='json')
    assert response.status_code == 400
    request.assert_not_called()


def test_address_not_found_in_us_returns_400(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return make_response([])

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start_address': 'No such place', 'finish_address': 'Boston, MA',
    }, format='json')
    assert response.status_code == 400
    assert 'No matching US location' in response.data['detail']
    assert calls == [SEARCH_URL]


def test_coordinates_that_reverse_geocode_outside_us_are_rejected(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return make_response(reverse_success('ca'))

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': 49.2, 'longitude': -123.1},
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }, format='json')
    assert response.status_code == 400
    assert 'United States' in response.data['detail']
    assert calls == [REVERSE_URL]


@pytest.mark.parametrize(('osrm_payload', 'expected_status'), [
    ({'code': 'NoRoute', 'routes': []}, 422),
    ({'code': 'InvalidQuery', 'message': 'bad request'}, 502),
])
def test_osrm_route_failures_are_mapped_to_http_errors(monkeypatch, osrm_payload, expected_status):
    def fake_get(url, **kwargs):
        if url == REVERSE_URL:
            return make_response(reverse_success())
        return make_response(osrm_payload)

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': 40.7, 'longitude': -74.0},
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }, format='json')
    assert response.status_code == expected_status
    assert 'detail' in response.data


def test_osrm_timeout_returns_504_without_exposing_exception(monkeypatch):
    def fake_get(url, **kwargs):
        if url == REVERSE_URL:
            return make_response(reverse_success())
        raise httpx.ReadTimeout('private diagnostic details')

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': 40.7, 'longitude': -74.0},
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }, format='json')
    assert response.status_code == 504
    assert 'private diagnostic details' not in str(response.data)


def test_osrm_http_error_returns_502(monkeypatch):
    def fake_get(url, **kwargs):
        if url == REVERSE_URL:
            return make_response(reverse_success())
        return make_response({'error': 'secret upstream body'}, status_code=500)

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': 40.7, 'longitude': -74.0},
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }, format='json')
    assert response.status_code == 502
    assert 'secret upstream body' not in str(response.data)


def test_unexpected_internal_error_returns_safe_500(monkeypatch):
    request = Mock(side_effect=AssertionError('External HTTP request should not occur'))
    monkeypatch.setattr(httpx, 'get', request)
    monkeypatch.setattr(
        'routing.views.RoutePlanningService.plan',
        Mock(side_effect=RuntimeError('private secret and traceback details')),
    )

    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': 40.7, 'longitude': -74.0},
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }, format='json')

    assert response.status_code == 500
    assert response.data == {'detail': 'An unexpected error occurred while creating the route plan.'}
    assert 'private secret' not in str(response.data)
    request.assert_not_called()


def test_route_unit_conversions():
    assert convert_route_units(1609.344, 90) == (1.0, 1.5)


def test_default_cache_backend_is_process_local():
    assert settings.CACHES['default']['BACKEND'] == 'django.core.cache.backends.locmem.LocMemCache'


def test_successful_geocoding_and_routing_results_are_cached(monkeypatch):
    calls = []
    searches = iter([search_success(40.7, -74.0), search_success(40.8, -73.9)])

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == SEARCH_URL:
            return make_response(next(searches))
        return make_response(osrm_success())

    monkeypatch.setattr(httpx, 'get', fake_get)
    client = APIClient()
    payload = {'start_address': 'New York, NY', 'finish_address': 'Brooklyn, NY'}
    first = client.post('/api/v1/route-plan/', payload, format='json')
    first_call_count = len(calls)
    second = client.post('/api/v1/route-plan/', payload, format='json')

    assert first.status_code == second.status_code == 200
    assert first_call_count == 3  # Two geocoding requests and one route request.
    assert len(calls) == first_call_count
    first_counts = first.data['diagnostics']['upstream_requests']
    repeated_counts = second.data['diagnostics']['upstream_requests']
    assert first_counts['nominatim']['outbound_http_requests'] == 2
    assert first_counts['osrm']['outbound_http_requests'] == 1
    assert repeated_counts['nominatim']['outbound_http_requests'] == 0
    assert repeated_counts['nominatim']['cache_hits'] == 2
    assert repeated_counts['osrm']['outbound_http_requests'] == 0
    assert repeated_counts['osrm']['cache_hits'] == 1


def test_coordinate_route_cache_hit_avoids_all_external_requests(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == REVERSE_URL:
            return make_response(reverse_success())
        return make_response(osrm_success())

    monkeypatch.setattr(httpx, 'get', fake_get)
    client = APIClient()
    payload = {
        'start': {'latitude': 40.7, 'longitude': -74.0},
        'finish': {'latitude': 40.8, 'longitude': -73.9},
    }
    first = client.post('/api/v1/route-plan/', payload, format='json')
    first_count = len(calls)
    second = client.post('/api/v1/route-plan/', payload, format='json')

    assert first.status_code == second.status_code == 200
    assert sum(url.startswith(ROUTE_URL) for url in calls[:first_count]) == 1
    assert first_count == 3
    assert len(calls) == first_count
    assert first.data['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'] == 2
    assert first.data['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'] == 1
    assert second.data['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'] == 0
    assert second.data['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'] == 0


def test_long_route_returns_located_costed_fuel_stops(monkeypatch, settings):
    # Mock a Dallas-to-El Paso style Texas route. The 650-mile distance and
    # OSRM geometry are controlled fixtures; no live route data is assumed.
    start = (32.7767, -96.7970)
    finish = (31.7619, -106.4850)
    route_fraction = 300 / 650
    station_latitude = start[0] + (finish[0] - start[0]) * route_fraction
    station_longitude = start[1] + (finish[1] - start[1]) * route_fraction
    dataset = StationDataset(
        stations=(
            FuelStation('501', 'Example Fuel', 'I-10 EXIT 1', 'Example City', 'TX', Decimal('4.00'),
                        station_latitude, station_longitude),
            FuelStation('502', 'Second Fuel', 'I-10 EXIT 2', 'Example City', 'TX', Decimal('3.90'),
                        start[0] + (finish[0] - start[0]) * 450 / 650,
                        start[1] + (finish[1] - start[1]) * 450 / 650),
        ),
        stats=StationLoadStats(1, 0, 0, 0, 0),
    )
    monkeypatch.setattr(FuelStationRepository, 'load', lambda self: dataset)
    settings.STATION_GEOCODING_CANDIDATE_LIMIT = 1
    settings.ROUTE_CORRIDOR_WIDTH_MILES = 10
    settings.MAX_STATION_DETOUR_MILES = 20
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == REVERSE_URL:
            return make_response({
                'address': {
                    'country_code': 'us', 'state': 'Texas',
                    'ISO3166-2-lvl4': 'US-TX',
                },
            })
        if url == SEARCH_URL:
            assert kwargs['params']['countrycodes'] == 'us'
            return make_response([{
                'lat': str(station_latitude), 'lon': str(station_longitude),
                'address': {
                    'country_code': 'us', 'state': 'Texas',
                    'ISO3166-2-lvl4': 'US-TX', 'city': 'Example City',
                },
            }])
        long_route = {
            'code': 'Ok',
            'routes': [{
                'distance': 650 / 0.000621371,
                'duration': 39000,
                'geometry': {
                    'type': 'LineString',
                    'coordinates': [[start[1], start[0]], [finish[1], finish[0]]],
                },
            }],
        }
        return make_response(long_route)

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': start[0], 'longitude': start[1]},
        'finish': {'latitude': finish[0], 'longitude': finish[1]},
    }, format='json')

    assert response.status_code == 200
    assert response.data['fuel_optimization']['feasible'] is True
    assert response.data['fuel_stops'][0]['station_id'] == '501'
    assert response.data['fuel_stops'][0]['price_per_gallon'] == 4.0
    assert response.data['fuel_stops'][0]['estimated_gallons_purchased'] == 30.0
    assert response.data['fuel_stops'][0]['estimated_purchase_cost'] == 120.0
    assert response.data['total_fuel_cost'] == 120.0
    assert response.data['total_stop_purchase_cost'] == 120.0
    assert response.data['fuel_cost_basis']['definition'] == 'Estimated fuel purchased at selected stops only.'
    assert response.data['fuel_stops'][0]['coordinates'] == {
        'latitude': station_latitude,
        'longitude': station_longitude,
    }
    assert response.data['total_estimated_fuel_cost'] == 120.0
    assert response.data['optimization_status'] == 'optimized'
    assert response.data['station_coverage']['stations_considered'] == 2
    assert response.data['station_coverage']['stations_geocoded'] == 2
    assert response.data['station_coverage']['candidates_available'] == 2
    assert response.data['station_coverage']['candidates_excluded_by_limit'] == 1
    assert response.data['station_coverage']['stations_matched'] == 2
    station_diagnostics = response.data['diagnostics']['station_geocoding']
    assert station_diagnostics['cache_hits'] == 0
    assert station_diagnostics['cache_misses'] == 0
    assert station_diagnostics['failed_requests'] == 0
    assert station_diagnostics['requests'] == []
    assert response.data['diagnostics']['station_coordinate_lookup']['outbound_http_requests'] == 0
    assert response.data['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'] == 2
    assert response.data['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'] == 1
    assert response.data['diagnostics']['timings_ms']['candidate_station_selection'] >= 0
    assert response.data['diagnostics']['timings_ms']['station_route_matching'] >= 0
    assert response.data['diagnostics']['timings_ms']['fuel_optimization'] >= 0
    assert any('incomplete' in warning.lower() for warning in response.data['warnings'])
    assert sum(url.startswith(ROUTE_URL) for url, _ in calls) == 1


def test_long_route_reports_no_feasible_plan_when_candidate_is_unlocated(monkeypatch, settings):
    dataset = StationDataset(
        stations=(FuelStation('502', 'Far Fuel', 'I-80 EXIT 2', 'Example City', 'PA', Decimal('4.00')),),
        stats=StationLoadStats(1, 0, 0, 0, 0),
    )
    monkeypatch.setattr(FuelStationRepository, 'load', lambda self: dataset)
    settings.STATION_GEOCODING_CANDIDATE_LIMIT = 10

    def fake_get(url, **kwargs):
        if url == REVERSE_URL:
            return make_response({
                'address': {'country_code': 'us', 'state': 'Pennsylvania', 'ISO3166-2-lvl4': 'US-PA'},
            })
        if url == SEARCH_URL:
            return make_response([])
        return make_response({
            'code': 'Ok',
            'routes': [{
                'distance': 1200 / 0.000621371,
                'duration': 72000,
                'geometry': {'type': 'LineString', 'coordinates': [[-75, 40], [-75, 41]]},
            }],
        })

    monkeypatch.setattr(httpx, 'get', fake_get)
    response = APIClient().post('/api/v1/route-plan/', {
        'start': {'latitude': 40, 'longitude': -75},
        'finish': {'latitude': 41, 'longitude': -75},
    }, format='json')

    assert response.status_code == 200
    assert response.data['fuel_optimization']['feasible'] is False
    assert response.data['fuel_stops'] == []
    assert response.data['total_fuel_cost'] is None
    assert response.data['total_stop_purchase_cost'] is None
    assert response.data['fuel_cost_basis']['initial_tank_cost_included'] is False
    assert response.data['total_estimated_fuel_cost'] is None
    assert response.data['optimization_status'] == 'no_feasible_plan'
    assert 'No feasible' in response.data['fuel_optimization']['message']
    assert response.data['station_coverage']['stations_considered'] == 0
    assert response.data['station_coverage']['stations_geocoded'] == 0
    assert response.data['station_coverage']['stations_skipped'] == 1
