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
CACHE_VERSION = "v2"

DEFAULT_ORIGIN_RADIUS_MILES = 25.0

ORIGIN_SOURCE_WITHIN_RADIUS = "cheapest_within_25_miles_of_origin"
ORIGIN_SOURCE_NEAREST = "nearest_station_along_route"


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
    origin_price_source: str
    origin_pump_offset_miles: float
    origin_pump_label: str
    candidate_stations: int
    stations_loaded: int


def _cache_key(
    start_lat: float,
    start_lon: float,
    finish_lat: float,
    finish_lon: float,
    range_miles: float,
    mpg: float,
    corridor_miles: float,
    initial_fuel_miles: float,
) -> str:
    """A stable cache key over every resolved input, coordinates rounded so two
    requests that resolve to the same point cannot miss each other over noise
    in the last few decimal places.
    """
    payload = {
        "start": [round(start_lat, 6), round(start_lon, 6)],
        "finish": [round(finish_lat, 6), round(finish_lon, 6)],
        "range_miles": range_miles,
        "mpg": mpg,
        "corridor_miles": corridor_miles,
        "initial_fuel_miles": initial_fuel_miles,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"fuelroute:route-plan:{CACHE_VERSION}:{digest}"


def _prune_candidates(
    candidates: list[RouteStation], total_distance_miles: float
) -> list[RouteStation]:
    """Drop candidates that can never appear in an optimal plan.

    Two things get removed.

    Stations in one town share a city centroid, so several can land on exactly the
    same offset along the route. Among stations at the same offset only the cheapest
    can ever be worth stopping at: a dearer one sits in the same place, so any fuel
    bought there could have been bought next door for less. Keeping the others is not
    merely wasteful, it produces visible nonsense, because the greedy can "drive" zero
    miles to a dearer twin and buy nothing, leaving a pointless zero gallon stop in
    the plan.

    Anything at or beyond the destination is also dropped. The resampled polyline is
    marginally shorter than the distance the routing provider reports, so today no
    candidate can sit past the end, but relying on that is a trap: the feasibility
    check measures gaps against the tank range and would reject a perfectly drivable
    trip if one ever did.
    """
    within_route = [c for c in candidates if c.offset_miles < total_distance_miles - 1e-9]
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


def build_plan(
    start: str,
    finish: str,
    *,
    range_miles: float,
    mpg: float,
    corridor_miles: float,
    initial_fuel_miles: float,
    origin_radius_miles: float = DEFAULT_ORIGIN_RADIUS_MILES,
) -> PlanResult:
    """Resolve two locations and return the cheapest fuel stop plan between them.

    Raises places.LocationError when start or finish cannot be resolved,
    routing.RoutingError when the routing provider fails, and
    optimizer.RouteNotFeasible when the corridor holds no stations at all or the
    trip cannot be covered by the given range.
    """
    start_lat, start_lon = places.resolve_location(start)
    finish_lat, finish_lon = places.resolve_location(finish)

    key = _cache_key(
        start_lat,
        start_lon,
        finish_lat,
        finish_lon,
        range_miles,
        mpg,
        corridor_miles,
        initial_fuel_miles,
    )
    cached: _CachedPayload | None = cache.get(key)
    if cached is not None:
        return PlanResult(
            start=ResolvedPoint(query=start, latitude=start_lat, longitude=start_lon),
            finish=ResolvedPoint(query=finish, latitude=finish_lat, longitude=finish_lon),
            range_miles=range_miles,
            mpg=mpg,
            corridor_miles=corridor_miles,
            initial_fuel_miles=initial_fuel_miles,
            route=cached.route,
            fuel_plan=cached.fuel_plan,
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
    resampled_points, cumulative = geo.resample_polyline(route.coordinates, RESAMPLE_SPACING_MILES)
    candidates = stations.stations_along_route(resampled_points, cumulative, corridor_miles)

    candidates = _prune_candidates(candidates, route.distance_miles)

    if not candidates:
        raise RouteNotFeasible(
            f"No fuel stations lie within {corridor_miles:.1f} miles of the route. "
            "Try a wider corridor_miles."
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
    compute_ms = (time.perf_counter() - compute_started) * 1000

    stations_loaded = len(stations.load_stations())
    cache.set(
        key,
        _CachedPayload(
            route=route,
            fuel_plan=fuel_plan,
            origin_price_source=origin_price_source,
            origin_pump_offset_miles=origin_pump_offset_miles,
            origin_pump_label=origin_pump_label,
            candidate_stations=len(ordered_candidates),
            stations_loaded=stations_loaded,
        ),
        timeout=settings.ROUTE_PLAN_CACHE_SECONDS,
    )

    return PlanResult(
        start=ResolvedPoint(query=start, latitude=start_lat, longitude=start_lon),
        finish=ResolvedPoint(query=finish, latitude=finish_lat, longitude=finish_lon),
        range_miles=range_miles,
        mpg=mpg,
        corridor_miles=corridor_miles,
        initial_fuel_miles=initial_fuel_miles,
        route=route,
        fuel_plan=fuel_plan,
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
