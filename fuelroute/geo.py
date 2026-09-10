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
