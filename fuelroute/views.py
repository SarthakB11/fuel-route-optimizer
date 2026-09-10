"""HTTP views for the fuel route planning API and the browser map page.

This module stays thin on purpose: request parsing goes through
RoutePlanQuerySerializer, the actual planning work happens in fuelroute.planner,
and response shaping happens in build_response_payload. A view here only wires
those pieces together and maps domain errors onto status codes.
"""

from __future__ import annotations

import functools
import json
import logging
import time
import tomllib
from pathlib import Path
from typing import Any

from django.conf import settings
from django.shortcuts import redirect, render
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from fuelroute import stations
from fuelroute.optimizer import RouteNotFeasible
from fuelroute.places import LocationError
from fuelroute.planner import build_plan
from fuelroute.routing import RouteUnavailable, RoutingError
from fuelroute.serializers import RoutePlanQuerySerializer, build_response_payload

logger = logging.getLogger(__name__)

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _error_response(message: str, status_code: int, detail: Any = None) -> Response:
    """Build the documented {"error": ..., "detail": ...} error body."""
    body = {"error": message, "detail": detail if detail is not None else {}}
    return Response(body, status=status_code)


class RoutePlanView(APIView):
    """GET /api/v1/route-plan, the cheapest fuel stop plan between two US locations."""

    def get(self, request: Request) -> Response:
        query = RoutePlanQuerySerializer(data=request.query_params)
        if not query.is_valid():
            return _error_response("Invalid query parameters.", 400, query.errors)

        params = query.validated_data
        started = time.perf_counter()
        try:
            result = build_plan(
                params["start"],
                params["finish"],
                range_miles=params["range_miles"],
                mpg=params["mpg"],
                corridor_miles=params["corridor_miles"],
                initial_fuel_miles=params["initial_fuel_miles"],
            )
        except LocationError as exc:
            return _error_response(str(exc), 400)
        except RouteUnavailable as exc:
            # The provider worked and the answer is that the trip cannot be driven,
            # so this is the caller's problem, not a bad gateway. Must be caught
            # before RoutingError, which it subclasses.
            return _error_response(str(exc), 422)
        except RoutingError as exc:
            logger.warning("Routing provider failure: %s", exc)
            return _error_response("The routing provider could not compute a route.", 502)
        except RouteNotFeasible as exc:
            return _error_response(str(exc), 422)

        total_ms = (time.perf_counter() - started) * 1000
        return Response(build_response_payload(result, total_ms), status=200)


@functools.lru_cache(maxsize=1)
def _app_version() -> str:
    """Read the app version from pyproject.toml so it cannot drift from it."""
    with _PYPROJECT_PATH.open("rb") as handle:
        data = tomllib.load(handle)
    return str(data["project"]["version"])


@functools.lru_cache(maxsize=1)
def _dataset_generated_at() -> str:
    """Read the generated_at timestamp straight from the committed dataset file."""
    with Path(settings.STATION_DATA_FILE).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return str(payload["generated_at"])


class HealthView(APIView):
    """GET /api/v1/health, a liveness check reporting the loaded dataset size."""

    def get(self, request: Request) -> Response:  # noqa: ARG002
        loaded = stations.load_stations()
        return Response(
            {
                "status": "ok",
                "station_count": len(loaded),
                "dataset_generated_at": _dataset_generated_at(),
                "app_version": _app_version(),
            },
            status=200,
        )


def map_view(request: Request):
    """GET /map, a Leaflet page that fetches the JSON endpoint and draws the plan."""
    return render(request, "fuelroute/map.html")


def index_view(request: Request):  # noqa: ARG001
    """GET /, redirect to the map page so the project has a usable landing page."""
    return redirect("map")
