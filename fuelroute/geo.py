"""Pure geometry helpers for working with latitude/longitude polylines.

No Django, no I/O. Everything here is deterministic and safe to unit test in
isolation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

EARTH_RADIUS_MILES: float = 3958.7613

# Degrees latitude are a near constant number of miles. Degrees longitude shrink
# toward the poles by a factor of cos(latitude), so callers must supply a latitude
# whenever they convert a longitude delta to miles.
MILES_PER_DEGREE_LATITUDE: float = 69.0


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great circle distance between two lat/lon points, in miles."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.asin(min(1.0, math.sqrt(a)))
    return EARTH_RADIUS_MILES * c


def cumulative_miles(coords: Sequence[tuple[float, float]]) -> list[float]:
    """Running distance along a (lat, lon) polyline. First element is 0.0."""
    if not coords:
        return []
    totals = [0.0]
    for (lat1, lon1), (lat2, lon2) in zip(coords, coords[1:], strict=False):
        totals.append(totals[-1] + haversine_miles(lat1, lon1, lat2, lon2))
    return totals


def resample_polyline(
    coords: Sequence[tuple[float, float]], spacing_miles: float
) -> tuple[list[tuple[float, float]], list[float]]:
    """Densify or thin a polyline to roughly one point per spacing_miles.

    Returns the sampled points and their cumulative distance along the route.
    Always keeps the first and last vertex.
    """
    if spacing_miles <= 0:
        raise ValueError("spacing_miles must be positive")
    if len(coords) < 2:
        cumulative = cumulative_miles(coords)
        return list(coords), cumulative

    source_cumulative = cumulative_miles(coords)
    total = source_cumulative[-1]

    if total <= 0:
        return [coords[0], coords[-1]], [0.0, 0.0]

    # Ceiling, not rounding: every resulting segment must be no longer than
    # spacing_miles, and rounding down can leave one segment over budget.
    sample_count = max(1, math.ceil(total / spacing_miles))
    targets = [total * i / sample_count for i in range(sample_count + 1)]

    sampled_points: list[tuple[float, float]] = []
    sampled_cumulative: list[float] = []
    segment_index = 0
    for target in targets:
        while (
            segment_index < len(source_cumulative) - 2
            and source_cumulative[segment_index + 1] < target
        ):
            segment_index += 1
        seg_start_dist = source_cumulative[segment_index]
        seg_end_dist = source_cumulative[segment_index + 1]
        seg_length = seg_end_dist - seg_start_dist
        fraction = 0.0 if seg_length <= 0 else (target - seg_start_dist) / seg_length
        fraction = min(1.0, max(0.0, fraction))
        lat1, lon1 = coords[segment_index]
        lat2, lon2 = coords[segment_index + 1]
        point = (lat1 + (lat2 - lat1) * fraction, lon1 + (lon2 - lon1) * fraction)
        sampled_points.append(point)
        sampled_cumulative.append(target)

    sampled_points[0] = coords[0]
    sampled_points[-1] = coords[-1]
    sampled_cumulative[0] = 0.0
    sampled_cumulative[-1] = total
    return sampled_points, sampled_cumulative


def _miles_per_degree_longitude(latitude: float) -> float:
    """Miles per degree of longitude at a given latitude, guarded near the poles."""
    cos_lat = math.cos(math.radians(latitude))
    if abs(cos_lat) < 1e-6:
        cos_lat = 1e-6
    return MILES_PER_DEGREE_LATITUDE * cos_lat


class RoutePointIndex:
    """Uniform lat/lon grid over route sample points for nearest point lookup.

    Cell size is derived from the query radius so a lookup touches a small block
    of cells around the query point. Build is O(points); each query is O(points in
    that block). The grid uses one reference latitude for the whole index, the
    largest absolute latitude among the indexed points, to convert the longitude
    axis to miles. Anchoring on the most poleward point means every cell is at
    least cell_miles wide at every latitude actually present in the data, which is
    what keeps the neighbourhood search exact rather than approximate: a point
    within max_miles of the query can never be more than one cell_miles sized cell
    away, in either axis, than the query's own cell.
    """

    def __init__(
        self,
        points: Sequence[tuple[float, float]],
        cumulative: Sequence[float],
        cell_miles: float,
    ) -> None:
        if len(points) != len(cumulative):
            raise ValueError("points and cumulative must be the same length")
        if cell_miles <= 0:
            raise ValueError("cell_miles must be positive")
        self._points = list(points)
        self._cumulative = list(cumulative)
        self._cell_miles = cell_miles
        reference_lat = max((abs(lat) for lat, _ in self._points), default=0.0)
        self._lat_size = cell_miles / MILES_PER_DEGREE_LATITUDE
        self._lon_size = cell_miles / _miles_per_degree_longitude(reference_lat)
        self._cells: dict[tuple[int, int], list[int]] = {}
        for idx, (lat, lon) in enumerate(self._points):
            cell = self._cell_key(lat, lon)
            self._cells.setdefault(cell, []).append(idx)

    def _cell_key(self, lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / self._lat_size), math.floor(lon / self._lon_size))

    def nearest(self, lat: float, lon: float, max_miles: float) -> tuple[float, float] | None:
        """Return (offset_miles_along_route, detour_miles) for the closest route
        sample point within max_miles, or None. On a route that loops back on
        itself, the nearest point wins.
        """
        if not self._points:
            return None

        center_cell = self._cell_key(lat, lon)
        # A query radius wider than the cell the index was built with needs a
        # wider neighbourhood, otherwise a point could sit outside the block.
        reach = max(1, math.ceil(max_miles / self._cell_miles))

        best_index: int | None = None
        best_distance = math.inf
        for d_row in range(-reach, reach + 1):
            for d_col in range(-reach, reach + 1):
                cell = (center_cell[0] + d_row, center_cell[1] + d_col)
                for idx in self._cells.get(cell, ()):
                    point_lat, point_lon = self._points[idx]
                    distance = haversine_miles(lat, lon, point_lat, point_lon)
                    if distance < best_distance:
                        best_distance = distance
                        best_index = idx

        if best_index is None or best_distance > max_miles:
            return None
        return self._cumulative[best_index], best_distance


def perpendicular_offset_miles(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """Distance in miles from a point to the segment start-end.

    The local flat earth metric: latitude scales by a constant, longitude by
    cos(latitude) taken once at the midpoint of the segment. Over the few miles a
    simplification tolerance cares about that is indistinguishable from a great
    circle, and it costs two multiplications rather than four trigonometric calls.

    This is the definition simplify_polyline works to. That function inlines the
    same arithmetic in its inner loop, because a coast to coast polyline runs the
    loop hundreds of thousands of times and a Python call per iteration is the
    dominant cost.
    """
    lon_scale = _miles_per_degree_longitude((start[0] + end[0]) / 2)
    origin_x = start[1] * lon_scale
    origin_y = start[0] * MILES_PER_DEGREE_LATITUDE
    seg_x = end[1] * lon_scale - origin_x
    seg_y = end[0] * MILES_PER_DEGREE_LATITUDE - origin_y
    point_x = point[1] * lon_scale - origin_x
    point_y = point[0] * MILES_PER_DEGREE_LATITUDE - origin_y

    seg_length_sq = seg_x * seg_x + seg_y * seg_y
    if seg_length_sq <= 1e-24:
        # A degenerate segment is a single point, which routing providers do emit
        # as repeated vertices. Measure to that point rather than dividing by zero.
        return math.hypot(point_x, point_y)

    projection = (point_x * seg_x + point_y * seg_y) / seg_length_sq
    projection = min(1.0, max(0.0, projection))
    return math.hypot(point_x - projection * seg_x, point_y - projection * seg_y)


def simplify_polyline(
    coords: Sequence[tuple[float, float]], tolerance_miles: float
) -> list[tuple[float, float]]:
    """Douglas-Peucker simplification of a (lat, lon) polyline.

    Drops every vertex that lies within tolerance_miles of the line kept in its
    place, so the returned polyline is a subsequence of the input whose maximum
    deviation from it is at most the tolerance. The first and last vertex are
    always kept, and a tolerance of zero or less returns the input untouched.

    The implementation is iterative rather than the textbook recursion on purpose.
    A routing provider answers a coast to coast query with tens of thousands of
    vertices, and the worst case recursion depth is the vertex count, which
    overruns the interpreter's stack limit long before the input is unreasonable.
    """
    count = len(coords)
    if tolerance_miles <= 0 or count < 3:
        return list(coords)

    # Hoisted out of the loop: the latitude axis has a fixed scale, so each point's
    # y coordinate in miles is computed once rather than once per segment it is
    # tested against.
    lats = [lat for lat, _ in coords]
    lons = [lon for _, lon in coords]
    ys = [lat * MILES_PER_DEGREE_LATITUDE for lat in lats]

    keep = [False] * count
    keep[0] = True
    keep[count - 1] = True

    stack = [(0, count - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue

        lon_scale = _miles_per_degree_longitude((lats[first] + lats[last]) / 2)
        origin_x = lons[first] * lon_scale
        origin_y = ys[first]
        seg_x = lons[last] * lon_scale - origin_x
        seg_y = ys[last] - origin_y
        seg_length_sq = seg_x * seg_x + seg_y * seg_y
        inverse_length_sq = 0.0 if seg_length_sq <= 1e-24 else 1.0 / seg_length_sq

        # Squared distances throughout, seeded at the squared tolerance: a vertex
        # has to beat it to be kept, the split index stays -1 when the whole run is
        # within it, and the loop never pays for a square root it does not need.
        worst_distance_sq = tolerance_miles * tolerance_miles
        worst_index = -1
        for index in range(first + 1, last):
            point_x = lons[index] * lon_scale - origin_x
            point_y = ys[index] - origin_y
            if inverse_length_sq:
                projection = (point_x * seg_x + point_y * seg_y) * inverse_length_sq
                if projection < 0.0:
                    projection = 0.0
                elif projection > 1.0:
                    projection = 1.0
                offset_x = point_x - projection * seg_x
                offset_y = point_y - projection * seg_y
            else:
                offset_x = point_x
                offset_y = point_y
            distance_sq = offset_x * offset_x + offset_y * offset_y
            if distance_sq > worst_distance_sq:
                worst_distance_sq = distance_sq
                worst_index = index

        if worst_index >= 0:
            keep[worst_index] = True
            stack.append((first, worst_index))
            stack.append((worst_index, last))

    return [coords[index] for index in range(count) if keep[index]]
