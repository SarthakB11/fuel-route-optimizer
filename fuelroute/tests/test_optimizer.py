"""Tests for the three clause greedy in fuelroute.optimizer.

Every candidates list here includes a station at offset_miles == 0.0 (or, for
the initial_fuel_miles tests, within initial_fuel_miles of the origin), the
caller's chosen stand in for the origin pump that plan_fuel_stops requires as a
precondition. That prepending is views.py's job in the real system, these
tests do it by hand.
"""

from __future__ import annotations

import pytest

from fuelroute.optimizer import RouteNotFeasible, plan_fuel_stops
from fuelroute.stations import RouteStation, Station

TOLERANCE = 1e-6


def _station(stop_id: str, offset_miles: float, price: float) -> RouteStation:
    station = Station(
        stop_id=stop_id,
        name=f"Station {stop_id}",
        address="I-00, EXIT 0",
        city="Testville",
        state="OK",
        latitude=36.0,
        longitude=-96.0,
        price_per_gallon=price,
    )
    return RouteStation(station=station, offset_miles=offset_miles, detour_miles=1.0)


def _assert_plan_invariants(plan, total_distance_miles, mpg, range_miles, initial_fuel_miles=0.0):
    # No leg between consecutive visited stops, nor origin to first, nor last to
    # destination, may exceed range_miles. The first stop is within
    # initial_fuel_miles, an empty tank cannot cover any more than that.
    if plan.stops:
        offsets = [stop.offset_miles for stop in plan.stops]
        assert offsets[0] - initial_fuel_miles <= TOLERANCE
        for earlier, later in zip(offsets, offsets[1:], strict=False):
            assert later - earlier <= range_miles + TOLERANCE
        assert total_distance_miles - offsets[-1] <= range_miles + TOLERANCE

    assert plan.total_gallons == pytest.approx(
        max(0.0, total_distance_miles - initial_fuel_miles) / mpg, abs=1e-6
    )

    if plan.stops:
        assert plan.stops[-1].cumulative_cost == pytest.approx(plan.total_cost, abs=1e-6)
        # The tank is calibrated to arrive at the destination with nothing left.
        last = plan.stops[-1]
        assert last.tank_miles_on_departure == pytest.approx(
            total_distance_miles - last.offset_miles, abs=1e-6
        )


def test_destination_clause_buys_only_what_is_needed_and_empties_the_tank() -> None:
    candidates = [_station("O", 0.0, 5.0)]
    plan = plan_fuel_stops(candidates, total_distance_miles=400.0, range_miles=500.0, mpg=10.0)

    assert len(plan.stops) == 1
    stop = plan.stops[0]
    assert stop.gallons == pytest.approx(400.0 / 10.0)
    assert stop.tank_miles_on_departure == pytest.approx(400.0)
    _assert_plan_invariants(plan, total_distance_miles=400.0, mpg=10.0, range_miles=500.0)


def test_mandatory_regression_destination_first_is_not_optimal() -> None:
    # The exact counterexample that disproved the original spec text: checking
    # whether the destination is in range before checking for a real, strictly
    # cheaper station gives $53.04 by buying the whole trip at the origin.
    # Checking the cheaper station first, the corrected order, buys just enough
    # to reach it and finishes there for $50.22, the true optimum.
    candidates = [_station("origin", 0.0, 4.42), _station("cheap", 100.0, 3.01)]
    plan = plan_fuel_stops(candidates, total_distance_miles=120.0, range_miles=120.0, mpg=10.0)

    assert plan.total_cost == pytest.approx(50.22)
    visited_ids = [stop.station.stop_id for stop in plan.stops]
    assert visited_ids == ["origin", "cheap"]
    origin_stop, cheap_stop = plan.stops
    assert origin_stop.gallons == pytest.approx(10.0)
    assert origin_stop.cost == pytest.approx(44.2)
    assert cheap_stop.gallons == pytest.approx(2.0)
    assert cheap_stop.cost == pytest.approx(6.02)
    _assert_plan_invariants(plan, total_distance_miles=120.0, mpg=10.0, range_miles=120.0)


def test_prefers_nearest_cheaper_station_over_dearer_nearer_one() -> None:
    # C1 sits closer but costs more than the starting price, so it must be
    # skipped. C2 sits farther, inside range, and is strictly cheaper.
    candidates = [
        _station("O", 0.0, 5.0),
        _station("C1", 100.0, 6.0),
        _station("C2", 280.0, 3.0),
    ]
    plan = plan_fuel_stops(candidates, total_distance_miles=730.0, range_miles=500.0, mpg=10.0)

    visited_ids = [stop.station.stop_id for stop in plan.stops]
    assert visited_ids == ["O", "C2"]

    o_stop, c2_stop = plan.stops
    assert o_stop.gallons == pytest.approx(280.0 / 10.0)
    assert o_stop.cost == pytest.approx(o_stop.gallons * 5.0)
    assert c2_stop.tank_miles_on_arrival == pytest.approx(0.0, abs=1e-9)
    assert c2_stop.gallons == pytest.approx((730.0 - 280.0) / 10.0)

    expected_total = o_stop.cost + c2_stop.cost
    assert plan.total_cost == pytest.approx(expected_total)
    _assert_plan_invariants(plan, total_distance_miles=730.0, mpg=10.0, range_miles=500.0)


def test_fallback_fills_to_range_and_prefers_farthest_on_price_tie() -> None:
    # No station ahead is cheaper than the current stop, so the fallback fires:
    # fill the tank fully and drive to the cheapest in range station, breaking
    # the D1 versus D2 price tie by taking the farther one, D2.
    candidates = [
        _station("O", 0.0, 5.0),
        _station("D1", 100.0, 6.0),
        _station("D2", 380.0, 6.0),
    ]
    plan = plan_fuel_stops(candidates, total_distance_miles=830.0, range_miles=500.0, mpg=10.0)

    visited_ids = [stop.station.stop_id for stop in plan.stops]
    assert visited_ids == ["O", "D2"]

    o_stop, d2_stop = plan.stops
    assert o_stop.tank_miles_on_departure == pytest.approx(500.0)
    assert o_stop.gallons == pytest.approx(500.0 / 10.0)
    assert d2_stop.tank_miles_on_arrival == pytest.approx(500.0 - 380.0)
    _assert_plan_invariants(plan, total_distance_miles=830.0, mpg=10.0, range_miles=500.0)


def test_route_not_feasible_when_gap_exceeds_range() -> None:
    candidates = [_station("O", 0.0, 5.0), _station("B", 600.0, 3.0)]
    with pytest.raises(RouteNotFeasible, match=r"600"):
        plan_fuel_stops(candidates, total_distance_miles=900.0, range_miles=500.0, mpg=10.0)


def test_route_not_feasible_no_station_within_initial_fuel() -> None:
    # With the default initial_fuel_miles of 0, an empty tank cannot reach a
    # station that is not sitting at offset 0.
    candidates = [_station("A", 600.0, 5.0)]
    with pytest.raises(RouteNotFeasible):
        plan_fuel_stops(candidates, total_distance_miles=900.0, range_miles=500.0, mpg=10.0)


def test_route_not_feasible_destination_out_of_range_of_last_station() -> None:
    candidates = [_station("O", 0.0, 5.0)]
    with pytest.raises(RouteNotFeasible):
        plan_fuel_stops(candidates, total_distance_miles=900.0, range_miles=500.0, mpg=10.0)


def test_route_not_feasible_no_candidates_and_destination_out_of_initial_fuel() -> None:
    with pytest.raises(RouteNotFeasible):
        plan_fuel_stops([], total_distance_miles=900.0, range_miles=500.0, mpg=10.0)


def test_trip_shorter_than_initial_fuel_costs_zero() -> None:
    plan = plan_fuel_stops(
        [], total_distance_miles=50.0, range_miles=500.0, mpg=10.0, initial_fuel_miles=100.0
    )
    assert plan.stops == ()
    assert plan.total_cost == 0.0
    assert plan.total_gallons == 0.0


def test_trip_exactly_equal_to_initial_fuel_costs_zero() -> None:
    plan = plan_fuel_stops(
        [], total_distance_miles=100.0, range_miles=500.0, mpg=10.0, initial_fuel_miles=100.0
    )
    assert plan.total_cost == 0.0


def test_initial_fuel_is_subtracted_from_total_gallons() -> None:
    candidates = [_station("O", 0.0, 5.0)]
    plan = plan_fuel_stops(
        candidates,
        total_distance_miles=400.0,
        range_miles=500.0,
        mpg=10.0,
        initial_fuel_miles=15.0,
    )
    assert plan.total_gallons == pytest.approx((400.0 - 15.0) / 10.0)
    # The 15 free miles are already in the tank on arrival at the origin pump.
    assert plan.stops[0].tank_miles_on_arrival == pytest.approx(15.0)


def test_initial_fuel_can_place_the_first_stop_off_the_origin() -> None:
    # The first candidate need not sit exactly at offset 0, only within
    # initial_fuel_miles of it, an empty tank cannot move but a partly full one
    # can coast a little before the first purchase.
    candidates = [_station("first", 15.0, 5.0), _station("B", 300.0, 2.0)]
    plan = plan_fuel_stops(
        candidates,
        total_distance_miles=750.0,
        range_miles=500.0,
        mpg=10.0,
        initial_fuel_miles=20.0,
    )
    assert plan.stops[0].tank_miles_on_arrival == pytest.approx(5.0)
    _assert_plan_invariants(
        plan, total_distance_miles=750.0, mpg=10.0, range_miles=500.0, initial_fuel_miles=20.0
    )


def test_cumulative_cost_equals_total_cost() -> None:
    candidates = [
        _station("O", 0.0, 5.0),
        _station("C1", 100.0, 6.0),
        _station("C2", 280.0, 3.0),
    ]
    plan = plan_fuel_stops(candidates, total_distance_miles=730.0, range_miles=500.0, mpg=10.0)
    assert plan.stops[-1].cumulative_cost == pytest.approx(plan.total_cost)
