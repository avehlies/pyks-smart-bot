"""Shared fixtures.

Database tests need a disposable Postgres database. Point TEST_DATABASE_URL at one, e.g.

    TEST_DATABASE_URL=postgresql://yks:yks@localhost:5432/yks_test uv run pytest

The schema is dropped and rebuilt with Alembic at the start of the run. Without the variable,
database tests are skipped.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from yks.config import normalize_database_url
from yks.db import Database

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL not set")
    url = normalize_database_url(raw)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield url


@pytest.fixture(scope="session")
async def db(database_url: str) -> AsyncIterator[Database]:
    database = Database(database_url)
    yield database
    await database.dispose()


@pytest.fixture(autouse=True)
async def _clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Truncate every table after each database test."""
    yield
    if "db" not in request.fixturenames:
        return
    database: Database = request.getfixturevalue("db")
    async with database.session() as session:
        await session.execute(
            text(
                "TRUNCATE guild_settings, welcomed_members, bot_state, best_episode_votes, "
                "reaction_tallies, clips RESTART IDENTITY"
            )
        )
