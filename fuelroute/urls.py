"""URL routes for the fuel route app."""

from __future__ import annotations

from django.urls import path

from fuelroute import views

urlpatterns = [
    path("", views.index_view, name="index"),
    path("map", views.map_view, name="map"),
    path("api/v1/route-plan", views.RoutePlanView.as_view(), name="route-plan"),
    path("api/v1/health", views.HealthView.as_view(), name="health"),
]
