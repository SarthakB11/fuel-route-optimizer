"""Place name normalisation shared by the offline dataset build and the runtime resolver.

Both `scripts/build_dataset.py` and `fuelroute/places.py` must key their lookups with the
exact same normalised string, or a city that geocoded cleanly during the build would fail
to resolve at request time. This module is the single source of truth for that string.
"""

import re
import unicodedata

_NON_ALNUM_SPACE = re.compile(r"[^A-Z0-9 ]")
_WHITESPACE_RUN = re.compile(r"\s+")

_TOKEN_EXPANSIONS = {
    "FT": "FORT",
    "ST": "SAINT",
    "STE": "SAINT",
    "MT": "MOUNT",
}


def normalize_place_name(text: str) -> str:
    """Uppercase text, strip punctuation to spaces, collapse whitespace, expand abbreviations.

    Accents are folded first, so an accented spelling and its plain form share a key.
    Then replaces every character that is not A-Z, 0-9 or space with a space, collapses
    runs of whitespace, and expands the whole word tokens FT, ST, STE and MT to FORT,
    SAINT, SAINT and MOUNT so that variants such as "Ft Worth" and "Fort Worth"
    normalise to the same key.
    """
    # Fold accents first. Without this, "Espanola" and "Espanola" written with a tilde
    # normalise to different keys, and the accented spelling a caller is most likely to
    # copy from a map would fail to resolve against a gazetteer that stores the plain
    # form. NFKD splits a letter from its diacritic so the diacritic can be dropped.
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    upper = folded.upper()
    cleaned = _NON_ALNUM_SPACE.sub(" ", upper)
    collapsed = _WHITESPACE_RUN.sub(" ", cleaned).strip()
    tokens = [_TOKEN_EXPANSIONS.get(token, token) for token in collapsed.split(" ")]
    return " ".join(tokens)


def collapse_spaces_key(text: str) -> str:
    """Return the normalised name with all spaces removed, for the space collapsed tier."""
    return normalize_place_name(text).replace(" ", "")
