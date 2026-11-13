# syntax=docker/dockerfile:1
#
# Cloudy API image: FastAPI + pytubefix. Runtime needs:
#   - ffmpeg + ffprobe binaries   merge adaptive video+audio, convert MP3
#   - pytubefix's bundled Node.js PO-token generation (nodejs-wheel-binaries,
#                                 installed with pytubefix from uv.lock; needs
#                                 libstdc++6)
# scripts/check_deps.py verifies all of it at build time.
#
# Fallback if a wheel/image problem shows up: switch to python:3.14-slim-trixie
# and lower .python-version / requires-python to 3.14.

ARG PYTHON_VERSION=3.15
ARG UV_VERSION=0.13.6

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- builder: resolves and builds the virtualenv ---------------------------
# Some locked dependencies (pyyaml, zstandard via langsmith) have no wheel for
# Python 3.15 yet, so uv compiles them from source and needs a C toolchain.
# The toolchain lives only in this stage; the runtime image stays slim.
FROM python:${PYTHON_VERSION}-slim-trixie AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first for layer caching, then the project itself. --frozen
# installs exactly what uv.lock pins.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# --- runtime ---------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie

# ffmpeg also provides ffprobe. libstdc++6 is needed by pytubefix's bundled
# node binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

# HOME/XDG_CACHE_HOME give the non-root user writable home and cache dirs
# (wiped on restarts, which is fine).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/tmp/home \
    XDG_CACHE_HOME=/tmp/cache \
    WORK_DIR=/tmp/cloudy \
    FORWARDED_ALLOW_IPS="*"

WORKDIR /app

# Same base image and path as the builder, so the venv's interpreter symlinks
# and the editable install of the project (src/) resolve unchanged.
COPY --from=builder /app/.venv ./.venv
COPY pyproject.toml uv.lock .python-version README.md ./
COPY src ./src

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /tmp/home /tmp/cache /tmp/cloudy \
    && chown -R app:app /tmp/home /tmp/cache /tmp/cloudy
USER app

# Fail the build (not the first download) if ffmpeg/ffprobe or pytubefix's
# bundled node is missing or can't run. Runs as `app` to catch permission issues.
COPY --chown=app:app scripts/check_deps.py /app/scripts/check_deps.py
RUN python /app/scripts/check_deps.py

EXPOSE 8000
# Shell form so ${PORT} (set by Railway) expands; exec so SIGTERM reaches uvicorn.
CMD exec fastapi run src/cloudy_api/main.py --host 0.0.0.0 --port ${PORT:-8000} --workers 1
