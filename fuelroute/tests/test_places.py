"""Tests for fuelroute.places. Uses a tiny fixture index in tmp_path, never the
real committed data/places.json, so these tests do not depend on the dataset
build having run.
"""

from __future__ import annotations

import json

import pytest

from fuelroute import places
from fuelroute.places import AmbiguousLocation, UnknownLocation, resolve_location

# Entries are [latitude, longitude, state, population]. SALEM deliberately keeps one
# entry in the older three element shape, so the resolver is exercised against an index
# file written before the population column existed.
FIXTURE_INDEX = {
    "by_city": {
        "SPRINGFIELD": [
            [39.78, -89.65, "IL", 114394],
            [42.10148, -72.58981, "MA", 154341],
            [37.21, -93.29, "MO", 170188],
        ],
        "DENVER": [
            [39.73915, -104.9847, "CO", 729019],
            [42.67137, -92.3374, "IA", 1835],
            [35.53125, -81.0298, "NC", 2309],
            [40.23315, -76.13717, "PA", 3875],
        ],
        "PORTLAND": [
            [43.65737, -70.2589, "ME", 66881],
            [45.52345, -122.67621, "OR", 652503],
        ],
        "AUSTIN": [[39.49296, -117.06588, "NV", 192], [30.26715, -97.74306, "TX", 964254]],
        "SALEM": [[42.51954, -70.89673, "MA"], [44.9429, -123.0351, "OR", 177723]],
        "FAIRVIEW": [[40.62285, -74.05236, "NJ"], [35.98785, -86.70858, "TN"]],
        "WASHINGTON": [[38.89511, -77.03637, "DC", 689545], [37.13081, -113.50829, "UT", 24299]],
        "CHARLESTON": [[32.77657, -79.93092, "SC", 150227], [38.34982, -81.63262, "WV", 46536]],
        "SALT LAKE CITY": [[40.76078, -111.89105, "UT", 215548]],
        "NEW YORK": [[40.71427, -74.00597, "NY", 8804190]],
        "SAINT LOUIS": [[38.62727, -90.19789, "MO", 293310]],
        "FORT WORTH": [[32.72541, -97.32085, "TX", 918915]],
        "LOS ANGELES": [[34.05223, -118.24368, "CA", 3971883]],
        "MC CALLA": [[33.34872, -87.01416, "AL", 0]],
        "HELENA": [[46.59271, -112.03611, "MT", 32315]],
        "ESPANOLA": [[35.99113, -106.08058, "NM", 10495]],
    },
    "by_city_state": {
        "SPRINGFIELD|IL": [39.78, -89.65],
        "SPRINGFIELD|MA": [42.10148, -72.58981],
        "SPRINGFIELD|MO": [37.21, -93.29],
        "DENVER|CO": [39.73915, -104.9847],
        "DENVER|IA": [42.67137, -92.3374],
        "DENVER|NC": [35.53125, -81.0298],
        "DENVER|PA": [40.23315, -76.13717],
        "PORTLAND|ME": [43.65737, -70.2589],
        "PORTLAND|OR": [45.52345, -122.67621],
        "AUSTIN|NV": [39.49296, -117.06588],
        "AUSTIN|TX": [30.26715, -97.74306],
        "SALEM|MA": [42.51954, -70.89673],
        "SALEM|OR": [44.9429, -123.0351],
        "FAIRVIEW|NJ": [40.62285, -74.05236],
        "FAIRVIEW|TN": [35.98785, -86.70858],
        "WASHINGTON|DC": [38.89511, -77.03637],
        "WASHINGTON|UT": [37.13081, -113.50829],
        "CHARLESTON|SC": [32.77657, -79.93092],
        "CHARLESTON|WV": [38.34982, -81.63262],
        "SALT LAKE CITY|UT": [40.76078, -111.89105],
        "NEW YORK|NY": [40.71427, -74.00597],
        "SAINT LOUIS|MO": [38.62727, -90.19789],
        "FORT WORTH|TX": [32.72541, -97.32085],
        "LOS ANGELES|CA": [34.05223, -118.24368],
        "MC CALLA|AL": [33.34872, -87.01416],
        "HELENA|MT": [46.59271, -112.03611],
        "ESPANOLA|NM": [35.99113, -106.08058],
    },
    "state_names": {
        "ILLINOIS": "IL",
        "MISSOURI": "MO",
        "MASSACHUSETTS": "MA",
        "COLORADO": "CO",
        "IOWA": "IA",
        "NORTH CAROLINA": "NC",
        "PENNSYLVANIA": "PA",
        "MAINE": "ME",
        "OREGON": "OR",
        "NEVADA": "NV",
        "TEXAS": "TX",
        "NEW JERSEY": "NJ",
        "TENNESSEE": "TN",
        "CALIFORNIA": "CA",
        "UTAH": "UT",
        "NEW YORK": "NY",
        "DISTRICT OF COLUMBIA": "DC",
        "VIRGINIA": "VA",
        "WEST VIRGINIA": "WV",
        "SOUTH CAROLINA": "SC",
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


def test_resolve_city_and_state_without_a_comma() -> None:
    # Nobody punctuates a search box, and "Denver CO" is not an unknown location.
    assert resolve_location("Denver CO") == (39.73915, -104.9847)
    assert resolve_location("Denver Colorado") == (39.73915, -104.9847)


def test_resolve_multi_word_city_and_state_without_a_comma() -> None:
    assert resolve_location("Salt Lake City Utah") == (40.76078, -111.89105)
    assert resolve_location("New York New York") == (40.71427, -74.00597)


def test_comma_free_split_prefers_the_reading_that_resolves() -> None:
    # "Virginia" is a state, so the first split tried reads this as a city called
    # "Charleston West" in Virginia. That city does not exist, so the two word state
    # name has to get its turn and the answer is Charleston, West Virginia.
    assert resolve_location("Charleston West Virginia") == (38.34982, -81.63262)


def test_comma_free_input_that_names_no_real_city_is_unknown() -> None:
    with pytest.raises(UnknownLocation, match="Nowhereville"):
        resolve_location("Nowhereville Texas")


def test_single_token_input_is_read_as_a_city_not_a_state() -> None:
    # "Washington" is a city name here, not an empty city in Washington state, and it
    # resolves to the District of Columbia on population, 689,545 against 24,299 in Utah.
    assert resolve_location("Washington") == (38.89511, -77.03637)
    assert resolve_location("New York") == (40.71427, -74.00597)


def test_state_punctuation_is_ignored() -> None:
    # "D.C." is how the district is written everywhere, and a trailing period after a
    # state code is a typing habit, not a different place.
    assert resolve_location("Washington, D.C.") == (38.89511, -77.03637)
    assert resolve_location("Washington, D.C") == (38.89511, -77.03637)
    assert resolve_location("Washington D.C.") == (38.89511, -77.03637)
    assert resolve_location("Denver, CO.") == (39.73915, -104.9847)
    assert resolve_location("Denver, Colorado.") == (39.73915, -104.9847)


def test_abbreviated_city_names_with_periods_still_resolve() -> None:
    # normalize_place_name expands FT and ST, and the period is punctuation it strips.
    assert resolve_location("St. Louis, MO") == (38.62727, -90.19789)
    assert resolve_location("Ft. Worth, TX") == (32.72541, -97.32085)
    assert resolve_location("Ft. Worth Texas") == (32.72541, -97.32085)


def test_resolve_unique_city_without_state() -> None:
    assert resolve_location("Los Angeles") == (34.05223, -118.24368)


def test_resolve_ambiguous_city_without_state_raises() -> None:
    with pytest.raises(AmbiguousLocation, match="Springfield"):
        resolve_location("Springfield")


def test_ambiguous_error_names_every_candidate_state() -> None:
    # The caller has to be told which states to choose between, or the 400 is a dead end.
    with pytest.raises(AmbiguousLocation, match=r"IL, MA, MO"):
        resolve_location("Springfield")


def test_bare_name_resolves_to_the_population_dominant_city() -> None:
    # Denver, Colorado is 188 times the size of the next largest Denver, so a bare
    # "Denver" resolves there rather than returning a 400 nobody wants.
    assert resolve_location("Denver") == (39.73915, -104.9847)


def test_bare_name_resolves_when_one_namesake_is_tiny() -> None:
    # Austin, Texas against Austin, Nevada: a large city and a hamlet.
    assert resolve_location("Austin") == (30.26715, -97.74306)


def test_bare_name_stays_ambiguous_when_the_ratio_is_just_under_the_threshold() -> None:
    # Portland, Oregon has 652,503 people and Portland, Maine 66,881, a ratio of 9.8.
    # That is under the 10x rule on purpose: a Maine caller typing "Portland" does not
    # mean Oregon, so the resolver asks for a state instead of guessing.
    with pytest.raises(AmbiguousLocation, match=r"ME, OR"):
        resolve_location("Portland")


def test_legacy_three_element_entry_still_loads() -> None:
    # An index file built before the population column existed must not crash the
    # resolver. A missing population reads as 0, so Salem, Oregon wins on its own count.
    assert resolve_location("Salem") == (44.9429, -123.0351)


def test_legacy_entries_alone_stay_ambiguous() -> None:
    # Two entries with no population at all give the dominance rule nothing to work
    # with, and the caller is asked for a state rather than handed an arbitrary city.
    with pytest.raises(AmbiguousLocation, match=r"NJ, TN"):
        resolve_location("Fairview")


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
