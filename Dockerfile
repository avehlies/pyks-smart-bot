# The Discord bot. (The clip transcription worker has its own image: clip-worker/Dockerfile.)
FROM python:3.12-slim

# ffmpeg: voice playback for !listen.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them. The workspace member's pyproject is
# needed for uv to read the lockfile, but the worker itself is not installed or copied.
COPY pyproject.toml uv.lock README.md ./
COPY clip-worker/pyproject.toml clip-worker/README.md clip-worker/
RUN uv sync --frozen --no-dev --no-install-workspace

COPY alembic.ini ./
COPY migrations migrations
COPY assets assets
COPY src src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 yks
USER yks

EXPOSE 3000
CMD ["yks-bot"]
