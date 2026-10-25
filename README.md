# pyks-smart-bot

A Python rewrite of [yks-smart-bot](https://github.com/avehlies/yks-smart-bot), the Your Kickstarter Sucks Discord bot. See [PLAN.md](PLAN.md) for the roadmap.

## Running with Docker

```sh
cp .env.example .env   # then fill in AUTH_TOKEN and the IDs
docker compose up --build
```

This starts Postgres, applies migrations (`migrate` service), the bot, and the **clip worker**. The clip worker is a separate application that transcribes clips for `/findtheclimp`; see [clip-worker/README.md](clip-worker/README.md). The first time the worker starts, it downloads its speech model (about 75 MB for `tiny.en`) into the `whisper-models` volume.

After the first deploy (and whenever slash commands change), send `!sync` as the bot owner to register `/findtheclimp`. Global registration can take a while to appear; `!sync here` registers it in the current server instantly.

## Moving over from the TypeScript bot

The import reads the old MongoDB (it never writes to it) and upserts into Postgres. Running it again is safe: rows are updated, not duplicated. Imported:
- per-server settings: welcome toggle, RSS channel, already-welcomed members
- `!best` votes
- `:this:` tallies for mailbag and kickstarter posts
- the latest-episode titles and number, so the watcher doesn't re-announce current episodes
- every clip, with its transcript

Clips whose old transcript was empty (the TS bot stored `""` for failures as well as silence) are queued for the clip worker again; pass `--keep-empty-transcripts` to skip that.

1. **Fill in `.env`** with the same `AUTH_TOKEN` and channel/guild IDs as the old bot. `YKS_GUILD_ID`, `YKS_CLIP_CHANNEL_ID`, `YKS_MAILBAG_CHANNEL_ID` and `YKS_KICKSTARTER_BOT_CHANNEL_ID` are used to place imported clips and tallies. Then add `MONGO_URL`, built from the old `MONGO_*` values:
   - Atlas: `mongodb+srv://USER:PASSWORD@HOST/DB?retryWrites=true&w=majority`
   - Self-hosted: `mongodb://USER:PASSWORD@HOST:PORT/DB?authSource=admin`
2. **Start the database** and apply migrations: `docker compose up -d postgres migrate`
3. **Dry run**, to see what would be imported and any warnings:
   ```sh
   docker compose run --rm bot uv run --no-sync --with "pymongo>=4.10" yks-import-mongo --dry-run
   ```
4. **Stop the TypeScript bot**, so the two don't both answer commands.
5. **Import for real**: the same command without `--dry-run`.
6. **Start everything**: `docker compose up -d --build`
7. **Register the slash command**: send `!sync` as the owner.
8. **Spot-check**: `!help`, `!latest`, `!best`, `!topmail`, `!climp`, `/findtheclimp`, `!listen`, and `!newmember <your id>`.
9. **Watch the clip worker** catch up on queued clips: `docker compose logs -f clip-worker`, or run `SELECT status, count(*) FROM clips GROUP BY status;`.

To roll back, stop the new stack and start the TypeScript bot again. MongoDB was never modified.

## Local development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                               # the bot only; add --all-packages for the clip worker too
docker compose up -d postgres         # or point DATABASE_URL at your own Postgres
uv run alembic upgrade head
uv run yks-bot
```

The repository is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/): the bot is the root package (`src/yks`) and the clip worker is `clip-worker/` (`yks_clip_worker`). They share one `uv.lock` but are installed into separate images, and the worker never imports bot code.

In the Discord Developer Portal, the bot needs the **Server Members** and **Message Content** privileged intents enabled.

### Tests and linting

```sh
uv run ruff check . && uv run ruff format --check .
# Database tests run against a throwaway database (its schema is dropped and rebuilt):
TEST_DATABASE_URL=postgresql://yks:yks@localhost:5432/yks_test uv run pytest
```

Without `TEST_DATABASE_URL`, the database tests are skipped.

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on every pull request:

- **Lint:** `ruff check` and `ruff format --check`.
- **Tests:** applies the migrations to a Postgres service container, runs `alembic check` (fails if the models changed without a migration), then runs `pytest` for both the bot and the clip worker.
- **Docker build:** builds the bot and clip-worker images (without pushing), and checks that each contains only its own application.

### Adding a simple command

Most fixed-response commands are a single entry in `SIMPLE_COMMANDS` in `src/yks/bot/simple_commands.py`; put any images in `assets/`. Tests check that aliases are unique and that every image exists and is under Discord's 10 MB upload limit.

### Database migrations

After changing `src/yks/db/models.py`:

```sh
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
```

Review the generated file in `migrations/versions/` before committing.

## Layout

```
src/yks/
  config.py             settings from environment / .env
  feeds.py              podcast RSS fetching, parsing and caching
  db/                   SQLAlchemy models, engine, and queries (guilds, state, votes, tallies)
  bot/
    client.py           bot class: prefix, intents, global checks, extension loading
    checks.py           per-user cooldowns and per-guild rate gate
    triggers.py         regex "easter egg" triggers on ordinary messages
    simple_commands.py  data table of config-driven commands (text/embed/file responses)
    welcome_image.py    the "JF has blessed your timeline" image (Pillow)
    episodes.py         embeds for !latest, !today and new-episode announcements
    cogs/               one module per feature
assets/                 images the bot posts, plus the bundled DejaVu Sans font
clip-worker/            the transcription sidecar (own package, Dockerfile, tests)
migrations/             Alembic migrations
tests/                  data/ holds sample RSS feeds used by the tests
```
