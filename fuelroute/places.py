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
# Periods and spaces inside a state token carry no meaning, see _resolve_state_abbreviation.
_STATE_PUNCTUATION = re.compile(r"[.\s]+")
# The longest state name, DISTRICT OF COLUMBIA, is three words, so a comma free input
# needs at most its last three tokens tried as a state name.
_MAX_STATE_TOKENS = 3

# Approximate bounding boxes, min_lat, max_lat, min_lon, max_lon. City centroid
# geocoding means these only need to be roughly right, not survey accurate.
_CONUS_BOUNDS = (24.396308, 49.384358, -125.0, -66.93457)
_ALASKA_BOUNDS = (51.214183, 71.365162, -179.148909, -129.9795)
_HAWAII_BOUNDS = (18.910361, 22.235, -160.2471, -154.8066)

# Thresholds for resolving a bare city name that exists in several states, see
# _dominant_match for why both are needed and what they let through.
_DOMINANCE_RATIO = 10
_DOMINANCE_FLOOR = 50_000


class LocationError(ValueError):
    """Base class for a location string that could not be resolved."""


class UnknownLocation(LocationError):
    """Raised when a location string matches nothing in the places index."""


class AmbiguousLocation(LocationError):
    """Raised when a bare city name matches several cities and none of them dominates."""


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

    Periods and inner spaces are dropped before that two letter test, because people
    type the punctuation they were taught: "Washington, D.C." and "Denver, CO." are
    the same request as "Washington, DC" and "Denver, CO". A full state name goes
    through normalize_place_name instead, which already turns punctuation into spaces,
    so a trailing period on "Colorado." costs nothing either.
    """
    condensed = _STATE_PUNCTUATION.sub("", state_text)
    if len(condensed) == 2 and condensed.isalpha():
        candidate = condensed.upper()
        return candidate if candidate in index.valid_abbreviations else None
    return index.state_names.get(normalize_place_name(state_text))


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


def _city_and_state_without_comma(raw: str, index: _PlacesIndex) -> tuple[float, float] | None:
    """Resolve a comma free input like "Denver CO" or "Salt Lake City Utah".

    The comma is the only thing that tells the parser where the city stops, so without
    one every split has to be tried: the last token as an abbreviation or a one word
    state name, then the last two and three tokens as a longer state name. A split only
    counts when the leftover city actually exists in that state, which is what keeps
    "Charleston West Virginia" from being read as a city called "Charleston West" in
    Virginia, a state name that does match the last token on its own.

    At least one token is always left for the city, so a bare "Washington" or "New York"
    stays a city name and falls through to the population dominance path rather than
    becoming an empty city in a state. Returns None when no split resolves, leaving the
    caller to try the whole string as a bare city name.
    """
    tokens = raw.split()
    for state_token_count in range(1, _MAX_STATE_TOKENS + 1):
        if len(tokens) <= state_token_count:
            break
        abbreviation = _resolve_state_abbreviation(" ".join(tokens[-state_token_count:]), index)
        if abbreviation is None:
            continue
        hit = _lookup_city_state(" ".join(tokens[:-state_token_count]), abbreviation, index)
        if hit is not None:
            return hit[0], hit[1]
    return None


def _entry_population(entry: list) -> int:
    """Population of a by_city entry, tolerating the older three element shape.

    Entries are [latitude, longitude, state, population]. An index file built before
    the population column existed stops at the state, and a missing population reads
    as 0, which only means such an entry can never win a dominance contest.
    """
    return int(entry[3]) if len(entry) > 3 else 0


def _dominant_match(matches: list[list]) -> list | None:
    """Pick the one city a bare name obviously means, or None when the name is ambiguous.

    Every map application resolves "Denver" to Denver, Colorado rather than refusing to
    answer, and a caller typing a bare city name expects the same. The index holds four
    Denvers, and the Colorado one has 729,019 people against 3,875 in the next largest,
    in Pennsylvania, so there is nothing to be pedantic about. The largest match wins when
    it holds at least _DOMINANCE_RATIO times the population of the second largest and is
    itself at least _DOMINANCE_FLOOR people, the floor being what stops one hamlet from
    winning on a technicality against a smaller hamlet.

    The rule deliberately refuses where the name really is a coin flip, and "Portland" is
    the worked example of it refusing: Portland, Oregon has 652,503 people and Portland,
    Maine has 66,881, a ratio of 9.8 that lands just under the threshold, so both stay in
    play and the caller is asked for a state. That is the intended answer, since a Maine
    trucker typing "Portland" does not mean Oregon. "Springfield" refuses by a wider
    margin, with 170,188 in Missouri against 154,341 in Massachusetts.
    """
    ranked = sorted(matches, key=_entry_population, reverse=True)
    largest = _entry_population(ranked[0])
    runner_up = _entry_population(ranked[1])
    if largest >= _DOMINANCE_FLOOR and largest >= _DOMINANCE_RATIO * runner_up:
        return ranked[0]
    return None


def _bare_city(raw: str, text: str, index: _PlacesIndex) -> tuple[float, float]:
    """Resolve a location that names no state at all, raising if it names no one city.

    text is the caller's original string, quoted back in the error so the message shows
    what they typed rather than the stripped form the lookup ran on.
    """
    matches = _lookup_city_only(raw, index)
    if not matches:
        raise UnknownLocation(f"Unknown location '{text}'.")
    match = matches[0]
    if len(matches) > 1:
        dominant = _dominant_match(matches)
        if dominant is None:
            states = ", ".join(sorted({str(entry[2]) for entry in matches}))
            raise AmbiguousLocation(
                f"'{text}' matches more than one city ({states}), specify a state to disambiguate."
            )
        match = dominant
    return match[0], match[1]


def resolve_location(text: str) -> tuple[float, float]:
    """Resolve a user supplied location to (latitude, longitude) with no network call.

    Accepts, in this order:
      "34.05,-118.24"          bare lat,lon
      "Los Angeles, CA"        city and two letter state
      "Los Angeles, California" city and full state name
      "Los Angeles CA"         the same two, with the comma left out
      "Los Angeles"            bare city name, unique or population dominant
    A bare city name that exists in several states resolves when one of them clearly
    dominates by population, see _dominant_match, and raises AmbiguousLocation otherwise.
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
            pair = _city_and_state_without_comma(raw, index)
            latitude, longitude = pair if pair is not None else _bare_city(raw, text, index)

    if not _in_bounds(latitude, longitude):
        raise UnknownLocation(f"Location '{text}' is outside the supported US bounding boxes.")

    return latitude, longitude
