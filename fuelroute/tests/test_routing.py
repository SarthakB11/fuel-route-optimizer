"""Tests for fuelroute.routing. Every test is offline: a fake session stands in
for requests.Session so no test ever reaches router.project-osrm.org.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from fuelroute import routing
from fuelroute.routing import RoutingError, fetch_route


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, bad_json: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("not valid json")
        return self._payload


class FakeSession:
    def __init__(
        self, response: FakeResponse | None = None, exception: Exception | None = None
    ) -> None:
        self.call_count = 0
        self.last_url: str | None = None
        self.last_params: dict[str, Any] | None = None
        self.last_timeout: float | None = None
        self._response = response
        self._exception = exception

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None):
        self.call_count += 1
        self.last_url = url
        self.last_params = params
        self.last_timeout = timeout
        if self._exception is not None:
            raise self._exception
        return self._response


def _ok_payload(distance_meters: float = 100000.0, duration_seconds: float = 3600.0) -> dict:
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_meters,
                "duration": duration_seconds,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-100.0, 35.0], [-99.0, 35.5], [-98.5, 36.2]],
                },
            }
        ],
    }


def test_fetch_route_parses_stubbed_payload_into_miles_and_point_order() -> None:
    session = FakeSession(response=FakeResponse(payload=_ok_payload()))
    route = fetch_route((35.0, -100.0), (36.2, -98.5), session=session)

    assert route.provider == "OSRM"
    assert route.api_calls == 1
    assert route.coordinates == ((35.0, -100.0), (35.5, -99.0), (36.2, -98.5))
    assert route.distance_miles == pytest.approx(100000.0 * 0.000621371192)
    assert route.duration_seconds == pytest.approx(3600.0)
    assert session.call_count == 1


def test_fetch_route_calls_get_exactly_once() -> None:
    session = FakeSession(response=FakeResponse(payload=_ok_payload()))
    fetch_route((35.0, -100.0), (36.2, -98.5), session=session)
    assert session.call_count == 1


def test_fetch_route_builds_lon_lat_url_from_module_base_url(monkeypatch) -> None:
    monkeypatch.setattr(routing, "OSRM_BASE_URL", "http://fake-osrm.test")
    session = FakeSession(response=FakeResponse(payload=_ok_payload()))
    fetch_route((35.0, -100.0), (36.2, -98.5), session=session)
    assert session.last_url == "http://fake-osrm.test/route/v1/driving/-100.0,35.0;-98.5,36.2"


def test_fetch_route_raises_on_non_200_status() -> None:
    session = FakeSession(response=FakeResponse(status_code=500, payload={}))
    with pytest.raises(RoutingError):
        fetch_route((35.0, -100.0), (36.2, -98.5), session=session)


def test_fetch_route_raises_on_malformed_body() -> None:
    session = FakeSession(response=FakeResponse(payload={"code": "Ok", "routes": [{}]}))
    with pytest.raises(RoutingError):
        fetch_route((35.0, -100.0), (36.2, -98.5), session=session)


def test_fetch_route_raises_on_bad_json() -> None:
    session = FakeSession(response=FakeResponse(bad_json=True))
    with pytest.raises(RoutingError):
        fetch_route((35.0, -100.0), (36.2, -98.5), session=session)


def test_fetch_route_raises_on_no_route_code() -> None:
    session = FakeSession(response=FakeResponse(payload={"code": "NoRoute", "routes": []}))
    with pytest.raises(RoutingError):
        fetch_route((35.0, -100.0), (36.2, -98.5), session=session)


def test_fetch_route_raises_on_empty_route_list() -> None:
    session = FakeSession(response=FakeResponse(payload={"code": "Ok", "routes": []}))
    with pytest.raises(RoutingError):
        fetch_route((35.0, -100.0), (36.2, -98.5), session=session)


def test_fetch_route_raises_on_timeout() -> None:
    session = FakeSession(exception=requests.exceptions.Timeout("timed out"))
    with pytest.raises(RoutingError):
        fetch_route((35.0, -100.0), (36.2, -98.5), session=session)


def test_fetch_route_raises_on_connection_error() -> None:
    session = FakeSession(exception=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(RoutingError):
        fetch_route((35.0, -100.0), (36.2, -98.5), session=session)
