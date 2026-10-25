# One image for the bot (and, from Phase 6, the clip worker).
# Build the worker variant with: docker build --build-arg EXTRAS="--extra worker" .
FROM python:3.12-slim

# ffmpeg: voice playback (bot) and audio conversion (worker).
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

ARG EXTRAS=""

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project ${EXTRAS}

COPY . .
RUN uv sync --frozen --no-dev ${EXTRAS}

RUN useradd --create-home --uid 1000 yks
USER yks

EXPOSE 3000
CMD ["yks-bot"]
