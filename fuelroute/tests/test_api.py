"""Tests for the HTTP layer: fuelroute.views and fuelroute.urls.

Every test is offline and deterministic. fuelroute.routing.fetch_route is
monkeypatched to a counting fake, and the station and place datasets are tiny
fixtures written to tmp_path, never the real committed 6626 row dataset.
"""

from __future__ import annotations

import json

import pytest
from django.core.cache import cache

from fuelroute import geo, places, routing, stations
from fuelroute.routing import RoutingError

pytestmark = pytest.mark.django_db

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
        "AMBIG CITY": [[35.0, -90.0, "MS"], [40.0, -85.0, "IN"]],
    },
    "by_city_state": {
        "ORIGIN CITY|OK": [36.0, -100.0],
        "FINISH CITY|AR": [36.0, -95.0],
        "AMBIG CITY|MS": [35.0, -90.0],
        "AMBIG CITY|IN": [40.0, -85.0],
    },
    "state_names": {"OKLAHOMA": "OK", "ARKANSAS": "AR", "MISSISSIPPI": "MS", "INDIANA": "IN"},
}

# A cheap station a few miles off the route line (so corridor width matters)
# and a positive offset inside the 25 mile origin radius, plus a pricier
# station closer to the origin, a midpoint and a near finish station.
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

# Every station sits well beyond the 25 mile origin radius, so the
# nearest-along-route fallback has to fire. B1 is not the cheapest station
# overall, which proves the fallback picks by offset, not by price.
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


class _RaisingFetchRoute:
    """A fake fetch_route that always raises, to exercise the 502 path."""

    def __call__(self, origin, destination, *, session=None):
        raise RoutingError("simulated OSRM outage")


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
        monkeypatch.setattr(
            stations, "DEFAULT_STATION_DATA_FILE", _write_stations(tmp_path, station_rows)
        )
        stations._load_stations_cached.cache_clear()
        monkeypatch.setattr(
            places, "DEFAULT_PLACE_DATA_FILE", _write_places(tmp_path, places_payload)
        )
        places._load_index.cache_clear()

    yield _configure
    stations._load_stations_cached.cache_clear()
    places._load_index.cache_clear()


@pytest.fixture(autouse=True)
def _clear_django_cache():
    cache.clear()
    yield
    cache.clear()


def _plan_url(**params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"/api/v1/route-plan?{query}"


def test_happy_path_returns_200_with_documented_shape(
    client, configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
    )
    assert response.status_code == 200
    body = response.json()

    assert isinstance(body["request"], dict)
    assert body["request"]["start"]["query"] == "Origin City, OK"
    assert isinstance(body["request"]["start"]["latitude"], float)
    assert isinstance(body["request"]["start"]["longitude"], float)

    assert body["route"]["provider"] == "OSRM"
    assert isinstance(body["route"]["distance_miles"], float)
    assert isinstance(body["route"]["duration_hours"], float)
    assert body["route"]["geometry"]["type"] == "LineString"
    assert isinstance(body["route"]["geometry"]["coordinates"], list)

    fuel_plan = body["fuel_plan"]
    assert isinstance(fuel_plan["stops"], list)
    assert isinstance(fuel_plan["stop_count"], int)
    assert isinstance(fuel_plan["total_cost_usd"], float)
    assert isinstance(fuel_plan["total_gallons"], float)
    assert isinstance(fuel_plan["average_price_per_gallon"], float)
    assert fuel_plan["stop_count"] == len(fuel_plan["stops"])

    stop = fuel_plan["stops"][0]
    for key in (
        "stop_id",
        "kind",
        "name",
        "address",
        "city",
        "state",
        "latitude",
        "longitude",
        "offset_miles",
        "detour_miles",
        "price_per_gallon",
        "gallons",
        "cost_usd",
        "cumulative_cost_usd",
        "tank_miles_on_arrival",
        "tank_miles_on_departure",
    ):
        assert key in stop

    assert isinstance(body["assumptions"], dict)
    assert "origin_price_source" in body["assumptions"]

    performance = body["performance"]
    for key in (
        "total_ms",
        "routing_ms",
        "compute_ms",
        "external_api_calls",
        "candidate_stations",
        "stations_loaded",
        "cache",
    ):
        assert key in performance
    assert performance["cache"] == "miss"
    assert performance["external_api_calls"] == 1


def test_routing_mock_called_once_cold_then_zero_on_repeat(
    client, configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute()
    monkeypatch.setattr(routing, "fetch_route", fake)

    url = _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)

    first = client.get(url)
    assert first.status_code == 200
    assert fake.call_count == 1
    assert first.json()["performance"]["cache"] == "miss"

    second = client.get(url)
    assert second.status_code == 200
    assert fake.call_count == 1
    body = second.json()
    assert body["performance"]["cache"] == "hit"
    assert body["performance"]["external_api_calls"] == 0


def test_origin_pump_appears_once_and_is_flagged_departure_fill_up(
    client, configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
    )
    assert response.status_code == 200
    stops = response.json()["fuel_plan"]["stops"]

    departure_stops = [s for s in stops if s["kind"] == "departure_fill_up"]
    assert len(departure_stops) == 1
    assert departure_stops[0]["offset_miles"] == 0.0
    # A1 is the chosen origin pump. It originally sat at a positive offset
    # inside the corridor, so without de-duplication it would appear twice.
    assert departure_stops[0]["stop_id"] == "A1"

    stop_ids = [s["stop_id"] for s in stops]
    assert len(stop_ids) == len(set(stop_ids))


def test_origin_price_source_cheapest_within_radius(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
    )
    assert response.status_code == 200
    assert (
        response.json()["assumptions"]["origin_price_source"]
        == "cheapest_within_25_miles_of_origin"
    )


def test_origin_price_source_nearest_along_route(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_BEYOND_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
    )
    assert response.status_code == 200
    assert response.json()["assumptions"]["origin_price_source"] == "nearest_station_along_route"


def test_total_gallons_times_mpg_matches_distance(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15, mpg=10)
    )
    assert response.status_code == 200
    body = response.json()
    total_gallons = body["fuel_plan"]["total_gallons"]
    distance_miles = body["route"]["distance_miles"]
    assert total_gallons * 10.0 == pytest.approx(distance_miles, abs=0.5)


@pytest.mark.parametrize(
    "params",
    [
        {"mpg": -1},
        {"range_miles": 0},
        {"corridor_miles": 51},
        {"corridor_miles": 0},
    ],
)
def test_bad_parameters_return_400(client, configure_dataset, monkeypatch, params) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    query = {"start": "Origin City, OK", "finish": "Finish City, AR"}
    query.update(params)
    response = client.get(_plan_url(**query))
    assert response.status_code == 400
    assert "error" in response.json()


def test_unknown_place_returns_400(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(_plan_url(start="Nowhereville, ZZ", finish="Finish City, AR"))
    assert response.status_code == 400


def test_ambiguous_place_returns_400(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(_plan_url(start="Ambig City", finish="Finish City, AR"))
    assert response.status_code == 400


def test_routing_error_returns_502(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _RaisingFetchRoute())

    response = client.get(_plan_url(start="Origin City, OK", finish="Finish City, AR"))
    assert response.status_code == 502
    assert "error" in response.json()


def test_route_not_feasible_returns_422(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(
            start="Origin City, OK", finish="Finish City, AR", corridor_miles=15, range_miles=50
        )
    )
    assert response.status_code == 422
    assert "error" in response.json()


def test_no_stations_in_corridor_returns_422(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=1)
    )
    assert response.status_code == 422


def test_health_returns_200(client, configure_dataset) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)

    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["station_count"] == len(ROWS_WITHIN_RADIUS)
    assert "dataset_generated_at" in body
    assert "app_version" in body


def test_map_page_returns_200_and_contains_leaflet(client) -> None:
    response = client.get("/map")
    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert "leaflet" in content.lower()


def test_root_redirects_to_map(client) -> None:
    response = client.get("/")
    assert response.status_code in (301, 302)
    assert response.url == "/map"
