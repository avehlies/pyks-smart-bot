# syntax=docker/dockerfile:1
# Both images, from one file. Build from the repository root:
#   docker build --target bot .           (the default target, so plain `docker build .` works too)
#   docker build --target clip-worker .
#   docker build --target short-worker .
# They share the base, uv and uv's download cache, but each venv holds only its own package and
# dependencies: the bot image has no worker code or faster-whisper, the workers have no bot code or
# discord.py.
#
# Each image syncs its venv in place. Building the venvs in a shared builder stage and copying them
# into the final stages was measured slower: every lockfile change re-copies a ~500 MB venv.
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:0.11 AS uv

FROM python:${PYTHON_VERSION}-slim AS runtime-base
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
WORKDIR /app

# The clip transcription worker. uv is mounted for the build steps, not kept in the image.
FROM runtime-base AS clip-worker
# Whisper models are downloaded here on first use; mount a volume to keep them.
ENV HF_HOME=/models
RUN useradd --create-home --uid 1000 worker && mkdir -p /models && chown worker /models
# Dependencies first, so code changes don't reinstall them. uv needs every workspace member's
# pyproject to read the lockfile, even when it installs only one package. The uv cache mount is
# shared with the bot's build, so wheels are downloaded once and reused when the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
COPY clip-worker/pyproject.toml clip-worker/README.md clip-worker/
COPY short-worker/pyproject.toml short-worker/README.md short-worker/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package yks-clip-worker --no-install-workspace
COPY clip-worker/src clip-worker/src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package yks-clip-worker
USER worker
# Fails when the worker's heartbeat file is stale: the database is unreachable, or a clip has been
# stuck for far longer than TRANSCRIBE_TIMEOUT_SECONDS allows. See clip-worker/README.md.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["yks-clip-worker-health"]
CMD ["yks-clip-worker"]

# The YouTube Shorts worker: finds shorts and records them (the bot posts them; this image has no
# Discord code). Small: asyncpg, httpx and pydantic-settings only.
FROM runtime-base AS short-worker
RUN useradd --create-home --uid 1000 worker
COPY pyproject.toml uv.lock README.md ./
COPY clip-worker/pyproject.toml clip-worker/README.md clip-worker/
COPY short-worker/pyproject.toml short-worker/README.md short-worker/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package yks-short-worker --no-install-workspace
COPY short-worker/src short-worker/src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package yks-short-worker
USER worker
# Fails when the worker's heartbeat file is stale (the database is unreachable). YouTube being
# down doesn't make it unhealthy. See short-worker/README.md.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["yks-short-worker-health"]
CMD ["yks-short-worker"]

# The Discord bot. Keep this stage last: it's the default target.
FROM runtime-base AS bot
# ffmpeg: voice playback for !listen.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# uv stays in the bot image: one-off tools run with `uv run --no-sync --with ...` (see docs/DEPLOY.md).
COPY --from=uv /uv /usr/local/bin/uv
RUN useradd --create-home --uid 1000 yks
# Dependencies first, as in the worker stage.
COPY pyproject.toml uv.lock README.md ./
COPY clip-worker/pyproject.toml clip-worker/README.md clip-worker/
COPY short-worker/pyproject.toml short-worker/README.md short-worker/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package pyks-smart-bot --no-install-workspace
COPY alembic.ini ./
COPY migrations migrations
COPY assets assets
COPY src src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package pyks-smart-bot
USER yks
EXPOSE 3000
CMD ["yks-bot"]
