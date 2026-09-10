"""Tests for fuelroute.places. Uses a tiny fixture index in tmp_path, never the
real committed data/places.json, so these tests do not depend on the dataset
build having run.
"""

from __future__ import annotations

import json

import pytest

from fuelroute import places
from fuelroute.places import AmbiguousLocation, UnknownLocation, resolve_location

FIXTURE_INDEX = {
    "by_city": {
        "SPRINGFIELD": [[39.78, -89.65, "IL"], [37.21, -93.29, "MO"]],
        "LOS ANGELES": [[34.05223, -118.24368, "CA"]],
        "MC CALLA": [[33.34872, -87.01416, "AL"]],
        "HELENA": [[46.59271, -112.03611, "MT"]],
        "ESPANOLA": [[35.99113, -106.08058, "NM"]],
    },
    "by_city_state": {
        "SPRINGFIELD|IL": [39.78, -89.65],
        "SPRINGFIELD|MO": [37.21, -93.29],
        "LOS ANGELES|CA": [34.05223, -118.24368],
        "MC CALLA|AL": [33.34872, -87.01416],
        "HELENA|MT": [46.59271, -112.03611],
        "ESPANOLA|NM": [35.99113, -106.08058],
    },
    "state_names": {
        "ILLINOIS": "IL",
        "MISSOURI": "MO",
        "CALIFORNIA": "CA",
        "ALABAMA": "AL",
        "MONTANA": "MT",
        "NEW MEXICO": "NM",
    },
}


@pytest.fixture(autouse=True)
def _fixture_index(tmp_path, monkeypatch):
    """Point places.py at a small, controlled index instead of the real dataset."""
    index_path = tmp_path / "places.json"
    index_path.write_text(json.dumps(FIXTURE_INDEX), encoding="utf-8")
    monkeypatch.setattr(places, "DEFAULT_PLACE_DATA_FILE", index_path)
    places._load_index.cache_clear()
    yield
    places._load_index.cache_clear()


def test_resolve_bare_lat_lon() -> None:
    assert resolve_location("34.05,-118.24") == (34.05, -118.24)


def test_resolve_bare_lat_lon_with_spaces() -> None:
    assert resolve_location("34.05, -118.24") == (34.05, -118.24)


def test_resolve_city_and_two_letter_state() -> None:
    assert resolve_location("Los Angeles, CA") == (34.05223, -118.24368)


def test_resolve_city_and_full_state_name() -> None:
    assert resolve_location("Los Angeles, California") == (34.05223, -118.24368)


def test_resolve_montana_abbreviation_is_not_expanded_to_mount() -> None:
    # MT is a token normalize_place_name expands to MOUNT for city names, the
    # state abbreviation path must not run that expansion.
    assert resolve_location("Helena, MT") == (46.59271, -112.03611)


def test_resolve_unique_city_without_state() -> None:
    assert resolve_location("Los Angeles") == (34.05223, -118.24368)


def test_resolve_ambiguous_city_without_state_raises() -> None:
    with pytest.raises(AmbiguousLocation, match="Springfield"):
        resolve_location("Springfield")


def test_resolve_ambiguous_city_disambiguated_by_state() -> None:
    assert resolve_location("Springfield, IL") == (39.78, -89.65)
    assert resolve_location("Springfield, MO") == (37.21, -93.29)


def test_resolve_space_collapsed_fallback() -> None:
    # The index stores "MC CALLA" with a space, a user typing it solid should
    # still resolve via the space collapsed fallback tier.
    assert resolve_location("McCalla, AL") == (33.34872, -87.01416)


def test_resolve_unknown_city_raises_with_input_named() -> None:
    with pytest.raises(UnknownLocation, match="Nowhereville"):
        resolve_location("Nowhereville, CA")


def test_resolve_unknown_state_raises() -> None:
    with pytest.raises(UnknownLocation):
        resolve_location("Los Angeles, ZZ")


def test_resolve_out_of_bounds_coordinates_raises() -> None:
    with pytest.raises(UnknownLocation):
        resolve_location("48.8566,2.3522")


def test_resolve_empty_string_raises() -> None:
    with pytest.raises(UnknownLocation):
        resolve_location("")


def test_accented_place_names_resolve_to_the_plain_form() -> None:
    """A caller copying an accented spelling off a map should not get a 400.

    The gazetteer stores the plain ascii spelling, so the resolver folds diacritics
    before looking a name up. Before it did, the accented characters were replaced by
    spaces, the key became "ESPA OLA", and the lookup missed.
    """
    assert resolve_location("Espa\u00f1ola, NM") == resolve_location("Espanola, NM")
    assert resolve_location("Espa\u00f1ola, New Mexico") == (35.99113, -106.08058)
