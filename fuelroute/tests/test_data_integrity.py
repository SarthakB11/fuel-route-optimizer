"""Integrity checks on the generated data files, data/stations.json and data/places.json.

These tests load the committed JSON directly and never touch the Django database, so
they run in any environment that has the repository checked out and the build step run.
"""

import hashlib
import json
import math
from pathlib import Path

import pytest

from fuelroute import places
from fuelroute.normalize import collapse_spaces_key, normalize_place_name
from fuelroute.places import AmbiguousLocation, resolve_location

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
STATIONS_PATH = DATA_DIR / "stations.json"
PLACES_PATH = DATA_DIR / "places.json"
CSV_PATH = DATA_DIR / "truckstop-fuel-prices.csv"

# Continental US plus Alaska and Hawaii, generous enough to avoid pinning individual states.
US_MIN_LAT, US_MAX_LAT = 17.0, 72.0
US_MIN_LON, US_MAX_LON = -180.0, -65.0

# A self contained haversine, deliberately not imported from fuelroute.geo: that module
# belongs to another part of the build and this test must stand on its own.
EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


ALL_STATE_ABBREVIATIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}  # fmt: skip

# Reference coordinates for city center sanity checks, quoted from public knowledge.
# (city, state) -> (latitude, longitude)
KNOWN_CITY_COORDINATES = {
    ("NEW YORK", "NY"): (40.7128, -74.0060),
    ("NEW YORK CITY", "NY"): (40.7128, -74.0060),
    ("LOS ANGELES", "CA"): (34.0522, -118.2437),
    ("CHICAGO", "IL"): (41.8781, -87.6298),
    ("HOUSTON", "TX"): (29.7604, -95.3698),
    ("PHOENIX", "AZ"): (33.4484, -112.0740),
    ("PHILADELPHIA", "PA"): (39.9526, -75.1652),
    ("SAN ANTONIO", "TX"): (29.4241, -98.4936),
    ("SAN DIEGO", "CA"): (32.7157, -117.1611),
    ("DALLAS", "TX"): (32.7767, -96.7970),
    ("SEATTLE", "WA"): (47.6062, -122.3321),
    ("MIAMI", "FL"): (25.7617, -80.1918),
    ("DENVER", "CO"): (39.7392, -104.9903),
    ("BOSTON", "MA"): (42.3601, -71.0589),
    ("ATLANTA", "GA"): (33.7490, -84.3880),
}

# The twenty largest US cities by population, continuing past the fifteen above.
TOP_TWENTY_EXTRA_CITY_COORDINATES = {
    ("AUSTIN", "TX"): (30.2672, -97.7431),
    ("JACKSONVILLE", "FL"): (30.3322, -81.6557),
    ("FORT WORTH", "TX"): (32.7555, -97.3308),
    ("SAN JOSE", "CA"): (37.3382, -121.8863),
    ("COLUMBUS", "OH"): (39.9612, -82.9988),
    ("CHARLOTTE", "NC"): (35.2271, -80.8431),
    ("INDIANAPOLIS", "IN"): (39.7684, -86.1581),
    ("SAN FRANCISCO", "CA"): (37.7749, -122.4194),
    ("OKLAHOMA CITY", "OK"): (35.4676, -97.5164),
}

MAX_CITY_CENTER_DRIFT_MILES = 30.0

# Bare city names, with no state, that a caller has every right to expect back without
# an argument, and the state each one has to land in. Some are nationally unique in the
# index and some win on population dominance, see fuelroute.places._dominant_match.
DOMINANT_BARE_CITY_NAMES = {
    "Denver": "CO",
    "Chicago": "IL",
    "Seattle": "WA",
    "Miami": "FL",
    "Boston": "MA",
    "Houston": "TX",
    "Atlanta": "GA",
    "Phoenix": "AZ",
    "Dallas": "TX",
}

# Names where no single city dominates, so the resolver has to keep asking for a state.
# Springfield is a three way tie in the six figures and Portland, Oregon is only 9.8
# times Portland, Maine, just under the dominance ratio.
AMBIGUOUS_BARE_CITY_NAMES = ("Springfield", "Portland")

# Forms people type that carry a state without a tidy "City, ST" shape, checked against
# the real index because the punctuation and the token splitting both depend on what the
# gazetteer actually holds. Each value is the city and state key the input has to land on.
UNTIDY_LOCATION_INPUTS = {
    "Denver Colorado": "DENVER|CO",
    "Denver CO": "DENVER|CO",
    "Denver, CO.": "DENVER|CO",
    "Salt Lake City Utah": "SALT LAKE CITY|UT",
    "New York New York": "NEW YORK|NY",
    "Charleston West Virginia": "CHARLESTON|WV",
    "Washington, D.C.": "WASHINGTON|DC",
    "Washington D.C.": "WASHINGTON|DC",
    "St. Louis, MO": "SAINT LOUIS|MO",
    "Ft. Worth, TX": "FORT WORTH|TX",
}


def load_stations() -> dict:
    return json.loads(STATIONS_PATH.read_text(encoding="utf-8"))


def load_places() -> dict:
    return json.loads(PLACES_PATH.read_text(encoding="utf-8"))


def test_stations_json_parses() -> None:
    payload = load_stations()
    assert isinstance(payload["stations"], list)
    assert len(payload["stations"]) > 0


def test_places_json_parses() -> None:
    payload = load_places()
    assert isinstance(payload["by_city_state"], dict)
    assert isinstance(payload["by_city"], dict)
    assert isinstance(payload["state_names"], dict)


def test_station_coordinates_are_finite_and_in_us_bounds() -> None:
    payload = load_stations()
    for station in payload["stations"]:
        lat, lon = station["latitude"], station["longitude"]
        assert math.isfinite(lat)
        assert math.isfinite(lon)
        assert US_MIN_LAT <= lat <= US_MAX_LAT, station
        assert US_MIN_LON <= lon <= US_MAX_LON, station


def test_station_prices_are_positive_and_reasonable() -> None:
    payload = load_stations()
    for station in payload["stations"]:
        price = station["price_per_gallon"]
        assert math.isfinite(price)
        assert price > 0
        assert price < 10.0


def test_station_stop_ids_are_unique() -> None:
    payload = load_stations()
    stop_ids = [station["stop_id"] for station in payload["stations"]]
    assert len(stop_ids) == len(set(stop_ids))


def test_station_count_matches_row_counts_geocoded() -> None:
    payload = load_stations()
    assert len(payload["stations"]) == payload["row_counts"]["geocoded"]


def test_geocode_tiers_sum_to_station_count() -> None:
    payload = load_stations()
    total = sum(payload["geocode_tiers"].values())
    assert total == len(payload["stations"])


def test_source_csv_sha256_matches_csv_on_disk() -> None:
    payload = load_stations()
    actual = hashlib.sha256(CSV_PATH.read_bytes()).hexdigest()
    assert payload["source_csv_sha256"] == actual


def test_places_json_has_all_51_state_names() -> None:
    payload = load_places()
    state_names = payload["state_names"]
    assert len(state_names) == 51
    assert set(state_names.values()) == ALL_STATE_ABBREVIATIONS
    assert state_names["DISTRICT OF COLUMBIA"] == "DC"
    assert state_names["CALIFORNIA"] == "CA"


def test_places_json_resolves_known_cities() -> None:
    payload = load_places()
    by_city_state = payload["by_city_state"]

    willow_beach = by_city_state["WILLOW BEACH|AZ"]
    assert 35.0 <= willow_beach[0] <= 36.5
    assert -115.5 <= willow_beach[1] <= -113.5

    for (city, state), (ref_lat, ref_lon) in KNOWN_CITY_COORDINATES.items():
        key = f"{city}|{state}"
        assert key in by_city_state, f"{key} missing from places.json"
        lat, lon = by_city_state[key]
        drift = haversine_miles(lat, lon, ref_lat, ref_lon)
        assert drift <= MAX_CITY_CENTER_DRIFT_MILES, f"{key} drifted {drift:.1f} miles"


def test_places_json_resolves_twenty_largest_cities() -> None:
    payload = load_places()
    by_city_state = payload["by_city_state"]
    reference = {**KNOWN_CITY_COORDINATES, **TOP_TWENTY_EXTRA_CITY_COORDINATES}
    # KNOWN_CITY_COORDINATES double books New York under both its colloquial and its
    # GeoNames primary name, so this is nineteen distinct cities plus the primary name
    # alias, covering the twenty largest US cities by population.
    for (city, state), (ref_lat, ref_lon) in reference.items():
        if city == "NEW YORK CITY":
            continue
        key = f"{city}|{state}"
        assert key in by_city_state, f"{key} missing from places.json"
        lat, lon = by_city_state[key]
        drift = haversine_miles(lat, lon, ref_lat, ref_lon)
        assert drift <= MAX_CITY_CENTER_DRIFT_MILES, f"{key} drifted {drift:.1f} miles"


def test_places_json_size_under_four_megabytes() -> None:
    size = PLACES_PATH.stat().st_size
    assert size < 4 * 1024 * 1024, f"places.json is {size:,} bytes, over the 4 MB budget"


def test_normalize_place_name_documented_cases() -> None:
    assert normalize_place_name("Ft Worth") == "FORT WORTH"
    assert normalize_place_name("St Louis") == "SAINT LOUIS"
    assert normalize_place_name("Mt Vernon") == "MOUNT VERNON"
    assert normalize_place_name("Mc Calla") == "MC CALLA"
    assert collapse_spaces_key("Mc Calla") == "MCCALLA"


@pytest.fixture
def real_places_index(monkeypatch):
    """Resolve against the committed index rather than whatever PLACE_DATA_FILE points at."""
    monkeypatch.setattr(places, "DEFAULT_PLACE_DATA_FILE", PLACES_PATH)
    places._load_index.cache_clear()
    yield
    places._load_index.cache_clear()


def test_dominant_bare_city_names_resolve_against_the_real_index(real_places_index) -> None:
    by_city_state = load_places()["by_city_state"]
    for city, state in DOMINANT_BARE_CITY_NAMES.items():
        expected = by_city_state[f"{normalize_place_name(city)}|{state}"]
        assert resolve_location(city) == tuple(expected), f"{city} did not resolve to {state}"


def test_genuinely_ambiguous_bare_city_names_still_raise(real_places_index) -> None:
    for city in AMBIGUOUS_BARE_CITY_NAMES:
        with pytest.raises(AmbiguousLocation):
            resolve_location(city)


def test_every_by_city_entry_carries_a_population() -> None:
    """The fourth element is what the dominance rule reads, so no entry may be missing it."""
    for name, entries in load_places()["by_city"].items():
        for entry in entries:
            assert len(entry) == 4, f"{name} entry {entry} is missing its population"
            assert isinstance(entry[3], int)
            assert entry[3] >= 0


def test_untidy_location_inputs_resolve_against_the_real_index(real_places_index) -> None:
    by_city_state = load_places()["by_city_state"]
    for text, key in UNTIDY_LOCATION_INPUTS.items():
        assert resolve_location(text) == tuple(by_city_state[key]), f"{text} did not reach {key}"
