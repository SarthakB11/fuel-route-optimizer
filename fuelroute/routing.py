"""OSRM route fetching. No Django import, so this module stays usable outside a
running Django process (scripts, tests, a future worker).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

METERS_TO_MILES = 0.000621371192

OSRM_BASE_URL: str = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_TIMEOUT_SECONDS: float = float(os.environ.get("OSRM_TIMEOUT_SECONDS", "20"))


class RoutingError(RuntimeError):
    """Raised when OSRM cannot be reached or returns something unusable."""


@dataclass(frozen=True, slots=True)
class Route:
    coordinates: tuple[tuple[float, float], ...]
    distance_miles: float
    duration_seconds: float
    provider: str
    api_calls: int
    elapsed_ms: float


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
    http = session if session is not None else requests.Session()
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

    if response.status_code != 200:
        raise RoutingError(f"OSRM returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RoutingError("OSRM response was not valid JSON") from exc

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

    return Route(
        coordinates=coordinates,
        distance_miles=distance_miles,
        duration_seconds=duration_seconds,
        provider="OSRM",
        api_calls=1,
        elapsed_ms=elapsed_ms,
    )
