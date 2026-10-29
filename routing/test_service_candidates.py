from decimal import Decimal

import httpx
from django.core.cache import cache
from django.conf import settings
from django.test import override_settings

import routing.clients.geocoding as geocoding
from routing.clients.geocoding import NominatimClient
from routing.services import RoutePlanningService
from routing.state_corridor import state_corridor
from routing.station_data import FuelStation, StationDataset, StationLoadStats


def test_chicago_denver_state_corridor_includes_plausible_intermediate_states():
    assert state_corridor('IL', 'CO') == {'IL', 'IA', 'MO', 'NE', 'KS', 'OK', 'CO'}


def test_intermediate_state_stations_can_form_a_feasible_chicago_denver_plan(settings):
    start = {'latitude': 41.8, 'longitude': -87.6}
    finish = {'latitude': 39.7, 'longitude': -105.0}
    distance_miles = 1004.4
    line = [[start['longitude'], start['latitude']], [finish['longitude'], finish['latitude']]]
    stations = (
        FuelStation('IA-1', 'Iowa Fuel', 'I-80', 'Des Moines', 'IA', Decimal('3.50'),
                    start['latitude'] + (finish['latitude'] - start['latitude']) * 300 / distance_miles,
                    start['longitude'] + (finish['longitude'] - start['longitude']) * 300 / distance_miles),
        FuelStation('NE-1', 'Nebraska Fuel', 'I-80', 'Omaha', 'NE', Decimal('3.40'),
                    start['latitude'] + (finish['latitude'] - start['latitude']) * 700 / distance_miles,
                    start['longitude'] + (finish['longitude'] - start['longitude']) * 700 / distance_miles),
    )
    dataset = StationDataset(stations, StationLoadStats(2, 0, 0, 0, 0))

    class StubRepository:
        def load(self):
            return dataset

    class StubGeocoder:
        def verify_us_coordinates(self, coordinates):
            is_start = coordinates == start
            return {
                'coordinates': coordinates,
                'state_code': 'IL' if is_start else 'CO',
            }

    class StubRouter:
        def route(self, start_coordinates, finish_coordinates):
            return {
                'distance_miles': distance_miles,
                'duration_minutes': 1069.0,
                'geometry': {'type': 'LineString', 'coordinates': line},
            }

    settings.STATION_GEOCODING_CANDIDATE_LIMIT = 25
    result = RoutePlanningService(
        geocoder=StubGeocoder(),
        router=StubRouter(),
        station_repository=StubRepository(),
    ).plan({'start': start, 'finish': finish})

    coverage = result['station_coverage']
    assert coverage['candidate_states_evaluated'] == ['CO', 'IA', 'IL', 'KS', 'MO', 'NE', 'OK']
    assert coverage['candidates_available'] == 2
    assert coverage['dataset_stations_loaded'] == 2
    assert coverage['stations_considered'] == 2
    assert coverage['stations_geocoded'] == 2
    assert coverage['stations_matched'] == 2
    assert [candidate['state'] for candidate in coverage['matched_candidate_positions']] == ['IA', 'NE']
    assert coverage['matched_candidate_positions'][0]['along_route_mile'] == 300.0
    assert coverage['matched_candidate_positions'][1]['along_route_mile'] == 700.0
    assert all(candidate['access_offset_miles'] == 0.0 for candidate in coverage['matched_candidate_positions'])
    assert result['fuel_optimization']['feasible'] is True
    assert result['optimization_status'] == 'optimized'
    assert [stop['station_id'] for stop in result['fuel_stops']] == ['IA-1', 'NE-1']
    assert [leg['estimated_distance_miles'] for leg in result['fuel_optimization']['route_legs']] == [300.0, 400.0, 304.4]
    assert all(leg['within_maximum_range'] for leg in result['fuel_optimization']['route_legs'])
    route_points = [0.0] + [stop['approximate_along_route_mile'] for stop in result['fuel_stops']] + [distance_miles]
    assert all(
        route_points[index + 1] - route_points[index] <= settings.VEHICLE_MAX_RANGE_MILES
        for index in range(len(route_points) - 1)
    )


def test_route_matched_search_keeps_feasible_multistop_plan(settings):
    start = {'latitude': 41.0, 'longitude': -90.0}
    finish = {'latitude': 40.0, 'longitude': -80.0}
    distance_miles = 1200.0
    positions = {'01': 400.0, '02': 800.0, '03': 960.0}
    stations = tuple(
        FuelStation(
            station_id, label, 'I-80', city, 'IA', Decimal(price),
            start['latitude'] + (finish['latitude'] - start['latitude']) * positions[station_id] / distance_miles,
            start['longitude'] + (finish['longitude'] - start['longitude']) * positions[station_id] / distance_miles,
        )
        for station_id, label, city, price in (
            ('01', 'First IA', 'Iowa City', '3.50'),
            ('02', 'Second IA', 'Des Moines', '3.40'),
            ('03', 'Later IA', 'Council Bluffs', '2.00'),
        )
    )
    dataset = StationDataset(stations, StationLoadStats(3, 0, 0, 0, 0))

    class StubRepository:
        def load(self):
            return dataset

    class StubGeocoder:
        def verify_us_coordinates(self, coordinates):
            return {'coordinates': coordinates, 'state_code': 'IL' if coordinates == start else 'CO'}

    class StubRouter:
        def route(self, _start, _finish):
            return {
                'distance_miles': distance_miles,
                'duration_minutes': 1200,
                'geometry': {'type': 'LineString', 'coordinates': [
                    [start['longitude'], start['latitude']],
                    [finish['longitude'], finish['latitude']],
                ]},
            }

    with override_settings(STATION_GEOCODING_CANDIDATE_LIMIT=8):
        result = RoutePlanningService(
            geocoder=StubGeocoder(), router=StubRouter(), station_repository=StubRepository()
        ).plan({'start': start, 'finish': finish})

    assert result['diagnostics']['station_geocoding']['requests'] == []
    assert [stop['station_id'] for stop in result['fuel_stops']] == ['01', '02']
    assert result['fuel_optimization']['feasible'] is True
    assert result['fuel_optimization']['optimality_scope'] == 'best_among_route_matched_candidates'
    assert result['fuel_optimization']['candidate_search_complete_within_corridor'] is True
    search = result['diagnostics']['candidate_search']
    assert search['candidate_budget'] == 8
    assert search['candidates_available_in_corridor'] == 3
    assert search['candidates_shortlisted'] == 3
    assert search['candidates_evaluated'] == 3
    assert search['station_geocoding_attempts'] == 0
    assert search['candidates_prechecked_by_optimizer'] == 3
    assert search['candidates_not_evaluated_after_stop'] == 0
    assert search['stop_reason'] == 'feasible_plan_found'
    assert search['globally_complete'] is False
    positions_with_endpoints = [
        0.0,
        *[stop['approximate_along_route_mile'] for stop in result['fuel_stops']],
        distance_miles,
    ]
    assert all(
        positions_with_endpoints[index + 1] - positions_with_endpoints[index]
        <= settings.VEHICLE_MAX_RANGE_MILES
        for index in range(len(positions_with_endpoints) - 1)
    )
    assert any('candidate limit' in warning for warning in result['warnings'])


def test_candidate_budget_is_applied_after_matching_and_no_plan_is_scoped_to_matched_candidates():
    start = {'latitude': 41.0, 'longitude': -90.0}
    finish = {'latitude': 40.0, 'longitude': -80.0}
    distance_miles = 1200.0
    stations = tuple(
        FuelStation(
            f'{index:02}', f'IA {index}', f'I-80 {index}', 'Iowa City', 'IA', Decimal('3.50'),
            start['latitude'] + (finish['latitude'] - start['latitude']) * (650 + index) / distance_miles,
            start['longitude'] + (finish['longitude'] - start['longitude']) * (650 + index) / distance_miles,
        ) for index in range(23)
    )
    dataset = StationDataset(stations, StationLoadStats(23, 0, 0, 0, 0))
    calls = []

    class StubRepository:
        def load(self):
            return dataset

    class StubGeocoder:
        def verify_us_coordinates(self, coordinates):
            return {'coordinates': coordinates, 'state_code': 'IL' if coordinates == start else 'CO'}

    class StubRouter:
        def route(self, _start, _finish):
            return {
                'distance_miles': distance_miles,
                'duration_minutes': 1200,
                'geometry': {'type': 'LineString', 'coordinates': [
                    [start['longitude'], start['latitude']],
                    [finish['longitude'], finish['latitude']],
                ]},
            }

    with override_settings(STATION_GEOCODING_CANDIDATE_LIMIT=8):
        result = RoutePlanningService(
            geocoder=StubGeocoder(), router=StubRouter(), station_repository=StubRepository()
        ).plan({'start': start, 'finish': finish})

    assert calls == []
    assert result['optimization_status'] == 'no_feasible_plan'
    assert result['fuel_stops'] == []
    assert result['total_stop_purchase_cost'] is None
    assert 'among 23 evaluated station candidates' in result['fuel_optimization']['message']
    assert result['fuel_optimization']['optimality_scope'] == 'no_feasible_plan_among_route_matched_candidates'
    search = result['diagnostics']['candidate_search']
    assert search['candidate_budget'] == 8
    assert search['candidates_available_in_corridor'] == 23
    assert search['candidates_selected_for_evaluation'] == 8
    assert search['candidates_shortlisted'] == 8
    assert search['candidates_route_matched'] == 23
    assert search['candidates_prechecked_by_optimizer'] == 23
    assert search['candidates_evaluated'] == 23
    assert search['station_geocoding_attempts'] == 0
    assert search['stop_reason'] == 'all_located_corridor_candidates_evaluated'
    assert search['search_complete_within_corridor'] is True
    assert result['station_coverage']['candidates_excluded_by_limit'] == 15
    assert len(result['station_coverage']['matched_candidate_positions']) == 23
    assert len(search['selected_candidate_ids']) == 8
    candidate_positions = {
        item['station_id']: item['along_route_mile']
        for item in result['station_coverage']['matched_candidate_positions']
    }
    selected_positions = [candidate_positions[item] for item in search['selected_candidate_ids']]
    assert selected_positions == sorted(selected_positions)
    assert selected_positions[0] == 650.0
    assert selected_positions[-1] == 672.0
    assert all(candidate['along_route_mile'] > 500 for candidate in result['station_coverage']['matched_candidate_positions'])


def test_candidates_after_original_eight_are_matched_and_can_make_plan_feasible(settings):
    start = {'latitude': 41.8, 'longitude': -87.6}
    finish = {'latitude': 39.7, 'longitude': -105.0}
    distance_miles = 1004.4

    def point_at(position, *, latitude_offset=0.0):
        fraction = position / distance_miles
        return (
            start['latitude'] + (finish['latitude'] - start['latitude']) * fraction + latitude_offset,
            start['longitude'] + (finish['longitude'] - start['longitude']) * fraction,
        )

    stations = tuple(
        FuelStation(
            f'{index:02}', f'Station {index}', f'Address {index}', 'Iowa City', 'IA',
            Decimal('3.50'), *point_at(100 + index * 10, latitude_offset=3.0),
        )
        for index in range(8)
    ) + (
        FuelStation('08', 'Route stop 1', 'I-80', 'Iowa City', 'IA', Decimal('3.50'), *point_at(300)),
        FuelStation('09', 'Route stop 2', 'I-80', 'Omaha', 'NE', Decimal('3.40'), *point_at(700)),
    )
    dataset = StationDataset(stations, StationLoadStats(10, 0, 0, 0, 0))

    class StubRepository:
        def load(self):
            return dataset

    class StubGeocoder:
        def verify_us_coordinates(self, coordinates):
            return {'coordinates': coordinates, 'state_code': 'IL' if coordinates == start else 'CO'}

    route_calls = []

    class StubRouter:
        def route(self, _start, _finish):
            route_calls.append((_start, _finish))
            return {
                'distance_miles': distance_miles,
                'duration_minutes': 1069,
                'geometry': {'type': 'LineString', 'coordinates': [
                    [start['longitude'], start['latitude']],
                    [finish['longitude'], finish['latitude']],
                ]},
            }

    with override_settings(STATION_GEOCODING_CANDIDATE_LIMIT=8):
        result = RoutePlanningService(
            geocoder=StubGeocoder(), router=StubRouter(), station_repository=StubRepository()
        ).plan({'start': start, 'finish': finish})

    assert result['optimization_status'] == 'optimized'
    assert [stop['station_id'] for stop in result['fuel_stops']] == ['08', '09']
    legs = result['fuel_optimization']['route_legs']
    assert [leg['estimated_distance_miles'] for leg in legs] == [300.0, 400.0, 304.4]
    assert all(leg['within_maximum_range'] for leg in legs)
    assert result['station_coverage']['stations_considered'] == 10
    assert result['station_coverage']['stations_matched'] == 2
    search = result['diagnostics']['candidate_search']
    assert search['candidates_route_matched'] == 2
    assert search['selected_candidate_ids'] == ['08', '09']
    assert len(route_calls) == 1
    assert result['diagnostics']['station_coordinate_lookup']['outbound_http_requests'] == 0


def test_station_candidates_use_stored_coordinates_without_station_geocoding(settings):
    stations = (
        FuelStation('1', 'First', 'Same address', 'Chicago', 'IL', Decimal('3.50'), 41, -90),
        FuelStation('2', 'Second', 'Same address', 'Chicago', 'IL', Decimal('3.60'), 41.2, -89.5),
        FuelStation('3', 'Missing', 'Unmapped address', 'Chicago', 'IL', Decimal('3.70')),
    )
    dataset = StationDataset(stations, StationLoadStats(3, 0, 0, 0, 0))

    class StubRepository:
        def load(self):
            return dataset

    service = RoutePlanningService(station_repository=StubRepository())
    route = {
        'distance_miles': 1200,
        'geometry': {'type': 'LineString', 'coordinates': [[-91, 41], [-89, 41]]},
    }

    with override_settings(STATION_GEOCODING_CANDIDATE_LIMIT=3):
        _matched, coverage, plan = service._evaluate_stations(
            {'state_code': 'IL'}, {'state_code': 'IL'}, route
        )

    station_diagnostics = service._diagnostics['station_geocoding']
    assert coverage['stations_considered'] == 2
    assert coverage['candidates_selected_for_evaluation'] == 2
    assert coverage['stations_geocoded'] == 2
    assert coverage['stations_skipped'] == 1
    assert station_diagnostics['requests'] == []
    assert station_diagnostics['cache_hits'] == 0
    assert station_diagnostics['cache_misses'] == 0
    assert station_diagnostics['total_duration_ms'] == 0
    assert service._diagnostics['station_coordinate_lookup']['outbound_http_requests'] == 0
    assert service._diagnostics['candidate_search']['station_geocoding_attempts'] == 0
    assert len(_matched) == 2
    assert plan is not None


def test_unenriched_stations_are_not_queried_during_route_planning(monkeypatch, settings):
    stations = tuple(
        FuelStation(str(index), f'Station {index}', f'Address {index}', 'Chicago', 'IL', Decimal('3.50'))
        for index in range(3)
    )
    dataset = StationDataset(stations, StationLoadStats(3, 0, 0, 0, 0))

    class StubRepository:
        def load(self):
            return dataset

    def unexpected_station_lookup(*_args, **_kwargs):
        raise AssertionError('Route planning must not geocode station addresses.')

    service = RoutePlanningService(station_repository=StubRepository())
    monkeypatch.setattr(service.geocoder, 'geocode_us_place', unexpected_station_lookup)
    route = {
        'distance_miles': 600,
        'geometry': {'type': 'LineString', 'coordinates': [[-91, 41], [-89, 41]]},
    }

    _matched, coverage, plan = service._evaluate_stations(
        {'state_code': 'IL'}, {'state_code': 'IL'}, route
    )

    assert coverage['stations_considered'] == 0
    assert coverage['stations_missing_stored_coordinates'] == 3
    assert service._diagnostics['station_geocoding']['requests'] == []
    assert service._diagnostics['station_coordinate_lookup']['outbound_http_requests'] == 0
    assert plan.feasible is False


def test_cold_long_route_stops_after_feasible_candidate_and_reuses_cache(monkeypatch):
    cache.clear()
    with geocoding._nominatim_lock:
        geocoding._last_nominatim_request_at = 0.0
    start = {'latitude': 41.8, 'longitude': -87.6}
    finish = {'latitude': 41.8, 'longitude': -80.0}
    stations = tuple(
        FuelStation(
            str(index), f'Station {index}', f'Unique address {index}', 'Chicago', 'IL',
            Decimal('3.50'), 41.8, -83.8,
        )
        for index in range(25)
    )
    dataset = StationDataset(stations, StationLoadStats(25, 0, 0, 0, 0))
    calls = {'nominatim': 0, 'osrm': 0}

    class StubRepository:
        def load(self):
            return dataset

    def fake_get(url, **kwargs):
        if url in (settings.NOMINATIM_SEARCH_URL, settings.NOMINATIM_REVERSE_URL):
            calls['nominatim'] += 1
            if url == settings.NOMINATIM_REVERSE_URL:
                payload = {
                    'address': {'country_code': 'us', 'state': 'Illinois', 'ISO3166-2-lvl4': 'US-IL'},
                }
            else:
                payload = [{
                    'lat': '41.8', 'lon': '-83.8',
                    'address': {'country_code': 'us', 'state': 'Illinois', 'ISO3166-2-lvl4': 'US-IL'},
                }]
            return httpx.Response(200, json=payload, request=httpx.Request('GET', 'https://test.invalid'))
        calls['osrm'] += 1
        return httpx.Response(200, json={
            'code': 'Ok',
            'routes': [{
                'distance': 600 / 0.000621371,
                'duration': 36_000,
                'geometry': {'type': 'LineString', 'coordinates': [[-87.6, 41.8], [-80.0, 41.8]]},
            }],
        }, request=httpx.Request('GET', 'https://test.invalid'))

    monkeypatch.setattr(httpx, 'get', fake_get)
    monkeypatch.setattr('routing.clients.geocoding.time.sleep', lambda _seconds: None)

    def run_plan():
        return RoutePlanningService(
            geocoder=NominatimClient(),
            station_repository=StubRepository(),
        ).plan({'start': start, 'finish': finish})

    with override_settings(STATION_GEOCODING_CANDIDATE_LIMIT=8):
        cold = run_plan()
        assert cold['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'] == 2
        assert cold['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'] == 1
        assert cold['diagnostics']['station_geocoding']['requests'] == []
        assert cold['diagnostics']['station_coordinate_lookup']['outbound_http_requests'] == 0
        assert cold['diagnostics']['candidate_search']['candidate_budget'] == 8
        assert cold['diagnostics']['candidate_search']['candidates_shortlisted'] == 8
        assert cold['diagnostics']['candidate_search']['candidates_evaluated'] == 25
        assert cold['diagnostics']['candidate_search']['stop_reason'] == 'feasible_plan_found'
        assert cold['diagnostics']['candidate_search']['globally_complete'] is False
        assert cold['station_coverage']['candidates_not_evaluated_after_stop'] == 0
        assert calls == {'nominatim': 2, 'osrm': 1}

        repeated = run_plan()
        assert repeated['diagnostics']['upstream_requests']['nominatim']['outbound_http_requests'] == 0
        assert repeated['diagnostics']['upstream_requests']['nominatim']['cache_hits'] == 2
        assert repeated['diagnostics']['upstream_requests']['osrm']['outbound_http_requests'] == 0
        assert repeated['diagnostics']['upstream_requests']['osrm']['cache_hits'] == 1
        assert calls == {'nominatim': 2, 'osrm': 1}
