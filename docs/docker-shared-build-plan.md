# Plan: one Dockerfile, shared stages, two images

Status: **proposal, not implemented**. No Dockerfile, compose or CI changes yet.

## Where we are

| | Bot (`Dockerfile`) | Clip worker (`clip-worker/Dockerfile`) |
| --- | --- | --- |
| Base | `python:3.12-slim` + uv 0.11 | `python:3.12-slim` + uv 0.11 |
| apt | `ffmpeg` (voice playback) | none (PyAV ships its own ffmpeg libs) |
| `uv sync` | root package (implicitly) | `--package yks-clip-worker` |
| Runtime files | `src`, `migrations`, `alembic.ini`, `assets` | `clip-worker/src` |
| Runtime needs uv | **yes**: DEPLOY.md runs `uv run --no-sync --with pymongo yks-import-mongo` in the bot container | no |
| User | `yks` (1000) | `worker` (1000), owns `/models` (`HF_HOME`) |

Compose builds the bot image twice (for `migrate` and `bot`, both `build: .`) and the worker once. CI
runs a 2-job matrix, each job with its own GHA cache scope, then runs an import check in each image.

**What actually overlaps.** The images share the base image, the uv binary, the workspace metadata
copy, and only three runtime dependencies (`asyncpg`, `httpx`, `pydantic-settings`). Their heavy
dependencies differ: discord.py/SQLAlchemy/Pillow for the bot, faster-whisper (ctranslate2,
onnxruntime, PyAV, tokenizers) for the worker. Each image still needs its own virtualenv, so a shared
build can't avoid installing those twice. Most of the saving comes from caching, parallel stages and
deduplicated builds, not from installing packages once:

1. **uv cache mount** shared by both builder stages. Wheels are downloaded once per machine and reused
   when `uv.lock` changes, instead of re-downloading everything (biggest local win; ctranslate2 and
   onnxruntime are large).
2. **Parallel stages**: the bot's `apt-get install ffmpeg` runs at the same time as its `uv sync`
   instead of before it.
3. **One build per image in compose**: `migrate` and `bot` share one tagged image.
4. The shared stages (base, uv, metadata) are solved once per BuildKit session, and there is one
   Dockerfile to keep in sync instead of two.

I'll measure this before and after (see Verification) rather than promise a number.

## Proposed `Dockerfile` (root), stage by stage

```dockerfile
# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:0.11 AS uv

# Shared runtime base for both images: no uv, no build tooling.
FROM python:${PYTHON_VERSION}-slim AS runtime-base
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Shared builder: uv + workspace metadata (both members' pyproject/README, the lockfile).
FROM runtime-base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY clip-worker/pyproject.toml clip-worker/README.md clip-worker/

FROM builder AS bot-build
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package pyks-smart-bot --no-install-workspace
COPY alembic.ini ./
COPY migrations migrations
COPY assets assets
COPY src src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package pyks-smart-bot

FROM builder AS clip-worker-build
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package yks-clip-worker --no-install-workspace
COPY clip-worker/src clip-worker/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package yks-clip-worker

FROM runtime-base AS clip-worker
ENV HF_HOME=/models
RUN useradd --create-home --uid 1000 worker && mkdir -p /models && chown worker /models
COPY --from=clip-worker-build /opt/venv /opt/venv
COPY --from=clip-worker-build /app/clip-worker/src /app/clip-worker/src
USER worker
CMD ["yks-clip-worker"]

# Last stage = default target, so a plain `docker build .` still builds the bot.
FROM runtime-base AS bot
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
RUN useradd --create-home --uid 1000 yks
COPY --from=bot-build /opt/venv /opt/venv
COPY --from=bot-build /app /app
USER yks
EXPOSE 3000
CMD ["yks-bot"]
```

The comments in the real file will match the current Dockerfiles (why ffmpeg, why the worker
metadata is copied, `HF_HOME` volume, and so on).

### Design decisions

- **Both venvs at `/opt/venv`, built in separate stages.** Venvs are path-sensitive (script shebangs,
  `pyvenv.cfg`), so each is built at the path it runs from, and the final stages use the same
  `python:3.12-slim` base as the builder so the interpreter path matches.
- **Keep editable installs** (uv's default for workspace members) and copy the source alongside the
  venv. The bot needs this: `DEFAULT_ASSETS_DIR` is `Path(__file__).parents[2] / "assets"`
  (= `/app/assets`), and `alembic.ini` uses `prepend_sys_path = %(here)s/src`. A `--no-editable`
  install would move `yks` into site-packages and break the assets path. The worker could go
  non-editable, but keeping both the same is simpler.
- **`--package pyks-smart-bot` is now explicit** for the bot. Today it relies on `uv sync`
  defaulting to the workspace root.
- **The bot's final `/app` keeps today's contents**, including `pyproject.toml`, `uv.lock` and the
  worker's `pyproject.toml`/`README.md` (metadata only), so the `uv run --no-sync --with pymongo`
  command in DEPLOY.md keeps finding the workspace. It does not contain `clip-worker/src`.
- **The worker image drops uv** (about 35 MB) because nothing runs uv in that container. If we'd
  rather keep it for debugging, it's one `COPY --from=uv` line.
- **apt runs in the final `bot` stage**, not the builder, so it runs in parallel with `bot-build`
  and isn't in the worker's ancestry.
- `PYTHON_VERSION` becomes a build arg, so CI's `PYTHON_VERSION` comment points at one place.

## Compose

```yaml
x-app: &app
  image: yks-bot:local
  build:
    context: .
    target: bot
  ...

  clip-worker:
    image: yks-clip-worker:local
    build:
      context: .
      target: clip-worker
```

With the same `image:` on `migrate` and `bot`, compose tags one image instead of two
(`pyks-smart-bot-migrate`, `pyks-smart-bot-bot`). I'll check that `docker compose build` builds it
only once. If it still builds twice (from cache), I'll remove `build:` from `migrate`.
`docker compose build` builds both targets in one BuildKit session, so the shared stages and the uv
cache mount are reused. DEPLOY.md commands (`docker compose build`, `run --rm bot uv run ...`) stay
the same.

## CI (`.github/workflows/ci.yml`, `docker` job)

Keep the matrix: two parallel runners are faster than one, and a failure is reported per image.

```yaml
matrix:
  include:
    - image: bot
      target: bot
      check: <unchanged>
    - image: clip-worker
      target: clip-worker
      check: <unchanged>
steps:
  - uses: docker/build-push-action@v7
    with:
      context: .
      file: Dockerfile
      target: ${{ matrix.target }}
      push: false
      load: true
      tags: yks-${{ matrix.image }}:ci
      cache-from: |
        type=gha,scope=${{ matrix.image }}
        type=gha,scope=${{ matrix.image == 'bot' && 'clip-worker' || 'bot' }}
      cache-to: type=gha,mode=max,scope=${{ matrix.image }}
```

- Each job writes its own scope (`mode=max` exports the builder stages too) and reads both, so the
  shared stages can come from either image's cache. Code-only changes reuse the dependency layers,
  as they do now.
- BuildKit cache **mounts** (`/root/.cache/uv`) are not exported to GHA cache, so in CI the gain
  comes from layer caching, as today. That's fine: when only code changes, the dependency layers are
  already cached.
- **Isolation check: unchanged and still meaningful.** Each target's venv is synced with
  `--package <self>` only, and each final stage copies only its own source, so the existing
  `find_spec` asserts keep passing and would catch any regression (for example, someone switching to
  `--all-packages` or copying `/app` wholesale into the worker). I'd also add a file-level assertion
  to the bot check: `import pathlib; assert not pathlib.Path('/app/clip-worker/src').exists()`.
- Optional: add `docker/bake-action` with a `docker-bake.hcl` defining both targets, for one-command
  local builds of both images (`docker buildx bake`). Compose already covers this locally, so I'd
  skip it unless wanted.

## Other changes

- Delete `clip-worker/Dockerfile`. Update references in the root Dockerfile comment, `PLAN.md`'s
  layout section ("clip-worker/: own pyproject, Dockerfile, tests") and `clip-worker/README.md` if
  it mentions the build command.
- `.dockerignore`: change `tests` → `**/tests` (today `clip-worker/tests` is sent in the build context),
  and add `docs`, `.github`, `PLAN.md`. Keep `README.md` files: hatchling needs them for package
  metadata.

## Verification (local Docker 28 / buildx 0.19 / compose 2.40 are available)

1. Baseline timings on `main`: cold (`docker builder prune -af` first) and warm builds of both images,
   and a rebuild after a code-only change and after a `uv.lock` change.
2. Same on this branch: `docker build --target bot .`, `docker build --target clip-worker .`, and
   `docker compose build`.
3. Run both CI check commands against the new images. Also check the bot runs `alembic upgrade head`
   (migrate service) and finds its assets, the worker starts and writes to `/models`, and
   `docker compose run --rm bot uv run --no-sync --with "pymongo>=4.10" yks-import-mongo --help` works.
4. Compare image sizes (`docker images`) before and after. The worker should shrink by the uv binary.
5. Report the timing table in the PR description.

## Open questions for approval

1. Drop uv from the worker image? (Proposed: yes.)
2. Keep the CI matrix with cross-reading caches (proposed), or a single bake job?
3. Fold the `.dockerignore` tidy-up into this PR (proposed: yes, it's small)?
