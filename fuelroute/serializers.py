"""Query validation and response shaping for the route planning API.

All rounding for the HTTP response happens here: money to 2 decimal places,
prices per gallon and gallons to 3, miles to 1, coordinates to 6. The planner and optimizer
keep full floating point precision throughout the calculation, this module is
the only place a value gets truncated for display.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.conf import settings
from rest_framework import serializers

from fuelroute.planner import (
    GEOMETRY_CHOICES,
    GEOMETRY_FULL,
    GEOMETRY_SIMPLIFIED,
    ORIGIN_SOURCE_NEAREST,
    ORIGIN_SOURCE_NONE,
    SIMPLIFY_TOLERANCE_MILES,
    PlanResult,
)

MONEY_DP = 2
PRICE_DP = 3
# Gallons get a third decimal place because a caller checking the plan back against
# the distance multiplies them by mpg. At mpg=1000 a hundredth of a gallon is four
# miles, which is enough to look like an error in the arithmetic.
GALLONS_DP = 3
MILES_DP = 1
COORD_DP = 6

# Past this, the point the caller asked for and the point the route starts from are
# different places and the response should say so out loud.
SNAP_NOTE_MILES = 5.0

# Long enough for any real place name with a state and a country on the end, short
# enough that a pasted document cannot be echoed back inside an error message.
MAX_LOCATION_LENGTH = 200


class RoutePlanQuerySerializer(serializers.Serializer):
    """Validates the query parameters for GET /api/v1/route-plan."""

    start = serializers.CharField(
        allow_blank=False, trim_whitespace=True, max_length=MAX_LOCATION_LENGTH
    )
    finish = serializers.CharField(
        allow_blank=False, trim_whitespace=True, max_length=MAX_LOCATION_LENGTH
    )
    range_miles = serializers.FloatField(required=False, default=settings.DEFAULT_RANGE_MILES)
    mpg = serializers.FloatField(required=False, default=settings.DEFAULT_MPG)
    corridor_miles = serializers.FloatField(required=False, default=settings.DEFAULT_CORRIDOR_MILES)
    initial_fuel_miles = serializers.FloatField(required=False, default=0.0)
    geometry = serializers.ChoiceField(
        choices=GEOMETRY_CHOICES, required=False, default=GEOMETRY_SIMPLIFIED
    )

    def validate_range_miles(self, value: float) -> float:
        if value <= 0:
            raise serializers.ValidationError("range_miles must be strictly positive.")
        return value

    def validate_mpg(self, value: float) -> float:
        if value <= 0:
            raise serializers.ValidationError("mpg must be strictly positive.")
        return value

    def validate_corridor_miles(self, value: float) -> float:
        if not (0 < value <= 50):
            raise serializers.ValidationError(
                "corridor_miles must be greater than 0 and at most 50."
            )
        return value

    def validate_initial_fuel_miles(self, value: float) -> float:
        if value < 0:
            raise serializers.ValidationError("initial_fuel_miles must not be negative.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Reject more starting fuel than the tank can physically hold.

        Without this the API happily reports a tank arriving somewhere with 700 miles
        of fuel in a 500 mile tank. It is also the one regime where the feasibility
        check is wrong, because it measures every gap against range_miles rather than
        against the fuel actually on board, so it can refuse a leg the vehicle could
        in fact coast. Ruling the input out is both the honest answer and the smaller
        change.
        """
        initial_fuel = attrs.get("initial_fuel_miles", 0.0)
        range_miles = attrs.get("range_miles")
        if range_miles is not None and initial_fuel > range_miles:
            raise serializers.ValidationError(
                {
                    "initial_fuel_miles": (
                        f"initial_fuel_miles ({initial_fuel:g}) cannot exceed "
                        f"range_miles ({range_miles:g}); the tank does not hold that much."
                    )
                }
            )
        return attrs


def _stop_payload(stop: Any, leg_miles: float) -> dict[str, Any]:
    """Round and flatten one FuelStop for the response, tagging it by kind.

    The kind is keyed on the offset, not on the position in the list. When the caller
    supplies initial_fuel_miles the vehicle can pass the origin pump without buying
    anything, that stop is dropped from the plan, and the first remaining entry is an
    ordinary stop somewhere down the road rather than a departure fill up.
    """
    station = stop.station
    return {
        "stop_id": station.stop_id,
        "kind": "departure_fill_up" if stop.offset_miles == 0.0 else "en_route",
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "latitude": round(station.latitude, COORD_DP),
        "longitude": round(station.longitude, COORD_DP),
        "offset_miles": round(stop.offset_miles, MILES_DP),
        "leg_miles": round(leg_miles, MILES_DP),
        "detour_miles": round(stop.detour_miles, MILES_DP),
        "price_per_gallon": round(stop.price_per_gallon, PRICE_DP),
        "gallons": round(stop.gallons, GALLONS_DP),
        "cost_usd": round(stop.cost, MONEY_DP),
        "cumulative_cost_usd": round(stop.cumulative_cost, MONEY_DP),
        "tank_miles_on_arrival": round(stop.tank_miles_on_arrival, MILES_DP),
        "tank_miles_on_departure": round(stop.tank_miles_on_departure, MILES_DP),
    }


def _stop_payloads(stops: Sequence[Any]) -> list[dict[str, Any]]:
    """Render the stops in order, giving each one the distance driven to reach it.

    offset_miles answers "where on the route is this?", which is what a map needs.
    leg_miles answers "how far do I drive before the next fill up?", which is what a
    driver reading the plan on the road needs, and deriving it from two offsets is
    exactly the arithmetic the response should not make its caller do. Both are taken
    from full precision offsets and rounded once, so neither inherits the other's
    rounding error.
    """
    payloads: list[dict[str, Any]] = []
    previous_offset = 0.0
    for stop in stops:
        payloads.append(_stop_payload(stop, stop.offset_miles - previous_offset))
        previous_offset = stop.offset_miles
    return payloads


def _simplification_note(result: PlanResult) -> str:
    """Say what was done to the geometry, in both modes, so the counts make sense."""
    if result.geometry == GEOMETRY_FULL:
        return (
            "geometry=full was requested, so the route geometry is exactly what the "
            "routing provider returned and geometry_vertices equals source_vertices. "
            f"The default simplifies it to a tolerance of {SIMPLIFY_TOLERANCE_MILES:g} "
            "miles."
        )
    return (
        "The route geometry is simplified with Douglas-Peucker to a tolerance of "
        f"{SIMPLIFY_TOLERANCE_MILES:g} miles, about 30 metres: no vertex the routing "
        "provider sent lies further than that from the line returned here, which is "
        "invisible at any zoom the map page offers. Station matching ran on the full "
        "polyline, so nothing in the plan depends on this. Pass geometry=full for "
        "every vertex the provider sent."
    )


def _snapped_endpoint_note(result: PlanResult) -> str | None:
    """Say which endpoint the routing provider moved, when it moved one far enough.

    Coordinates in the middle of a lake or a field are answered with a real route
    from the nearest road, and every mile and every dollar below is for that route,
    not for the point the caller typed. Below the threshold this is the ordinary
    business of putting a city centroid on the nearest street and saying so would be
    noise; above it, the answer is to a different question than the one asked.
    """
    moved = []
    if result.route.origin_snap_miles > SNAP_NOTE_MILES:
        moved.append(f"the start by {result.route.origin_snap_miles:.1f} miles")
    if result.route.destination_snap_miles > SNAP_NOTE_MILES:
        moved.append(f"the finish by {result.route.destination_snap_miles:.1f} miles")
    if not moved:
        return None
    return (
        "The routing provider moved the requested points onto the road network, "
        + " and ".join(moved)
        + ". Every distance and price below is for the snapped route, not for the "
        "coordinates as they were given."
    )


def _origin_leg_note(result: PlanResult) -> str:
    """Explain the stop drawn at mile zero, and say so plainly when it is a stand in.

    When the price file has no station near the origin, the departure fill up borrows
    a station further along the route. The plan's cost is still right, because that
    price is fixed from the data before the optimiser runs, but the first stop is then
    drawn at a place the driver has not reached yet. Saying where it actually is beats
    letting a reader discover the discrepancy in the coordinates.
    """
    if result.origin_price_source == ORIGIN_SOURCE_NONE:
        return "The trip covers no distance, so no fuel is bought and no station is priced."
    if result.origin_price_source == ORIGIN_SOURCE_NEAREST:
        return (
            "No station in the price file lies within the origin search radius. The "
            f"departure fill up is priced at {result.origin_pump_label}, which is "
            f"{result.origin_pump_offset_miles:.1f} miles along the route, and fuel for "
            "the opening leg is billed at that rate. The stop is listed at offset 0 "
            "because that is where the vehicle is fuelled, so its coordinates are the "
            "station's, not the origin's."
        )
    return (
        "The first stop is a fill up at offset 0, taken at the cheapest station near "
        "the origin. It is billed like any other stop, so the cost accounts for every "
        "mile driven from mile 0."
    )


def build_response_payload(result: PlanResult, total_ms: float, *, map_url: str) -> dict[str, Any]:
    """Assemble the full JSON body for a successful route plan response."""
    fuel_plan = result.fuel_plan
    stops = _stop_payloads(fuel_plan.stops)
    average_price = (
        fuel_plan.total_cost / fuel_plan.total_gallons if fuel_plan.total_gallons > 0 else 0.0
    )
    geometry_coordinates = [
        [round(lon, COORD_DP), round(lat, COORD_DP)] for lat, lon in result.geometry_coordinates
    ]

    assumptions: dict[str, Any] = {}
    snapped_endpoint = _snapped_endpoint_note(result)
    if snapped_endpoint is not None:
        assumptions["snapped_endpoint"] = snapped_endpoint
    if result.origin_price_source == ORIGIN_SOURCE_NONE:
        assumptions["trivial_route"] = (
            "start and finish coincide, so there is no route to drive and no fuel to "
            "buy. The routing provider was not called."
        )

    assumptions.update(
        {
            "empty_tank": (
                "The tank is treated as empty at the origin and empty on arrival, aside "
                "from any initial_fuel_miles supplied."
            ),
            "origin_price_source": result.origin_price_source,
            "origin_leg_billing": _origin_leg_note(result),
            "geocoding": (
                "Stations are geocoded to their city centroid, not the exact highway "
                "exit, so the corridor width absorbs that offset."
            ),
            "detour_miles": (
                "detour_miles measures how far a station sits from the route. It is not "
                "added to the distance driven or to the trip cost."
            ),
            "corridor_miles": (
                f"Only stations within {result.corridor_miles:.1f} miles of the route "
                "are considered as candidates."
            ),
            "simplification": _simplification_note(result),
        }
    )

    # Key order matters here for a human reading the raw body in a browser. The route
    # geometry can run to tens of thousands of coordinates and would otherwise bury the
    # answer, so the plan and the totals come first and the geometry sits near the end.
    return {
        "request": {
            "start": {
                "query": result.start.query,
                "latitude": round(result.start.latitude, COORD_DP),
                "longitude": round(result.start.longitude, COORD_DP),
                "snapped_to_road_miles": round(result.route.origin_snap_miles, MILES_DP),
            },
            "finish": {
                "query": result.finish.query,
                "latitude": round(result.finish.latitude, COORD_DP),
                "longitude": round(result.finish.longitude, COORD_DP),
                "snapped_to_road_miles": round(result.route.destination_snap_miles, MILES_DP),
            },
            "range_miles": round(result.range_miles, MILES_DP),
            "mpg": round(result.mpg, MILES_DP),
            "corridor_miles": round(result.corridor_miles, MILES_DP),
            "initial_fuel_miles": round(result.initial_fuel_miles, MILES_DP),
            "geometry": result.geometry,
        },
        # The link that turns "return a map of the route" into something a browser can
        # open, rather than something the caller has to assemble from the parameters
        # they just sent. Near the top because it is the one field a human pastes.
        "map_url": map_url,
        "fuel_plan": {
            "stops": stops,
            "stop_count": len(stops),
            "total_cost_usd": round(fuel_plan.total_cost, MONEY_DP),
            "total_gallons": round(fuel_plan.total_gallons, GALLONS_DP),
            "average_price_per_gallon": round(average_price, PRICE_DP),
        },
        "route": {
            "provider": result.route.provider,
            "distance_miles": round(result.route.distance_miles, MILES_DP),
            "duration_hours": round(result.route.duration_seconds / 3600, MONEY_DP),
            # Both counts, so a caller can see what was dropped without asking for the
            # full geometry to compare against.
            "geometry_vertices": len(geometry_coordinates),
            "source_vertices": len(result.route.coordinates),
            "geometry": {"type": "LineString", "coordinates": geometry_coordinates},
        },
        "assumptions": assumptions,
        "performance": {
            "total_ms": round(total_ms, 2),
            "routing_ms": round(result.routing_ms, 2),
            "compute_ms": round(result.compute_ms, 2),
            "external_api_calls": result.external_api_calls,
            "candidate_stations": result.candidate_stations,
            "stations_loaded": result.stations_loaded,
            "cache": result.cache,
        },
    }
