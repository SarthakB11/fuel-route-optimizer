"""A slow, obviously correct reference solver for the fuel stop problem.

This is test only code. It exists so test_optimizer_dp.py can check the greedy
in fuelroute/optimizer.py against an independent, brute force ground truth on
small randomised instances. Clarity beats speed here on purpose: every reachable
(station, integer gallons in tank) state is explored, and every possible
purchase amount between each pair of stations is tried, so there is no clever
shortcut that could hide a bug shared with the greedy.

Restrictions that keep the state space small and integral, acceptable because
this only ever runs against small generated instances:
  - candidate offsets, range_miles and initial_fuel_miles must be integers,
  - mpg must be 1.0, so gallons and miles are the same integer quantity.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from fuelroute.optimizer import RouteNotFeasible
from fuelroute.stations import RouteStation

INFINITY = math.inf


def dp_min_total_cost(
    candidates: Sequence[RouteStation],
    total_distance_miles: float,
    *,
    range_miles: float,
    initial_fuel_miles: float = 0.0,
) -> float:
    """Return the true minimum total cost for the trip.

    candidates must already include a station at offset_miles == 0.0 (or, more
    generally, within initial_fuel_miles of the origin), the same precondition
    plan_fuel_stops has. Raises RouteNotFeasible under the same conditions as
    plan_fuel_stops, so a test can assert both the greedy and this reference
    agree on feasibility as well as on cost.
    """
    if total_distance_miles <= initial_fuel_miles:
        return 0.0

    if range_miles != int(range_miles) or initial_fuel_miles != int(initial_fuel_miles):
        raise ValueError("dp_min_total_cost requires integer range_miles and initial_fuel_miles")
    range_cap = int(range_miles)

    if not candidates:
        gap = total_distance_miles - initial_fuel_miles
        raise RouteNotFeasible(f"No fuel stations available, {gap:.1f} miles short.")

    offsets = [candidate.offset_miles for candidate in candidates]
    prices = [candidate.station.price_per_gallon for candidate in candidates]
    if any(offset != int(offset) for offset in offsets):
        raise ValueError("dp_min_total_cost requires integer candidate offsets")

    # An empty tank cannot move, so the first candidate, the caller's chosen
    # stand in for the origin pump, must already sit within the fuel that was
    # in the tank at the start. There is no separate origin leg any more, the
    # DP just needs a valid starting state to search forward from.
    origin_gap = offsets[0] - initial_fuel_miles
    if origin_gap > 1e-9:
        raise RouteNotFeasible(f"First station is {origin_gap:.1f} miles beyond initial fuel.")
    for previous_offset, next_offset in zip(offsets, offsets[1:], strict=False):
        if next_offset - previous_offset > range_miles:
            raise RouteNotFeasible(f"Gap of {next_offset - previous_offset:.1f} miles too wide.")
    destination_gap = total_distance_miles - offsets[-1]
    if destination_gap > range_miles:
        raise RouteNotFeasible(f"Destination is {destination_gap:.1f} miles beyond range.")

    start_fuel = int(max(0.0, initial_fuel_miles - offsets[0]))

    station_count = len(candidates)
    # dp[i][g] is the minimum spend to have arrived at station i with exactly g
    # integer miles of fuel already in the tank.
    dp = [[INFINITY] * (range_cap + 1) for _ in range(station_count)]
    dp[0][start_fuel] = 0.0

    best_finish = INFINITY
    for i in range(station_count):
        price = prices[i]
        offset_i = offsets[i]

        finish_gap = total_distance_miles - offset_i
        if finish_gap <= range_miles:
            for g in range(range_cap + 1):
                if dp[i][g] == INFINITY:
                    continue
                # finish_gap and g are both integral by construction, round only
                # guards against float noise carried in the offsets.
                buy = max(0, int(round(finish_gap)) - g)
                candidate_cost = dp[i][g] + buy * price
                if candidate_cost < best_finish:
                    best_finish = candidate_cost

        for j in range(i + 1, station_count):
            gap = offsets[j] - offset_i
            if gap > range_miles:
                break
            gap_int = int(round(gap))
            for g in range(range_cap + 1):
                if dp[i][g] == INFINITY:
                    continue
                min_buy = max(0, gap_int - g)
                max_buy = range_cap - g
                if max_buy < min_buy:
                    continue
                for buy in range(min_buy, max_buy + 1):
                    new_g = g + buy - gap_int
                    new_cost = dp[i][g] + buy * price
                    if new_cost < dp[j][new_g]:
                        dp[j][new_g] = new_cost

    if best_finish == INFINITY:
        raise RouteNotFeasible("No reachable sequence of stations covers the destination.")

    return best_finish
