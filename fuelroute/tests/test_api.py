"""Tests for the HTTP layer: fuelroute.views and fuelroute.urls.

Every test is offline and deterministic. fuelroute.routing.fetch_route is
monkeypatched to a counting fake, and the station and place datasets are tiny
fixtures written to tmp_path, never the real committed 6626 row dataset.
"""

from __future__ import annotations

import json
import logging
import math
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import cache

from fuelroute import geo, places, routing, stations
from fuelroute.routing import RouteUnavailable, RoutingError

pytestmark = pytest.mark.django_db

ROUTE_COORDS = [
    (36.0, -100.0),
    (36.0, -99.0),
    (36.0, -98.0),
    (36.0, -97.0),
    (36.0, -96.0),
    (36.0, -95.0),
]

# The same trip as ROUTE_COORDS, but wandering the way a road does instead of
# running dead straight, so simplification has something to remove. Kept separate
# because the station matching tests depend on ROUTE_COORDS staying a straight line.
ZIGZAG_COORDS = [
    (
        36.0 + 0.03 * math.sin(step / 399 * 40.0) + 0.005 * math.sin(step / 399 * 300.0),
        -100.0 + step / 399 * 5.0,
    )
    for step in range(400)
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

    def __init__(
        self,
        coordinates=ROUTE_COORDS,
        distance_miles=None,
        duration_seconds=3600.0,
        origin_snap_miles=0.0,
        destination_snap_miles=0.0,
    ):
        self.coordinates = tuple(coordinates)
        self.distance_miles = (
            distance_miles
            if distance_miles is not None
            else geo.cumulative_miles(list(self.coordinates))[-1]
        )
        self.duration_seconds = duration_seconds
        self.origin_snap_miles = origin_snap_miles
        self.destination_snap_miles = destination_snap_miles
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
            origin_snap_miles=self.origin_snap_miles,
            destination_snap_miles=self.destination_snap_miles,
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


def test_unroutable_pair_returns_422_not_502(client, configure_dataset, monkeypatch) -> None:
    """Two points with no road between them is the caller's problem, not an outage.

    OSRM answers correctly with a NoRoute code, so replying 502 would tell the caller
    the service is broken when in fact the trip cannot be driven. A mainland origin
    and a Hawaii destination is the realistic way to hit this.
    """

    class _NoRouteFetch:
        call_count = 0

        def __call__(self, *args, **kwargs):
            type(self).call_count += 1
            raise RouteUnavailable(
                "No driving route exists between these two locations (routing service "
                "reported NoRoute)."
            )

    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _NoRouteFetch())

    response = client.get(_plan_url(start="Origin City, OK", finish="Finish City, AR"))
    assert response.status_code == 422
    assert "No driving route exists" in response.json()["error"]


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


def test_initial_fuel_may_not_exceed_the_tank(client, configure_dataset) -> None:
    """A 700 mile head start in a 500 mile tank is not a plan, it is a bad request.

    Left unbounded the API answered 200 and reported a tank arriving somewhere with
    more fuel in it than the tank holds. It is also the one regime where the
    feasibility check measures gaps against the tank range rather than the fuel
    actually on board, so rejecting the input closes both problems at once.
    """
    configure_dataset(ROWS_WITHIN_RADIUS)
    response = client.get(
        _plan_url(
            start="Origin City, OK",
            finish="Finish City, AR",
            range_miles=500,
            initial_fuel_miles=700,
        )
    )
    assert response.status_code == 400
    assert "initial_fuel_miles" in response.json()["detail"]


def test_initial_fuel_equal_to_the_tank_is_allowed(client, configure_dataset, monkeypatch) -> None:
    """The boundary itself is a legitimate request: a full tank at the origin."""
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())
    response = client.get(
        _plan_url(
            start="Origin City, OK",
            finish="Finish City, AR",
            range_miles=500,
            initial_fuel_miles=500,
        )
    )
    assert response.status_code == 200


def test_no_stop_in_a_plan_ever_buys_zero_gallons(client, configure_dataset, monkeypatch) -> None:
    """Every stop returned is a stop the driver would actually make."""
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())
    response = client.get(_plan_url(start="Origin City, OK", finish="Finish City, AR"))

    assert response.status_code == 200
    stops = response.json()["fuel_plan"]["stops"]
    assert stops, "expected at least one stop"
    assert all(stop["gallons"] > 0 for stop in stops)


def test_departure_kind_is_keyed_on_the_offset_not_the_list_position(
    client, configure_dataset, monkeypatch
) -> None:
    """Only a stop at mile zero is the departure fill up.

    When initial fuel lets the vehicle pass the origin pump without buying, that stop
    is dropped and the first remaining entry is an ordinary one. Deriving the label
    from the list position would mislabel it.
    """
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())
    response = client.get(_plan_url(start="Origin City, OK", finish="Finish City, AR"))

    stops = response.json()["fuel_plan"]["stops"]
    for stop in stops:
        expected = "departure_fill_up" if stop["offset_miles"] == 0.0 else "en_route"
        assert stop["kind"] == expected
    assert sum(1 for stop in stops if stop["kind"] == "departure_fill_up") <= 1


def test_geometry_is_simplified_by_default_and_reports_both_counts(
    client, configure_dataset, monkeypatch
) -> None:
    """The default body carries a drawable line, not the provider's raw vertex dump."""
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute(coordinates=ZIGZAG_COORDS))

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
    )
    assert response.status_code == 200
    route = response.json()["route"]

    assert route["source_vertices"] == len(ZIGZAG_COORDS)
    assert route["geometry_vertices"] < route["source_vertices"]
    assert route["geometry_vertices"] == len(route["geometry"]["coordinates"])
    assert response.json()["request"]["geometry"] == "simplified"
    assert "0.02" in response.json()["assumptions"]["simplification"]


def test_geometry_full_returns_every_vertex_the_provider_sent(
    client, configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute(coordinates=ZIGZAG_COORDS))

    response = client.get(
        _plan_url(
            start="Origin City, OK", finish="Finish City, AR", corridor_miles=15, geometry="full"
        )
    )
    assert response.status_code == 200
    route = response.json()["route"]
    assert route["geometry_vertices"] == route["source_vertices"] == len(ZIGZAG_COORDS)


def test_geometry_does_not_move_the_stops(client, configure_dataset, monkeypatch) -> None:
    """Simplification touches the response geometry and nothing else.

    Station matching runs against the full resampled polyline, so the plan a caller
    gets must not depend on which geometry they asked for.
    """
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute(coordinates=ZIGZAG_COORDS))

    plans = []
    for geometry in ("simplified", "full"):
        response = client.get(
            _plan_url(
                start="Origin City, OK",
                finish="Finish City, AR",
                corridor_miles=15,
                geometry=geometry,
            )
        )
        assert response.status_code == 200
        plans.append(response.json()["fuel_plan"])
    assert plans[0] == plans[1]


def test_unknown_geometry_choice_returns_400(client, configure_dataset, monkeypatch) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", geometry="nonsense")
    )
    assert response.status_code == 400
    assert "geometry" in response.json()["detail"]


def test_geometry_choice_is_part_of_the_cache_key(client, configure_dataset, monkeypatch) -> None:
    """A cached simplified body must never be served to a caller asking for full.

    The cached payload carries the geometry that was returned, so the choice has to
    be in the key. Without it the second request here would be a hit and would hand
    back the thinned line under a full request.
    """
    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute(coordinates=ZIGZAG_COORDS)
    monkeypatch.setattr(routing, "fetch_route", fake)

    first = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
    )
    assert first.status_code == 200
    assert first.json()["performance"]["cache"] == "miss"
    assert fake.call_count == 1

    second = client.get(
        _plan_url(
            start="Origin City, OK", finish="Finish City, AR", corridor_miles=15, geometry="full"
        )
    )
    assert second.status_code == 200
    assert second.json()["performance"]["cache"] == "miss"
    assert fake.call_count == 2
    route = second.json()["route"]
    assert route["geometry_vertices"] == route["source_vertices"] == len(ZIGZAG_COORDS)


def test_map_url_is_absolute_and_round_trips_the_query(
    client, configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    params = {"start": "Origin City, OK", "finish": "Finish City, AR", "corridor_miles": 15}
    response = client.get(_plan_url(**params))
    assert response.status_code == 200

    parts = urlsplit(response.json()["map_url"])
    assert parts.scheme
    assert parts.netloc
    assert parts.path == "/map"
    round_tripped = parse_qs(parts.query)
    for key, value in params.items():
        assert round_tripped[key] == [str(value)]


def test_leg_miles_is_the_distance_driven_since_the_previous_stop(
    client, configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    # A tank short enough to force a second stop, so consecutive legs can be checked
    # against consecutive offsets rather than against the whole trip.
    response = client.get(
        _plan_url(
            start="Origin City, OK",
            finish="Finish City, AR",
            corridor_miles=15,
            range_miles=150,
        )
    )
    assert response.status_code == 200
    body = response.json()
    stops = body["fuel_plan"]["stops"]
    assert len(stops) >= 2

    # The first leg is measured from the origin, so it is the offset itself.
    assert stops[0]["leg_miles"] == stops[0]["offset_miles"]

    for earlier, later in zip(stops, stops[1:], strict=False):
        expected = later["offset_miles"] - earlier["offset_miles"]
        # Both fields are rounded from full precision offsets independently, so the
        # difference of two rounded offsets can sit half a display unit away.
        assert later["leg_miles"] == pytest.approx(expected, abs=0.11)

    driven = sum(stop["leg_miles"] for stop in stops)
    final_leg = body["route"]["distance_miles"] - stops[-1]["offset_miles"]
    assert driven + final_leg == pytest.approx(body["route"]["distance_miles"], abs=0.2)


def test_identical_start_and_finish_never_calls_the_routing_provider(
    client, configure_dataset, monkeypatch
) -> None:
    """A trip of no miles is answered without spending the request's one external call."""
    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute()
    monkeypatch.setattr(routing, "fetch_route", fake)

    response = client.get(_plan_url(start="Origin City, OK", finish="Origin City, OK"))
    assert response.status_code == 200
    assert fake.call_count == 0

    body = response.json()
    assert body["route"]["distance_miles"] == 0.0
    assert body["fuel_plan"]["stops"] == []
    assert body["fuel_plan"]["stop_count"] == 0
    assert body["fuel_plan"]["total_cost_usd"] == 0.0
    assert body["fuel_plan"]["total_gallons"] == 0.0
    assert body["performance"]["external_api_calls"] == 0
    assert body["performance"]["cache"] == "miss"
    assert "coincide" in body["assumptions"]["trivial_route"]


def test_a_trivial_route_is_never_served_from_the_cache(
    client, configure_dataset, monkeypatch
) -> None:
    """Repeating it stays a miss with no calls, because it is not worth storing."""
    configure_dataset(ROWS_WITHIN_RADIUS)
    fake = _FakeFetchRoute()
    monkeypatch.setattr(routing, "fetch_route", fake)

    url = _plan_url(start="Origin City, OK", finish="Origin City, OK")
    for _ in range(2):
        body = client.get(url).json()
        assert body["performance"]["cache"] == "miss"
        assert body["performance"]["external_api_calls"] == 0
    assert fake.call_count == 0


def test_an_overlong_start_is_rejected_as_a_field_error(client, configure_dataset) -> None:
    """A pasted document is a bad parameter, not something to echo back in a message."""
    configure_dataset(ROWS_WITHIN_RADIUS)

    response = client.get(_plan_url(start="A" * 300, finish="Finish City, AR"))
    assert response.status_code == 400
    assert "start" in response.json()["detail"]


def test_a_completed_plan_logs_one_structured_line(
    client, configure_dataset, monkeypatch, caplog
) -> None:
    """One line per plan, carrying coordinates rather than the caller's query text."""
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    with caplog.at_level(logging.INFO, logger="fuelroute.planner"):
        response = client.get(
            _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
        )
    assert response.status_code == 200

    records = [r for r in caplog.records if r.name == "fuelroute.planner"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert message.startswith("route-plan start=(36.0, -100.0) finish=(36.0, -95.0)")
    for field in ("distance_miles=", "stops=", "total_cost_usd=", "cache=miss"):
        assert field in message
    # The query text is user input and must stay out of the log.
    assert "Origin City" not in message


def test_a_cache_hit_logs_its_own_line_with_no_routing_time(
    client, configure_dataset, monkeypatch, caplog
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())
    url = _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)

    client.get(url)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="fuelroute.planner"):
        assert client.get(url).status_code == 200

    records = [r for r in caplog.records if r.name == "fuelroute.planner"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "cache=hit" in message
    assert "routing_ms=0" in message


# A trip of a tenth of a mile, shorter than any corridor setting is wide, with one
# station about a third of a mile off it. Every station this close projects onto the
# route's final point, which is the case that used to be pruned away entirely.
TINY_ROUTE_COORDS = [(39.7392, -104.9903), (39.7406, -104.9903)]

# Deliberately a whisker under the polyline's own great circle length, which is what
# OSRM does: it reported 0.49026 miles for a route whose resampled polyline measured
# 0.49031. Pruning candidates against the provider's smaller number is what used to
# discard every station on a trip this short.
TINY_ROUTE_MILES = geo.cumulative_miles(TINY_ROUTE_COORDS)[-1] - 5e-5

TINY_ROWS = [
    {
        "stop_id": "T1",
        "name": "Downtown Pump",
        "address": "1 MAIN ST",
        "city": "Origin City",
        "state": "OK",
        "latitude": 39.7449,
        "longitude": -104.9903,
        "price_per_gallon": 3.00,
    }
]

# Far enough from the route that no corridor the API accepts can reach it, which is
# what a region with no coverage in the price file looks like.
ROWS_NOWHERE_NEAR = [
    {
        "stop_id": "N1",
        "name": "Distant Pump",
        "address": "US-1",
        "city": "Far City",
        "state": "TX",
        "latitude": 33.0,
        "longitude": -100.0,
        "price_per_gallon": 3.00,
    }
]

# About 21 miles off the route: outside a 1 mile corridor, inside the 50 mile
# ceiling, so a wider corridor really would find it.
ROWS_JUST_OUTSIDE = [
    {
        "stop_id": "J1",
        "name": "Outside Pump",
        "address": "US-2",
        "city": "Origin City",
        "state": "OK",
        "latitude": 36.3,
        "longitude": -99.0,
        "price_per_gallon": 3.00,
    }
]


def test_a_trip_shorter_than_the_corridor_still_gets_a_plan(
    client, configure_dataset, monkeypatch
) -> None:
    """A tenth of a mile across a city is a valid trip, not a 422.

    Every station near a route this short projects onto its final point. Pruning that
    offset threw away every candidate and answered with "no stations within 12.0 miles
    of the route", which is both wrong and impossible to act on.
    """
    configure_dataset(TINY_ROWS)
    monkeypatch.setattr(
        routing,
        "fetch_route",
        _FakeFetchRoute(coordinates=TINY_ROUTE_COORDS, distance_miles=TINY_ROUTE_MILES),
    )

    response = client.get(_plan_url(start="39.7392,-104.9903", finish="39.7406,-104.9903"))
    assert response.status_code == 200

    body = response.json()
    stops = body["fuel_plan"]["stops"]
    assert len(stops) == 1
    assert stops[0]["kind"] == "departure_fill_up"
    assert stops[0]["gallons"] == pytest.approx(body["route"]["distance_miles"] / 10.0, abs=0.001)
    assert stops[0]["gallons"] > 0


def test_a_region_with_no_coverage_says_no_corridor_can_help(
    client, configure_dataset, monkeypatch
) -> None:
    """Telling a caller to widen the corridor is bad advice where there is nothing to find."""
    configure_dataset(ROWS_NOWHERE_NEAR)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(_plan_url(start="Origin City, OK", finish="Finish City, AR"))
    assert response.status_code == 422
    error = response.json()["error"]
    assert "no stations within 50 miles" in error.lower()
    assert "no corridor setting can make it feasible" in error


def test_a_narrow_corridor_says_how_many_a_wider_one_would_find(
    client, configure_dataset, monkeypatch
) -> None:
    """When widening would work, the advice comes with the number that proves it."""
    configure_dataset(ROWS_JUST_OUTSIDE)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=1)
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert "No fuel stations lie within 1.0 miles of the route." in error
    assert "1 lie within 50 miles" in error


def test_snap_distances_are_reported_and_default_to_zero(
    client, configure_dataset, monkeypatch
) -> None:
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request"]["start"]["snapped_to_road_miles"] == 0.0
    assert body["request"]["finish"]["snapped_to_road_miles"] == 0.0
    assert "snapped_endpoint" not in body["assumptions"]


def test_a_far_snapped_endpoint_is_called_out_in_the_assumptions(
    client, configure_dataset, monkeypatch
) -> None:
    """Coordinates in the ocean get a real route from the coast, and must say so.

    Without this the caller sees a perfectly plausible plan and no hint that it starts
    somewhere they did not ask for.
    """
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(
        routing,
        "fetch_route",
        _FakeFetchRoute(origin_snap_miles=42.4, destination_snap_miles=0.2),
    )

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request"]["start"]["snapped_to_road_miles"] == 42.4
    assert body["request"]["finish"]["snapped_to_road_miles"] == 0.2

    note = body["assumptions"]["snapped_endpoint"]
    assert "the start by 42.4 miles" in note
    # The finish moved a fifth of a mile, which is ordinary and not worth saying.
    assert "the finish" not in note


def test_gallons_survive_a_round_trip_at_an_extreme_mpg(
    client, configure_dataset, monkeypatch
) -> None:
    """Two decimal places on gallons is four miles of fuel at mpg=1000."""
    configure_dataset(ROWS_WITHIN_RADIUS)
    monkeypatch.setattr(routing, "fetch_route", _FakeFetchRoute())

    response = client.get(
        _plan_url(start="Origin City, OK", finish="Finish City, AR", corridor_miles=15, mpg=1000)
    )
    assert response.status_code == 200
    body = response.json()
    recovered = body["fuel_plan"]["total_gallons"] * 1000.0
    assert recovered == pytest.approx(body["route"]["distance_miles"], abs=1.0)
