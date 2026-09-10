# Runtime image for the fuel route optimizer.
#
# The service is read only and holds no user data, so there is nothing to mount and
# no migration step: the station and place indexes are committed files that are
# copied in with the rest of the project and parsed once at startup.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements are copied on their own so the dependency layer is rebuilt only when
# the pins change, not on every edit to the source.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 app
COPY --chown=app:app . .
USER app

EXPOSE 8000

# Two workers is the sensible default for a small container: the plan cache is per
# process, so more workers means more duplicated cache and more resident station
# tables. The 60 second timeout leaves room for a slow response from the routing
# provider, which is the only outbound call a request makes.
CMD ["gunicorn", "config.wsgi:application", \
     "--workers", "2", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "60", \
     "--access-logfile", "-"]
