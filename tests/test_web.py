"""The bot's HTTP server: GET /healthz."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from yks.bot.client import YKSBot
from yks.bot.web import Health, build_app
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


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def connect(bot: YKSBot, monkeypatch: pytest.MonkeyPatch, *, ready: bool, ws_open: bool) -> None:
    monkeypatch.setattr(bot, "is_ready", lambda: ready)
    monkeypatch.setattr(bot, "ws", SimpleNamespace(open=ws_open))


async def get(app_client: TestClient) -> tuple[int, dict]:
    response = await app_client.get("/healthz")
    return response.status, await response.json()


async def test_healthz(bot: YKSBot, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    connect(bot, monkeypatch, ready=True, ws_open=True)
    status, body = await get(client)
    assert status == 200
    assert body == {"ok": True, "discord": True, "disconnected_for": 0}


async def test_healthz_allows_startup_and_reconnects_then_fails(
    bot: YKSBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    app = build_app(bot, health=Health(bot, grace=120, clock=clock))
    async with TestClient(TestServer(app)) as client:
        # Starting up: not connected yet, but within the grace period.
        connect(bot, monkeypatch, ready=False, ws_open=False)
        clock.now += 60
        assert (await get(client))[0] == 200
        clock.now += 61
        status, body = await get(client)
        assert status == 503
        assert body == {"ok": False, "discord": False, "disconnected_for": 121}

        connect(bot, monkeypatch, ready=True, ws_open=True)
        assert (await get(client))[0] == 200

        # The gateway drops: is_ready() stays true, but the websocket is closed.
        connect(bot, monkeypatch, ready=True, ws_open=False)
        assert (await get(client))[1]["discord"] is False
        clock.now += 100
        assert (await get(client))[0] == 200  # discord.py is still reconnecting
        clock.now += 30
        assert (await get(client))[0] == 503
        connect(bot, monkeypatch, ready=True, ws_open=True)
        assert (await get(client))[0] == 200


async def test_share_clip_is_gone(client: TestClient) -> None:
    response = await client.post("/api/share-clip", json={})
    assert response.status == 404
