# Fuel Route Optimizer

[![CI](https://github.com/SarthakB11/fuel-route-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/SarthakB11/fuel-route-optimizer/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Django 6.1](https://img.shields.io/badge/django-6.1-092E20.svg)](https://www.djangoproject.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A Django API that plans a driving route between two US locations and works out the
cheapest way to fuel a 500 mile vehicle along it.

Give it a start and a finish. It returns the route geometry, an ordered list of the
truck stops to buy fuel at, how many gallons to buy at each one, and what the trip
costs in total at 10 miles per gallon.

```
GET /api/v1/route-plan?start=Seattle,%20WA&finish=Miami,%20FL
```

```jsonc
{
  "route":     { "provider": "OSRM", "distance_miles": 3301.5, "geometry": { "type": "LineString", ... } },
  "fuel_plan": { "stop_count": 20, "total_gallons": 330.15, "total_cost_usd": 1026.29, "stops": [ ... ] },
  "performance": { "total_ms": 1857.46, "routing_ms": 1613.12, "compute_ms": 53.31, "external_api_calls": 1 }
}
```

There is also a browser map at `/map?start=Seattle,%20WA&finish=Miami,%20FL` that draws
the route and the stops.

## What makes this interesting

The exercise has two hard constraints, and everything in the design follows from them:
call the routing service **once** per request, and be **fast**.

- **One external call per request.** The route geometry comes from a single OSRM
  request. Place names are resolved against a local index rather than a geocoding
  service, and the fuel stop search runs entirely in memory, so a normal request makes
  exactly one outbound HTTP call. The response reports the real count in
  `performance.external_api_calls`, and a cache hit reports zero.
- **The price file has no coordinates.** It lists 8151 truck stops by city and state
  only. Geocoding those at request time would mean thousands of calls to a free
  geocoder, so it happens once, offline, in `scripts/build_dataset.py`, and the result
  is committed. The runtime never geocodes a station.
- **Picking stops is a real optimisation.** A 500 mile tank cannot cross the country in
  fewer than seven fills, and the cheapest plan is not "stop at the nearest station when
  the tank runs low": on Seattle to Miami the optimiser buys 1.3 gallons at one station
  purely to reach a cheaper one, and fills the tank outright at the cheapest station on
  the route. It comes in at $1,026 against $1,155 for the same trip bought at the price
  file's average, and the test suite checks the result against an exact dynamic program
  on randomised instances rather than trusting that it looks reasonable.

## Quick start

Requires Python 3.12 or newer.

```bash
git clone https://github.com/SarthakB11/fuel-route-optimizer.git
cd fuel-route-optimizer

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python manage.py runserver
```

Then open <http://127.0.0.1:8000/map> in a browser, or:

```bash
curl "http://127.0.0.1:8000/api/v1/route-plan?start=Denver,+CO&finish=Chicago,+IL"
```

No database to create, no migrations to run, no API key to obtain. The station dataset
is committed, so a clean checkout works immediately.

Configuration is optional and lives in environment variables; see `.env.example`.

## The API

### `GET /api/v1/route-plan`

| Parameter            | Default  | Meaning                                                                           |
| -------------------- | -------- | --------------------------------------------------------------------------------- |
| `start`              | required | Origin. `"Denver, CO"`, `"Denver, Colorado"`, `"Denver"` or `"39.7392,-104.9903"` |
| `finish`             | required | Destination, same formats                                                         |
| `range_miles`        | 500      | How far the vehicle goes on a full tank                                           |
| `mpg`                | 10       | Miles per gallon                                                                  |
| `corridor_miles`     | 12       | How far off the route a station may sit to count                                  |
| `initial_fuel_miles` | 0        | Miles of fuel already in the tank at the origin                                   |

The response has five blocks: `request` with the resolved inputs, `route` with the
provider, distance, duration and GeoJSON geometry, `fuel_plan` with the ordered stops
and the totals, `assumptions` in plain English, and `performance` with the timings and
the external call count.

Each stop reports the station, its coordinates, how far along the route it is, how far
off the route it sits, the price, the gallons to buy, the cost, and the running total.

Errors return `{"error": ..., "detail": ...}` with a status of 400 for a bad parameter
or an unresolvable place, 422 for a trip that cannot be completed within the tank
range, and 502 when the routing provider fails. No stack traces are exposed.

### `GET /api/v1/health`

Station count, dataset generation timestamp, version.

### `GET /map`

A Leaflet page that calls the API and draws the route, the numbered stops and the
totals. Takes the same query parameters.

## Routing provider, and why

**OSRM's public demo server**, `https://router.project-osrm.org`. It is free, needs no
API key or account, and returns the complete route geometry, distance and duration in
a single request. That last point is what matters here: the whole per request budget is
one call, and OSRM fits in it.

OpenRouteService was the alternative and does the same job, but it requires a free API
key, which puts a credential in the path of anyone trying to run the project from a
clean checkout, for no benefit.

`OSRM_BASE_URL` is read from the environment, so pointing at a self hosted OSRM
instance is a configuration change, not a code change. The public demo server is best
effort and rate limited, which is a good reason to run your own for anything serious.

## How the fuel stops are chosen

The tank is empty at the origin, so the vehicle fuels before setting off. Every mile of
the trip is paid for and the tank arrives empty, so total gallons is exactly the trip
distance divided by mpg. What the optimiser decides is **where those gallons are
bought**.

Treat the destination as a pump whose fuel is free, and the rule is one sentence:

> At each stop, if a cheaper pump is reachable on a full tank, buy just enough fuel to
> reach it. Otherwise fill the tank and drive to the cheapest pump in range.

It is optimal by an exchange argument: any gallon bought at one price while a cheaper
pump was still reachable could have been bought at the cheaper pump instead, for the
same journey and less money.

The subtlety worth knowing about is clause order. Checking "can I reach the
destination?" before "is there a cheaper pump on the way?" is the natural way to write
it, and it is wrong. With pumps at mile 0 at $4.42 and mile 100 at $3.01 on a 120 mile
trip, finishing straight from the origin costs $53.04 while stopping at the cheaper
pump costs $50.22. That case is a regression test.

`docs/DESIGN.md` has the full argument, the complexity analysis, and the record of what
the exact dynamic program caught when the first version of this was written.

One property of minimising dollars and nothing else is worth knowing about before you
read a plan. Because stopping is free in this model, the optimiser is happy to make
many small purchases: on the Seattle to Miami route it buys 1.3 gallons at one station
purely to reach a cheaper one 13 miles later, then fills the tank there. That is
genuinely the cheapest plan, and it is what the exercise asks for, but a real driver
also values their time. Adding a fixed penalty per stop and preferring the plan with
the lowest combined cost would consolidate those, and the greedy would need to become
a small dynamic program to stay optimal. That is a deliberate non goal here.

## Performance

The routing call dominates, which is exactly why it happens once and gets cached.

Measured on Seattle to Miami, 3,301 miles, 35,438 geometry vertices from OSRM, 398
candidate stations in the corridor, 20 fuel stops:

| Stage                                                         | Time                          |
| ------------------------------------------------------------- | ----------------------------- |
| OSRM routing call                                             | 1,613 ms                      |
| All local work: resample, match stations, optimise, serialise | 53 ms                         |
| **Total, cold**                                               | **1,857 ms**                  |
| **Total, cached repeat**                                      | **5 ms, zero external calls** |

Los Angeles to New York, 2,793 miles with 477 candidates, has the same shape: 1,221 ms
in OSRM, 52 ms of local work, 1,475 ms total.

The routing call is about 97 percent of a cold request and is the one part not under
this service's control, which is the whole argument for making it once and caching the
result. The local work stays near 50 ms whether the route is 600 miles or 3,300.

Station matching avoids the obvious quadratic trap. Comparing every station to every
one of OSRM's 34,000 geometry vertices would be 225 million distance calculations. The
polyline is resampled to about one point per mile and those points go into a uniform
latitude and longitude grid, so each station needs one lookup against a 3x3 block of
cells. That makes the work proportional to stations plus route points rather than their
product.

The 6,626 station table is parsed once per process at startup, about 20 ms, and held in
memory. Plans are memoised in Django's cache keyed on the resolved inputs.

## The dataset

`data/truckstop-fuel-prices.csv` is the supplied price list, committed
unmodified for provenance. `scripts/build_dataset.py` turns it into
`data/stations.json`:

- drops 620 Canadian rows, since both endpoints must be in the USA
- deduplicates 7531 rows into 6626 stops, keeping the lowest posted price per stop
- geocodes every stop by joining city and state against the GeoNames populated place
  gazetteer, in four tiers: exact name (6581), name with spaces removed (24), GeoNames
  alternate name (14), and a hand checked override table (7)

Nothing is left unresolved and the script exits non zero if anything ever is, so a
future price file cannot quietly lose stations. Stations are placed at their city
centroid rather than the exact highway exit, which the corridor width absorbs; detour
distances are reported but not added to the miles driven.

To rebuild from scratch, which downloads the GeoNames US gazetteer:

```bash
python scripts/build_dataset.py
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Every test runs offline and deterministically. The routing provider is mocked at the
session boundary, and no test touches the network.

The suite covers the geometry helpers against known distances, the grid index against a
brute force scan, the routing client's parsing and its failure modes, place resolution,
the dataset's integrity, and the API's status codes and response shape. Two of them
carry most of the weight:

- **the greedy against an exact dynamic program** on randomised instances, which is the
  real proof that the optimiser is correct rather than merely plausible
- **an assertion that the routing mock is called exactly once** on a cold request and
  exactly zero times on a repeat, which pins the constraint the whole design exists to
  satisfy

A prose check also fails the build on stray em dashes, keeping the documentation
consistent.

## Layout

```
config/                 Django project: settings, URLs, WSGI and ASGI entry points
fuelroute/
  geo.py                haversine, polyline resampling, the route point grid index
  stations.py           station records, the loader, corridor matching
  places.py             local place name to coordinate resolution
  routing.py            the OSRM client, one call per route
  optimizer.py          the fuel stop greedy
  planner.py            orchestration: resolve, route, match, optimise, cache
  views.py              thin DRF views
  serializers.py        input validation and response shaping
  tests/                offline test suite, including the dynamic program reference
scripts/build_dataset.py  the offline geocoding build step
data/                   the price file and the generated station and place indexes
postman/                importable collection with worked examples
docs/DESIGN.md          decisions, trade offs, and what the tests caught
```

## Licence

MIT. See `LICENSE`.
