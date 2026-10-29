from decimal import Decimal

from routing.optimization import FuelStopOptimizer
from routing.route_matching import RouteStation, RouteStationMatcher
from routing.station_data import FuelStation


def station(station_id, price='3.00'):
    return FuelStation(
        station_id=station_id,
        name=f'Station {station_id}',
        address='I-80 exit 1',
        city='Example',
        state='NY',
        retail_price=Decimal(price),
    )


def candidate(station_id, position, price='3.00', offset=0.0):
    return RouteStation(
        station=station(station_id, price),
        along_route_mile=position,
        distance_from_route_miles=offset,
        estimated_detour_miles=offset * 2,
    )


def test_route_corridor_match_rejects_far_stations_and_sorts_by_route_position():
    matcher = RouteStationMatcher(corridor_width_miles=10, max_detour_miles=20)
    geometry = {'type': 'LineString', 'coordinates': [[0, 0], [2, 0]]}
    inputs = [
        (station('late'), {'coordinates': {'latitude': 0, 'longitude': 1.5}}),
        (station('far'), {'coordinates': {'latitude': 0.5, 'longitude': 1}}),
        (station('early'), {'coordinates': {'latitude': 0, 'longitude': 0.5}}),
    ]

    matched = matcher.match_many(inputs, geometry, route_distance_miles=140)

    assert [item.station.station_id for item in matched] == ['early', 'late']
    assert matched[0].along_route_mile < matched[1].along_route_mile
    assert matched[0].distance_from_route_miles < 0.1

    detour_limited = RouteStationMatcher(corridor_width_miles=50, max_detour_miles=20)
    too_costly_to_access = detour_limited.match(
        station('detour'),
        {'coordinates': {'latitude': 0.3, 'longitude': 1}},
        geometry,
        route_distance_miles=140,
    )
    assert too_costly_to_access is None


def test_optimizer_enforces_each_leg_range_and_chooses_lower_cost_path():
    optimizer = FuelStopOptimizer()
    candidates = [
        candidate('cheap-first', 300, '2.00'),
        candidate('expensive-middle', 450, '10.00'),
        candidate('cheap-last', 700, '3.00'),
    ]

    plan = optimizer.optimize(900, candidates, max_range_miles=500, fuel_economy_mpg=10)

    assert plan.feasible
    assert [stop['station_id'] for stop in plan.stops] == ['cheap-first', 'cheap-last']
    assert [stop['estimated_gallons_purchased'] for stop in plan.stops] == [30.0, 40.0]
    assert [stop['estimated_purchase_cost'] for stop in plan.stops] == [60.0, 120.0]
    assert plan.total_estimated_cost == 180.0


def test_optimizer_accounts_for_fuel_economy_and_refills_consumed_fuel():
    plan = FuelStopOptimizer().optimize(
        600,
        [candidate('midpoint', 300, '4.00')],
        max_range_miles=500,
        fuel_economy_mpg=10,
    )

    assert plan.feasible
    assert plan.stops[0]['estimated_gallons_purchased'] == 30.0
    assert plan.stops[0]['estimated_purchase_cost'] == 120.0
    assert plan.total_estimated_cost == 120.0


def test_total_cost_matches_sum_of_per_stop_costs_after_currency_rounding():
    plan = FuelStopOptimizer().optimize(
        9,
        [candidate('first', 3, '3.35'), candidate('second', 6, '3.35')],
        max_range_miles=4,
        fuel_economy_mpg=10,
    )

    assert [stop['estimated_purchase_cost'] for stop in plan.stops] == [1.01, 1.01]
    assert plan.total_estimated_cost == 2.02
    assert sum(stop['estimated_purchase_cost'] for stop in plan.stops) == plan.total_estimated_cost


def test_route_within_full_tank_range_requires_no_stops():
    plan = FuelStopOptimizer().optimize(400, [], max_range_miles=500, fuel_economy_mpg=10)
    assert plan.feasible
    assert plan.stops == []
    assert plan.total_estimated_cost == 0.0


def test_no_feasible_plan_is_explicit_when_range_gaps_are_too_large():
    plan = FuelStopOptimizer().optimize(
        900,
        [candidate('too-late', 600)],
        max_range_miles=500,
        fuel_economy_mpg=10,
    )
    assert not plan.feasible
    assert plan.stops == []
    assert plan.total_estimated_cost is None
    assert 'No feasible' in plan.reason
