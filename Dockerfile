# python:3.12-slim has official multi-arch images (amd64, arm64, armv7),
# so this builds fine directly on a Raspberry Pi -- no cross-compiling needed.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so this layer is cached across rebuilds
# unless requirements.txt actually changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code -- wildcard rather than an explicit file list, so a new
# module added to the project (there have been several since this repo
# started: analytics.py, baserunning.py, compare_stats.py, glossary.py,
# pitch_discipline.py, pitching_extra.py, pitching_splits.py, rolling.py,
# team_schedule.py) is never accidentally left out of the image again --
# that exact bug (an explicit COPY list going stale) shipped a container
# that would crash on startup with an ImportError.
COPY *.py ./
COPY templates ./templates
COPY static ./static

# Bake in the build time -- this is the single most useful thing to
# check if a deploy "isn't updating": hit /version on the live site.
# If that timestamp doesn't change after a push, the deploy itself
# never actually happened (wrong branch, auto-deploy off, build
# failed silently, etc.) -- that's a Render/git problem to chase down
# on Render's dashboard, not a code problem. If it DOES change but the
# site still looks the same, the deploy is fine and the issue is
# somewhere else (browser cache, stale disk-cached game data, looking
# at the wrong URL/service).
RUN date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/build_info.txt

# Where cbl_api.py persists completed-game feeds to disk. Mounted as a
# volume in docker-compose.yml so the cache survives container
# restarts/rebuilds instead of re-fetching 30-40+ games per player
# from cbl.ca every time.
ENV CBL_GAMEDAY_CACHE_DIR=/app/.cbl_cache/gameday
RUN mkdir -p /app/.cbl_cache/gameday

# Run as a non-root user rather than the container's default root.
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Shell form (not JSON array) so $PORT actually gets expanded --
# Render injects its own PORT env var and expects the app to bind to
# it; locally (Pi/docker-compose) PORT is unset, so this falls back
# to 5000 same as before.
CMD waitress-serve --host=0.0.0.0 --port=${PORT:-5000} app:app
