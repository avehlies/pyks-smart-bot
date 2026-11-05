"""The bot's HTTP server: GET /healthz."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp.test_utils import TestClient, TestServer

from yks.bot.client import YKSBot
from yks.bot.web import build_app
from yks.config import Settings
from yks.db import Database

from .harness import make_bot


@pytest.fixture
async def bot() -> AsyncIterator[YKSBot]:
    settings = Settings(_env_file=None)
    db = Database(settings.database_url)  # unused by the endpoint
    instance = await make_bot(settings, db)
    instance.get_cog("Tallies").prune_mailbag.cancel()
    yield instance
    await instance.http_client.aclose()
    await db.dispose()


@pytest.fixture
async def client(bot: YKSBot) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(build_app(bot))) as test_client:
        yield test_client


async def test_healthz(client: TestClient) -> None:
    response = await client.get("/healthz")
    assert response.status == 200
    assert (await response.json())["ok"] is True


async def test_share_clip_is_gone(client: TestClient) -> None:
    response = await client.post("/api/share-clip", json={})
    assert response.status == 404
