"""Django settings for the fuel route optimizer.

The service is read only: it holds no user data and needs no database at runtime.
The station table is loaded from a generated JSON file once per process, and route
plans are memoised in the local memory cache.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "insecure-development-key-override-via-DJANGO_SECRET_KEY",
)
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "rest_framework",
    "fuelroute",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.gzip.GZipMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# No models are defined. SQLite is configured only so that management commands and
# the test runner have a valid backend to point at.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "route-plans",
        "TIMEOUT": int(os.environ.get("ROUTE_PLAN_CACHE_SECONDS", "900")),
        "OPTIONS": {"MAX_ENTRIES": 512},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "UNAUTHENTICATED_USER": None,
}

# Domain configuration. Defaults match the brief: a 500 mile tank at 10 mpg.
STATION_DATA_FILE = Path(os.environ.get("STATION_DATA_FILE", BASE_DIR / "data" / "stations.json"))
PLACE_DATA_FILE = Path(os.environ.get("PLACE_DATA_FILE", BASE_DIR / "data" / "places.json"))
DEFAULT_RANGE_MILES = float(os.environ.get("DEFAULT_RANGE_MILES", "500"))
DEFAULT_MPG = float(os.environ.get("DEFAULT_MPG", "10"))
DEFAULT_CORRIDOR_MILES = float(os.environ.get("DEFAULT_CORRIDOR_MILES", "12"))
ROUTE_PLAN_CACHE_SECONDS = int(os.environ.get("ROUTE_PLAN_CACHE_SECONDS", "900"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
