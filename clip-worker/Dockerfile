# The clip transcription worker. Build from the repository root:
#   docker build -f clip-worker/Dockerfile .
# It shares the uv workspace lockfile with the bot, but installs only its own package and
# dependencies (no bot code, no discord.py).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # Whisper models are downloaded here on first use; mount a volume to keep them.
    HF_HOME=/models

WORKDIR /app

# Workspace metadata first, so dependency layers are cached across code changes.
COPY pyproject.toml uv.lock README.md ./
COPY clip-worker/pyproject.toml clip-worker/README.md clip-worker/
RUN uv sync --frozen --no-dev --package yks-clip-worker --no-install-workspace

COPY clip-worker/src clip-worker/src
RUN uv sync --frozen --no-dev --package yks-clip-worker

RUN useradd --create-home --uid 1000 worker && mkdir -p /models && chown worker /models
USER worker

CMD ["yks-clip-worker"]
