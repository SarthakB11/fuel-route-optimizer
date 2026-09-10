"""Query validation and response shaping for the route planning API.

All rounding for the HTTP response happens here: money to 2 decimal places,
prices per gallon to 3, miles to 1, coordinates to 6. The planner and optimizer
keep full floating point precision throughout the calculation, this module is
the only place a value gets truncated for display.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from rest_framework import serializers

from fuelroute.planner import ORIGIN_SOURCE_NEAREST, PlanResult

MONEY_DP = 2
PRICE_DP = 3
MILES_DP = 1
COORD_DP = 6


class RoutePlanQuerySerializer(serializers.Serializer):
    """Validates the query parameters for GET /api/v1/route-plan."""

    start = serializers.CharField(allow_blank=False, trim_whitespace=True)
    finish = serializers.CharField(allow_blank=False, trim_whitespace=True)
    range_miles = serializers.FloatField(required=False, default=settings.DEFAULT_RANGE_MILES)
    mpg = serializers.FloatField(required=False, default=settings.DEFAULT_MPG)
    corridor_miles = serializers.FloatField(required=False, default=settings.DEFAULT_CORRIDOR_MILES)
    initial_fuel_miles = serializers.FloatField(required=False, default=0.0)

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


def _stop_payload(stop: Any) -> dict[str, Any]:
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
        "detour_miles": round(stop.detour_miles, MILES_DP),
        "price_per_gallon": round(stop.price_per_gallon, PRICE_DP),
        "gallons": round(stop.gallons, MONEY_DP),
        "cost_usd": round(stop.cost, MONEY_DP),
        "cumulative_cost_usd": round(stop.cumulative_cost, MONEY_DP),
        "tank_miles_on_arrival": round(stop.tank_miles_on_arrival, MILES_DP),
        "tank_miles_on_departure": round(stop.tank_miles_on_departure, MILES_DP),
    }


def _origin_leg_note(result: PlanResult) -> str:
    """Explain the stop drawn at mile zero, and say so plainly when it is a stand in.

    When the price file has no station near the origin, the departure fill up borrows
    a station further along the route. The plan's cost is still right, because that
    price is fixed from the data before the optimiser runs, but the first stop is then
    drawn at a place the driver has not reached yet. Saying where it actually is beats
    letting a reader discover the discrepancy in the coordinates.
    """
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


def build_response_payload(result: PlanResult, total_ms: float) -> dict[str, Any]:
    """Assemble the full JSON body for a successful route plan response."""
    fuel_plan = result.fuel_plan
    stops = [_stop_payload(stop) for stop in fuel_plan.stops]
    average_price = (
        fuel_plan.total_cost / fuel_plan.total_gallons if fuel_plan.total_gallons > 0 else 0.0
    )
    geometry_coordinates = [
        [round(lon, COORD_DP), round(lat, COORD_DP)] for lat, lon in result.route.coordinates
    ]

    # Key order matters here for a human reading the raw body in a browser. The route
    # geometry is tens of thousands of coordinates and would otherwise bury the answer,
    # so the plan and the totals come first and the geometry sits near the end.
    return {
        "request": {
            "start": {
                "query": result.start.query,
                "latitude": round(result.start.latitude, COORD_DP),
                "longitude": round(result.start.longitude, COORD_DP),
            },
            "finish": {
                "query": result.finish.query,
                "latitude": round(result.finish.latitude, COORD_DP),
                "longitude": round(result.finish.longitude, COORD_DP),
            },
            "range_miles": round(result.range_miles, MILES_DP),
            "mpg": round(result.mpg, MILES_DP),
            "corridor_miles": round(result.corridor_miles, MILES_DP),
            "initial_fuel_miles": round(result.initial_fuel_miles, MILES_DP),
        },
        "fuel_plan": {
            "stops": stops,
            "stop_count": len(stops),
            "total_cost_usd": round(fuel_plan.total_cost, MONEY_DP),
            "total_gallons": round(fuel_plan.total_gallons, MONEY_DP),
            "average_price_per_gallon": round(average_price, PRICE_DP),
        },
        "route": {
            "provider": result.route.provider,
            "distance_miles": round(result.route.distance_miles, MILES_DP),
            "duration_hours": round(result.route.duration_seconds / 3600, MONEY_DP),
            "geometry": {"type": "LineString", "coordinates": geometry_coordinates},
        },
        "assumptions": {
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
        },
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
