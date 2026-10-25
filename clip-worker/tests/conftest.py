"""Fixtures for the clip worker's tests.

Like the bot's database tests, these need TEST_DATABASE_URL (a disposable Postgres database).
The schema comes from the bot's Alembic migrations: the `clips` table is the contract between
the two applications.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from yks_clip_worker.config import asyncpg_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def worker_dsn() -> Iterator[str]:
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL not set")
    dsn = asyncpg_dsn(raw)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+asyncpg://"))
    command.upgrade(config, "head")
    yield dsn


@pytest.fixture
async def pool(worker_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(worker_dsn, min_size=1, max_size=4)
    yield pool
    await pool.execute("TRUNCATE clips RESTART IDENTITY")
    await pool.close()
