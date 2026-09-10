"""Integrity checks on the generated data files, data/stations.json and data/places.json.

These tests load the committed JSON directly and never touch the Django database, so
they run in any environment that has the repository checked out and the build step run.
"""

import hashlib
import json
import math
from pathlib import Path

from fuelroute.normalize import collapse_spaces_key, normalize_place_name

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
STATIONS_PATH = DATA_DIR / "stations.json"
PLACES_PATH = DATA_DIR / "places.json"
CSV_PATH = DATA_DIR / "truckstop-fuel-prices.csv"

# Continental US plus Alaska and Hawaii, generous enough to avoid pinning individual states.
US_MIN_LAT, US_MAX_LAT = 17.0, 72.0
US_MIN_LON, US_MAX_LON = -180.0, -65.0

ALL_STATE_ABBREVIATIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}  # fmt: skip


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

    los_angeles = by_city_state["LOS ANGELES|CA"]
    assert 33.5 <= los_angeles[0] <= 34.5
    assert -119.0 <= los_angeles[1] <= -117.5

    chicago = by_city_state["CHICAGO|IL"]
    assert 41.5 <= chicago[0] <= 42.2
    assert -88.0 <= chicago[1] <= -87.3

    willow_beach = by_city_state["WILLOW BEACH|AZ"]
    assert 35.0 <= willow_beach[0] <= 36.5
    assert -115.5 <= willow_beach[1] <= -113.5


def test_normalize_place_name_documented_cases() -> None:
    assert normalize_place_name("Ft Worth") == "FORT WORTH"
    assert normalize_place_name("St Louis") == "SAINT LOUIS"
    assert normalize_place_name("Mt Vernon") == "MOUNT VERNON"
    assert normalize_place_name("Mc Calla") == "MC CALLA"
    assert collapse_spaces_key("Mc Calla") == "MCCALLA"
