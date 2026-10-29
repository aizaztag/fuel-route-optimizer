import math
from dataclasses import dataclass

from django.conf import settings

from .station_data import FuelStation

EARTH_RADIUS_MILES = 3958.7613


@dataclass(frozen=True)
class RouteStation:
    station: FuelStation
    along_route_mile: float
    distance_from_route_miles: float
    estimated_detour_miles: float
    coordinates: tuple[float, float] | None = None  # (latitude, longitude)


def _haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(value)))


def _nearest_position(
    point: tuple[float, float],
    line: list[list[float]],
    route_distance_miles: float,
) -> tuple[float, float] | None:
    if len(line) < 2:
        return None

    segment_lengths = [
        _haversine_miles(tuple(line[index]), tuple(line[index + 1]))
        for index in range(len(line) - 1)
    ]
    geometry_length = sum(segment_lengths)
    if geometry_length <= 0:
        return None

    cumulative_miles = 0.0
    best_distance = math.inf
    best_along = 0.0
    point_lon, point_lat = point
    for index, (start, finish) in enumerate(zip(line, line[1:])):
        reference_lat = math.radians((point_lat + start[1] + finish[1]) / 3)
        x_scale = EARTH_RADIUS_MILES * math.cos(reference_lat) * math.pi / 180
        y_scale = EARTH_RADIUS_MILES * math.pi / 180
        start_xy = (start[0] * x_scale, start[1] * y_scale)
        finish_xy = (finish[0] * x_scale, finish[1] * y_scale)
        point_xy = (point_lon * x_scale, point_lat * y_scale)
        dx = finish_xy[0] - start_xy[0]
        dy = finish_xy[1] - start_xy[1]
        segment_squared = dx * dx + dy * dy
        fraction = 0.0 if segment_squared == 0 else (
            (point_xy[0] - start_xy[0]) * dx + (point_xy[1] - start_xy[1]) * dy
        ) / segment_squared
        fraction = min(1.0, max(0.0, fraction))
        nearest_xy = (start_xy[0] + fraction * dx, start_xy[1] + fraction * dy)
        distance = math.hypot(point_xy[0] - nearest_xy[0], point_xy[1] - nearest_xy[1])
        if distance < best_distance:
            best_distance = distance
            best_along = cumulative_miles + fraction * segment_lengths[index]
        cumulative_miles += segment_lengths[index]

    scaled_along = best_along * route_distance_miles / geometry_length
    return min(route_distance_miles, max(0.0, scaled_along)), best_distance


class RouteStationMatcher:
    """Matches geocoded stations to a route polyline using approximate miles."""

    def __init__(
        self,
        corridor_width_miles: float | None = None,
        max_detour_miles: float | None = None,
    ) -> None:
        self.corridor_width_miles = (
            settings.ROUTE_CORRIDOR_WIDTH_MILES
            if corridor_width_miles is None
            else corridor_width_miles
        )
        self.max_detour_miles = (
            settings.MAX_STATION_DETOUR_MILES
            if max_detour_miles is None
            else max_detour_miles
        )

    def match(
        self,
        station: FuelStation,
        place: dict,
        geometry: dict,
        route_distance_miles: float,
    ) -> RouteStation | None:
        coordinates = geometry.get('coordinates', [])
        point = (
            float(place['coordinates']['longitude']),
            float(place['coordinates']['latitude']),
        )
        nearest = _nearest_position(point, coordinates, route_distance_miles)
        if nearest is None:
            return None
        along_route_mile, distance_from_route = nearest
        estimated_detour = distance_from_route * 2
        if distance_from_route > self.corridor_width_miles:
            return None
        if estimated_detour > self.max_detour_miles:
            return None
        return RouteStation(
            station=station,
            along_route_mile=along_route_mile,
            distance_from_route_miles=distance_from_route,
            estimated_detour_miles=estimated_detour,
            coordinates=(
                float(place['coordinates']['latitude']),
                float(place['coordinates']['longitude']),
            ),
        )

    def match_many(
        self,
        stations_and_places: list[tuple[FuelStation, dict]],
        geometry: dict,
        route_distance_miles: float,
    ) -> list[RouteStation]:
        matched = [
            candidate
            for station, place in stations_and_places
            if (candidate := self.match(station, place, geometry, route_distance_miles)) is not None
        ]
        return sorted(
            matched,
            key=lambda candidate: (candidate.along_route_mile, candidate.station.station_id),
        )
