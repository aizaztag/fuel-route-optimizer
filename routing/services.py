import logging
from time import perf_counter
from typing import Any

from django.conf import settings

from .clients.geocoding import NominatimClient
from .clients.osrm import OSRMClient
from .optimization import FuelStopOptimizer
from .route_matching import RouteStationMatcher
from .station_data import FuelStationRepository, StationDatasetError
from .state_corridor import state_corridor

logger = logging.getLogger(__name__)

NO_STOPS_COVERAGE = {
    'stations_considered': 0,
    'stations_geocoded': 0,
    'stations_skipped': 0,
    'stations_matched': 0,
    'stations_eligible': 0,
    'stored_coordinate_matches_available': 0,
    'stored_coordinate_no_matches': 0,
    'stored_coordinate_failures': 0,
    'stations_missing_stored_coordinates': 0,
    'candidates_available': 0,
    'candidates_excluded_by_limit': 0,
    'geocode_failures': 0,
    'endpoint_states_evaluated': [],
    'candidate_states_evaluated': [],
    'matched_candidate_positions': [],
    'stations_deferred_due_to_rate_limit': 0,
    'candidates_selected_for_evaluation': 0,
    'candidates_shortlisted': 0,
    'station_geocoding_attempts': 0,
    'candidates_not_evaluated_after_stop': 0,
    'dataset_rows_seen': 0,
    'dataset_stations_loaded': 0,
    'dataset_malformed_rows': 0,
    'dataset_invalid_price_rows': 0,
    'dataset_duplicate_station_ids': 0,
    'dataset_duplicate_rows': 0,
}


class RoutePlanningService:
    """Orchestrates route resolution, bounded station lookup and fuel planning."""

    def __init__(
        self,
        geocoder: NominatimClient | None = None,
        router: OSRMClient | None = None,
        station_repository: FuelStationRepository | None = None,
        matcher: RouteStationMatcher | None = None,
        optimizer: FuelStopOptimizer | None = None,
    ) -> None:
        self.geocoder = geocoder or NominatimClient()
        self.router = router or OSRMClient()
        self.station_repository = station_repository or FuelStationRepository()
        self.matcher = matcher or RouteStationMatcher()
        self.optimizer = optimizer or FuelStopOptimizer()
        self._diagnostics: dict[str, Any] = {
            'timings_ms': {
                'start_geocoding': 0.0,
                'finish_geocoding': 0.0,
                'osrm_route': 0.0,
                'candidate_station_selection': 0.0,
                'station_geocoding_total': 0.0,
                'station_route_matching': 0.0,
                'fuel_optimization': 0.0,
                'route_planning_service_total': 0.0,
                'total_request_processing': 0.0,
            },
            'endpoint_geocoding': {},
            'upstream_requests': {},
            'station_geocoding': {
                'total_duration_ms': 0.0,
                'cache_hits': 0,
                'cache_misses': 0,
                'failed_requests': 0,
                'failure_categories': {},
                'requests': [],
            },
            'station_coordinate_lookup': {
                'source': 'local_database',
                'matched_coordinates_available': 0,
                'no_match_records': 0,
                'failed_records': 0,
                'missing_or_stale_records': 0,
                'outbound_http_requests': 0,
            },
        }

    def _geocode_endpoint(self, label: str, method: Any, value: Any) -> dict:
        lookup_events = getattr(self.geocoder, 'lookup_events', [])
        event_index = len(lookup_events)
        started = perf_counter()
        failed = False
        try:
            return method(value)
        except Exception:
            failed = True
            raise
        finally:
            elapsed_ms = round((perf_counter() - started) * 1000, 3)
            event = lookup_events[event_index] if len(lookup_events) > event_index else {}
            self._diagnostics['timings_ms'][f'{label}_geocoding'] = elapsed_ms
            self._diagnostics['endpoint_geocoding'][label] = {
                'duration_ms': elapsed_ms,
                'cache': event.get('cache_status', 'unknown'),
                'http_status': event.get('http_status'),
                'failed': failed or event.get('failed', False),
                'failure_category': event.get('failure_category'),
                'failure_source': event.get('failure_source'),
            }
            logger.info(
                'Endpoint geocoding: endpoint=%s duration_ms=%.3f cache=%s '
                'http_status=%s failed=%s',
                label, elapsed_ms, event.get('cache_status', 'unknown'),
                event.get('http_status'), failed or event.get('failed', False),
            )

    def _resolve_endpoints(self, validated_data: dict[str, Any]) -> tuple[dict, dict]:
        if 'start_address' in validated_data:
            start_place = self._geocode_endpoint(
                'start', self.geocoder.geocode_us_place, validated_data['start_address']
            )
            finish_place = self._geocode_endpoint(
                'finish', self.geocoder.geocode_us_place, validated_data['finish_address']
            )
        else:
            start_place = self._geocode_endpoint(
                'start', self.geocoder.verify_us_coordinates, validated_data['start']
            )
            finish_place = self._geocode_endpoint(
                'finish', self.geocoder.verify_us_coordinates, validated_data['finish']
            )
        return start_place, finish_place

    @staticmethod
    def _warning_messages(route_distance: float, station_coverage: dict) -> list[str]:
        warnings = []
        if route_distance > settings.VEHICLE_MAX_RANGE_MILES:
            warnings.append(
                'Station coverage is incomplete: candidates are limited to estimated shortest '
                'state-adjacency corridors and the configured candidate limit; actual route states '
                'and stations may not all be evaluated.'
            )
            warnings.append(
                'Station access and detour distances are straight-line estimates, not exact driving routes.'
            )
            if station_coverage['stations_skipped'] or station_coverage['candidates_excluded_by_limit']:
                warnings.append(
                    'Some candidates lack stored coordinates or were not evaluated because the candidate limit was reached.'
                )
        return warnings

    def _evaluate_stations(
        self, start_place: dict, finish_place: dict, route: dict
    ) -> tuple[list, dict, Any]:
        selection_started = perf_counter()
        dataset = self.station_repository.load()
        endpoint_states = {
            state_code
            for state_code in (start_place.get('state_code'), finish_place.get('state_code'))
            if state_code
        }
        candidate_states = state_corridor(
            start_place.get('state_code'),
            finish_place.get('state_code'),
        )
        all_candidates = [
            station for station in dataset.stations
            if station.state in candidate_states
        ]
        located_candidates = [
            station for station in all_candidates
            if station.latitude is not None and station.longitude is not None
        ]
        coordinate_stats = dataset.coordinate_stats or {
            'matched': len(located_candidates), 'no_match': 0, 'failed': 0,
            'missing_or_stale': len(all_candidates) - len(located_candidates), 'by_state': {},
        }
        state_coordinate_stats = coordinate_stats.get('by_state', {})
        corridor_coordinate_stats = {
            key: (
                sum(state_coordinate_stats.get(state, {}).get(key, 0) for state in candidate_states)
                if state_coordinate_stats
                else coordinate_stats.get(key, 0)
            )
            for key in ('matched', 'no_match', 'failed', 'missing_or_stale')
        }
        candidates_by_state: dict[str, list] = {}
        for station in sorted(located_candidates, key=lambda item: item.station_id):
            candidates_by_state.setdefault(station.state, []).append(station)
        limit = max(0, settings.STATION_GEOCODING_CANDIDATE_LIMIT)
        selected = []
        selected_per_state = {state: 0 for state in sorted(candidate_states)}
        while len(selected) < limit:
            added = False
            for state in sorted(candidate_states):
                bucket = candidates_by_state.get(state, [])
                index = selected_per_state[state]
                if index < len(bucket):
                    selected.append(bucket[index])
                    selected_per_state[state] += 1
                    added = True
                    if len(selected) >= limit:
                        break
            if not added:
                break
        selection_duration_ms = round((perf_counter() - selection_started) * 1000, 3)
        self._diagnostics['timings_ms']['candidate_station_selection'] = selection_duration_ms
        self._diagnostics['station_coordinate_lookup'].update({
            'matched_coordinates_available': sum(
                station.state in candidate_states and station.latitude is not None
                and station.longitude is not None for station in dataset.stations
            ),
            'no_match_records': corridor_coordinate_stats['no_match'],
            'failed_records': corridor_coordinate_stats['failed'],
            'missing_or_stale_records': corridor_coordinate_stats['missing_or_stale'],
        })
        logger.info(
            'Candidate station selection: duration_ms=%.3f dataset_stations=%d '
            'candidate_states=%s candidates_available=%d stored_coordinates=%d selected=%d',
            selection_duration_ms, len(dataset.stations), sorted(candidate_states),
            len(all_candidates), len(located_candidates), len(selected),
        )

        matched: list[Any] = []
        geocode_failures = 0
        matching_duration_ms = 0.0
        optimization_duration_ms = 0.0
        optimization_iterations = 0
        plan = None
        candidates_evaluated = 0

        # Candidate matching now uses coordinates enriched in a separate local batch.
        # Normal route requests never geocode fuel stations.
        for station in selected[:limit]:
            candidates_evaluated += 1
            matching_started = perf_counter()
            place = {'coordinates': {
                'latitude': station.latitude,
                'longitude': station.longitude,
            }}
            matched.extend(self.matcher.match_many(
                [(station, place)], route['geometry'], route['distance_miles']
            ))
            matched.sort(key=lambda candidate: (
                candidate.along_route_mile, candidate.station.station_id
            ))
            matching_duration_ms += (perf_counter() - matching_started) * 1000

            # Re-run the existing dynamic program on the accumulated matched set.
            # A feasible result is valid under the optimizer's per-leg range checks;
            # it is the best result found so far, not a global-cost guarantee.
            optimization_started = perf_counter()
            plan = self.optimizer.optimize(
                route_distance_miles=route['distance_miles'],
                candidates=matched,
                max_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
                fuel_economy_mpg=settings.VEHICLE_FUEL_ECONOMY_MPG,
            )
            optimization_duration_ms += (perf_counter() - optimization_started) * 1000
            optimization_iterations += 1
            if plan.feasible:
                break

        # Even if every lookup failed or missed the route, produce the optimizer's
        # standard infeasible result without changing its behavior.
        if plan is None:
            optimization_started = perf_counter()
            plan = self.optimizer.optimize(
                route_distance_miles=route['distance_miles'],
                candidates=matched,
                max_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
                fuel_economy_mpg=settings.VEHICLE_FUEL_ECONOMY_MPG,
            )
            optimization_duration_ms += (perf_counter() - optimization_started) * 1000
            optimization_iterations += 1

        not_evaluated_after_stop = len(selected) - candidates_evaluated
        if plan.feasible:
            stop_reason = 'feasible_plan_found'
        elif not all_candidates:
            stop_reason = 'no_candidates_available'
        elif not located_candidates:
            stop_reason = 'no_stored_coordinates_available'
        elif candidates_evaluated >= len(located_candidates):
            stop_reason = 'all_located_corridor_candidates_evaluated'
        elif candidates_evaluated >= limit:
            stop_reason = 'candidate_budget_exhausted'
        else:
            stop_reason = 'candidate_search_ended'
        search_complete = candidates_evaluated >= len(all_candidates)
        located_search_complete = candidates_evaluated >= len(located_candidates)
        self._diagnostics['candidate_search'] = {
            'candidate_budget': limit,
            'candidates_available_in_corridor': len(all_candidates),
            'candidates_with_stored_coordinates': len(located_candidates),
            'candidates_without_stored_coordinates': len(all_candidates) - len(located_candidates),
            'candidates_selected_for_evaluation': len(selected),
            'candidates_shortlisted': len(selected),
            'candidates_evaluated': candidates_evaluated,
            'station_geocoding_attempts': 0,
            'candidates_not_evaluated_after_stop': not_evaluated_after_stop,
            'search_complete_within_corridor': search_complete,
            'search_complete_within_located_corridor': located_search_complete,
            'globally_complete': False,
            'optimality_scope': 'best_among_evaluated_candidates',
            'stop_reason': stop_reason,
            'optimization_iterations': optimization_iterations,
        }

        station_geocoding_duration_ms = 0.0
        self._diagnostics['station_geocoding']['total_duration_ms'] = station_geocoding_duration_ms
        self._diagnostics['timings_ms']['station_geocoding_total'] = station_geocoding_duration_ms
        logger.info(
            'Station geocoding complete: duration_ms=%.3f requests=0; station coordinates are local',
            station_geocoding_duration_ms,
        )

        matching_duration_ms = round(matching_duration_ms, 3)
        self._diagnostics['timings_ms']['station_route_matching'] = matching_duration_ms
        logger.info(
            'Station route matching: duration_ms=%.3f evaluated=%d matched=%d',
            matching_duration_ms, candidates_evaluated, len(matched),
        )
        self._diagnostics['timings_ms']['fuel_optimization'] = round(
            optimization_duration_ms, 3
        )
        logger.info(
            'Fuel optimization: duration_ms=%.3f iterations=%d feasible=%s',
            optimization_duration_ms, optimization_iterations, plan.feasible,
        )
        matched_positions = [
            {
                'station_id': candidate.station.station_id,
                'state': candidate.station.state,
                'along_route_mile': round(candidate.along_route_mile, 2),
                'access_offset_miles': round(candidate.distance_from_route_miles, 2),
            }
            for candidate in matched
        ]
        coverage = {
            'stations_considered': candidates_evaluated,
            'stations_geocoded': candidates_evaluated,
            'stations_skipped': len(all_candidates) - len(located_candidates),
            'stations_matched': len(matched),
            'stations_eligible': len(matched),
            'candidates_available': len(all_candidates),
            'candidates_excluded_by_limit': len(located_candidates) - len(selected),
            'geocode_failures': corridor_coordinate_stats['failed'],
            'stored_coordinate_matches_available': len(located_candidates),
            'stored_coordinate_no_matches': corridor_coordinate_stats['no_match'],
            'stored_coordinate_failures': corridor_coordinate_stats['failed'],
            'stations_missing_stored_coordinates': corridor_coordinate_stats['missing_or_stale'],
            'endpoint_states_evaluated': sorted(endpoint_states),
            'candidate_states_evaluated': sorted(candidate_states),
            'matched_candidate_positions': matched_positions,
            'stations_deferred_due_to_rate_limit': 0,
            'candidates_selected_for_evaluation': len(selected),
            'candidates_shortlisted': len(selected),
            'station_geocoding_attempts': 0,
            'candidates_not_evaluated_after_stop': not_evaluated_after_stop,
            'dataset_rows_seen': dataset.stats.rows_seen,
            'dataset_stations_loaded': len(dataset.stations),
            'dataset_malformed_rows': dataset.stats.malformed_rows,
            'dataset_invalid_price_rows': dataset.stats.invalid_price_rows,
            'dataset_duplicate_station_ids': dataset.stats.duplicate_station_ids,
            'dataset_duplicate_rows': dataset.stats.duplicate_rows,
        }
        return matched, coverage, plan

    def plan(self, validated_data: dict[str, Any]) -> dict[str, Any]:
        request_started = perf_counter()
        start_place, finish_place = self._resolve_endpoints(validated_data)
        start = start_place['coordinates']
        finish = finish_place['coordinates']
        route_started = perf_counter()
        route = self.router.route(start, finish)
        route_duration_ms = round((perf_counter() - route_started) * 1000, 3)
        self._diagnostics['timings_ms']['osrm_route'] = route_duration_ms
        logger.info('OSRM route stage: duration_ms=%.3f distance_miles=%.2f', route_duration_ms, route['distance_miles'])

        if route['distance_miles'] <= settings.VEHICLE_MAX_RANGE_MILES:
            matched = []
            coverage = dict(NO_STOPS_COVERAGE)
            optimization_started = perf_counter()
            plan = self.optimizer.optimize(
                route_distance_miles=route['distance_miles'],
                candidates=matched,
                max_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
                fuel_economy_mpg=settings.VEHICLE_FUEL_ECONOMY_MPG,
            )
            optimization_duration_ms = round((perf_counter() - optimization_started) * 1000, 3)
            self._diagnostics['timings_ms']['fuel_optimization'] = optimization_duration_ms
            self._diagnostics['candidate_search'] = {
                'candidate_budget': max(0, settings.STATION_GEOCODING_CANDIDATE_LIMIT),
                'candidates_available_in_corridor': 0,
                'candidates_with_stored_coordinates': 0,
                'candidates_without_stored_coordinates': 0,
                'candidates_selected_for_evaluation': 0,
                'candidates_shortlisted': 0,
                'candidates_evaluated': 0,
                'station_geocoding_attempts': 0,
                'candidates_not_evaluated_after_stop': 0,
                'search_complete_within_corridor': True,
                'search_complete_within_located_corridor': True,
                'globally_complete': False,
                'optimality_scope': 'not_required_within_full_tank_range',
                'stop_reason': 'not_required_within_full_tank_range',
                'optimization_iterations': 1,
            }
        else:
            matched, coverage, plan = self._evaluate_stations(start_place, finish_place, route)

        logger.info(
            'Fuel optimization summary: duration_ms=%.3f feasible=%s',
            self._diagnostics['timings_ms']['fuel_optimization'], plan.feasible,
        )
        if not plan.feasible:
            logger.warning(
                'No feasible fuel plan: route_miles=%.2f max_range_miles=%.2f '
                'dataset_stations=%d candidates_available=%d selected=%d geocoded=%d '
                'matched=%d matched_positions=%s',
                route['distance_miles'], settings.VEHICLE_MAX_RANGE_MILES,
                coverage['dataset_stations_loaded'], coverage['candidates_available'],
                coverage['stations_considered'], coverage['stations_geocoded'],
                coverage['stations_matched'], coverage['matched_candidate_positions'],
            )
        warnings = self._warning_messages(route['distance_miles'], coverage)
        candidate_search = self._diagnostics['candidate_search']
        if route['distance_miles'] > settings.VEHICLE_MAX_RANGE_MILES and not plan.feasible:
            warnings.append(
                f"No feasible plan was found among {candidate_search['candidates_evaluated']} "
                'evaluated station candidates; this does not establish that no feasible route exists.'
            )
        elif (
            route['distance_miles'] > settings.VEHICLE_MAX_RANGE_MILES
            and candidate_search['candidates_not_evaluated_after_stop']
        ):
            warnings.append(
            'Candidate search stopped after finding a feasible plan; unevaluated stations '
                'may offer a lower estimated cost.'
            )
        if not plan.feasible and not warnings:
            warnings.append('No feasible fuel-stop plan was found for this route.')

        if plan.feasible:
            message = (
                'The route is within the full-tank range; no fuel stops are required.'
                if not plan.stops
                else 'The lowest-cost feasible plan found among evaluated candidate stations was selected.'
            )
            optimization_status = 'no_stops_needed' if not plan.stops else 'optimized'
        else:
            message = (
                f"No feasible plan was found among {candidate_search['candidates_evaluated']} "
                'evaluated station candidates; this does not establish that no feasible route exists.'
            )
            optimization_status = 'no_feasible_plan'

        self._diagnostics['timings_ms']['route_planning_service_total'] = round(
            (perf_counter() - request_started) * 1000, 3
        )
        lookup_events = getattr(self.geocoder, 'lookup_events', [])
        osrm_event = getattr(self.router, 'last_request_diagnostics', {})
        self._diagnostics['upstream_requests'] = {
            'nominatim': {
                'outbound_http_requests': sum(
                    bool(event.get('outbound_http_request')) for event in lookup_events
                ),
                'cache_hits': sum(event.get('cache_status') == 'hit' for event in lookup_events),
                'cache_misses': sum(event.get('cache_status') == 'miss' for event in lookup_events),
                'failed_requests': sum(bool(event.get('failed')) for event in lookup_events),
                'failure_categories': self._failure_category_counts(lookup_events),
                'external_duration_ms': round(sum(
                    event.get('external_duration_ms') or 0 for event in lookup_events
                ), 3),
            },
            'osrm': {
                'outbound_http_requests': int(bool(osrm_event.get('outbound_http_request'))),
                'cache_hits': int(osrm_event.get('cache_status') == 'hit'),
                'cache_misses': int(osrm_event.get('cache_status') == 'miss'),
                'failed_requests': int(bool(osrm_event.get('failed'))),
                'http_status': osrm_event.get('http_status'),
                'external_duration_ms': osrm_event.get('external_duration_ms'),
            },
        }
        logger.info(
            'Upstream request totals: nominatim_outbound=%d nominatim_cache_hits=%d '
            'nominatim_cache_misses=%d osrm_outbound=%d osrm_cache=%s',
            self._diagnostics['upstream_requests']['nominatim']['outbound_http_requests'],
            self._diagnostics['upstream_requests']['nominatim']['cache_hits'],
            self._diagnostics['upstream_requests']['nominatim']['cache_misses'],
            self._diagnostics['upstream_requests']['osrm']['outbound_http_requests'],
            osrm_event.get('cache_status', 'unknown'),
        )
        return {
            'start': start,
            'finish': finish,
            'distance_miles': route['distance_miles'],
            'estimated_duration_minutes': route['duration_minutes'],
            'geometry': route['geometry'],
            'fuel_stops': plan.stops,
            'total_fuel_cost': plan.total_estimated_cost,
            'total_stop_purchase_cost': plan.total_estimated_cost,
            'fuel_cost_basis': {
                'definition': 'Estimated fuel purchased at selected stops only.',
                'initial_tank': 'full',
                'initial_tank_cost_included': False,
                'fuel_economy_mpg': settings.VEHICLE_FUEL_ECONOMY_MPG,
            },
            'optimization_status': optimization_status,
            'total_estimated_fuel_cost': plan.total_estimated_cost,
            'station_coverage': coverage,
            'diagnostics': self._diagnostics,
            'warnings': warnings,
            'fuel_optimization': {
                'implemented': True,
                'feasible': plan.feasible,
                'message': message,
                'fuel_economy_mpg': settings.VEHICLE_FUEL_ECONOMY_MPG,
                'maximum_range_miles': settings.VEHICLE_MAX_RANGE_MILES,
                'initial_tank': 'full',
                'purchase_assumption': 'Refill to a full tank at each selected stop; purchase replaces fuel used since the prior fill.',
                'starting_fuel_cost_included': False,
                'route_legs': self._route_leg_details(
                    plan, matched, route['distance_miles'], settings.VEHICLE_MAX_RANGE_MILES
                ),
                'optimality_scope': (
                    candidate_search['optimality_scope']
                    if plan.feasible
                    else 'no_feasible_plan_among_evaluated_candidates'
                ),
                'candidate_search_complete_within_corridor': candidate_search[
                    'search_complete_within_corridor'
                ],
            },
        }

    @staticmethod
    def _route_leg_details(
        plan: Any,
        candidates: list[Any],
        route_distance_miles: float,
        max_range_miles: float,
    ) -> list[dict[str, Any]]:
        """Expose the exact approximate legs used to validate a feasible optimizer path."""
        if not plan.feasible:
            return []

        by_station_id = {candidate.station.station_id: candidate for candidate in candidates}
        legs: list[dict[str, Any]] = []
        previous_position = 0.0
        previous_offset = 0.0
        previous_station_id: str | None = None
        for stop in plan.stops:
            candidate = by_station_id.get(stop['station_id'])
            if candidate is None:
                continue
            distance = (
                candidate.along_route_mile - previous_position
                + previous_offset
                + candidate.distance_from_route_miles
            )
            legs.append({
                'from_station_id': previous_station_id,
                'to_station_id': candidate.station.station_id,
                'estimated_distance_miles': round(distance, 3),
                'within_maximum_range': distance <= max_range_miles,
            })
            previous_position = candidate.along_route_mile
            previous_offset = candidate.distance_from_route_miles
            previous_station_id = candidate.station.station_id

        final_distance = route_distance_miles - previous_position + previous_offset
        legs.append({
            'from_station_id': previous_station_id,
            'to_station_id': None,
            'estimated_distance_miles': round(final_distance, 3),
            'within_maximum_range': final_distance <= max_range_miles,
        })
        return legs

    @staticmethod
    def _failure_category_counts(events: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in events:
            category = event.get('failure_category')
            if category:
                counts[category] = counts.get(category, 0) + 1
        return counts
