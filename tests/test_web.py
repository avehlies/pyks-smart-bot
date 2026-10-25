"""The bot's HTTP API: POST /api/share-clip and GET /healthz."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from yks.bot.client import YKSBot
from yks.bot.web import build_app
from yks.config import Settings
from yks.db import Database

from .fakes import FakeChannel, FakeGuild
from .harness import make_bot

MEDIA = {
    "https://cdn.discordapp.com/attachments/1/2/small.mp4": b"small clip",
    "https://cdn.discordapp.com/attachments/1/3/huge.mp4": b"x" * (11 * 1024 * 1024),
}


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def clip_channel() -> FakeChannel:
    return FakeChannel(guild=FakeGuild(), name="clips")


@pytest.fixture
async def bot(clip_channel: FakeChannel, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[YKSBot]:
    settings = Settings(
        _env_file=None,
        yks_clip_channel_id=clip_channel.id,
        bot_auth_username="site",
        bot_auth_password="s3cret",
    )
    db = Database(settings.database_url)  # unused by the API
    instance = await make_bot(settings, db)
    instance.get_cog("Tallies").prune_mailbag.cancel()
    monkeypatch.setattr(instance, "get_channel", clip_channel.guild.get_channel)
    await instance.http_client.aclose()
    instance.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, content=MEDIA[str(r.url)])
                if str(r.url) in MEDIA
                else httpx.Response(404)
            )
        )
    )
    yield instance
    await instance.http_client.aclose()
    await db.dispose()


@pytest.fixture
async def client(bot: YKSBot) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(build_app(bot))) as test_client:
        yield test_client


async def test_share_clip_posts_file_and_credit(
    client: TestClient, clip_channel: FakeChannel
) -> None:
    body = {"userId": "1234", "page": "jf/best of", "url": next(iter(MEDIA))}
    response = await client.post("/api/share-clip", json=body, headers=basic("site", "s3cret"))
    assert response.status == 200
    assert await response.json() == {"ok": True}
    (sent,) = clip_channel.sent
    assert sent.kwargs["embed"].description == (
        "<@1234> shared a clip from https://im-at.work/clips/jf/best%20of"
    )
    assert sent.kwargs["file"].filename == "small.mp4"
    assert sent.file_bytes == [b"small clip"]


async def test_share_clip_links_big_files(client: TestClient, clip_channel: FakeChannel) -> None:
    url = list(MEDIA)[1]
    body = {"userId": "1", "page": "p", "url": url}
    response = await client.post("/api/share-clip", json=body, headers=basic("site", "s3cret"))
    assert response.status == 200
    assert clip_channel.sent[0].content == url
    assert "file" not in clip_channel.sent[0].kwargs


@pytest.mark.parametrize(
    ("headers", "status", "message"),
    [
        ({}, 401, "Missing Authorization Header"),
        (basic("site", "wrong"), 401, "Invalid credentials."),
        ({"Authorization": "Basic !!!"}, 401, "Invalid credentials."),
    ],
)
async def test_share_clip_auth(
    client: TestClient, clip_channel: FakeChannel, headers: dict, status: int, message: str
) -> None:
    body = {"userId": "1", "page": "p", "url": next(iter(MEDIA))}
    response = await client.post("/api/share-clip", json=body, headers=headers)
    assert response.status == status
    assert (await response.json())["message"] == message
    assert clip_channel.sent == []


@pytest.mark.parametrize(
    "body",
    [
        {"page": "p", "url": "https://cdn.discordapp.com/a.mp4"},
        {"userId": "<@&everyone>", "page": "p", "url": "https://cdn.discordapp.com/a.mp4"},
        {"userId": "1", "page": "p", "url": "http://cdn.discordapp.com/a.mp4"},
        ["not", "an", "object"],
    ],
)
async def test_share_clip_bad_requests(
    client: TestClient, clip_channel: FakeChannel, body: object
) -> None:
    response = await client.post("/api/share-clip", json=body, headers=basic("site", "s3cret"))
    assert response.status == 400
    assert clip_channel.sent == []


async def test_share_clip_disabled_without_credentials(bot: YKSBot, client: TestClient) -> None:
    bot.settings = bot.settings.model_copy(update={"bot_auth_password": ""})
    response = await client.post("/api/share-clip", json={}, headers=basic("site", ""))
    assert response.status == 503


async def test_healthz(client: TestClient) -> None:
    response = await client.get("/healthz")
    assert response.status == 200
    assert (await response.json())["ok"] is True
