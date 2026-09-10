"""The fuel stop optimizer. Pure computation, no Django, no I/O.

The model: a pump sits at the origin. An empty tank cannot move, so the trip is
only well posed if the vehicle can fuel at offset 0. The caller of
plan_fuel_stops is responsible for prepending that origin pump to candidates at
offset_miles == 0.0, this module just sees an ordered station list and never
special cases the first entry. Choosing which real station stands in for the
origin pump (cheapest within a small radius, or nearest along the route) is the
caller's job, not this module's, so the optimisation problem stays well posed:
retroactively billing the opening miles at a distant station's price, tried in
an earlier version of this module, let a plan skip real stations and had no
stable optimum.

The tank is empty at the origin, every mile of the trip is paid for, and the
tank is empty on arrival, so total_gallons == total_distance_miles / mpg once
initial_fuel_miles is subtracted. From then on a greedy walk over the ordered
candidate stations picks where to buy fuel and how much: prefer a real,
strictly cheaper station in range if one exists, otherwise finish at the
destination if it is in range, otherwise fill up and drive to the cheapest
station in range. See the comment inside plan_fuel_stops for why the
destination check must come after the cheaper station search, not before,
despite that being the more obvious reading of the three clauses.

The correctness argument compactly: treat the destination as a pump priced at
zero. The whole algorithm then collapses to one rule, at each stop, if a
cheaper pump is reachable buy just enough to get there, otherwise fill up and
drive to the cheapest reachable pump, and the destination, priced at zero, is
always the cheapest thing reachable once nothing real beats it. Optimality is
an exchange argument: any gallon bought at price c while a cheaper pump was
reachable could have been deferred to that cheaper pump instead, so a plan
that ever does that is never the minimum.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fuelroute.stations import RouteStation, Station

TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class FuelStop:
    station: Station
    offset_miles: float
    detour_miles: float
    gallons: float
    price_per_gallon: float
    cost: float
    cumulative_cost: float
    tank_miles_on_arrival: float
    tank_miles_on_departure: float


@dataclass(frozen=True, slots=True)
class FuelPlan:
    stops: tuple[FuelStop, ...]
    total_cost: float
    total_gallons: float
    total_distance_miles: float


class RouteNotFeasible(ValueError):
    """Raised when no sequence of the candidate stations can cover the trip."""


def _describe(candidate: RouteStation) -> str:
    """Name a station the way a driver would read it: what, where, how far along."""
    station = candidate.station
    return f"{station.name} in {station.city}, {station.state} (mile {candidate.offset_miles:.0f})"


def _check_feasibility(
    candidates: Sequence[RouteStation],
    total_distance_miles: float,
    range_miles: float,
    initial_fuel_miles: float,
) -> None:
    """Validate every leg implied by the sorted candidate list is drivable.

    Checking only consecutive pairs is sufficient: offsets are sorted, so if a
    station cannot reach the next one in range, it cannot reach any later one
    either, and skipping a station never bridges a gap that is otherwise too
    wide.
    """
    if not candidates:
        gap = total_distance_miles - initial_fuel_miles
        raise RouteNotFeasible(
            f"No fuel stations available and the destination is {gap:.1f} miles "
            "beyond the initial fuel range."
        )

    # Every message below names the stations that bound the gap and where they
    # sit along the route. A bare "996.7 miles apart" tells the caller the trip
    # failed; naming Coachella, CA at mile 258 tells them why, and lets them see
    # at a glance that the price file, not the service, is what ran out.
    origin_gap = candidates[0].offset_miles - initial_fuel_miles
    if origin_gap > TOLERANCE:
        raise RouteNotFeasible(
            f"The first station on the route, {_describe(candidates[0])}, is "
            f"{origin_gap:.1f} miles beyond the {initial_fuel_miles:.1f} miles of "
            "initial fuel. An empty tank cannot reach it."
        )

    for previous, current in zip(candidates, candidates[1:], strict=False):
        gap = current.offset_miles - previous.offset_miles
        if gap > range_miles + TOLERANCE:
            raise RouteNotFeasible(
                f"The trip cannot be completed on a {range_miles:.0f} mile tank: after "
                f"{_describe(previous)} the next station on the route is "
                f"{_describe(current)}, {gap:.1f} miles further on. The price file has "
                "no station in between within the corridor."
            )

    destination_gap = total_distance_miles - candidates[-1].offset_miles
    if destination_gap > range_miles + TOLERANCE:
        raise RouteNotFeasible(
            f"The trip cannot be completed on a {range_miles:.0f} mile tank: the last "
            f"station on the route is {_describe(candidates[-1])}, and the destination "
            f"is {destination_gap:.1f} miles beyond it with no station in between "
            "within the corridor."
        )


def _reachable(
    candidates: Sequence[RouteStation], start_index: int, reach_miles: float
) -> list[int]:
    """Indices of every candidate after start_index within reach_miles, nearest first."""
    result = []
    for j in range(start_index + 1, len(candidates)):
        if candidates[j].offset_miles > reach_miles + TOLERANCE:
            break
        result.append(j)
    return result


def _select_fallback(candidates: Sequence[RouteStation], reach: list[int]) -> int:
    """Clause 3: the cheapest station in reach, farthest away on a price tie."""
    best_price = min(candidates[j].station.price_per_gallon for j in reach)
    tied = [
        j for j in reach if abs(candidates[j].station.price_per_gallon - best_price) <= TOLERANCE
    ]
    return max(tied, key=lambda j: candidates[j].offset_miles)


def plan_fuel_stops(
    candidates: Sequence[RouteStation],
    total_distance_miles: float,
    *,
    range_miles: float = 500.0,
    mpg: float = 10.0,
    initial_fuel_miles: float = 0.0,
) -> FuelPlan:
    """Plan the cheapest sequence of fuel stops for a trip of total_distance_miles.

    candidates must already include a station at offset_miles == 0.0 (or, more
    generally, within initial_fuel_miles of the origin), the caller's chosen
    stand in for the origin pump. Raises RouteNotFeasible when no leg between
    the candidates and the destination can be covered by a single tank of
    range_miles.
    """
    if total_distance_miles <= initial_fuel_miles:
        return FuelPlan(
            stops=(),
            total_cost=0.0,
            total_gallons=0.0,
            total_distance_miles=total_distance_miles,
        )

    _check_feasibility(candidates, total_distance_miles, range_miles, initial_fuel_miles)

    stops: list[FuelStop] = []
    cumulative_cost = 0.0
    index = 0
    fuel = max(0.0, initial_fuel_miles - candidates[0].offset_miles)

    while True:
        current = candidates[index]
        position = current.offset_miles
        price = current.station.price_per_gallon
        reach_miles = position + range_miles
        reach = _reachable(candidates, index, reach_miles)
        dest_in_range = (total_distance_miles - position) <= range_miles + TOLERANCE

        # A real, strictly cheaper station reachable from here, and positioned
        # before the destination, always wins over finishing the trip at the
        # current, more expensive price: any fuel bought now that is not needed
        # to reach that cheaper station would otherwise be bought there for
        # less. Only once no such station exists does it become correct to
        # check whether the destination itself is already in range. Checking
        # the destination before this real station search is the more obvious
        # reading of the three clauses, but it is not optimal, it can strand
        # the tank paying the current, higher price for miles a cheaper
        # station further on would have covered. Verified against
        # fuelroute/tests/dp_reference.py on thousands of randomised
        # instances, this ordering is the one that always matches the true
        # optimum.
        cheaper = [
            j
            for j in reach
            if candidates[j].station.price_per_gallon < price - TOLERANCE
            and candidates[j].offset_miles < total_distance_miles - TOLERANCE
        ]

        if cheaper:
            # No destination check is needed here. "cheaper" already excludes any
            # station at or past the destination, so the station chosen below always
            # sits strictly before it, and buying just enough to reach it can never
            # overshoot the end of the trip.
            next_index = min(cheaper)
            next_offset = candidates[next_index].offset_miles
            miles_needed = max(0.0, (next_offset - position) - fuel)
            tank_departure = fuel + miles_needed
        elif dest_in_range:
            miles_needed = max(0.0, (total_distance_miles - position) - fuel)
            gallons = miles_needed / mpg
            cost = gallons * price
            cumulative_cost += cost
            tank_departure = fuel + miles_needed
            stops.append(
                FuelStop(
                    station=current.station,
                    offset_miles=position,
                    detour_miles=current.detour_miles,
                    gallons=gallons,
                    price_per_gallon=price,
                    cost=cost,
                    cumulative_cost=cumulative_cost,
                    tank_miles_on_arrival=fuel,
                    tank_miles_on_departure=tank_departure,
                )
            )
            break
        else:
            next_index = _select_fallback(candidates, reach)
            next_offset = candidates[next_index].offset_miles
            miles_needed = max(0.0, range_miles - fuel)
            tank_departure = fuel + miles_needed

        gallons = miles_needed / mpg
        cost = gallons * price
        cumulative_cost += cost
        stops.append(
            FuelStop(
                station=current.station,
                offset_miles=position,
                detour_miles=current.detour_miles,
                gallons=gallons,
                price_per_gallon=price,
                cost=cost,
                cumulative_cost=cumulative_cost,
                tank_miles_on_arrival=fuel,
                tank_miles_on_departure=tank_departure,
            )
        )

        fuel = tank_departure - (next_offset - position)
        index = next_index

    total_gallons = sum(stop.gallons for stop in stops)
    total_cost = stops[-1].cumulative_cost

    return FuelPlan(
        stops=tuple(stops),
        total_cost=total_cost,
        total_gallons=total_gallons,
        total_distance_miles=total_distance_miles,
    )
