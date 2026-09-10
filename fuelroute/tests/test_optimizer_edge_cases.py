"""Degenerate instances that a random generator produces rarely or never.

test_optimizer_dp.py checks the greedy against the exact dynamic program on randomised
instances, which is the strongest evidence that the algorithm is right in general. It
is weak evidence about the boundaries, because a uniform generator almost never emits a
station sitting exactly at the range limit, two stations at the same offset, or a trip
whose length is exactly the tank range. Those are pinned here by hand, with the
expected cost worked out independently of the implementation.
"""

from __future__ import annotations

import pytest

from fuelroute.optimizer import RouteNotFeasible, plan_fuel_stops
from fuelroute.stations import RouteStation, Station

MPG = 10.0


def _candidates(pairs: list[tuple[float, float]]) -> list[RouteStation]:
    """Build candidates from (offset_miles, price_per_gallon) pairs."""
    return [
        RouteStation(
            station=Station(
                stop_id=str(index),
                name=f"Station {index}",
                address="",
                city="Testville",
                state="TX",
                latitude=31.0,
                longitude=-97.0,
                price_per_gallon=price,
            ),
            offset_miles=offset,
            detour_miles=0.0,
        )
        for index, (offset, price) in enumerate(pairs)
    ]


def _cost(pairs, total, *, range_miles=500.0, initial_fuel_miles=0.0) -> float:
    plan = plan_fuel_stops(
        _candidates(pairs),
        total,
        range_miles=range_miles,
        mpg=MPG,
        initial_fuel_miles=initial_fuel_miles,
    )
    return plan.total_cost


def test_the_clause_order_counterexample() -> None:
    """The case that proves the destination check must not come first.

    Pumps at mile 0 at $4.42 and mile 100 at $3.01, a 120 mile trip, a 120 mile range.
    Finishing straight from the origin buys 12 gallons at $4.42 for $53.04. Stopping at
    the cheaper pump buys 10 gallons at $4.42 and 2 at $3.01, which is $50.22. An
    implementation that checks "is the destination in range" before "is anything
    cheaper reachable" returns the first number.
    """
    assert _cost([(0.0, 4.42), (100.0, 3.01)], 120.0, range_miles=120.0) == pytest.approx(50.22)


def test_single_station_with_destination_exactly_at_range() -> None:
    """A trip exactly as long as the tank is feasible, not off by one."""
    assert _cost([(0.0, 3.0)], 500.0) == pytest.approx(150.0)


def test_destination_one_mile_beyond_range_is_infeasible() -> None:
    with pytest.raises(RouteNotFeasible):
        _cost([(0.0, 3.0)], 501.0)


def test_station_exactly_at_the_range_boundary_is_reachable() -> None:
    """A station at exactly range_miles away can be reached, so the trip is possible."""
    assert _cost([(0.0, 4.0), (500.0, 2.0)], 900.0) == pytest.approx(280.0)


def test_station_one_mile_past_the_range_boundary_is_not() -> None:
    with pytest.raises(RouteNotFeasible):
        _cost([(0.0, 4.0), (501.0, 2.0)], 900.0)


def test_two_stations_at_the_same_offset_pick_the_cheaper() -> None:
    """Distinct stations can share an offset once both are snapped to a city centroid."""
    assert _cost([(0.0, 4.0), (100.0, 3.0), (100.0, 2.5)], 300.0) == pytest.approx(90.0)


def test_two_stations_at_the_same_offset_and_the_same_price() -> None:
    assert _cost([(0.0, 4.0), (100.0, 3.0), (100.0, 3.0)], 300.0) == pytest.approx(100.0)


def test_price_tie_in_the_fill_up_branch_still_makes_progress() -> None:
    """With every price equal the plan must still terminate and cost the same.

    The fill up branch breaks a tie by taking the farthest station in range. Taking the
    nearest would still be optimal on price but could revisit the same short hop
    repeatedly, so this also guards against a stall.
    """
    assert _cost([(0.0, 5.0), (200.0, 5.0), (400.0, 5.0), (400.0, 5.0)], 900.0) == (
        pytest.approx(450.0)
    )


def test_monotonically_falling_prices_defer_every_purchase() -> None:
    """Buy the minimum at each stop, because the next one is always cheaper."""
    assert _cost([(0.0, 5.0), (100.0, 4.0), (200.0, 3.0), (300.0, 2.0)], 400.0) == (
        pytest.approx(140.0)
    )


def test_monotonically_rising_prices_buy_everything_at_the_first_stop() -> None:
    assert _cost([(0.0, 2.0), (100.0, 3.0), (200.0, 4.0), (300.0, 5.0)], 400.0) == (
        pytest.approx(80.0)
    )


def test_a_cheap_station_beyond_range_cannot_rescue_an_impossible_gap() -> None:
    """A tempting price does not make an unreachable station reachable."""
    with pytest.raises(RouteNotFeasible):
        _cost([(0.0, 4.0), (100.0, 3.9), (900.0, 1.0)], 1000.0)


def test_zero_length_trip_costs_nothing() -> None:
    plan = plan_fuel_stops(_candidates([(0.0, 3.0)]), 0.0, range_miles=500.0, mpg=MPG)
    assert plan.total_cost == pytest.approx(0.0)
    assert plan.stops == ()


@pytest.mark.parametrize("initial_fuel", [100.0, 500.0])
def test_initial_fuel_covering_the_whole_trip_costs_nothing(initial_fuel: float) -> None:
    assert _cost([(0.0, 3.0)], 100.0, initial_fuel_miles=initial_fuel) == pytest.approx(0.0)


def test_initial_fuel_partially_covers_the_trip() -> None:
    """150 miles are already in the tank, so only 250 of the 400 are paid for."""
    assert _cost([(0.0, 4.0), (200.0, 2.0)], 400.0, initial_fuel_miles=150.0) == pytest.approx(60.0)


def test_first_station_unreachable_on_an_empty_tank_is_infeasible() -> None:
    """An empty tank cannot drive the 10 miles to the first pump."""
    with pytest.raises(RouteNotFeasible):
        _cost([(10.0, 4.0), (200.0, 2.0)], 400.0)


def test_first_station_reachable_on_the_initial_fuel() -> None:
    """The same instance becomes feasible with exactly enough fuel to coast there."""
    assert _cost([(10.0, 4.0), (200.0, 2.0)], 400.0, initial_fuel_miles=10.0) == pytest.approx(
        116.0
    )
