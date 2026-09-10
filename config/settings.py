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
# Off by default so an unmapped exception can never return Django's HTML traceback
# to an API caller. Set DJANGO_DEBUG=1 locally when you want the debug page. The map
# page works either way: Leaflet comes from a CDN, so nothing depends on the static
# file server that DEBUG would otherwise provide.
DEBUG = _env_bool("DJANGO_DEBUG", False)
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
    # JSON only, deliberately. The browsable API renderer pulls its stylesheets from
    # {% static "rest_framework/..." %}, and with DEBUG off runserver does not serve
    # static files, so pasting an endpoint into a browser would render unstyled HTML
    # with a screenful of 404s behind it. Browsers pretty print a JSON body natively,
    # which is a better result than a broken page, and /map is the human facing view.
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
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
