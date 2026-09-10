"""OSRM route fetching. No Django import, so this module stays usable outside a
running Django process (scripts, tests, a future worker).
"""

from __future__ import annotations

import functools
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

METERS_TO_MILES = 0.000621371192

OSRM_BASE_URL: str = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_TIMEOUT_SECONDS: float = float(os.environ.get("OSRM_TIMEOUT_SECONDS", "20"))


# OSRM answers with one of these when the request was well formed but the two
# points cannot be connected by road.
_NO_ROUTE_CODES = frozenset({"NoRoute", "NoSegment", "NoTrips"})


class RoutingError(RuntimeError):
    """Raised when OSRM cannot be reached or returns something unusable."""


class RouteUnavailable(RoutingError):
    """No driving route exists between the two points.

    Distinct from a provider failure: OSRM answered correctly, the answer is that
    the trip cannot be driven. Two points on separate road networks, for example a
    mainland origin and a Hawaii destination, land here. Callers map this onto a
    client error rather than a bad gateway, because retrying will not help.
    """


@dataclass(frozen=True, slots=True)
class Route:
    coordinates: tuple[tuple[float, float], ...]
    distance_miles: float
    duration_seconds: float
    provider: str
    api_calls: int
    elapsed_ms: float
    # How far each requested point had to move to reach a road. Default zero so a
    # provider response without waypoints, or a caller building a Route by hand,
    # still produces a usable object.
    origin_snap_miles: float = 0.0
    destination_snap_miles: float = 0.0


def _waypoint_snap_miles(waypoint: Any) -> float:
    """The snap distance of one waypoint, in miles, or zero if it is not readable."""
    if not isinstance(waypoint, dict):
        return 0.0
    try:
        return float(waypoint["distance"]) * METERS_TO_MILES
    except (KeyError, TypeError, ValueError):
        return 0.0


def _snap_distances(payload: dict[str, Any]) -> tuple[float, float]:
    """How far OSRM moved each requested point to put it on a road.

    OSRM reports this per waypoint and it is the only thing in the response that
    says the route does not begin where the caller asked. A pair of coordinates in
    the Pacific is answered with a perfectly good route from the nearest coast road,
    and without this the caller has no way to notice. The field is informational, so
    a missing or malformed waypoint list reports zero rather than failing a request
    that otherwise has a route in it.
    """
    waypoints = payload.get("waypoints")
    if not isinstance(waypoints, list) or len(waypoints) < 2:
        return 0.0, 0.0
    return _waypoint_snap_miles(waypoints[0]), _waypoint_snap_miles(waypoints[-1])


@functools.lru_cache(maxsize=1)
def _shared_session() -> requests.Session:
    """One connection pool for the process.

    A fresh Session per call throws away the pooled TCP and TLS connection, which is
    the most expensive part of talking to the routing service, and leaks the socket
    because nothing closes it. Tests inject their own session and never touch this.
    """
    return requests.Session()


def fetch_route(
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    session: requests.Session | None = None,
) -> Route:
    """One GET to OSRM for the full route geometry.

    Coordinates in and out are (lat, lon). OSRM speaks (lon, lat), so this
    function swaps at the boundary and nowhere else.
    """
    http = session if session is not None else _shared_session()
    lat1, lon1 = origin
    lat2, lon2 = destination
    url = f"{OSRM_BASE_URL}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "alternatives": "false",
        "steps": "false",
    }

    started = time.monotonic()
    try:
        response = http.get(url, params=params, timeout=OSRM_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RoutingError(f"OSRM request failed: {exc}") from exc
    elapsed_ms = (time.monotonic() - started) * 1000

    # OSRM signals "these two points cannot be connected by road" as HTTP 400 with a
    # NoRoute code in the body, so the body has to be inspected before the status is
    # judged. Reading the status first would report an impossible trip as a provider
    # failure, which sends the caller looking for an outage that is not there.
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        code = payload.get("code")
        if code in _NO_ROUTE_CODES:
            raise RouteUnavailable(
                "No driving route exists between these two locations. They are most "
                "likely on separate road networks, for example one of them is on an "
                "island."
            )

    if response.status_code != 200:
        raise RoutingError(f"OSRM returned HTTP {response.status_code}")

    if payload is None:
        raise RoutingError("OSRM response was not valid JSON")

    if not isinstance(payload, dict):
        raise RoutingError("OSRM response body was not a JSON object")

    code = payload.get("code")
    if code != "Ok":
        raise RoutingError(f"OSRM returned code {code!r}")

    routes = payload.get("routes") or []
    if not routes:
        raise RoutingError("OSRM returned an empty route list")

    try:
        best = routes[0]
        geometry = best.get("geometry") or {}
        raw_coordinates = geometry.get("coordinates") or []
        if not raw_coordinates:
            raise RoutingError("OSRM route geometry had no coordinates")
        coordinates = tuple((lat, lon) for lon, lat in raw_coordinates)
        distance_miles = float(best["distance"]) * METERS_TO_MILES
        duration_seconds = float(best["duration"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RoutingError(f"OSRM response body was malformed: {exc}") from exc

    origin_snap_miles, destination_snap_miles = _snap_distances(payload)

    return Route(
        coordinates=coordinates,
        distance_miles=distance_miles,
        duration_seconds=duration_seconds,
        provider="OSRM",
        api_calls=1,
        elapsed_ms=elapsed_ms,
        origin_snap_miles=origin_snap_miles,
        destination_snap_miles=destination_snap_miles,
    )
