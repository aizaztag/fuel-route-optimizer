# Fuel Route Optimizer

The API plans a driving route and estimates fuel stops from the supplied station-price CSV. A small Leaflet page is available at the site root.

## Setup

The project uses Python 3.12 and the existing `.venv`.

```powershell
cd C:\Users\aizaz\OneDrive\Desktop\fuel-route-optimizer
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

Edit `.env` before starting the server. Replace the secret placeholder and set `NOMINATIM_USER_AGENT` to an identifying application name with a contact you monitor. Django reads process environment variables and does not load `.env` itself. In PowerShell, import the simple `KEY=value` lines into the current shell, then start the server:

```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        Set-Item -Path "Env:$($matches[1].Trim())" -Value $matches[2].Trim()
    }
}
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

The default development secret and `DJANGO_DEBUG=True` are for local use only. Set a private secret and `DJANGO_DEBUG=False` for a deployment, and do not commit `.env`.

## API

The project also serves a minimal interactive Leaflet map at `http://127.0.0.1:8000/`.
Enter US addresses or switch to latitude/longitude, then choose **Plan route**. It
draws the returned route and start/finish markers, and shows selected station markers
with names and prices. The page calls the same JSON endpoint documented below. Leaflet
is loaded from its public CDN and map tiles come from OpenStreetMap; a browser with
internet access is needed to display the basemap.

`POST /api/v1/route-plan/`

Use exactly one of the two request forms.

### Address request

```json
{
  "start_address": "New York, NY",
  "finish_address": "Boston, MA"
}
```

Each address is looked up with Nominatim's US country filter.

### Coordinate request

```json
{
  "start": {"latitude": 40.7128, "longitude": -74.0060},
  "finish": {"latitude": 42.3601, "longitude": -71.0589}
}
```

Coordinates are checked for numeric latitude/longitude ranges, then reverse geocoded. A point is accepted only when Nominatim returns a US country code; coordinate bounds alone are not treated as proof that a point is in the USA.

### Response shape

Successful route requests return HTTP 200. Example values below illustrate the schema and are not a result for the example request:

```json
{
  "start": {"latitude": 40.7128, "longitude": -74.006},
  "finish": {"latitude": 42.3601, "longitude": -71.0589},
  "distance_miles": 215.4,
  "estimated_duration_minutes": 242.8,
  "geometry": {
    "type": "LineString",
    "coordinates": [[-74.006, 40.7128], [-71.0589, 42.3601]]
  },
  "fuel_stops": [],
  "total_fuel_cost": 0.0,
  "total_stop_purchase_cost": 0.0,
  "fuel_cost_basis": {
    "definition": "Estimated fuel purchased at selected stops only.",
    "initial_tank": "full",
    "initial_tank_cost_included": false,
    "fuel_economy_mpg": 10.0
  },
  "optimization_status": "no_stops_needed",
  "total_estimated_fuel_cost": 0.0,
  "station_coverage": {
    "stations_considered": 0,
    "stations_geocoded": 0,
    "stations_skipped": 0,
    "stations_matched": 0,
    "stations_eligible": 0,
    "candidates_available": 0,
    "candidates_excluded_by_limit": 0,
    "candidates_selected_for_evaluation": 0,
    "candidates_not_evaluated_after_stop": 0,
    "geocode_failures": 0,
    "endpoint_states_evaluated": [],
    "candidate_states_evaluated": [],
    "matched_candidate_positions": [],
    "dataset_rows_seen": 0,
    "dataset_stations_loaded": 0,
    "dataset_malformed_rows": 0,
    "dataset_invalid_price_rows": 0,
    "dataset_duplicate_station_ids": 0,
    "dataset_duplicate_rows": 0
  },
  "warnings": [],
  "fuel_optimization": {
    "implemented": true,
    "feasible": true,
    "message": "The route is within the full-tank range; no fuel stops are required.",
    "fuel_economy_mpg": 10.0,
    "maximum_range_miles": 500.0,
    "initial_tank": "full",
    "purchase_assumption": "Refill to a full tank at each selected stop; purchase replaces fuel used since the prior fill.",
    "starting_fuel_cost_included": false
  }
}
```

Each `fuel_stops` item includes the CSV station ID, name, address, city, state, price per gallon, estimated gallons and purchase cost, approximate along-route mile, distance from route, estimated detour, and geocoded station coordinates when available. GeoJSON coordinates are in Leaflet-ready `[longitude, latitude]` order. `total_fuel_cost` and the explicit alias `total_stop_purchase_cost` are estimates of fuel purchased at selected stops only (`0.0` when no stop is needed and `null` when no feasible plan exists). The vehicle starts with a full tank and that initial fuel's cost is excluded; therefore this value is **not** the complete fuel cost of the trip. The API exposes this definition in `fuel_cost_basis`. `optimization_status` is `no_stops_needed`, `optimized`, or `no_feasible_plan`. The older `total_estimated_fuel_cost` and nested `fuel_optimization` fields remain in the response for compatibility. Long-route plans are the lowest-cost plans among evaluated candidates; the bounded search does not establish global cost optimality.

Responses also include additive `diagnostics`: stage durations in milliseconds, separate start/finish geocoding durations, a station-geocoding section that reports zero route-time station lookups, local stored-coordinate counts, and `upstream_requests` counts for actual Nominatim and OSRM HTTP requests versus cache hits/misses. Server logs record external request durations and HTTP statuses without logging addresses, coordinates, full URLs, or credentials. Nominatim's existing one-request-per-second pacing remains in place for start/finish geocoding.

Endpoint geocoding diagnostics retain sanitized failure categories such as `empty_result`, `non_us`, `timeout`, `connection_error`, `http_error`, `rate_limited` (HTTP 429), and `invalid_response`. The enrichment command logs batch durations, HTTP statuses, and aggregate failure categories without printing submitted addresses. Route responses retain the existing station-geocoding diagnostics fields for compatibility, with an empty station request list and zero station external requests. For endpoint lookups, a 429 remains HTTP 502 and adds `error_code: "rate_limited"`.

Input errors return HTTP 400. Unfound or non-US locations return HTTP 400; no driving route returns HTTP 422; service failures return HTTP 502; timeouts return HTTP 504; an unusable local station dataset returns HTTP 503. Error bodies contain a safe `detail` string.

## Data loading and duplicate policy

The original `fuel-prices-for-be-assessment.csv` stays in the project root and is excluded by `.gitignore`. Required headers are validated. Blank/malformed rows and rows with missing identity fields are skipped; non-numeric, non-finite, zero, and negative prices are skipped and counted. No coordinates or prices are synthesized.

Station IDs are trimmed and case-normalized. Duplicate IDs collapse deterministically to the row with the lowest valid retail price; ties use normalized name, address, city, and state ordering. This treats repeated prices as snapshots and favors the lowest observed valid price; the CSV has no price timestamp, so this selection is not a guarantee of current price. The response includes dataset row, malformed row, invalid price, and duplicate counts when a long route requires station evaluation. Parsed data is cached using the CSV path, size, and modification time as the cache key.

## Fuel assumptions and algorithm

Default vehicle assumptions:

- Maximum range on a full tank: 500 miles.
- Fuel economy: 10 miles per gallon, implying 50 gallons of usable fuel at the default range.
- The vehicle starts with a full tank.
- The starting tank's cost is excluded from the estimated trip purchase cost.
- At each selected stop, the vehicle refills to a full tank. The purchase replaces fuel consumed since the previous fill; no extra reserve is modeled.

The planner builds a forward-only graph from start, route-ordered eligible stations, and destination. An edge exists only when the estimated driving distance for that leg is within maximum range. A dynamic program finds the lowest estimated purchase-cost path through that graph. Every selected station fills the tank back to capacity, purchasing the gallons consumed on the incoming leg at that station's price. It can select a more expensive nearby station when doing so avoids a costlier feasible sequence. Ties favor fewer stops, then stable station ID ordering.

Vehicle range and fuel economy can be configured with `VEHICLE_MAX_RANGE_MILES` and `VEHICLE_FUEL_ECONOMY_MPG`. Route matching uses `ROUTE_CORRIDOR_WIDTH_MILES` and rejects stations whose estimated round-trip detour exceeds `MAX_STATION_DETOUR_MILES`.

## Station coordinate enrichment and candidate coverage

The source CSV remains unchanged and has no coordinates. Run the explicit, one-time enrichment command after `migrate` to submit stations in bounded batches to the US Census Geocoder:

```powershell
.\.venv\Scripts\python.exe manage.py enrich_station_coordinates
```

The command defaults to 1,000 records per request (the provider documents a 10,000-record maximum and a 5 MB web batch-file ceiling). It submits the existing station ID, address, city, state, and a blank ZIP field. The provider requires a structured street address; stations without a usable street address may be returned as unmatched. The batch results are persisted in the local `StationCoordinate` SQLite table, including no-match and failure status. The command force-reads the current CSV instead of relying on the request cache. A rerun skips unchanged successful records and known unmatched/failed records; use `--retry-failures` to retry the latter. New station IDs and changed address/city/state or Census benchmark values are queued for enrichment; unchanged addresses with updated prices are not geocoded again. An interrupted run can be rerun safely; already saved successful work is not submitted again. The command is never run by a route request.

### Refreshing station data

The CSV remains the source of truth for station names, addresses, states, and retail prices. The repository checks the CSV file signature (path, size, and nanosecond modification time) on each load and caches parsed rows for `STATION_DATA_CACHE_TTL` seconds (default one hour). A normal file replacement with a changed signature is picked up on the next request; SQLite is only used to attach coordinates and does not store or override fuel prices. Since the default Django cache is per-process, use this deterministic refresh procedure after replacing the CSV:

1. Put the updated `fuel-prices-for-be-assessment.csv` in the project root.
2. Run `python manage.py enrich_station_coordinates` from the project virtual environment. This force-reads and validates the new CSV, refreshes coordinates only for new/changed address inputs, and skips successful unchanged coordinates. Price-only updates do not trigger Census requests.
3. Restart every Django server/worker process. This clears each process's local parsed-CSV cache so the next API request reads the current CSV prices and metadata. Restarting is important when a replacement preserves the old file size and timestamp; a separate management command cannot clear another process's `LocMemCache`.

Station IDs removed from the CSV stop participating in routes immediately after refresh; their SQLite coordinate rows are retained but ignored. If an ID is later reused, its address hash must match before its old coordinates can be attached.

The Census Bureau documents public batch address geocoding, US address coverage, address-range-derived coordinates, and the 10,000-record per-file cap in its [Geocoding Services API guide](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html). It is a suitable free public batch option for this demo; its results are approximate address-range interpolations, not verified truck-stop entrances or exact parcel coordinates. We do not submit station addresses to public Nominatim in bulk.

For routes longer than the configured full-tank range, the API loads stored coordinates locally, builds an approximate state corridor, and route-matches every corridor candidate that has current stored coordinates against OSRM's returned geometry before applying `STATION_GEOCODING_CANDIDATE_LIMIT` (default 8). The bounded shortlist retains the full matched-set optimizer's feasible stop sequence, when one exists, then fills remaining slots by spreading candidates along the route. Route requests make no station-geocoding calls. If enrichment has not been run, coordinates are missing, or the Census service could not match an address, that station cannot be considered and a suitable real station may be absent from the results. The optimizer checks the full route-matched set and the bounded shortlist; a feasible plan is cost-effective among route-matched candidates, but this is not a global-optimality guarantee because of the corridor and incomplete coordinate coverage. Diagnostics distinguish CSV candidates, stored-coordinate availability, route-matched candidates, shortlist candidates, failed/no-match/stale coordinate records, and search completeness within the approximate corridor. Feasible responses include `fuel_optimization.route_legs`, showing the estimated distance and 500-mile range check for each start/stop/finish leg. Access and detour distances remain straight-line estimates, not road-route detours.

Start/finish address geocoding still uses Nominatim. OSRM is still requested once on a route-cache miss; both services retain their existing cache behavior. Route diagnostics separately report Nominatim and OSRM outbound counts, cache hits/misses, external elapsed time, and total request stages. Station coordinate lookup diagnostics report local stored/missing/no-match/failed counts and zero outbound requests.

The approximate shortest-state corridor may omit a non-shortest state path selected by OSRM, and the candidate limit can omit stations in any evaluated state. The API returns a coverage warning for long routes and does not claim nationwide or global optimality. Route matching estimates distance to the OSRM geometry in miles. Access and detour distances use straight-line offsets; they are estimates, not exact driving detours. The optimizer applies those estimated access legs to reachability, so real road access can differ.

Nominatim lookups for start/finish use the configured identifying User-Agent, explicit timeout, Django-cache entries for successful results, and serialized pacing with a minimum one-second interval between requests. Not-found/non-US results use a short negative cache; transient HTTP errors are not cached. A process-local lock prevents concurrent Nominatim calls within one server process; multi-process deployments should use a shared cache/lock design and comply with the public service's usage policy. OSRM route results are cached. Station coordinates use the persistent local SQLite table instead of Nominatim or Django's process-local cache.

## Mocked cold/warm benchmark

Run the reproducible benchmark test with all HTTP responses mocked (it makes no live requests):

```powershell
.\.venv\Scripts\python.exe -m pytest -s routing/test_route_benchmark.py
```

It prints one `ROUTE_PLAN_BENCHMARK=` JSON record with measured cold/repeated request latency, outbound Nominatim and OSRM counts, station-geocoder count, candidate counts, and optimization status. These timings measure local Django handling plus the configured Nominatim pacing against mocked responses; they do not predict live provider latency. The expected request pattern is two cold endpoint geocoding calls and one cold OSRM call for a coordinate request, then zero external calls on a repeated request while the caches remain warm. Long-route fuel-station coordinates are always local reads.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `NOMINATIM_SEARCH_URL` | `https://nominatim.openstreetmap.org/search` | Start/finish address search endpoint |
| `NOMINATIM_REVERSE_URL` | `https://nominatim.openstreetmap.org/reverse` | Verify coordinate country/state |
| `NOMINATIM_USER_AGENT` | `FuelRouteOptimizer/0.1 (local development)` | HTTP identification; set a monitored contact |
| `NOMINATIM_TIMEOUT_SECONDS` | `5` | Geocoding timeout |
| `NOMINATIM_MIN_INTERVAL_SECONDS` | `1` | Minimum spacing between Nominatim requests |
| `OSRM_ROUTE_URL` | `https://router.project-osrm.org/route/v1/driving` | Driving route endpoint |
| `OSRM_TIMEOUT_SECONDS` | `8` | Routing timeout |
| `GEOCODING_CACHE_TTL` | `86400` | Successful geocoding cache lifetime, seconds |
| `GEOCODING_FAILURE_CACHE_TTL` | `300` | Not-found/non-US cache lifetime, seconds |
| `ROUTING_CACHE_TTL` | `3600` | Route cache lifetime, seconds |
| `FUEL_STATION_CSV_PATH` | Project-root CSV | Dataset path |
| `STATION_DATA_CACHE_TTL` | `3600` | Parsed CSV cache lifetime, seconds |
| `CENSUS_GEOCODER_BATCH_URL` | Census `locations/addressbatch` | Endpoint used only by explicit enrichment command |
| `CENSUS_GEOCODER_BENCHMARK` | `Public_AR_Current` | Census address locator benchmark |
| `CENSUS_GEOCODER_TIMEOUT_SECONDS` | `120` | Timeout for one enrichment batch request |
| `STATION_GEOCODING_CANDIDATE_LIMIT` | `8` | Maximum stored-coordinate candidates evaluated per route |
| `ROUTE_CORRIDOR_WIDTH_MILES` | `15` | Maximum station-to-route distance |
| `MAX_STATION_DETOUR_MILES` | `30` | Maximum estimated round-trip station detour |
| `VEHICLE_MAX_RANGE_MILES` | `500` | Configured full-tank range |
| `VEHICLE_FUEL_ECONOMY_MPG` | `10` | Fuel economy used for purchase estimates |
| `CACHE_DEFAULT_TIMEOUT` | `300` | Default Django cache lifetime, seconds |

The default Django cache is local memory. It is suitable for local development but is per-process and clears on restart; use a shared backend for multi-worker deployment.

## Manual Postman test

1. Start the local server using the PowerShell command above. This stage does not verify that public Nominatim or OSRM services are reachable; a live Postman request will depend on your network and their availability.
2. In Postman, create a `POST` request to `http://127.0.0.1:8000/api/v1/route-plan/`.
3. Select **Body → raw → JSON** and send this US address request:

   ```json
   {
     "start_address": "New York, NY",
     "finish_address": "Boston, MA"
   }
   ```

4. A successful response has HTTP 200 and includes `start`, `finish`, `distance_miles`, `estimated_duration_minutes`, GeoJSON `geometry`, `fuel_stops`, `total_fuel_cost`, `total_stop_purchase_cost`, `fuel_cost_basis`, `optimization_status`, `station_coverage`, `warnings`, and `fuel_optimization`. Use actual values returned by the services; the README example is illustrative.
5. To test the coordinate form, send:

   ```json
   {
     "start": {"latitude": 40.7128, "longitude": -74.0060},
     "finish": {"latitude": 42.3601, "longitude": -71.0589}
   }
   ```

6. For failures, inspect the HTTP status and JSON body. Validation and unverified/non-US locations use 400; no OSRM route uses 422; upstream errors use 502; timeouts use 504; unreadable station data uses 503; unexpected server failures return a generic 500 `detail` without stack traces. A 200 response can still contain `fuel_optimization.feasible: false` when no feasible candidate plan is found.

Public Nominatim use is rate-limited by the client to at least one request per second per server process. Set the User-Agent contact first, and avoid running multiple app processes against the public endpoint without a shared rate limiter.
