# pyks-smart-bot

A Python rewrite of [yks-smart-bot](https://github.com/avehlies/yks-smart-bot), the Your Kickstarter Sucks Discord bot. See [PLAN.md](PLAN.md) for the roadmap.

## Running with Docker

```sh
cp .env.example .env   # then fill in AUTH_TOKEN and the IDs
docker compose up --build
```

This starts Postgres, applies migrations (`migrate` service), and starts the bot.

## Local development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
docker compose up -d postgres         # or point DATABASE_URL at your own Postgres
uv run alembic upgrade head
uv run yks-bot
```

In the Discord Developer Portal, the bot needs the **Server Members** and **Message Content** privileged intents enabled.

### Tests and linting

```sh
uv run ruff check . && uv run ruff format --check .
# Database tests run against a throwaway database (its schema is dropped and rebuilt):
TEST_DATABASE_URL=postgresql://yks:yks@localhost:5432/yks_test uv run pytest
```

Without `TEST_DATABASE_URL`, the database tests are skipped.

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
  config.py        settings from environment / .env
  db/              SQLAlchemy models and engine
  bot/
    client.py      bot class: prefix, intents, global checks, extension loading
    checks.py      per-user cooldowns and per-guild rate gate
    cogs/          one module per feature
migrations/        Alembic migrations
tests/
```
