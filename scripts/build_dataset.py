#!/usr/bin/env python3
"""Offline build step that turns the raw fuel price CSV into the committed JSON data files.

Reads the truck stop CSV and a GeoNames US gazetteer dump, drops non US rows, keeps the
cheapest price per truck stop, geocodes every stop to a city centroid, and writes
`data/stations.json` (the station list the route planner reads) and `data/places.json`
(the offline index the location resolver uses to turn "City, ST" into coordinates).

Run from the repository root:

    .venv/bin/python scripts/build_dataset.py --geonames path/to/US.txt

With no `--geonames`, the script downloads the GeoNames US dump into `build/` and
extracts it there. That directory is gitignored, so a fresh checkout has to run this
once (or point `--geonames` at a copy) before the API can serve requests.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fuelroute.normalize import collapse_spaces_key, normalize_place_name  # noqa: E402

logger = logging.getLogger("build_dataset")

GEONAMES_URL = "https://download.geonames.org/export/dump/US.zip"
DEFAULT_BUILD_DIR = Path("build")
DEFAULT_CSV = Path("data/truckstop-fuel-prices.csv")
DEFAULT_OVERRIDES = Path("data/geocode_overrides.json")
DEFAULT_STATIONS_OUT = Path("data/stations.json")
DEFAULT_PLACES_OUT = Path("data/places.json")
DEFAULT_MIN_PLACE_POPULATION = 1000
# Alternate GeoNames names are only indexed for places at least this populous, since
# alternate names on tiny places are mostly noise. A station city is always included
# regardless of population, see build_places_payload.
ALT_NAME_MIN_POPULATION = 50000

# US bounding box used as a sanity guard on every resolved coordinate. Generous enough to
# cover the continental states, Alaska and Hawaii without pinning down individual states.
US_BOUNDS = {"min_lat": 17.0, "max_lat": 72.0, "min_lon": -180.0, "max_lon": -65.0}

STATE_NAMES: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}  # fmt: skip
VALID_STATES: frozenset[str] = frozenset(STATE_NAMES.values())
assert len(VALID_STATES) == 51, "50 states plus DC"  # noqa: S101
# Alternate names that normalise to a bare two letter state code are junk (GeoNames lists
# postal abbreviations such as "NY" alongside real alternate names like "New York"), so
# they are skipped when building places.json. Full state names are not skipped: "New
# York" is both the state's full name and the colloquial name for New York City, and the
# latter is exactly the alternate name places.json needs to index.
STATE_ABBREVIATION_KEYS: frozenset[str] = VALID_STATES

GEONAMES_COLUMNS = [
    "geonameid", "name", "asciiname", "alternatenames", "latitude", "longitude",
    "feature_class", "feature_code", "country_code", "cc2", "admin1_code",
    "admin2_code", "admin3_code", "admin4_code", "population", "elevation", "dem",
    "timezone", "modification_date",
]  # fmt: skip


@dataclass(frozen=True, slots=True)
class GazetteerEntry:
    """A single GeoNames feature class P row, trimmed to what geocoding needs."""

    latitude: float
    longitude: float
    population: int


@dataclass(slots=True)
class Gazetteer:
    """The three lookup tiers built from GeoNames, plus every place for the places index."""

    by_name: dict[tuple[str, str], GazetteerEntry] = field(default_factory=dict)
    by_collapsed_name: dict[tuple[str, str], GazetteerEntry] = field(default_factory=dict)
    by_alternate_name: dict[tuple[str, str], GazetteerEntry] = field(default_factory=dict)
    # Every feature class P row in the 50 states plus DC, keyed the same way as by_name,
    # kept separately from the min population filtered by_name so places.json can apply
    # its own population threshold.
    all_places: dict[tuple[str, str], GazetteerEntry] = field(default_factory=dict)
    # One row per qualifying GeoNames place, kept for a second pass over alternate names
    # when building places.json. (state, name_key, entry, raw alternatenames field.)
    place_rows: list[tuple[str, str, GazetteerEntry, str]] = field(default_factory=list)

    def resolve(self, city: str, state: str) -> tuple[GazetteerEntry, str] | None:
        """Look up a city in state through the three GeoNames tiers, in order."""
        name_key = normalize_place_name(city)
        hit = self.by_name.get((state, name_key))
        if hit is not None:
            return hit, "exact"
        collapsed_key = collapse_spaces_key(city)
        hit = self.by_collapsed_name.get((state, collapsed_key))
        if hit is not None:
            return hit, "space_collapsed"
        hit = self.by_alternate_name.get((state, name_key))
        if hit is not None:
            return hit, "alternate"
        return None


def _keep_max_population(
    table: dict[tuple[str, str], GazetteerEntry], key: tuple[str, str], entry: GazetteerEntry
) -> None:
    existing = table.get(key)
    if existing is None or entry.population > existing.population:
        table[key] = entry


def download_geonames(build_dir: Path) -> Path:
    """Download and extract the GeoNames US dump into build_dir, returning US.txt's path."""
    build_dir.mkdir(parents=True, exist_ok=True)
    zip_path = build_dir / "US.zip"
    txt_path = build_dir / "US.txt"
    if txt_path.exists():
        return txt_path
    logger.info("downloading %s", GEONAMES_URL)
    request = urllib.request.Request(  # noqa: S310
        GEONAMES_URL, headers={"User-Agent": "fuel-route-optimizer-dataset-build/1.0"}
    )
    with urllib.request.urlopen(request, timeout=120) as response, zip_path.open("wb") as out:  # noqa: S310
        out.write(response.read())
    with zipfile.ZipFile(zip_path) as archive:
        archive.extract("US.txt", build_dir)
    return txt_path


def resolve_geonames_path(geonames_arg: str | None, build_dir: Path) -> Path:
    """Return a usable US.txt path, downloading it if geonames_arg is absent."""
    if geonames_arg is None:
        return download_geonames(build_dir)
    path = Path(geonames_arg)
    if path.suffix == ".zip":
        extract_dir = path.parent
        with zipfile.ZipFile(path) as archive:
            archive.extract("US.txt", extract_dir)
        return extract_dir / "US.txt"
    return path


def load_gazetteer(geonames_path: Path) -> Gazetteer:
    """Read the GeoNames US.txt dump and build the geocoding lookup tables."""
    gazetteer = Gazetteer()
    row_count = 0
    with geonames_path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for columns in reader:
            row_count += 1
            if len(columns) < len(GEONAMES_COLUMNS):
                continue
            feature_class = columns[6]
            country_code = columns[8]
            state = columns[10]
            if feature_class != "P" or country_code != "US" or state not in VALID_STATES:
                continue
            asciiname = columns[2].strip() or columns[1].strip()
            if not asciiname:
                continue
            try:
                latitude = float(columns[4])
                longitude = float(columns[5])
                population = int(columns[14]) if columns[14] else 0
            except ValueError:
                continue
            entry = GazetteerEntry(latitude=latitude, longitude=longitude, population=population)
            name_key = normalize_place_name(asciiname)
            if not name_key:
                continue
            _keep_max_population(gazetteer.by_name, (state, name_key), entry)
            _keep_max_population(gazetteer.all_places, (state, name_key), entry)
            collapsed_key = collapse_spaces_key(asciiname)
            _keep_max_population(gazetteer.by_collapsed_name, (state, collapsed_key), entry)
            alternate_names = columns[3]
            if alternate_names:
                for alt in alternate_names.split(","):
                    alt_key = normalize_place_name(alt)
                    if alt_key and alt_key != name_key:
                        _keep_max_population(gazetteer.by_alternate_name, (state, alt_key), entry)
            gazetteer.place_rows.append((state, name_key, entry, alternate_names))
    logger.info(
        "read %d GeoNames rows, %d feature class P entries in scope",
        row_count,
        len(gazetteer.all_places),
    )
    return gazetteer


def load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    """Load the hand maintained manual geocode override table."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def override_key(city: str, state: str) -> str:
    """Build the lookup key used in the geocode overrides file."""
    return f"{normalize_place_name(city)}, {state}"


@dataclass(slots=True)
class CsvStop:
    """A single deduplicated truck stop, ready for geocoding."""

    stop_id: str
    name: str
    address: str
    city: str
    state: str
    price: float


def read_csv_rows(csv_path: Path) -> tuple[list[dict[str, str]], int]:
    """Read the raw CSV, returning US rows and the count of non US rows dropped."""
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    us_rows = []
    dropped = 0
    for row in rows:
        if row["State"] not in VALID_STATES:
            dropped += 1
            continue
        us_rows.append(row)
    return us_rows, dropped


def deduplicate_stops(us_rows: list[dict[str, str]]) -> list[CsvStop]:
    """Keep the minimum retail price row for each OPIS Truckstop ID."""
    best: dict[str, CsvStop] = {}
    for row in us_rows:
        stop_id = row["OPIS Truckstop ID"].strip()
        price = float(row["Retail Price"])
        existing = best.get(stop_id)
        if existing is None or price < existing.price:
            best[stop_id] = CsvStop(
                stop_id=stop_id,
                name=row["Truckstop Name"].strip(),
                address=row["Address"].strip(),
                city=row["City"].strip(),
                state=row["State"].strip(),
                price=price,
            )
    return list(best.values())


@dataclass(slots=True)
class GeocodedStop:
    stop: CsvStop
    latitude: float
    longitude: float
    tier: str


def geocode_stops(
    stops: list[CsvStop], gazetteer: Gazetteer, overrides: dict[str, dict[str, Any]]
) -> tuple[list[GeocodedStop], list[CsvStop]]:
    """Resolve every stop's city and state to coordinates, tracking which tier matched."""
    resolved: list[GeocodedStop] = []
    unresolved: list[CsvStop] = []
    for stop in stops:
        hit = gazetteer.resolve(stop.city, stop.state)
        if hit is not None:
            entry, tier = hit
            resolved.append(GeocodedStop(stop, entry.latitude, entry.longitude, tier))
            continue
        override = overrides.get(override_key(stop.city, stop.state))
        if override is not None:
            resolved.append(
                GeocodedStop(stop, override["latitude"], override["longitude"], "override")
            )
            continue
        unresolved.append(stop)
    return resolved, unresolved


def in_us_bounds(latitude: float, longitude: float) -> bool:
    """Sanity guard: a resolved coordinate must land inside the generous US bounding box."""
    return (
        US_BOUNDS["min_lat"] <= latitude <= US_BOUNDS["max_lat"]
        and US_BOUNDS["min_lon"] <= longitude <= US_BOUNDS["max_lon"]
    )


def row_level_tier_report(
    us_rows: list[dict[str, str]], gazetteer: Gazetteer, overrides: dict[str, dict[str, Any]]
) -> dict[str, int]:
    """Tally geocode tier hits per raw CSV row, for comparison against the spec's numbers."""
    tiers = {"exact": 0, "space_collapsed": 0, "alternate": 0, "override": 0, "unresolved": 0}
    for row in us_rows:
        city, state = row["City"].strip(), row["State"].strip()
        hit = gazetteer.resolve(city, state)
        if hit is not None:
            tiers[hit[1]] += 1
            continue
        if override_key(city, state) in overrides:
            tiers["override"] += 1
        else:
            tiers["unresolved"] += 1
    return tiers


def build_stations_payload(
    resolved: list[GeocodedStop],
    csv_path: Path,
    csv_rows: int,
    non_us_dropped: int,
    deduplicated: int,
    generated_at: str,
) -> dict[str, Any]:
    """Assemble the data/stations.json document."""
    tiers = {"exact": 0, "space_collapsed": 0, "alternate": 0, "override": 0}
    for item in resolved:
        tiers[item.tier] += 1
    stations = [
        {
            "stop_id": item.stop.stop_id,
            "name": item.stop.name,
            "address": item.stop.address,
            "city": item.stop.city,
            "state": item.stop.state,
            "latitude": round(item.latitude, 7),
            "longitude": round(item.longitude, 7),
            "price_per_gallon": round(item.stop.price, 8),
        }
        for item in resolved
    ]
    stations.sort(key=lambda station: int(station["stop_id"]))
    csv_bytes = csv_path.read_bytes()
    return {
        "generated_at": generated_at,
        "source_csv": csv_path.name,
        "source_csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "row_counts": {
            "csv_rows": csv_rows,
            "non_us_dropped": non_us_dropped,
            "deduplicated": deduplicated,
            "geocoded": len(resolved),
            "unresolved": 0,
        },
        "geocode_tiers": tiers,
        "stations": stations,
    }


def build_places_payload(
    gazetteer: Gazetteer,
    resolved: list[GeocodedStop],
    min_place_population: int,
    alt_name_min_population: int = ALT_NAME_MIN_POPULATION,
) -> tuple[dict[str, Any], int]:
    """Assemble the data/places.json offline location index.

    Returns the payload and the number of alternate name keys it added, so callers can
    report on the alternate name pass.
    """
    # (name_key, state) -> (latitude, longitude, population or None for station only rows)
    merged: dict[tuple[str, str], tuple[float, float, int | None]] = {}
    for (state, name_key), entry in gazetteer.all_places.items():
        if entry.population >= min_place_population:
            merged[(state, name_key)] = (entry.latitude, entry.longitude, entry.population)
    station_keys: set[tuple[str, str]] = set()
    for item in resolved:
        name_key = normalize_place_name(item.stop.city)
        if not name_key:
            continue
        station_keys.add((item.stop.state, name_key))
        merged[(item.stop.state, name_key)] = (item.latitude, item.longitude, None)

    # Second pass: alternate GeoNames names, so a colloquial input like "New York, NY"
    # resolves even though the GeoNames primary name is "New York City". A primary or
    # ascii name already in merged always wins, so an alternate can never displace it.
    alt_candidates: dict[tuple[str, str], tuple[float, float, int]] = {}
    for state, name_key, entry, alternate_names in gazetteer.place_rows:
        if not alternate_names:
            continue
        if entry.population < alt_name_min_population and (state, name_key) not in station_keys:
            continue
        for alt in alternate_names.split(","):
            alt = alt.strip()
            if not alt or not alt.isascii():
                continue
            alt_key = normalize_place_name(alt)
            if not alt_key or alt_key == name_key or alt_key in STATE_ABBREVIATION_KEYS:
                continue
            candidate_key = (state, alt_key)
            existing = alt_candidates.get(candidate_key)
            if existing is None or entry.population > existing[2]:
                alt_candidates[candidate_key] = (entry.latitude, entry.longitude, entry.population)

    alt_keys_added = 0
    for key, (latitude, longitude, population) in alt_candidates.items():
        if key in merged:
            continue
        merged[key] = (latitude, longitude, population)
        alt_keys_added += 1

    by_city_state: dict[str, list[float]] = {}
    by_city: dict[str, list[list[float | str]]] = {}
    for (state, name_key), (latitude, longitude, _population) in merged.items():
        by_city_state[f"{name_key}|{state}"] = [latitude, longitude]
        by_city.setdefault(name_key, []).append([latitude, longitude, state])
    for entries in by_city.values():
        entries.sort(key=lambda entry: entry[2])

    payload = {
        "by_city_state": by_city_state,
        "by_city": by_city,
        "state_names": dict(sorted(STATE_NAMES.items())),
    }
    return payload, alt_keys_added


def print_summary(
    csv_rows: int,
    non_us_dropped: int,
    deduplicated: int,
    row_tiers: dict[str, int],
    stop_tiers: dict[str, int],
    unresolved: list[CsvStop],
    stations_path: Path,
    places_path: Path,
    out_of_bounds: int,
    alt_keys_added: int,
) -> None:
    """Print the human readable build report."""
    lines = [
        "Fuel route dataset build report",
        "================================",
        f"CSV rows read: {csv_rows}",
        f"Non US rows dropped: {non_us_dropped}",
        f"Rows remaining: {csv_rows - non_us_dropped}",
        f"Deduplicated stops (unique OPIS Truckstop ID): {deduplicated}",
        "",
        "Geocode tiers, measured over the raw US rows (pre dedup, for comparison to spec):",
    ]
    row_total = sum(row_tiers.values())
    for tier, count in row_tiers.items():
        share = (count / row_total * 100) if row_total else 0.0
        lines.append(f"  {tier:16s} {count:6d}  ({share:.2f}%)")
    lines.append("")
    lines.append("Geocode tiers, measured over the deduplicated stops (what stations.json ships):")
    stop_total = sum(stop_tiers.values())
    for tier, count in stop_tiers.items():
        share = (count / stop_total * 100) if stop_total else 0.0
        lines.append(f"  {tier:16s} {count:6d}  ({share:.2f}%)")
    lines.append("")
    lines.append(f"Unresolved after overrides: {len(unresolved)}")
    for stop in unresolved:
        lines.append(f"  stop_id={stop.stop_id} city={stop.city!r} state={stop.state}")
    lines.append(f"Coordinates outside the US sanity bounding box: {out_of_bounds}")
    lines.append("")
    lines.append(f"Alternate name keys added to places.json: {alt_keys_added}")
    lines.append(f"{stations_path}: {stations_path.stat().st_size:,} bytes")
    lines.append(f"{places_path}: {places_path.stat().st_size:,} bytes")
    print("\n".join(lines))  # noqa: T201


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the build script."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to the source CSV")
    parser.add_argument(
        "--geonames",
        type=str,
        default=None,
        help="Path to a GeoNames US.txt or US.zip file. Downloads into build/ if omitted.",
    )
    parser.add_argument(
        "--build-dir", type=Path, default=DEFAULT_BUILD_DIR, help="Scratch dir for downloads"
    )
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--stations-out", type=Path, default=DEFAULT_STATIONS_OUT)
    parser.add_argument("--places-out", type=Path, default=DEFAULT_PLACES_OUT)
    parser.add_argument(
        "--min-place-population",
        type=int,
        default=DEFAULT_MIN_PLACE_POPULATION,
        help="Population floor for places.json entries not tied to a station",
    )
    parser.add_argument(
        "--alt-name-min-population",
        type=int,
        default=ALT_NAME_MIN_POPULATION,
        help="Population floor for indexing a place's GeoNames alternate names",
    )
    parser.add_argument(
        "--generated-at",
        type=str,
        default=None,
        help="ISO 8601 UTC timestamp to stamp in stations.json, defaults to now",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: build data/stations.json and data/places.json from the raw inputs."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    generated_at = args.generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    geonames_path = resolve_geonames_path(args.geonames, args.build_dir)
    gazetteer = load_gazetteer(geonames_path)
    overrides = load_overrides(args.overrides)

    us_rows, non_us_dropped = read_csv_rows(args.csv)
    stops = deduplicate_stops(us_rows)
    resolved, unresolved = geocode_stops(stops, gazetteer, overrides)

    if unresolved:
        logger.error("unresolved stops after overrides:")
        for stop in unresolved:
            logger.error("  stop_id=%s city=%r state=%s", stop.stop_id, stop.city, stop.state)
        return 1

    out_of_bounds = sum(1 for item in resolved if not in_us_bounds(item.latitude, item.longitude))
    if out_of_bounds:
        logger.error("%d resolved stations fall outside the US sanity bounding box", out_of_bounds)
        return 1

    stations_payload = build_stations_payload(
        resolved, args.csv, len(us_rows) + non_us_dropped, non_us_dropped, len(stops), generated_at
    )
    places_payload, alt_keys_added = build_places_payload(
        gazetteer, resolved, args.min_place_population, args.alt_name_min_population
    )

    args.stations_out.parent.mkdir(parents=True, exist_ok=True)
    args.stations_out.write_text(
        json.dumps(stations_payload, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.places_out.write_text(
        json.dumps(places_payload, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )

    row_tiers = row_level_tier_report(us_rows, gazetteer, overrides)
    stop_tiers = {"exact": 0, "space_collapsed": 0, "alternate": 0, "override": 0}
    for item in resolved:
        stop_tiers[item.tier] += 1

    print_summary(
        len(us_rows) + non_us_dropped,
        non_us_dropped,
        len(stops),
        row_tiers,
        stop_tiers,
        unresolved,
        args.stations_out,
        args.places_out,
        out_of_bounds,
        alt_keys_added,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
