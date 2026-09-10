"""Orchestration layer that ties location resolution, routing, station matching
and the fuel stop optimizer into one cacheable plan.

Kept separate from views.py so the HTTP layer stays thin: this module knows
nothing about DRF, request objects or serialization. It may read
django.conf.settings for configuration and django.core.cache for memoisation,
but it never imports a Django model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from fuelroute import geo, places, routing, stations
from fuelroute.optimizer import TOLERANCE, FuelPlan, RouteNotFeasible, plan_fuel_stops
from fuelroute.stations import RouteStation

# Sampling this fine keeps the corridor search accurate without materially
# slowing the search, since RoutePointIndex cost is linear in sample count.
RESAMPLE_SPACING_MILES = 1.0

# Bumped whenever the shape of the cached payload changes, so a stale entry from
# a previous deploy is never handed back as if it matched the current code.
CACHE_VERSION = "v3"

DEFAULT_ORIGIN_RADIUS_MILES = 25.0

# The widest corridor the API accepts. Used to tell "your corridor is too narrow"
# apart from "the price file has nothing here at all" when a match comes back empty.
MAX_CORRIDOR_MILES = 50.0

ORIGIN_SOURCE_WITHIN_RADIUS = "cheapest_within_25_miles_of_origin"
ORIGIN_SOURCE_NEAREST = "nearest_station_along_route"
# Only a trip whose start and finish coincide reports this: no fuel is bought, so
# no real station stands in for the pump at the origin. The serializer keys the
# trivial route wording off it.
ORIGIN_SOURCE_NONE = "no_fuel_required"

GEOMETRY_SIMPLIFIED = "simplified"
GEOMETRY_FULL = "full"
GEOMETRY_CHOICES = (GEOMETRY_SIMPLIFIED, GEOMETRY_FULL)

# About 30 metres. Dropping a vertex that sits within that distance of the line
# kept in its place is not visible at any zoom the map page offers, and on a long
# route it takes the vertex count and the body size down by an order of magnitude.
SIMPLIFY_TOLERANCE_MILES = 0.02

# Two points count as the same place within this many degrees, roughly four inches
# of latitude. Coordinates arrive from a place index or from the caller's own text,
# so exact float equality would be the wrong test.
COINCIDENT_DEGREES = 1e-6

# The provider name reported when no provider was asked. Saying "OSRM" for a route
# that never left the process would be a lie in the one field a caller uses to work
# out where the geometry came from.
TRIVIAL_ROUTE_PROVIDER = "none"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedPoint:
    """A location string the caller supplied, resolved to coordinates."""

    query: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class PlanResult:
    """Everything the serializer needs to render one route plan response."""

    start: ResolvedPoint
    finish: ResolvedPoint
    range_miles: float
    mpg: float
    corridor_miles: float
    initial_fuel_miles: float
    route: routing.Route
    fuel_plan: FuelPlan
    geometry: str
    geometry_coordinates: tuple[tuple[float, float], ...]
    origin_price_source: str
    origin_pump_offset_miles: float
    origin_pump_label: str
    routing_ms: float
    compute_ms: float
    external_api_calls: int
    candidate_stations: int
    stations_loaded: int
    cache: str


@dataclass(frozen=True, slots=True)
class _CachedPayload:
    """The subset of a plan that is safe to memoise across requests.

    Deliberately excludes the raw query text and the per request performance
    numbers: two different strings that resolve to the same coordinates should
    share a cache entry, and a cache hit must report its own timings, not the
    timings of whichever request happened to populate the entry.
    """

    route: routing.Route
    fuel_plan: FuelPlan
    geometry_coordinates: tuple[tuple[float, float], ...]
    origin_price_source: str
    origin_pump_offset_miles: float
    origin_pump_label: str
    candidate_stations: int
    stations_loaded: int


_FLIGHTS_GUARD = threading.Lock()
_FLIGHTS: dict[str, threading.Lock] = {}


def _flight_lock(key: str) -> threading.Lock:
    """One lock per cache key, created on first use and kept.

    The table grows by one small lock per distinct plan this process has ever
    computed, which the plan cache already bounds, so leaking them is cheaper and
    simpler than the reference counting a delete-on-release scheme would need. Like
    the cache it protects, this is per process.
    """
    with _FLIGHTS_GUARD:
        lock = _FLIGHTS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FLIGHTS[key] = lock
        return lock


def _cache_key(
    start_lat: float,
    start_lon: float,
    finish_lat: float,
    finish_lon: float,
    range_miles: float,
    mpg: float,
    corridor_miles: float,
    initial_fuel_miles: float,
    geometry: str,
) -> str:
    """A stable cache key over every resolved input, coordinates rounded so two
    requests that resolve to the same point cannot miss each other over noise
    in the last few decimal places.

    The geometry choice belongs in the key even though it changes no number in the
    plan, because the cached payload carries the geometry that was returned. Leaving
    it out would serve a simplified body to a caller who asked for the full one.
    """
    payload = {
        "start": [round(start_lat, 6), round(start_lon, 6)],
        "finish": [round(finish_lat, 6), round(finish_lon, 6)],
        "range_miles": range_miles,
        "mpg": mpg,
        "corridor_miles": corridor_miles,
        "initial_fuel_miles": initial_fuel_miles,
        "geometry": geometry,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"fuelroute:route-plan:{CACHE_VERSION}:{digest}"


def _prune_candidates(candidates: list[RouteStation], route_end_miles: float) -> list[RouteStation]:
    """Drop candidates that can never appear in an optimal plan.

    Two things get removed.

    Stations in one town share a city centroid, so several can land on exactly the
    same offset along the route. Among stations at the same offset only the cheapest
    can ever be worth stopping at: a dearer one sits in the same place, so any fuel
    bought there could have been bought next door for less. Keeping the others is not
    merely wasteful, it produces visible nonsense, because the greedy can "drive" zero
    miles to a dearer twin and buy nothing, leaving a pointless zero gallon stop in
    the plan.

    Anything strictly beyond route_end_miles is also dropped, since it cannot be
    stopped at and the feasibility check, which measures gaps against the tank range,
    would reject a perfectly drivable trip because of it. A station landing exactly on
    the end is kept. That is not a nicety: on a trip shorter than the corridor is wide,
    every nearby station projects onto the route's last point, so dropping that offset
    pruned every candidate and answered half a mile across downtown Denver with "no
    stations within 12.0 miles of the route". Keeping it is harmless downstream, because
    the optimiser only treats a station as a cheaper target when it sits strictly before
    the destination, and if the fallback clause ever drove to one the resulting zero
    gallon purchase is removed by _drop_empty_stops.

    The caller decides what counts as the end of the route. See build_plan for why that
    is not simply the distance the routing provider reports.
    """
    within_route = [c for c in candidates if c.offset_miles <= route_end_miles]
    cheapest_at_offset: dict[int, RouteStation] = {}
    for candidate in within_route:
        # Quantise to a thousandth of a mile so floating point noise in the projection
        # cannot hide two stations that are genuinely at the same point.
        key = round(candidate.offset_miles * 1000)
        existing = cheapest_at_offset.get(key)
        if (
            existing is None
            or candidate.station.price_per_gallon < existing.station.price_per_gallon
        ):
            cheapest_at_offset[key] = candidate
    return sorted(cheapest_at_offset.values(), key=lambda c: c.offset_miles)


def _empty_corridor_message(
    route_points: list[tuple[float, float]],
    cumulative: list[float],
    corridor_miles: float,
) -> str:
    """Explain an empty corridor in a way that gives the caller true advice.

    "Try a wider corridor_miles" is the right advice on a route with stations just
    outside the corridor and useless advice on San Francisco to Sacramento, where the
    price file has no coverage at any width. One extra pass at the widest corridor the
    API accepts separates the two cases. It costs a second grid build over points
    already in memory, on a request that is about to fail anyway, and no external call.
    """
    at_widest = stations.stations_along_route(route_points, cumulative, MAX_CORRIDOR_MILES)
    if not at_widest:
        return (
            f"The price file has no stations within {MAX_CORRIDOR_MILES:.0f} miles of any "
            "point on this route, so no corridor setting can make it feasible."
        )
    return (
        f"No fuel stations lie within {corridor_miles:.1f} miles of the route. "
        f"{len(at_widest)} lie within {MAX_CORRIDOR_MILES:.0f} miles of it, so a wider "
        "corridor_miles would reach them."
    )


def _drop_empty_stops(plan: FuelPlan) -> FuelPlan:
    """Remove stops where the plan buys nothing.

    A stop that buys zero gallons is a stop the driver would not make. These appear
    when initial_fuel_miles already covers the run to a cheaper station, so the
    departure pump is passed without buying. Dropping them changes no total, since a
    zero gallon purchase costs nothing and the remaining running totals are unaffected.
    """
    kept = tuple(stop for stop in plan.stops if stop.gallons > TOLERANCE)
    if len(kept) == len(plan.stops):
        return plan
    return FuelPlan(
        stops=kept,
        total_cost=plan.total_cost,
        total_gallons=plan.total_gallons,
        total_distance_miles=plan.total_distance_miles,
    )


def _choose_origin_pump(
    candidates: list[RouteStation], origin_radius_miles: float
) -> tuple[RouteStation, str]:
    """Pick the real station that stands in for the pump at the origin.

    Prefer the cheapest station within origin_radius_miles of the origin point.
    Failing that, fall back to the station nearest the origin by offset, so the
    optimizer always has a station to fuel from at mile zero.
    """
    within_radius = [c for c in candidates if c.offset_miles <= origin_radius_miles + 1e-9]
    if within_radius:
        cheapest = min(within_radius, key=lambda c: c.station.price_per_gallon)
        return cheapest, ORIGIN_SOURCE_WITHIN_RADIUS
    nearest = min(candidates, key=lambda c: c.offset_miles)
    return nearest, ORIGIN_SOURCE_NEAREST


def _build_candidate_list(
    candidates: list[RouteStation], origin_radius_miles: float
) -> tuple[list[RouteStation], str, float, str]:
    """Choose the origin pump, move it to offset zero, and drop its duplicate.

    The station chosen as the origin pump is removed from its original spot in
    the corridor and re-inserted at offset_miles == 0.0 so it can never appear
    twice in the plan.
    """
    origin, origin_price_source = _choose_origin_pump(candidates, origin_radius_miles)
    remaining = [c for c in candidates if c.station.stop_id != origin.station.stop_id]
    origin_stop = RouteStation(
        station=origin.station, offset_miles=0.0, detour_miles=origin.detour_miles
    )
    ordered = [origin_stop, *sorted(remaining, key=lambda c: c.offset_miles)]
    label = f"{origin.station.name}, {origin.station.city}, {origin.station.state}"
    return ordered, origin_price_source, origin.offset_miles, label


def _log_plan(result: PlanResult) -> None:
    """Emit one structured INFO line per completed plan.

    The resolved coordinates go in rather than the caller's query text. The text is
    user input and does not belong in a log file, and the coordinates are what the
    cache key and the routing call actually used, so they are also the more useful
    thing to grep for when a plan looks wrong.
    """
    logger.info(
        "route-plan start=%r finish=%r distance_miles=%.1f stops=%d "
        "total_cost_usd=%.2f routing_ms=%.0f compute_ms=%.0f cache=%s",
        (round(result.start.latitude, 4), round(result.start.longitude, 4)),
        (round(result.finish.latitude, 4), round(result.finish.longitude, 4)),
        result.route.distance_miles,
        len(result.fuel_plan.stops),
        result.fuel_plan.total_cost,
        result.routing_ms,
        result.compute_ms,
        result.cache,
    )


def _coincident_plan(
    start: ResolvedPoint,
    finish: ResolvedPoint,
    *,
    range_miles: float,
    mpg: float,
    corridor_miles: float,
    initial_fuel_miles: float,
    geometry: str,
) -> PlanResult:
    """The plan for a trip that starts where it finishes.

    Nothing is driven, so nothing is bought, and the routing provider has nothing to
    add: asking it to route a point to itself spends the request's one external call
    to be told a distance of zero. Answering here keeps that call for requests that
    need it, and keeps the fuel maths out of a degenerate case it was never posed
    for, where the origin pump would be chosen for a journey of no miles.

    Deliberately not cached. Recomputing it is cheaper than storing it, and a
    caller reading external_api_calls next to cache can then trust that a miss with
    zero calls means exactly this case.
    """
    point = (start.latitude, start.longitude)
    route = routing.Route(
        # Two positions rather than one: a GeoJSON LineString needs at least two,
        # and a consumer drawing it gets a degenerate line at the right place
        # rather than a shape it has to special case.
        coordinates=(point, point),
        distance_miles=0.0,
        duration_seconds=0.0,
        provider=TRIVIAL_ROUTE_PROVIDER,
        api_calls=0,
        elapsed_ms=0.0,
    )
    return PlanResult(
        start=start,
        finish=finish,
        range_miles=range_miles,
        mpg=mpg,
        corridor_miles=corridor_miles,
        initial_fuel_miles=initial_fuel_miles,
        route=route,
        fuel_plan=FuelPlan(stops=(), total_cost=0.0, total_gallons=0.0, total_distance_miles=0.0),
        geometry=geometry,
        geometry_coordinates=route.coordinates,
        origin_price_source=ORIGIN_SOURCE_NONE,
        origin_pump_offset_miles=0.0,
        origin_pump_label="",
        routing_ms=0.0,
        compute_ms=0.0,
        external_api_calls=0,
        candidate_stations=0,
        stations_loaded=len(stations.load_stations()),
        cache="miss",
    )


def build_plan(
    start: str,
    finish: str,
    *,
    range_miles: float,
    mpg: float,
    corridor_miles: float,
    initial_fuel_miles: float,
    geometry: str = GEOMETRY_SIMPLIFIED,
    origin_radius_miles: float = DEFAULT_ORIGIN_RADIUS_MILES,
) -> PlanResult:
    """Resolve two locations and return the cheapest fuel stop plan between them.

    Raises places.LocationError when start or finish cannot be resolved,
    routing.RoutingError when the routing provider fails, and
    optimizer.RouteNotFeasible when the corridor holds no stations at all or the
    trip cannot be covered by the given range.

    A thin wrapper so that every path that produces a plan, cache hit included, logs
    exactly one line, and every path that raises logs none.
    """
    result = _plan(
        start,
        finish,
        range_miles=range_miles,
        mpg=mpg,
        corridor_miles=corridor_miles,
        initial_fuel_miles=initial_fuel_miles,
        geometry=geometry,
        origin_radius_miles=origin_radius_miles,
    )
    _log_plan(result)
    return result


def _plan(
    start: str,
    finish: str,
    *,
    range_miles: float,
    mpg: float,
    corridor_miles: float,
    initial_fuel_miles: float,
    geometry: str,
    origin_radius_miles: float,
) -> PlanResult:
    """The planning work itself. See build_plan for the contract."""
    if geometry not in GEOMETRY_CHOICES:
        raise ValueError(f"geometry must be one of {GEOMETRY_CHOICES}, got {geometry!r}")

    start_lat, start_lon = places.resolve_location(start)
    finish_lat, finish_lon = places.resolve_location(finish)

    resolved_start = ResolvedPoint(query=start, latitude=start_lat, longitude=start_lon)
    resolved_finish = ResolvedPoint(query=finish, latitude=finish_lat, longitude=finish_lon)

    if (
        abs(start_lat - finish_lat) <= COINCIDENT_DEGREES
        and abs(start_lon - finish_lon) <= COINCIDENT_DEGREES
    ):
        return _coincident_plan(
            resolved_start,
            resolved_finish,
            range_miles=range_miles,
            mpg=mpg,
            corridor_miles=corridor_miles,
            initial_fuel_miles=initial_fuel_miles,
            geometry=geometry,
        )

    key = _cache_key(
        start_lat,
        start_lon,
        finish_lat,
        finish_lon,
        range_miles,
        mpg,
        corridor_miles,
        initial_fuel_miles,
        geometry,
    )
    # Single flight. Several identical cold requests arriving together would each
    # miss the cache and each call the routing provider, and the whole design exists
    # to make that call once. The first request for a key computes; the others wait
    # on the same lock, then find the stored plan. Keys never wait on each other.
    with _flight_lock(key):
        cached: _CachedPayload | None = cache.get(key)
        if cached is not None:
            return PlanResult(
                start=resolved_start,
                finish=resolved_finish,
                range_miles=range_miles,
                mpg=mpg,
                corridor_miles=corridor_miles,
                initial_fuel_miles=initial_fuel_miles,
                route=cached.route,
                fuel_plan=cached.fuel_plan,
                geometry=geometry,
                geometry_coordinates=cached.geometry_coordinates,
                origin_price_source=cached.origin_price_source,
                origin_pump_offset_miles=cached.origin_pump_offset_miles,
                origin_pump_label=cached.origin_pump_label,
                routing_ms=0.0,
                compute_ms=0.0,
                external_api_calls=0,
                candidate_stations=cached.candidate_stations,
                stations_loaded=cached.stations_loaded,
                cache="hit",
            )

        # A miss makes exactly one routing call. Route.elapsed_ms is measured inside
        # fetch_route itself, which keeps that timing independent of everything
        # computed below.
        route = routing.fetch_route((start_lat, start_lon), (finish_lat, finish_lon))

        compute_started = time.perf_counter()
        resampled_points, cumulative = geo.resample_polyline(
            route.coordinates, RESAMPLE_SPACING_MILES
        )
        candidates = stations.stations_along_route(resampled_points, cumulative, corridor_miles)

        # Offsets are measured along the resampled polyline, and its great circle
        # length sits either side of the road distance the provider reports: on half a
        # mile across Denver the polyline came out 0.49031 miles against OSRM's
        # 0.49026. Pruning against the smaller of the two discards a station that
        # projects onto the route's final point, and on a trip shorter than the
        # corridor is wide that is every station there is, so the whole plan comes back
        # as a 422. Take whichever measurement is longer.
        route_end_miles = max(route.distance_miles, cumulative[-1])
        candidates = _prune_candidates(candidates, route_end_miles)

        if not candidates:
            raise RouteNotFeasible(
                _empty_corridor_message(resampled_points, cumulative, corridor_miles)
            )

        (
            ordered_candidates,
            origin_price_source,
            origin_pump_offset_miles,
            origin_pump_label,
        ) = _build_candidate_list(candidates, origin_radius_miles)

        fuel_plan = _drop_empty_stops(
            plan_fuel_stops(
                ordered_candidates,
                route.distance_miles,
                range_miles=range_miles,
                mpg=mpg,
                initial_fuel_miles=initial_fuel_miles,
            )
        )
        # Only the geometry that leaves the building is thinned. Station matching above
        # ran on the full resampled polyline, so a simplified response can never move a
        # stop, change an offset or change a price.
        geometry_coordinates = (
            route.coordinates
            if geometry == GEOMETRY_FULL
            else tuple(geo.simplify_polyline(route.coordinates, SIMPLIFY_TOLERANCE_MILES))
        )
        compute_ms = (time.perf_counter() - compute_started) * 1000

        stations_loaded = len(stations.load_stations())
        cache.set(
            key,
            _CachedPayload(
                route=route,
                fuel_plan=fuel_plan,
                geometry_coordinates=geometry_coordinates,
                origin_price_source=origin_price_source,
                origin_pump_offset_miles=origin_pump_offset_miles,
                origin_pump_label=origin_pump_label,
                candidate_stations=len(ordered_candidates),
                stations_loaded=stations_loaded,
            ),
            timeout=settings.ROUTE_PLAN_CACHE_SECONDS,
        )

        return PlanResult(
            start=resolved_start,
            finish=resolved_finish,
            range_miles=range_miles,
            mpg=mpg,
            corridor_miles=corridor_miles,
            initial_fuel_miles=initial_fuel_miles,
            route=route,
            fuel_plan=fuel_plan,
            geometry=geometry,
            geometry_coordinates=geometry_coordinates,
            origin_price_source=origin_price_source,
            origin_pump_offset_miles=origin_pump_offset_miles,
            origin_pump_label=origin_pump_label,
            routing_ms=route.elapsed_ms,
            compute_ms=compute_ms,
            external_api_calls=route.api_calls,
            candidate_stations=len(ordered_candidates),
            stations_loaded=stations_loaded,
            cache="miss",
        )
