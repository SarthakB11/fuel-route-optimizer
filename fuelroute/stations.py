"""Loading the generated station table and matching stations onto a route."""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fuelroute.geo import RoutePointIndex

_REPO_STATION_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "stations.json"
# Honour the same environment variable settings.py reads, without importing Django,
# so this module works the same whether or not the Django app is running.
DEFAULT_STATION_DATA_FILE = Path(os.environ.get("STATION_DATA_FILE", str(_REPO_STATION_DATA_FILE)))


@dataclass(frozen=True, slots=True)
class Station:
    stop_id: str
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price_per_gallon: float


@functools.lru_cache(maxsize=8)
def _load_stations_cached(path_str: str) -> tuple[Station, ...]:
    """Parse the stations file once per distinct path, then reuse the result."""
    with Path(path_str).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    stations = []
    for row in payload["stations"]:
        stations.append(
            Station(
                stop_id=str(row["stop_id"]),
                name=row["name"],
                address=row["address"],
                city=row["city"],
                state=row["state"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                price_per_gallon=row["price_per_gallon"],
            )
        )
    return tuple(stations)


def load_stations(path: Path | None = None) -> tuple[Station, ...]:
    """Read the generated data/stations.json once. Cached with functools.lru_cache
    so the file is parsed on first use only, never per request.
    """
    resolved = path if path is not None else DEFAULT_STATION_DATA_FILE
    return _load_stations_cached(str(resolved))


@dataclass(frozen=True, slots=True)
class RouteStation:
    """A station matched onto a route."""

    station: Station
    offset_miles: float
    detour_miles: float


def stations_along_route(
    route_points: Sequence[tuple[float, float]],
    cumulative: Sequence[float],
    corridor_miles: float,
) -> list[RouteStation]:
    """Every station within corridor_miles of the route, sorted by offset_miles.

    Builds one RoutePointIndex and does one lookup per station, so the cost is
    O(stations + route_points), not O(stations * route_points).
    When two rows share an offset, the cheaper price sorts first.
    """
    index = RoutePointIndex(route_points, cumulative, cell_miles=corridor_miles)
    matches: list[RouteStation] = []
    for station in load_stations():
        hit = index.nearest(station.latitude, station.longitude, corridor_miles)
        if hit is None:
            continue
        offset_miles, detour_miles = hit
        matches.append(
            RouteStation(station=station, offset_miles=offset_miles, detour_miles=detour_miles)
        )
    matches.sort(key=lambda match: (match.offset_miles, match.station.price_per_gallon))
    return matches
