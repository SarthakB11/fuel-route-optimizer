"""Tests for fuelroute.planner. Everything here is offline and deterministic:
fuelroute.routing.fetch_route is monkeypatched to a counting fake that never
touches the network, and the station and place datasets are tiny fixtures
written to tmp_path, never the real committed 6626 row dataset.
"""

from __future__ import annotations

import json

import pytest

from fuelroute import geo, places, routing, stations
from fuelroute.optimizer import RouteNotFeasible
from fuelroute.places import UnknownLocation
from fuelroute.planner import (
    ORIGIN_SOURCE_NEAREST,
    ORIGIN_SOURCE_WITHIN_RADIUS,
    build_plan,
)

# A straight, constant latitude line so every station's offset along the route
# is simply its distance from the origin point, which keeps the fixtures easy
# to reason about.
ROUTE_COORDS = [
    (36.0, -100.0),
    (36.0, -99.0),
    (36.0, -98.0),
    (36.0, -97.0),
    (36.0, -96.0),
    (36.0, -95.0),
]

PLACES_PAYLOAD = {
    "by_city": {
        "ORIGIN CITY": [[36.0, -100.0, "OK"]],
        "FINISH CITY": [[36.0, -95.0, "AR"]],
    },
    "by_city_state": {
        "ORIGIN CITY|OK": [36.0, -100.0],
        "FINISH CITY|AR": [36.0, -95.0],
    },
    "state_names": {"OKLAHOMA": "OK", "ARKANSAS": "AR"},
}

# Dataset A: a cheap station a few miles inside the 25 mile origin radius (at a
# positive offset, not offset zero) and a pricier one closer to the origin, so
# the cheapest-within-radius rule has to reject the nearer station and the
# de-duplication logic has real work to do.
ROWS_WITHIN_RADIUS = [
    {
        "stop_id": "A1",
        "name": "Cheap Origin Pump",
        "address": "I-40, EXIT 1",
        "city": "Origin City",
        "state": "OK",
        "latitude": 36.05,
        "longitude": -99.8,
        "price_per_gallon": 2.80,
    },
    {
        "stop_id": "A2",
        "name": "Pricey Origin Pump",
        "address": "I-40, EXIT 2",
        "city": "Origin City",
        "state": "OK",
        "latitude": 36.05,
        "longitude": -99.9,
        "price_per_gallon": 4.50,
    },
    {
        "stop_id": "A3",
        "name": "Midpoint Pump",
        "address": "I-40, EXIT 100",
        "city": "Mid City",
        "state": "TX",
        "latitude": 36.05,
        "longitude": -97.5,
        "price_per_gallon": 3.00,
    },
    {
        "stop_id": "A4",
        "name": "Near Finish Pump",
        "address": "I-40, EXIT 200",
        "city": "Finish City",
        "state": "AR",
        "latitude": 36.05,
        "longitude": -95.05,
        "price_per_gallon": 3.20,
    },
]

# Dataset B: every station sits well beyond the 25 mile origin radius, so the
# nearest-along-route fallback has to fire instead. B1 is not the cheapest
# station overall, proving the fallback picks by offset, not by price.
ROWS_BEYOND_RADIUS = [
    {
        "stop_id": "B1",
        "name": "Nearest Far Pump",
        "address": "I-40, EXIT 40",
        "city": "Origin City",
        "state": "OK",
        "latitude": 36.05,
        "longitude": -99.3,
        "price_per_gallon": 5.00,
    },
    {
        "stop_id": "B2",
        "name": "Cheaper But Farther Pump",
        "address": "I-40, EXIT 100",
        "city": "Mid City",
        "state": "TX",
        "latitude": 36.05,
        "longitude": -97.5,
        "price_per_gallon": 3.00,
    },
    {
        "stop_id": "B3",
        "name": "Near Finish Pump",
        "address": "I-40, EXIT 200",
        "city": "Finish City",
        "state": "AR",
        "latitude": 36.05,
        "longitude": -95.05,
        "price_per_gallon": 3.20,
    },
]


class _FakeFetchRoute:
    """Stands in for fuelroute.routing.fetch_route, counting how often it runs."""

    def __init__(self, coordinates=ROUTE_COORDS, distance_miles=None, duration_seconds=3600.0):
        self.coordinates = tuple(coordinates)
        self.distance_miles = (
            distance_miles
            if distance_miles is not None
            else geo.cumulative_miles(list(self.coordinates))[-1]
        )
        self.duration_seconds = duration_seconds
        self.call_count = 0

    def __call__(self, origin, destination, *, session=None):
        self.call_count += 1
        return routing.Route(
            coordinates=self.coordinates,
            distance_miles=self.distance_miles,
            duration_seconds=self.duration_seconds,
            provider="OSRM",
            api_calls=1,
            elapsed_ms=1.5,
        )


def _write_stations(tmp_path, rows):
    path = tmp_path / "stations.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00Z",
                "source_csv": "test.csv",
                "source_csv_sha256": "0" * 64,
                "row_counts": {
                    "csv_rows": len(rows),
                    "non_us_dropped": 0,
                    "deduplicated": len(rows),
                    "geocoded": len(rows),
                    "unresolved": 0,
                },
                "geocode_tiers": {
                    "exact": len(rows),
                    "space_collapsed": 0,
                    "alternate": 0,
                    "override": 0,
                },
                "stations": rows,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_places(tmp_path, payload):
    path = tmp_path / "places.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def configure_dataset(tmp_path, monkeypatch):
    """Point stations.py and places.py at small fixture files for one test."""

    def _configure(station_rows, places_payload=PLACES_PAYLOAD):
        station_file = _write_stations(tmp_path, station_rows)
        monkeypatch.setattr(stations, "DEFAULT_STATION_DATA_FILE", station_file)
        stations._load_stations_cached.cache_clear()
        place_file = _write_places(tmp_path, places_payload)
        monkeypatch.setattr(places, "DEFAULT_PLACE_DATA_FILE", place_file)
        places._load_index.cache_clear()

    yield _configure
    stations._load_stations_cached.cache_clear()
    places._load_index.cache_clear()


@pytest.fixture(autouse=True)
def _clear_django_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def test_build_plan_calls_routing_once_then_hits_cache(configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute()
    monkeypatch.setattr(routing, "fetch_route", fake)

    first = build_plan(
        "Origin City, OK",
        "Finish City, AR",
        range_miles=500.0,
        mpg=10.0,
        corridor_miles=15.0,
        initial_fuel_miles=0.0,
    )
    assert first.cache == "miss"
    assert first.external_api_calls == 1
    assert fake.call_count == 1

    second = build_plan(
        "Origin City, OK",
        "Finish City, AR",
        range_miles=500.0,
        mpg=10.0,
        corridor_miles=15.0,
        initial_fuel_miles=0.0,
    )
    assert second.cache == "hit"
    assert second.external_api_calls == 0
    assert fake.call_count == 1


def test_cache_hit_reflects_the_current_request_query_text(configure_dataset, monkeypatch) -> None:
    # Two different strings that resolve to the same coordinates should share a
    # cache entry, but the echoed query text must always match what the caller
    # of this particular call actually typed, not whichever request populated
    # the cache first.
    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute()
    monkeypatch.setattr(routing, "fetch_route", fake)

    build_plan(
        "Origin City, OK",
        "Finish City, AR",
        range_miles=500.0,
        mpg=10.0,
        corridor_miles=15.0,
        initial_fuel_miles=0.0,
    )
    second = build_plan(
        "36.0,-100.0",
        "36.0,-95.0",
        range_miles=500.0,
        mpg=10.0,
        corridor_miles=15.0,
        initial_fuel_miles=0.0,
    )
    assert second.cache == "hit"
    assert fake.call_count == 1
    assert second.start.query == "36.0,-100.0"
    assert second.finish.query == "36.0,-95.0"


def test_origin_price_source_cheapest_within_radius(configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    result = build_plan(
        "Origin City, OK",
        "Finish City, AR",
        range_miles=500.0,
        mpg=10.0,
        corridor_miles=15.0,
        initial_fuel_miles=0.0,
    )
    assert result.origin_price_source == ORIGIN_SOURCE_WITHIN_RADIUS

    origin_stop = result.fuel_plan.stops[0]
    assert origin_stop.station.stop_id == "A1"
    assert origin_stop.offset_miles == pytest.approx(0.0, abs=1e-9)

    stop_ids = [stop.station.stop_id for stop in result.fuel_plan.stops]
    assert stop_ids.count("A1") == 1


def test_origin_price_source_nearest_along_route_when_none_in_radius(
    configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_BEYOND_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    result = build_plan(
        "Origin City, OK",
        "Finish City, AR",
        range_miles=500.0,
        mpg=10.0,
        corridor_miles=15.0,
        initial_fuel_miles=0.0,
    )
    assert result.origin_price_source == ORIGIN_SOURCE_NEAREST

    origin_stop = result.fuel_plan.stops[0]
    # B1 is nearest by offset even though B2 is cheaper, proving the fallback
    # picks by distance, not by price.
    assert origin_stop.station.stop_id == "B1"
    assert origin_stop.offset_miles == pytest.approx(0.0, abs=1e-9)


def test_origin_pump_deduplicated_when_also_in_corridor_at_positive_offset(
    configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    result = build_plan(
        "Origin City, OK",
        "Finish City, AR",
        range_miles=500.0,
        mpg=10.0,
        corridor_miles=15.0,
        initial_fuel_miles=0.0,
    )
    stop_ids = [stop.station.stop_id for stop in result.fuel_plan.stops]
    # A1 is the chosen origin pump. It originally sat at a positive offset
    # inside the corridor, so without de-duplication it would appear twice.
    assert stop_ids.count("A1") == 1
    assert len(stop_ids) == len(set(stop_ids))


def test_no_stations_in_corridor_raises_route_not_feasible(configure_dataset, monkeypatch) -> None:
    # A corridor far narrower than any station's detour excludes everything.
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    with pytest.raises(RouteNotFeasible, match="corridor"):
        build_plan(
            "Origin City, OK",
            "Finish City, AR",
            range_miles=500.0,
            mpg=10.0,
            corridor_miles=1.0,
            initial_fuel_miles=0.0,
        )


def test_route_not_feasible_from_optimizer_propagates(configure_dataset, monkeypatch) -> None:
    # Candidates exist, but the range is too short to bridge the gap between
    # the origin pump and the midpoint station.
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    with pytest.raises(RouteNotFeasible):
        build_plan(
            "Origin City, OK",
            "Finish City, AR",
            range_miles=50.0,
            mpg=10.0,
            corridor_miles=15.0,
            initial_fuel_miles=0.0,
        )


def test_total_gallons_matches_distance_over_mpg(configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute()
    monkeypatch.setattr(routing, "fetch_route", fake)

    result = build_plan(
        "Origin City, OK",
        "Finish City, AR",
        range_miles=500.0,
        mpg=10.0,
        corridor_miles=15.0,
        initial_fuel_miles=0.0,
    )
    assert result.fuel_plan.total_gallons == pytest.approx(
        result.route.distance_miles / 10.0, abs=1e-6
    )


def test_unknown_location_raises_before_any_routing_call(configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute()
    monkeypatch.setattr(routing, "fetch_route", fake)

    with pytest.raises(UnknownLocation):
        build_plan(
            "Nowhereville, ZZ",
            "Finish City, AR",
            range_miles=500.0,
            mpg=10.0,
            corridor_miles=15.0,
            initial_fuel_miles=0.0,
        )
    assert fake.call_count == 0


def test_concurrent_identical_cold_requests_share_one_routing_call(
    configure_dataset, monkeypatch
) -> None:
    """Six identical requests arriving together make one routing call, not six.

    Without single flight every one of them misses the cache and every one calls the
    provider, because none has stored a result yet. The fake below holds the first
    caller inside the routing call long enough for the others to pile up behind the
    per key lock, which is exactly the race this guards against.
    """
    import threading

    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute()
    started = threading.Event()

    def slow_fetch(origin, destination, *, session=None):
        started.set()
        threading.Event().wait(0.15)
        return fake(origin, destination, session=session)

    monkeypatch.setattr(routing, "fetch_route", slow_fetch)

    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            plan = build_plan(
                "Origin City, OK",
                "Finish City, AR",
                range_miles=500.0,
                mpg=10.0,
                corridor_miles=15.0,
                initial_fuel_miles=0.0,
            )
            results.append(plan.cache)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    threads[0].start()
    assert started.wait(2.0), "first request never reached the routing call"
    for thread in threads[1:]:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert not errors, errors
    assert len(results) == 6
    assert fake.call_count == 1
    assert results.count("miss") == 1
    assert results.count("hit") == 5
