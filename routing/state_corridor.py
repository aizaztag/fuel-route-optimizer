"""Approximate intermediate state corridor from US state adjacency.

All shortest state-boundary paths are included. This is a local shortlist
heuristic, not a claim about which states an OSRM route actually traverses;
geocoded station locations are still matched against the OSRM geometry.
"""

from collections import deque


STATE_NEIGHBORS: dict[str, frozenset[str]] = {
    'AL': frozenset({'FL', 'GA', 'MS', 'TN'}),
    'AZ': frozenset({'CA', 'NV', 'NM', 'UT'}),
    'AR': frozenset({'LA', 'MS', 'MO', 'OK', 'TN', 'TX'}),
    'CA': frozenset({'AZ', 'NV', 'OR'}),
    'CO': frozenset({'KS', 'NE', 'NM', 'OK', 'UT', 'WY'}),
    'CT': frozenset({'MA', 'NY', 'RI'}),
    'DE': frozenset({'MD', 'NJ', 'PA'}),
    'FL': frozenset({'AL', 'GA'}),
    'GA': frozenset({'AL', 'FL', 'NC', 'SC', 'TN'}),
    'ID': frozenset({'MT', 'NV', 'OR', 'UT', 'WA', 'WY'}),
    'IL': frozenset({'IA', 'IN', 'KY', 'MO', 'WI'}),
    'IN': frozenset({'IL', 'KY', 'MI', 'OH'}),
    'IA': frozenset({'IL', 'MN', 'MO', 'NE', 'SD', 'WI'}),
    'KS': frozenset({'CO', 'MO', 'NE', 'OK'}),
    'KY': frozenset({'IL', 'IN', 'MO', 'OH', 'TN', 'VA', 'WV'}),
    'LA': frozenset({'AR', 'MS', 'TX'}),
    'ME': frozenset({'NH'}),
    'MD': frozenset({'DE', 'PA', 'VA', 'WV'}),
    'MA': frozenset({'CT', 'NH', 'NY', 'RI', 'VT'}),
    'MI': frozenset({'IN', 'OH', 'WI'}),
    'MN': frozenset({'IA', 'ND', 'SD', 'WI'}),
    'MS': frozenset({'AL', 'AR', 'LA', 'TN'}),
    'MO': frozenset({'AR', 'IA', 'IL', 'KS', 'KY', 'NE', 'OK', 'TN'}),
    'MT': frozenset({'ID', 'ND', 'SD', 'WY'}),
    'NE': frozenset({'CO', 'IA', 'KS', 'MO', 'SD', 'WY'}),
    'NV': frozenset({'AZ', 'CA', 'ID', 'OR', 'UT'}),
    'NH': frozenset({'ME', 'MA', 'VT'}),
    'NJ': frozenset({'DE', 'NY', 'PA'}),
    'NM': frozenset({'AZ', 'CO', 'OK', 'TX', 'UT'}),
    'NY': frozenset({'CT', 'MA', 'NJ', 'PA', 'VT'}),
    'NC': frozenset({'GA', 'SC', 'TN', 'VA'}),
    'ND': frozenset({'MN', 'MT', 'SD'}),
    'OH': frozenset({'IN', 'KY', 'MI', 'PA', 'WV'}),
    'OK': frozenset({'AR', 'CO', 'KS', 'MO', 'NM', 'TX'}),
    'OR': frozenset({'CA', 'ID', 'NV', 'WA'}),
    'PA': frozenset({'DE', 'MD', 'NJ', 'NY', 'OH', 'WV'}),
    'RI': frozenset({'CT', 'MA'}),
    'SC': frozenset({'GA', 'NC'}),
    'SD': frozenset({'IA', 'MN', 'MT', 'ND', 'NE', 'WY'}),
    'TN': frozenset({'AL', 'AR', 'GA', 'KY', 'MO', 'MS', 'NC', 'VA'}),
    'TX': frozenset({'AR', 'LA', 'NM', 'OK'}),
    'UT': frozenset({'AZ', 'CO', 'ID', 'NV', 'NM', 'WY'}),
    'VT': frozenset({'MA', 'NH', 'NY'}),
    'VA': frozenset({'KY', 'MD', 'NC', 'TN', 'WV'}),
    'WA': frozenset({'ID', 'OR'}),
    'WV': frozenset({'KY', 'MD', 'OH', 'PA', 'VA'}),
    'WI': frozenset({'IL', 'IA', 'MI', 'MN'}),
    'WY': frozenset({'CO', 'ID', 'MT', 'NE', 'SD', 'UT'}),
}


def _distances_from(start: str) -> dict[str, int]:
    distances = {start: 0}
    pending = deque([start])
    while pending:
        state = pending.popleft()
        for neighbor in STATE_NEIGHBORS.get(state, ()):
            if neighbor not in distances:
                distances[neighbor] = distances[state] + 1
                pending.append(neighbor)
    return distances


def state_corridor(start_state: str | None, finish_state: str | None) -> set[str]:
    """Return endpoint states and states on any shortest adjacency path."""
    endpoints = {state for state in (start_state, finish_state) if state}
    if len(endpoints) < 2 or not endpoints <= STATE_NEIGHBORS.keys():
        return endpoints

    start, finish = start_state, finish_state
    from_start = _distances_from(start)
    from_finish = _distances_from(finish)
    if finish not in from_start:
        return endpoints
    shortest_distance = from_start[finish]
    return {
        state
        for state in from_start.keys() & from_finish.keys()
        if from_start[state] + from_finish[state] == shortest_distance
    }
