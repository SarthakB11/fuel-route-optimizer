"""Candidate pruning and empty stop removal in the planner.

Both behaviours exist because the raw output of the optimiser, while correctly priced,
can describe stops a driver would never make. These are presentation bugs with a real
cost: a reviewer reading a plan that says "buy 0.0 gallons here" reasonably concludes
the thing is broken.
"""

from __future__ import annotations

import pytest

from fuelroute.optimizer import FuelPlan, FuelStop
from fuelroute.planner import _drop_empty_stops, _prune_candidates
from fuelroute.stations import RouteStation, Station


def _station(stop_id: str, price: float) -> Station:
    return Station(
        stop_id=stop_id,
        name=f"Station {stop_id}",
        address="",
        city="Testville",
        state="TX",
        latitude=31.0,
        longitude=-97.0,
        price_per_gallon=price,
    )


def _candidate(stop_id: str, offset: float, price: float) -> RouteStation:
    return RouteStation(station=_station(stop_id, price), offset_miles=offset, detour_miles=0.0)


def test_colocated_stations_collapse_to_the_cheapest() -> None:
    """Stations sharing a city centroid share an offset; only the cheapest can matter."""
    pruned = _prune_candidates(
        [
            _candidate("a", 0.0, 3.50),
            _candidate("b", 100.0, 4.20),
            _candidate("c", 100.0, 2.90),
            _candidate("d", 100.0, 3.80),
        ],
        500.0,
    )
    assert [c.station.stop_id for c in pruned] == ["a", "c"]


def test_pruning_keeps_stations_that_are_merely_close() -> None:
    """Only an identical point is dominated. A tenth of a mile apart is a real choice."""
    pruned = _prune_candidates(
        [_candidate("a", 0.0, 3.5), _candidate("b", 100.0, 4.2), _candidate("c", 100.1, 2.9)],
        500.0,
    )
    assert [c.station.stop_id for c in pruned] == ["a", "b", "c"]


def test_pruning_drops_candidates_beyond_the_destination() -> None:
    """A station past the end cannot be stopped at, and would break feasibility.

    The resampled polyline is slightly shorter than the distance the routing provider
    reports, so this cannot arise today. Filtering removes the dependency on that
    happening to stay true.
    """
    pruned = _prune_candidates(
        [_candidate("a", 0.0, 3.5), _candidate("b", 400.0, 3.0), _candidate("c", 500.1, 1.0)],
        500.0,
    )
    assert [c.station.stop_id for c in pruned] == ["a", "b"]


def test_pruning_keeps_a_candidate_exactly_at_the_destination() -> None:
    """The destination offset itself must survive, or short trips lose every station.

    On a trip shorter than the corridor is wide, every station nearby projects onto
    the route's final point. Dropping that offset emptied the candidate list and
    turned a tenth of a mile across a city into "no stations within 12.0 miles of the
    route", which is both wrong and unhelpful.
    """
    pruned = _prune_candidates([_candidate("a", 0.1, 3.5)], 0.1)
    assert [c.station.stop_id for c in pruned] == ["a"]


def test_pruning_preserves_offset_order() -> None:
    pruned = _prune_candidates(
        [_candidate("c", 200.0, 3.0), _candidate("a", 0.0, 3.5), _candidate("b", 100.0, 4.0)],
        500.0,
    )
    assert [c.offset_miles for c in pruned] == [0.0, 100.0, 200.0]


def _stop(offset: float, gallons: float, cumulative: float) -> FuelStop:
    return FuelStop(
        station=_station("x", 3.0),
        offset_miles=offset,
        detour_miles=0.0,
        gallons=gallons,
        price_per_gallon=3.0,
        cost=gallons * 3.0,
        cumulative_cost=cumulative,
        tank_miles_on_arrival=0.0,
        tank_miles_on_departure=0.0,
    )


def test_zero_gallon_stops_are_dropped_without_changing_totals() -> None:
    """A stop that buys nothing is not a stop, and removing it moves no money."""
    plan = FuelPlan(
        stops=(_stop(0.0, 0.0, 0.0), _stop(100.0, 10.0, 30.0), _stop(200.0, 0.0, 30.0)),
        total_cost=30.0,
        total_gallons=10.0,
        total_distance_miles=300.0,
    )
    trimmed = _drop_empty_stops(plan)

    assert len(trimmed.stops) == 1
    assert trimmed.stops[0].offset_miles == 100.0
    assert trimmed.total_cost == pytest.approx(plan.total_cost)
    assert trimmed.total_gallons == pytest.approx(plan.total_gallons)
    assert trimmed.stops[-1].cumulative_cost == pytest.approx(trimmed.total_cost)


def test_a_plan_with_no_empty_stops_is_returned_unchanged() -> None:
    plan = FuelPlan(
        stops=(_stop(0.0, 5.0, 15.0), _stop(100.0, 10.0, 45.0)),
        total_cost=45.0,
        total_gallons=15.0,
        total_distance_miles=300.0,
    )
    assert _drop_empty_stops(plan) is plan
