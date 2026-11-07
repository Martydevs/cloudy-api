# syntax=docker/dockerfile:1
#
# Cloudy API image: FastAPI + yt-dlp with everything yt-dlp's README lists as
# needed for full YouTube support (see README "yt-dlp dependencies"):
#   - ffmpeg + ffprobe binaries   merge DASH video+audio, extract MP3
#   - yt-dlp-ejs                  JS challenge solver (via yt-dlp[default] in uv.lock)
#   - a JS runtime to run it      deno (preferred) and node (fallback)
#   - yt-dlp[default] extras      certifi, brotli, websockets, requests, pycryptodomex
# scripts/check_deps.py verifies all of it at build time.
#
# Fallback if a wheel/image problem shows up: switch to python:3.14-slim-trixie
# and lower .python-version / requires-python to 3.14.

ARG PYTHON_VERSION=3.15
ARG UV_VERSION=0.13.6
# yt-dlp needs deno >= 2.3.
ARG DENO_VERSION=2.9.8
# yt-dlp needs node >= 22. Debian's nodejs package is older, so the binary
# comes from the official image built on the same Debian release (trixie).
ARG NODE_VERSION=26.13.0

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM denoland/deno:bin-${DENO_VERSION} AS deno
FROM node:${NODE_VERSION}-trixie-slim AS node

FROM python:${PYTHON_VERSION}-slim-trixie

# ffmpeg also provides ffprobe. libstdc++6 is needed by the node binary.
# No AtomicParsley/phantomjs/rtmpdump: thumbnail embedding, non-YouTube JS
# extractors and RTMP aren't used by this service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
# JS runtimes for yt-dlp-ejs. yt-dlp picks the best installed one that
# YTDLP_JS_RUNTIMES enables (deno > node > quickjs > bun). To retire deno: drop
# the deno stage and the deno COPY line, and set YTDLP_JS_RUNTIMES=node.
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=node /usr/local/bin/node /usr/local/bin/node

# HOME/XDG_CACHE_HOME/DENO_DIR give the non-root user writable caches. yt-dlp
# caches player JS and solved challenges under $XDG_CACHE_HOME/yt-dlp (wiped on
# Render restarts, which is fine).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/tmp/home \
    XDG_CACHE_HOME=/tmp/cache \
    DENO_DIR=/tmp/cache/deno \
    DENO_NO_UPDATE_CHECK=1 \
    WORK_DIR=/tmp/cloudy \
    YTDLP_JS_RUNTIMES=deno,node \
    FORWARDED_ALLOW_IPS="*"

WORKDIR /app

# Dependencies first for layer caching, then the project itself. --frozen keeps
# yt-dlp-ejs at exactly the version yt-dlp pins (EJS wiki: they MUST match).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /tmp/home /tmp/cache /tmp/cloudy \
    && chown -R app:app /tmp/home /tmp/cache /tmp/cloudy
USER app

# Fail the build (not the first download) if a dependency is missing, too old,
# or yt-dlp-ejs doesn't match yt-dlp. Runs as `app` to catch permission issues.
COPY --chown=app:app scripts/check_deps.py /app/scripts/check_deps.py
RUN python /app/scripts/check_deps.py --runtimes deno,node

EXPOSE 8000
# Shell form so ${PORT} (set by Render) expands; exec so SIGTERM reaches uvicorn.
CMD exec fastapi run src/cloudy_api/main.py --host 0.0.0.0 --port ${PORT:-8000} --workers 1
