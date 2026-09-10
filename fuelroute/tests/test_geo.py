"""Tests for fuelroute.geo. Pure geometry, no Django fixtures needed."""

from __future__ import annotations

import math
import random

import pytest

from fuelroute.geo import (
    RoutePointIndex,
    cumulative_miles,
    haversine_miles,
    perpendicular_offset_miles,
    resample_polyline,
    simplify_polyline,
)

# Expected values are the standard great circle distance between city centres,
# checked against haversine_miles itself and cross referenced against commonly
# published straight line distances for the same pairs (New York City to Los
# Angeles around 2451 miles, London to Paris around 213 miles, Chicago to
# Houston around 940 miles). A 1% band absorbs the small difference between a
# spherical model and the slightly different coordinates a published source
# might use for "the city".
KNOWN_PAIRS = [
    ("NYC-LA", 40.7128, -74.0060, 34.0522, -118.2437, 2445.6),
    ("London-Paris", 51.5074, -0.1278, 48.8566, 2.3522, 213.5),
    ("Chicago-Houston", 41.8781, -87.6298, 29.7604, -95.3698, 941.9),
]


@pytest.mark.parametrize("name,lat1,lon1,lat2,lon2,expected_miles", KNOWN_PAIRS)
def test_haversine_known_city_pairs(name, lat1, lon1, lat2, lon2, expected_miles) -> None:
    distance = haversine_miles(lat1, lon1, lat2, lon2)
    assert distance == pytest.approx(expected_miles, rel=0.01), name


def test_haversine_same_point_is_zero() -> None:
    assert haversine_miles(35.0, -100.0, 35.0, -100.0) == pytest.approx(0.0, abs=1e-9)


def test_haversine_symmetric() -> None:
    a = haversine_miles(40.0, -90.0, 41.5, -88.0)
    b = haversine_miles(41.5, -88.0, 40.0, -90.0)
    assert a == pytest.approx(b, rel=1e-9)


def test_cumulative_miles_monotonic_and_starts_at_zero() -> None:
    rng = random.Random(1234)
    coords = [(35.0 + rng.uniform(-5, 5), -100.0 + rng.uniform(-5, 5)) for _ in range(30)]
    totals = cumulative_miles(coords)
    assert totals[0] == 0.0
    for earlier, later in zip(totals, totals[1:], strict=False):
        assert later >= earlier


def test_cumulative_miles_empty_and_single_point() -> None:
    assert cumulative_miles([]) == []
    assert cumulative_miles([(1.0, 2.0)]) == [0.0]


def test_resample_polyline_keeps_endpoints() -> None:
    coords = [(35.0, -100.0), (35.5, -99.0), (36.2, -98.5), (37.0, -97.0)]
    points, cumulative = resample_polyline(coords, spacing_miles=10.0)
    assert points[0] == coords[0]
    assert points[-1] == coords[-1]
    assert cumulative[0] == pytest.approx(0.0)
    assert cumulative[-1] == pytest.approx(cumulative_miles(coords)[-1])


def test_resample_polyline_hits_spacing_within_tolerance() -> None:
    coords = [(35.0, -100.0), (36.0, -100.0), (37.0, -100.0)]
    spacing = 8.0
    points, cumulative = resample_polyline(coords, spacing_miles=spacing)
    for earlier, later in zip(cumulative, cumulative[1:], strict=False):
        gap = later - earlier
        # Segments should sit close to the requested spacing, and never blow far
        # past it: the resampler must not silently thin below the target rate.
        assert gap <= spacing * 1.2 + 1e-6


def test_resample_polyline_rejects_non_positive_spacing() -> None:
    with pytest.raises(ValueError):
        resample_polyline([(0.0, 0.0), (1.0, 1.0)], spacing_miles=0.0)


def _brute_force_nearest(
    points: list[tuple[float, float]],
    cumulative: list[float],
    lat: float,
    lon: float,
    max_miles: float,
) -> tuple[float, float] | None:
    best_index = None
    best_distance = math.inf
    for idx, (point_lat, point_lon) in enumerate(points):
        distance = haversine_miles(lat, lon, point_lat, point_lon)
        if distance < best_distance:
            best_distance = distance
            best_index = idx
    if best_index is None or best_distance > max_miles:
        return None
    return cumulative[best_index], best_distance


def test_route_point_index_matches_brute_force_across_latitudes() -> None:
    # Span several degrees of latitude, from south Texas up to Minnesota, so a
    # reference latitude chosen carelessly (for example the mean) would make
    # the longitude cells too narrow at one end and miss real matches.
    rng = random.Random(42)
    points = [(rng.uniform(27.0, 47.0), rng.uniform(-104.0, -96.0)) for _ in range(400)]
    cumulative = [float(i) for i in range(len(points))]
    cell_miles = 15.0
    index = RoutePointIndex(points, cumulative, cell_miles=cell_miles)

    for _ in range(60):
        query_lat = rng.uniform(27.0, 47.0)
        query_lon = rng.uniform(-104.0, -96.0)
        expected = _brute_force_nearest(points, cumulative, query_lat, query_lon, cell_miles)
        actual = index.nearest(query_lat, query_lon, cell_miles)
        if expected is None:
            assert actual is None
        else:
            assert actual is not None
            assert actual[0] == pytest.approx(expected[0])
            assert actual[1] == pytest.approx(expected[1], rel=1e-6, abs=1e-6)


def test_route_point_index_returns_none_beyond_radius() -> None:
    points = [(35.0, -100.0), (35.0, -99.0)]
    cumulative = [0.0, 69.0]
    index = RoutePointIndex(points, cumulative, cell_miles=5.0)
    assert index.nearest(50.0, -70.0, max_miles=5.0) is None


def test_route_point_index_handles_route_that_doubles_back() -> None:
    # An out and back route: the polyline heads east then returns west over the
    # same latitude band. The nearest point to a query near the turnaround
    # should win regardless of which leg it came from.
    outbound = [(35.0, -100.0 + step * 0.1) for step in range(20)]
    inbound = [(35.0, -98.0 - step * 0.1) for step in range(20)]
    points = outbound + inbound
    cumulative = cumulative_miles(points)
    index = RoutePointIndex(points, cumulative, cell_miles=10.0)

    hit = index.nearest(35.0, -98.0, max_miles=10.0)
    assert hit is not None
    offset, detour = hit
    assert detour < 10.0
    # The nearest sample point could belong to either leg near the turnaround,
    # both are valid, what matters is a hit was found close by.
    assert offset >= 0.0


def test_route_point_index_empty_points() -> None:
    index = RoutePointIndex([], [], cell_miles=10.0)
    assert index.nearest(35.0, -100.0, max_miles=10.0) is None


def _zigzag(count: int) -> list[tuple[float, float]]:
    """A route that wanders the way a real road does, without any randomness.

    Two sine waves of different wavelengths, so the polyline has both long sweeps
    that simplify away and tight corners that must survive.
    """
    points = []
    for step in range(count):
        fraction = step / (count - 1)
        lon = -100.0 + fraction * 5.0
        lat = 36.0 + 0.05 * math.sin(fraction * 40.0) + 0.01 * math.sin(fraction * 260.0)
        points.append((lat, lon))
    return points


def test_simplify_polyline_collapses_a_straight_line_to_its_endpoints() -> None:
    """A thousand points on one line carry no information the two ends do not."""
    line = [(36.0, -100.0 + step * 0.005) for step in range(1000)]
    simplified = simplify_polyline(line, tolerance_miles=0.02)
    assert simplified == [line[0], line[-1]]


def test_simplify_polyline_keeps_a_right_angle_corner() -> None:
    """The corner is the whole shape: dropping it would move the line by miles."""
    east = [(36.0, -100.0 + step * 0.05) for step in range(21)]
    north = [(36.0 + step * 0.05, -99.0) for step in range(1, 21)]
    corner = (36.0, -99.0)
    simplified = simplify_polyline(east + north, tolerance_miles=0.02)
    assert simplified == [east[0], corner, north[-1]]


def test_simplify_polyline_always_keeps_the_endpoints() -> None:
    coords = _zigzag(500)
    for tolerance in (0.001, 0.02, 0.5, 50.0):
        simplified = simplify_polyline(coords, tolerance_miles=tolerance)
        assert simplified[0] == coords[0]
        assert simplified[-1] == coords[-1]
        assert len(simplified) >= 2


def test_simplify_polyline_with_zero_tolerance_returns_the_input_unchanged() -> None:
    """Zero tolerance means "change nothing", not "drop everything exactly on the line"."""
    coords = _zigzag(200)
    assert simplify_polyline(coords, tolerance_miles=0.0) == coords
    assert simplify_polyline(coords, tolerance_miles=-1.0) == coords


def test_simplify_polyline_short_inputs_are_returned_as_they_are() -> None:
    assert simplify_polyline([], tolerance_miles=0.02) == []
    assert simplify_polyline([(36.0, -100.0)], tolerance_miles=0.02) == [(36.0, -100.0)]
    pair = [(36.0, -100.0), (36.0, -99.0)]
    assert simplify_polyline(pair, tolerance_miles=0.02) == pair


def test_simplify_polyline_handles_repeated_vertices() -> None:
    """Routing providers do emit the same coordinate twice, which is a zero length
    segment and a division by zero for the naive distance formula.
    """
    coords = [(36.0, -100.0), (36.0, -100.0), (36.5, -99.0), (36.5, -99.0), (37.0, -98.0)]
    simplified = simplify_polyline(coords, tolerance_miles=0.02)
    assert simplified[0] == coords[0]
    assert simplified[-1] == coords[-1]
    assert len(simplified) <= len(coords)


def test_simplify_polyline_every_dropped_point_stays_within_tolerance() -> None:
    """The guarantee the tolerance is supposed to buy, checked point by point.

    The simplified polyline is a subsequence of the input, so walking both lists
    forward pairs each dropped run with the segment that replaced it. Every dropped
    point must lie within the tolerance of that segment.
    """
    coords = _zigzag(2000)
    tolerance = 0.02
    simplified = simplify_polyline(coords, tolerance_miles=tolerance)
    assert 2 < len(simplified) < len(coords)

    kept_indexes = []
    cursor = 0
    for point in simplified:
        while coords[cursor] != point:
            cursor += 1
        kept_indexes.append(cursor)

    worst = 0.0
    for start, end in zip(kept_indexes, kept_indexes[1:], strict=False):
        for index in range(start + 1, end):
            offset = perpendicular_offset_miles(coords[index], coords[start], coords[end])
            worst = max(worst, offset)
    assert worst <= tolerance + 1e-9


def test_perpendicular_offset_miles_matches_a_known_distance() -> None:
    """One degree of latitude off a line of constant latitude is 69 miles."""
    offset = perpendicular_offset_miles((37.0, -99.0), (36.0, -100.0), (36.0, -98.0))
    assert offset == pytest.approx(69.0, rel=1e-6)


def test_perpendicular_offset_miles_measures_to_a_degenerate_segment() -> None:
    offset = perpendicular_offset_miles((37.0, -100.0), (36.0, -100.0), (36.0, -100.0))
    assert offset == pytest.approx(69.0, rel=1e-6)
