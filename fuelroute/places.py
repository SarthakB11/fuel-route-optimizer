"""Resolve a user supplied place name or coordinate string with no network call.

Reads the committed data/places.json index, built offline by scripts/build_dataset.py
from the GeoNames US populated places dump. City centroids are the resolution
granularity: a city can span many square miles, so a resolved point may sit a few
miles from wherever the user actually meant within that city.
"""

from __future__ import annotations

import functools
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from fuelroute.normalize import collapse_spaces_key, normalize_place_name

_REPO_PLACE_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "places.json"
# Honour the same environment variable settings.py reads, without importing Django.
DEFAULT_PLACE_DATA_FILE = Path(os.environ.get("PLACE_DATA_FILE", str(_REPO_PLACE_DATA_FILE)))

_COORDINATE_PATTERN = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$")

# Approximate bounding boxes, min_lat, max_lat, min_lon, max_lon. City centroid
# geocoding means these only need to be roughly right, not survey accurate.
_CONUS_BOUNDS = (24.396308, 49.384358, -125.0, -66.93457)
_ALASKA_BOUNDS = (51.214183, 71.365162, -179.148909, -129.9795)
_HAWAII_BOUNDS = (18.910361, 22.235, -160.2471, -154.8066)


class LocationError(ValueError):
    """Base class for a location string that could not be resolved."""


class UnknownLocation(LocationError):
    """Raised when a location string matches nothing in the places index."""


class AmbiguousLocation(LocationError):
    """Raised when a bare city name matches more than one city in the index."""


@dataclass(frozen=True)
class _PlacesIndex:
    by_city: dict[str, list[list]]
    by_city_state: dict[str, list[float]]
    state_names: dict[str, str]
    valid_abbreviations: frozenset[str]
    collapsed_to_normalized: dict[str, tuple[str, ...]]


@functools.lru_cache(maxsize=8)
def _load_index(path_str: str) -> _PlacesIndex:
    """Parse data/places.json once per distinct path, then reuse the result."""
    with Path(path_str).open(encoding="utf-8") as handle:
        payload = json.load(handle)

    by_city: dict[str, list[list]] = payload["by_city"]
    by_city_state: dict[str, list[float]] = payload["by_city_state"]
    state_names: dict[str, str] = payload["state_names"]

    collapsed: dict[str, list[str]] = {}
    for normalized_city in by_city:
        collapsed.setdefault(collapse_spaces_key(normalized_city), []).append(normalized_city)

    return _PlacesIndex(
        by_city=by_city,
        by_city_state=by_city_state,
        state_names=state_names,
        valid_abbreviations=frozenset(state_names.values()),
        collapsed_to_normalized={key: tuple(value) for key, value in collapsed.items()},
    )


def _in_bounds(lat: float, lon: float) -> bool:
    for min_lat, max_lat, min_lon, max_lon in (_CONUS_BOUNDS, _ALASKA_BOUNDS, _HAWAII_BOUNDS):
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return True
    return False


def _resolve_state_abbreviation(state_text: str, index: _PlacesIndex) -> str | None:
    """Resolve a state token to its two letter abbreviation.

    A bare two letter token is treated as an abbreviation directly rather than
    run through normalize_place_name, because that function expands whole word
    tokens such as MT to MOUNT, which would corrupt the abbreviation for Montana.
    """
    stripped = state_text.strip()
    if len(stripped) == 2 and stripped.isalpha():
        candidate = stripped.upper()
        return candidate if candidate in index.valid_abbreviations else None
    return index.state_names.get(normalize_place_name(stripped))


def _city_state_candidates(city_text: str, index: _PlacesIndex) -> list[str]:
    """Every normalised city key that could plausibly match city_text."""
    normalized = normalize_place_name(city_text)
    candidates = [normalized]
    for alternate in index.collapsed_to_normalized.get(collapse_spaces_key(city_text), ()):
        if alternate not in candidates:
            candidates.append(alternate)
    return candidates


def _lookup_city_state(
    city_text: str, abbreviation: str, index: _PlacesIndex
) -> list[float] | None:
    for normalized_city in _city_state_candidates(city_text, index):
        hit = index.by_city_state.get(f"{normalized_city}|{abbreviation}")
        if hit is not None:
            return hit
    return None


def _lookup_city_only(city_text: str, index: _PlacesIndex) -> list[list]:
    matches: list[list] = []
    for normalized_city in _city_state_candidates(city_text, index):
        matches.extend(index.by_city.get(normalized_city, []))
    return matches


def resolve_location(text: str) -> tuple[float, float]:
    """Resolve a user supplied location to (latitude, longitude) with no network call.

    Accepts, in this order:
      "34.05,-118.24"          bare lat,lon
      "Los Angeles, CA"        city and two letter state
      "Los Angeles, California" city and full state name
      "Los Angeles"            unique city name, else AmbiguousLocation
    Matching reuses the normalisation in the build script and reads the committed
    data/places.json index. Raises UnknownLocation or AmbiguousLocation, both
    subclasses of LocationError, with a message naming the input.
    """
    raw = text.strip()
    if not raw:
        raise UnknownLocation("Location is empty.")

    coordinate_match = _COORDINATE_PATTERN.match(raw)
    if coordinate_match is not None:
        latitude = float(coordinate_match.group(1))
        longitude = float(coordinate_match.group(2))
    else:
        index = _load_index(str(DEFAULT_PLACE_DATA_FILE))
        city_text, separator, state_text = raw.rpartition(",")
        if separator:
            abbreviation = _resolve_state_abbreviation(state_text, index)
            if abbreviation is None:
                raise UnknownLocation(f"Unknown state in location '{text}'.")
            hit = _lookup_city_state(city_text, abbreviation, index)
            if hit is None:
                raise UnknownLocation(f"Unknown location '{text}'.")
            latitude, longitude = hit[0], hit[1]
        else:
            matches = _lookup_city_only(raw, index)
            if not matches:
                raise UnknownLocation(f"Unknown location '{text}'.")
            if len(matches) > 1:
                raise AmbiguousLocation(
                    f"'{text}' matches more than one city, specify a state to disambiguate."
                )
            latitude, longitude = matches[0][0], matches[0][1]

    if not _in_bounds(latitude, longitude):
        raise UnknownLocation(f"Location '{text}' is outside the supported US bounding boxes.")

    return latitude, longitude
