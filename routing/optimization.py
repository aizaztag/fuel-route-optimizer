from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math

from .route_matching import RouteStation

CENT = Decimal('0.01')


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FuelStopPlan:
    feasible: bool
    stops: list[dict]
    total_estimated_cost: float | None
    reason: str | None = None


@dataclass(frozen=True)
class _Node:
    position: float
    lateral_distance: float
    candidate: RouteStation | None


class FuelStopOptimizer:
    """Dynamic program minimizing purchase cost over route-ordered reachable stations.

    The initial tank is full and every selected stop refills it to full. A station's
    purchase replaces fuel consumed since the previous fill, so the incoming leg's
    gallons are charged at that station's price. This does not model reserves,
    partial-fill choices, traffic, or uncertain road detours.
    """

    def optimize(
        self,
        route_distance_miles: float,
        candidates: list[RouteStation],
        max_range_miles: float,
        fuel_economy_mpg: float,
    ) -> FuelStopPlan:
        if (
            not math.isfinite(max_range_miles)
            or not math.isfinite(fuel_economy_mpg)
            or max_range_miles <= 0
            or fuel_economy_mpg <= 0
        ):
            raise ValueError('Vehicle range and fuel economy must be positive.')
        if not math.isfinite(route_distance_miles) or route_distance_miles < 0:
            raise ValueError('Route distance cannot be negative.')

        ordered_candidates = sorted(
            (
                candidate for candidate in candidates
                if 0 < candidate.along_route_mile < route_distance_miles
            ),
            key=lambda candidate: (candidate.along_route_mile, candidate.station.station_id),
        )
        nodes = [_Node(0.0, 0.0, None)]
        nodes.extend(
            _Node(
                candidate.along_route_mile,
                candidate.distance_from_route_miles,
                candidate,
            )
            for candidate in ordered_candidates
        )
        nodes.append(_Node(route_distance_miles, 0.0, None))

        # Each dynamic-programming state stores (cost, node-index path).
        best: list[tuple[Decimal, tuple[int, ...]] | None] = [None] * len(nodes)
        best[0] = (Decimal('0'), (0,))
        mpg = Decimal(str(fuel_economy_mpg))

        for destination_index in range(1, len(nodes)):
            destination = nodes[destination_index]
            for source_index in range(destination_index):
                source_state = best[source_index]
                if source_state is None:
                    continue
                source = nodes[source_index]
                along_leg = destination.position - source.position
                if along_leg <= 0:
                    continue
                # Station offset is an estimated straight-line access leg. It is not
                # represented as an exact road detour; the API labels it accordingly.
                leg_miles = along_leg + source.lateral_distance + destination.lateral_distance
                if leg_miles > max_range_miles:
                    continue

                purchase_cost = Decimal('0')
                if destination.candidate is not None:
                    gallons = Decimal(str(leg_miles)) / mpg
                    purchase_cost = _money(gallons * destination.candidate.station.retail_price)
                new_state = (
                    source_state[0] + purchase_cost,
                    source_state[1] + (destination_index,),
                )
                current_best = best[destination_index]
                if current_best is None or _state_sort_key(new_state, nodes) < _state_sort_key(current_best, nodes):
                    best[destination_index] = new_state

        final_state = best[-1]
        if final_state is None:
            return FuelStopPlan(
                feasible=False,
                stops=[],
                total_estimated_cost=None,
                reason=(
                    'No feasible fuel-stop plan was found using the located station candidates '
                    'and configured maximum vehicle range.'
                ),
            )

        path = final_state[1]
        stops: list[dict] = []
        for path_offset in range(1, len(path) - 1):
            node_index = path[path_offset]
            previous_node_index = path[path_offset - 1]
            node = nodes[node_index]
            previous_node = nodes[previous_node_index]
            candidate = node.candidate
            if candidate is None:
                continue
            leg_miles = (
                node.position - previous_node.position
                + node.lateral_distance
                + previous_node.lateral_distance
            )
            gallons = Decimal(str(leg_miles)) / mpg
            cost = _money(gallons * candidate.station.retail_price)
            stop = {
                'station_id': candidate.station.station_id,
                'name': candidate.station.name,
                'address': candidate.station.address,
                'city': candidate.station.city,
                'state': candidate.station.state,
                'price_per_gallon': float(candidate.station.retail_price),
                'estimated_gallons_purchased': round(float(gallons), 3),
                'estimated_purchase_cost': float(cost),
                'approximate_along_route_mile': round(candidate.along_route_mile, 2),
                'distance_from_route_miles': round(candidate.distance_from_route_miles, 2),
                'estimated_detour_miles': round(candidate.estimated_detour_miles, 2),
            }
            if candidate.coordinates is not None:
                stop['coordinates'] = {
                    'latitude': candidate.coordinates[0],
                    'longitude': candidate.coordinates[1],
                }
            stops.append(stop)

        return FuelStopPlan(
            feasible=True,
            stops=stops,
            total_estimated_cost=float(final_state[0]),
        )


def _state_sort_key(
    state: tuple[Decimal, tuple[int, ...]],
    nodes: list[_Node],
) -> tuple[Decimal, int, tuple[str, ...]]:
    station_ids = tuple(
        nodes[index].candidate.station.station_id
        for index in state[1]
        if nodes[index].candidate is not None
    )
    return state[0], len(station_ids), station_ids
