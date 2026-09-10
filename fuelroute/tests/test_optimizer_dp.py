"""Cross check the greedy in fuelroute.optimizer against the brute force dynamic
program in dp_reference.py, the strongest correctness evidence in this repo.

mpg is fixed at 1.0 here so gallons and miles are the same integer quantity,
which is what lets dp_reference.py enumerate tank states exhaustively. Offsets,
range_miles and initial_fuel_miles are all integers for the same reason.

Every generated instance includes a station at offset_miles == 0.0, standing in
for the origin pump plan_fuel_stops requires its caller to prepend. That is a
precondition of the model, not a choice being tested here, views.py picks the
real station.
"""

from __future__ import annotations

import random

import pytest

from fuelroute.optimizer import RouteNotFeasible, plan_fuel_stops
from fuelroute.stations import RouteStation, Station
from fuelroute.tests.dp_reference import dp_min_total_cost

INSTANCE_COUNT = 220
TOLERANCE = 1e-6


def _station(stop_id: str, offset_miles: float, price: int) -> RouteStation:
    station = Station(
        stop_id=stop_id,
        name=f"Station {stop_id}",
        address="I-00, EXIT 0",
        city="Testville",
        state="OK",
        latitude=36.0,
        longitude=-96.0,
        price_per_gallon=float(price),
    )
    return RouteStation(station=station, offset_miles=offset_miles, detour_miles=0.5)


FUEL_REGIMES = ("zero", "fixed_50", "full_range", "random")


def _generate_instance(
    rng: random.Random, force_infeasible: bool, fuel_regime: str = "random"
) -> tuple[list[RouteStation], float, float, float]:
    """Build a small random instance with a station at offset 0.0.

    Feasible instances draw every interior and destination gap from
    [1, range_miles], which matches optimizer._check_feasibility exactly.
    Infeasible ones force exactly one of the three checked gaps, the origin
    pump missing from initial_fuel_miles range, an interior gap, or the final
    leg to the destination, past range_miles, and leave the rest normal.

    fuel_regime picks initial_fuel_miles deliberately rather than leaving it to
    chance, so the three interesting cases, no initial fuel, a fixed amount
    comfortably less than a full tank, and a full tank, are all exercised on
    purpose rather than only when the random draw happens to land there.
    "fixed_50" widens range_miles so 50.0 miles of initial fuel is meaningfully
    less than a full tank, not clipped down to it.
    """
    if fuel_regime == "fixed_50":
        range_miles = rng.randint(55, 80)
        initial_fuel = 50.0
    else:
        range_miles = rng.randint(10, 20)
        if fuel_regime == "zero":
            initial_fuel = 0.0
        elif fuel_regime == "full_range":
            initial_fuel = float(range_miles)
        else:
            initial_fuel = float(rng.choice([0, 0, 0, rng.randint(0, range_miles)]))

    station_count = rng.randint(2, 8)

    if station_count > 1:
        kinds = ["origin", "interior", "destination"]
    else:
        kinds = ["origin", "destination"]
    forced_kind = rng.choice(kinds) if force_infeasible else None

    if forced_kind == "origin":
        # Simulate the caller failing to find a pump within the fuel already in
        # the tank, the nearest candidate sits beyond it.
        offsets = [initial_fuel + range_miles + rng.randint(1, range_miles)]
    else:
        offsets = [0.0]

    forced_interior_index = rng.randint(1, station_count - 1) if forced_kind == "interior" else None
    for idx in range(1, station_count):
        if idx == forced_interior_index:
            gap = range_miles + rng.randint(1, range_miles)
        else:
            gap = rng.randint(1, range_miles)
        offsets.append(offsets[-1] + gap)

    if forced_kind == "destination":
        total_distance = offsets[-1] + range_miles + rng.randint(1, range_miles)
    else:
        total_distance = offsets[-1] + rng.randint(1, range_miles)

    prices = [rng.randint(1, 10) for _ in range(station_count)]
    candidates = [_station(str(i), offsets[i], prices[i]) for i in range(station_count)]
    return candidates, float(total_distance), float(range_miles), initial_fuel


def _instance_for_seed(seed: int) -> tuple[list[RouteStation], float, float, float]:
    """The exact instance test_greedy_matches_dp_reference[seed] runs on.

    Shared with the vacuity guard test so the two can never quietly drift out
    of sync with each other.
    """
    rng = random.Random(seed)
    force_infeasible = rng.random() < 0.15
    fuel_regime = FUEL_REGIMES[seed % len(FUEL_REGIMES)]
    return _generate_instance(rng, force_infeasible, fuel_regime=fuel_regime)


def _run_instance(seed: int) -> None:
    candidates, total_distance, range_miles, initial_fuel = _instance_for_seed(seed)

    plan = None
    dp_cost = None
    greedy_error: RouteNotFeasible | None = None
    dp_error: RouteNotFeasible | None = None

    try:
        plan = plan_fuel_stops(
            candidates,
            total_distance,
            range_miles=range_miles,
            mpg=1.0,
            initial_fuel_miles=initial_fuel,
        )
    except RouteNotFeasible as exc:
        greedy_error = exc

    try:
        dp_cost = dp_min_total_cost(
            candidates, total_distance, range_miles=range_miles, initial_fuel_miles=initial_fuel
        )
    except RouteNotFeasible as exc:
        dp_error = exc

    if greedy_error is not None or dp_error is not None:
        assert greedy_error is not None, (
            f"seed {seed}: greedy found a plan but the DP says infeasible ({dp_error})"
        )
        assert dp_error is not None, (
            f"seed {seed}: DP found a plan but the greedy says infeasible ({greedy_error})"
        )
        return

    assert plan is not None
    assert plan.total_cost == pytest.approx(dp_cost, abs=TOLERANCE), (
        f"seed {seed}: greedy {plan.total_cost} != dp {dp_cost}"
    )

    # The same invariants required of the optimizer, checked for free on every
    # one of these randomised instances. A trip shorter than initial_fuel_miles
    # is the one case where total_distance - initial_fuel goes negative, the
    # plan still needs 0 gallons, not a negative amount.
    assert plan.total_gallons == pytest.approx(
        max(0.0, total_distance - initial_fuel), abs=TOLERANCE
    )
    if plan.stops:
        offsets = [stop.offset_miles for stop in plan.stops]
        assert offsets[0] - initial_fuel <= TOLERANCE
        for earlier, later in zip(offsets, offsets[1:], strict=False):
            assert later - earlier <= range_miles + TOLERANCE
        assert total_distance - offsets[-1] <= range_miles + TOLERANCE
        last = plan.stops[-1]
        assert last.tank_miles_on_departure == pytest.approx(
            total_distance - last.offset_miles, abs=TOLERANCE
        )
        assert last.cumulative_cost == pytest.approx(plan.total_cost, abs=TOLERANCE)


@pytest.mark.parametrize("seed", range(INSTANCE_COUNT))
def test_greedy_matches_dp_reference(seed: int) -> None:
    _run_instance(seed)


def test_cross_check_feasible_share_is_not_vacuous() -> None:
    """Guards against the whole cross check above quietly checking nothing.

    If the generator ever regressed to not pinning a reachable origin pump,
    almost every instance would be infeasible, both implementations would
    correctly agree on "not feasible", and test_greedy_matches_dp_reference
    would pass having never compared a single real cost. Feasible instances
    over the same INSTANCE_COUNT seeds this file actually runs measured around
    83 percent, so a floor well below that still fails loudly on a regression.
    """
    feasible = 0
    for seed in range(INSTANCE_COUNT):
        candidates, total_distance, range_miles, initial_fuel = _instance_for_seed(seed)
        try:
            plan_fuel_stops(
                candidates,
                total_distance,
                range_miles=range_miles,
                mpg=1.0,
                initial_fuel_miles=initial_fuel,
            )
            feasible += 1
        except RouteNotFeasible:
            pass
    assert feasible > INSTANCE_COUNT * 0.5, (
        f"only {feasible}/{INSTANCE_COUNT} instances were feasible, "
        "the cross check above is not actually exercising the greedy"
    )


def test_generator_produces_a_meaningful_share_of_three_or_more_stops() -> None:
    rng = random.Random(20260910)
    feasible = 0
    three_or_more = 0
    for _ in range(300):
        candidates, total_distance, range_miles, initial_fuel = _generate_instance(
            rng, force_infeasible=False
        )
        plan = plan_fuel_stops(
            candidates,
            total_distance,
            range_miles=range_miles,
            mpg=1.0,
            initial_fuel_miles=initial_fuel,
        )
        feasible += 1
        if len(plan.stops) >= 3:
            three_or_more += 1
    assert feasible > 0
    assert three_or_more / feasible > 0.3


def test_generator_produces_some_infeasible_instances() -> None:
    rng = random.Random(13579)
    infeasible = 0
    for _ in range(300):
        candidates, total_distance, range_miles, initial_fuel = _generate_instance(
            rng, force_infeasible=True
        )
        try:
            plan_fuel_stops(
                candidates,
                total_distance,
                range_miles=range_miles,
                mpg=1.0,
                initial_fuel_miles=initial_fuel,
            )
        except RouteNotFeasible:
            infeasible += 1
    assert infeasible > 0
