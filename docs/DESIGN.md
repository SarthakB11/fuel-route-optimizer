# Design notes

This document records the decisions behind the service, the ones that were reversed,
and the evidence for the ones that stayed.

## The shape of the problem

The input is a pair of US locations. The output is a driving route, an ordered list of
places to buy fuel, and what the fuel costs. The vehicle covers 10 miles per gallon and
holds 500 miles of range, so a coast to coast trip needs six or more fill ups and the
interesting question is not _where can I refuel_ but _which sequence of refuels is
cheapest_.

Two constraints shaped the architecture more than anything else:

1. The routing provider should be called once per request.
2. The endpoint should be fast.

Both push the same way: get the geometry in a single call, then do every subsequent
step locally against data that is already in memory.

## The data problem nobody mentions until they open the file

The supplied price list has seven columns and none of them is a coordinate:

```
OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
```

You cannot decide whether a station lies on a route without knowing where it is, so the
8151 rows have to be geocoded before they are useful. Geocoding at request time is out:
it would mean thousands of calls to a free geocoder per request, which is both slow and
an abuse of a community service.

So geocoding is a one time offline build step, `scripts/build_dataset.py`, and its
output is committed. The runtime never geocodes anything.

The build does four things:

- **Drops 620 non US rows.** The `State` column carries nine Canadian provinces. Both
  endpoints are required to be in the USA, so these are removed and the count reported.
- **Deduplicates 7531 rows into 6626 stops.** 568 stop IDs appear more than once, with a
  mean price spread of $0.10 and a maximum of $0.90. The lowest posted price per stop is
  kept, which reads as the best price available there.
- **Geocodes by joining city and state against the GeoNames populated place gazetteer**
  through four tiers, falling through only when the previous tier misses:

  | Tier | Key                                 | Stops resolved |
  | ---- | ----------------------------------- | -------------- |
  | 1    | exact normalised name               | 6581           |
  | 2    | normalised name with spaces removed | 24             |
  | 3    | GeoNames alternate name             | 14             |
  | 4    | hand checked override table         | 7              |

  Normalisation uppercases, replaces anything that is not a letter, digit or space with
  a space, collapses whitespace, and expands the abbreviations `FT`, `ST`, `STE` and
  `MT`. Tier 2 exists because the source writes `Mc Calla` and `De Forest` where the
  gazetteer writes `McCalla` and `DeForest`. Tier 4 covers five places the gazetteer
  does not carry under any name the source uses; their coordinates were looked up
  individually and the source of each is recorded in `data/geocode_overrides.json`.

  Nothing is left unresolved, and the script exits non zero if anything ever is, so a
  future price file cannot silently drop stations.

- **Emits a place index** so the API can turn `"Denver, CO"` into coordinates without a
  network call. This is the detail that keeps a normal request at exactly one external
  call rather than three.

### The approximation this leaves

Stations are placed at their city centroid, not at the highway exit in the `Address`
column, and a truck stop can sit a few miles from the centre of the town it is named
after. The corridor width absorbs that error: at the default of 12 miles a station is
matched to the route if its town centre is within 12 miles of the driven line. Detour
miles are reported per stop but are not added to the distance driven, since the vehicle
is assumed to refuel at stops that are effectively on the route.

One consequence needs handling rather than absorbing. Several stations in the same town
share that centroid exactly, so they land on the same offset along the route. Only the
cheapest of each such group is kept, because a dearer station at an identical position
is dominated: any fuel bought there could have been bought next door for less. Leaving
the others in is not just wasteful, it produces visible nonsense, since the optimiser
can then "drive" zero miles to a dearer twin and buy nothing, leaving a pointless zero
gallon stop in the plan.

Parsing the price file into 6626 records takes about 20 ms, and it happens once per
process at startup, not per request.

## Choosing the routing provider

The requirement was a free map and routing API, called as little as possible.

**OSRM's public demo server** is what the service uses. It needs no API key and no
account, and a single request to `/route/v1/driving/{coords}?overview=full&geometries=geojson`
returns the entire route geometry, its distance and its duration. That is the whole
budget: one call, one response, everything needed.

OpenRouteService was the alternative. It returns comparable geometry but requires a free
API key, which makes the project harder to run from a clean checkout and puts a
credential in the setup path for no gain. `OSRM_BASE_URL` is read from the environment,
so pointing the service at a self hosted OSRM or a different compatible backend is a
configuration change rather than a code change.

Measured on a coast to coast request, OSRM returns about 34,000 geometry vertices for a
2,794 mile route and takes roughly 1.4 seconds. That call dominates the response time,
which is precisely why it happens once and why the result is cached.

## Matching stations to a route without a quadratic scan

The naive approach compares every station to every point on the route. With 6626
stations and a 34,000 vertex polyline that is 225 million distance calculations, which
is far too slow.

Two steps fix it:

1. **Resample the polyline** to roughly one point per mile. A 2,794 mile route becomes
   about 2,790 points instead of 34,000, and one mile is well inside the corridor width
   so nothing is lost.
2. **Index those points in a uniform latitude and longitude grid** whose cell size is
   derived from the corridor width, then look up each station once. A lookup touches
   only the 3x3 block of cells around the station, so the work is proportional to the
   number of stations plus the number of route points rather than their product.

The result is `O(stations + route_points)`. Matching 6626 stations against a coast to
coast route takes about 28 ms.

## The fuel stop algorithm

This is the part the exercise is really asking about, so it is worth stating precisely.

### The model

The tank is empty at the origin, so the vehicle fuels before it sets off. The station
that fill up happens at is chosen from the data, not by the optimiser: the cheapest
station within 25 miles of the origin, or, when there is none, the nearest station along
the route. Which rule fired is reported as `origin_price_source`, because the second one
is an approximation worth seeing. That station is then removed from its position further
along the route so it cannot appear twice in the plan.

Every mile of the trip is paid for and the tank arrives empty, so total gallons equals
total distance divided by mpg exactly. `initial_fuel_miles` defaults to 0 and can be
raised to model starting with fuel already in the tank.

### The rule

Treat the destination as a pump whose fuel is free. Then the whole algorithm is one
sentence:

> At each stop, if a cheaper pump is reachable on a full tank, buy just enough fuel to
> get there. Otherwise fill the tank and drive to the cheapest pump in range.

Written out, at a stop at offset `p` with price `c` and `f` miles of fuel in the tank,
where `reach` is every station within `p + range_miles` and `cheaper` is those among
them priced strictly below `c` and lying before the destination:

1. If `cheaper` is non empty, buy exactly enough to reach the **nearest** of them.
   No destination test is needed in this branch: `cheaper` already excludes anything at
   or past the destination, so the station chosen always lies strictly before the end
   of the trip.
2. Otherwise, if the destination is in range, buy exactly enough to finish.
3. Otherwise fill to capacity and drive to the **cheapest** station in range, breaking a
   price tie by taking the farthest so the trip makes progress.

### Why it is optimal

An exchange argument. Suppose a plan buys a gallon at price `c` while some cheaper pump
was reachable before that gallon was burned. Deferring that purchase to the cheaper pump
leaves the vehicle in the same place with the same fuel and strictly less money spent,
so the original plan was not optimal. Rule 1 never buys a gallon that could be deferred.
Rule 3 applies only when nothing cheaper is reachable, and then buying as much as
possible at the cheapest price available is the best that can be done.

### Why clause order matters

Checking the destination first is the natural way to write this and it is wrong. With
pumps at mile 0 at $4.42 and mile 100 at $3.01, on a 120 mile trip with a 120 mile
range, the destination is in range from the very start. Finishing immediately buys 12
gallons at $4.42 for $53.04. Buying 10 gallons at the origin, stopping at mile 100 and
buying 2 more at $3.01 costs $50.22. The cheaper pump has to be considered before the
destination. That case is pinned in the test suite.

### How it was verified

`fuelroute/tests/dp_reference.py` is a deliberately slow exact dynamic program over
station index and integer gallons in the tank. `test_optimizer_dp.py` generates
randomised instances and asserts the greedy matches the exact optimum on every one.

That cross check earned its place. The first version of both the model and the algorithm
failed it. The original model billed the opening miles at whatever the first stop
charged, which let a plan skip past nearby stations to retro-price the start of the trip
at a distant cheap pump; the problem had no stable optimum. The original algorithm put
the destination clause first. An exhaustive brute force over every subset of stations
disagreed with it on 680 of 1483 feasible instances, worst case by $34. With the model
corrected to put a real pump at the origin and the clause order fixed, the same brute
force agreed on all 1483.

The same cross check was then run on the real thing rather than on synthetic
instances: 25 random pairs of large US cities, routed through OSRM, matched against the
real station file with the corridor, pruning and departure pump rules exactly as the
service applies them, then handed to both the greedy and an exact dynamic program over
station and fuel level with offsets on a five mile grid so the state stays integral.
All 25 agreed to the cent, on routes from 136 miles (Memphis to Little Rock) to 2,750
miles (Seattle to Baltimore) and from 15 to 171 candidate stations. The synthetic
instances prove the algorithm; these prove the pipeline that feeds it.

Complexity is `O(k^2)` in the worst case for `k` candidate stations, since each stop
scans the stations within range, and close to `O(k)` in practice because the window is
bounded by the tank range. For a coast to coast route `k` is a few hundred and the
optimisation is a small part of the total time.

## Caching

Route plans are memoised in Django's local memory cache under a key derived from the
resolved coordinates and every numeric parameter. A hit skips the routing call
completely and reports `external_api_calls: 0` and `cache: "hit"`. Repeat requests
therefore cost single digit milliseconds and no external traffic at all.

A miss also takes a per key lock before it calls the provider. Without that, several
identical requests arriving together would each miss the cache and each call OSRM,
because none has stored a result yet. With it, the first computes and the rest wait
and then read its plan: six simultaneous cold requests were measured making exactly
one external call. The lock table and the cache are both per process, which is the
right scope for each other.

The station table is loaded once per process and held in memory. The place index is
loaded lazily on first use and held the same way.

## Thinning the geometry that leaves the building

OSRM describes Seattle to Miami with 35,438 vertices, about one every 500 feet. That
is the right density for matching stations against the road, and the wrong density for
a JSON body: 831 KB to draw a line no screen resolves to that detail. So the response
geometry is run through Douglas-Peucker at a 0.02 mile tolerance, about 30 metres,
which keeps 3,396 vertices in an 88 KB body, with the guarantee that no vertex OSRM
sent lies further than the tolerance from the returned line. Station matching runs on
the full polyline before any of this, so the plan is identical whether the caller asks
for the thinned line or, with `geometry=full`, the provider's own. The thinning costs
about 100 ms of CPU on a coast to coast route, which is stated in the README rather
than hidden, and both vertex counts are reported on every response.

## What is deliberately not here

- **No database.** The service is read only over a static dataset. Adding Postgres would
  add an operational dependency and a network hop to lookups that a dictionary already
  answers in microseconds.
- **No per stop routing calls.** Detour distance is straight line, not driven. Routing
  each detour would multiply the external call count by the number of stops, which the
  brief rules out and which would make the endpoint far slower.
- **No live prices.** The price file is a snapshot. `generated_at` and the source file
  hash are recorded in `data/stations.json` so the vintage of a plan is always knowable.
