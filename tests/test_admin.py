"""The web admin panel under /admin: Discord sign-in, the status page and clip management."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import discord
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from yks.bot.admin.auth import SESSION_COOKIE, STATE_COOKIE, Signer, safe_next
from yks.bot.client import YKSBot
from yks.bot.web import build_app
from yks.config import Settings
from yks.db import Clip, ClipStatus, Database
from yks.db.clips import NewClip, record_clips

from .fakes import FakeAttachment, FakeChannel, FakeGuild, FakeMember, FakeUser
from .harness import make_bot

SECRET = "test-secret"
OWNER = 1001
signer = Signer(SECRET)


def session_cookie(user_id: int = OWNER, *, expires_in: int = 3600) -> str:
    return signer.sign(
        "session", {"uid": str(user_id), "name": f"user{user_id}", "exp": time.time() + expires_in}
    )


def as_user(user_id: int = OWNER) -> tuple[dict[str, str], str]:
    """Request headers for a signed-in user, and the matching form token."""
    cookie = session_cookie(user_id)
    return {"Cookie": f"{SESSION_COOKIE}={cookie}"}, signer.csrf_token(cookie)


@pytest.fixture
def guild() -> FakeGuild:
    return FakeGuild()


@pytest.fixture
def clip_channel(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild, name="clips")


def discord_api(request: httpx.Request) -> httpx.Response:
    """Discord's OAuth2 token endpoint and /users/@me. Code "owner" signs in as OWNER."""
    if request.url.path.endswith("/oauth2/token"):
        code = parse_qs(request.content.decode())["code"][0]
        return httpx.Response(200, json={"access_token": f"token-{code}"})
    if request.url.path.endswith("/users/@me"):
        token = request.headers["Authorization"].removeprefix("Bearer token-")
        user_id = OWNER if token == "owner" else 2002
        return httpx.Response(200, json={"id": str(user_id), "username": "pat", "avatar": None})
    return httpx.Response(404)


async def _make(
    settings: Settings, db: Database, guild: FakeGuild, monkeypatch: pytest.MonkeyPatch
) -> YKSBot:
    instance = await make_bot(settings, db)
    instance.get_cog("Tallies").prune_mailbag.cancel()
    monkeypatch.setattr(instance, "get_channel", guild.get_channel)
    monkeypatch.setattr(instance, "get_guild", lambda gid: guild if gid == guild.id else None)
    await instance.http_client.aclose()
    instance.http_client = httpx.AsyncClient(transport=httpx.MockTransport(discord_api))
    return instance


def admin_settings(guild: FakeGuild, clip_channel: FakeChannel, **extra: Any) -> Settings:
    return Settings(
        _env_file=None,
        owner_id=OWNER,
        yks_guild_id=guild.id,
        yks_clip_channel_id=clip_channel.id,
        admin_base_url="https://bot.example.com",
        admin_client_id=42,
        admin_client_secret="shh",
        admin_session_secret=SECRET,
        **extra,
    )


# --- sign-in (no database) --------------------------------------------------------------------


@pytest.fixture
async def offline_bot(
    guild: FakeGuild, clip_channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[YKSBot]:
    settings = admin_settings(guild, clip_channel)
    db = Database(settings.database_url)  # never connected
    instance = await _make(settings, db, guild, monkeypatch)
    yield instance
    await instance.http_client.aclose()
    await db.dispose()


@pytest.fixture
async def offline(offline_bot: YKSBot) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(build_app(offline_bot))) as client:
        yield client


async def test_admin_is_off_without_oauth_settings(
    guild: FakeGuild, clip_channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None, owner_id=OWNER)
    db = Database(settings.database_url)
    bot = await _make(settings, db, guild, monkeypatch)
    async with TestClient(TestServer(build_app(bot))) as client:
        response = await client.get("/admin/", headers=as_user()[0])
        assert response.status == 503
    await bot.http_client.aclose()
    await db.dispose()


async def test_bare_admin_path_redirects(offline: TestClient) -> None:
    response = await offline.get("/admin", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/admin/"


async def test_signed_out_visitors_go_to_login(offline: TestClient) -> None:
    response = await offline.get("/admin/clips?status=failed", allow_redirects=False)
    assert response.status == 302
    location = urlparse(response.headers["Location"])
    assert location.path == "/admin/login"
    assert parse_qs(location.query) == {"next": ["/admin/clips?status=failed"]}


async def test_signed_out_posts_are_refused(offline: TestClient) -> None:
    response = await offline.post("/admin/clips/retry-failed", data={"csrf": "x"})
    assert response.status == 401


async def test_expired_or_forged_sessions_are_ignored(offline: TestClient) -> None:
    expired = session_cookie(expires_in=-1)
    forged = Signer("other-secret").sign("session", {"uid": str(OWNER), "name": "x", "exp": 2e9})
    for cookie in (expired, forged, "garbage", session_cookie().replace(".", ".x")):
        response = await offline.get(
            "/admin/", headers={"Cookie": f"{SESSION_COOKIE}={cookie}"}, allow_redirects=False
        )
        assert response.status == 302, cookie


async def login_and_callback(client: TestClient, code: str) -> Any:
    start = await client.get("/admin/login?next=/admin/clips", allow_redirects=False)
    assert start.status == 302
    authorize = urlparse(start.headers["Location"])
    params = parse_qs(authorize.query)
    assert authorize.netloc == "discord.com"
    assert params["client_id"] == ["42"]
    assert params["scope"] == ["identify"]
    assert params["redirect_uri"] == ["https://bot.example.com/admin/callback"]
    state_cookie = start.cookies[STATE_COOKIE].value
    return await client.get(
        f"/admin/callback?code={code}&state={params['state'][0]}",
        headers={"Cookie": f"{STATE_COOKIE}={state_cookie}"},
        allow_redirects=False,
    )


async def test_owner_signs_in_with_discord(offline: TestClient) -> None:
    response = await login_and_callback(offline, "owner")
    assert response.status == 302
    assert response.headers["Location"] == "/admin/clips"
    morsel = response.cookies[SESSION_COOKIE]
    assert morsel["httponly"] and morsel["secure"] and morsel["path"] == "/admin"
    session = signer.unsign("session", morsel.value)
    assert session is not None and session["uid"] == str(OWNER)


async def test_non_admins_cannot_sign_in(offline: TestClient) -> None:
    response = await login_and_callback(offline, "someone-else")
    assert response.status == 403
    assert SESSION_COOKIE not in response.cookies


async def test_manage_server_members_are_admins(
    offline_bot: YKSBot, offline: TestClient, guild: FakeGuild
) -> None:
    guild.members.append(
        FakeMember(id=2002, guild=guild, guild_permissions=discord.Permissions(manage_guild=True))
    )
    response = await login_and_callback(offline, "someone-else")
    assert response.status == 302
    # ...until they lose the permission: checked on every request, not just at sign-in.
    headers, _ = as_user(2002)
    guild.members.clear()
    assert (await offline.get("/admin/", headers=headers, allow_redirects=False)).status == 403


async def test_admin_user_ids_may_sign_in(offline_bot: YKSBot, offline: TestClient) -> None:
    offline_bot.settings = offline_bot.settings.model_copy(update={"admin_user_ids": {2002}})
    assert (await login_and_callback(offline, "someone-else")).status == 302


async def test_callback_rejects_a_bad_state(offline: TestClient) -> None:
    start = await offline.get("/admin/login", allow_redirects=False)
    state_cookie = start.cookies[STATE_COOKIE].value
    response = await offline.get(
        "/admin/callback?code=owner&state=not-the-state",
        headers={"Cookie": f"{STATE_COOKIE}={state_cookie}"},
    )
    assert response.status == 400
    assert (await offline.get("/admin/callback?code=owner&state=x")).status == 400


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/admin/clips?q=1", "/admin/clips?q=1"),
        ("https://evil.example/admin/", "/admin/"),
        ("//evil.example/admin/", "/admin/"),
        ("/elsewhere", "/admin/"),
        (None, "/admin/"),
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    assert safe_next(value) == expected


async def test_posts_need_the_form_token(offline: TestClient) -> None:
    headers, _ = as_user()
    response = await offline.post("/admin/backfill", data={"csrf": "wrong"}, headers=headers)
    assert response.status == 403
    response = await offline.post("/admin/backfill", data={}, headers=headers)
    assert response.status == 403


async def test_logout_clears_the_session(offline: TestClient) -> None:
    headers, token = as_user()
    response = await offline.post(
        "/admin/logout", data={"csrf": token}, headers=headers, allow_redirects=False
    )
    assert response.status == 302
    assert response.cookies[SESSION_COOKIE].value == ""


# --- pages (needs Postgres) -------------------------------------------------------------------


@pytest.fixture
async def bot(
    db: Database,
    database_url: str,
    guild: FakeGuild,
    clip_channel: FakeChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[YKSBot]:
    instance = await _make(
        admin_settings(guild, clip_channel, database_url=database_url), db, guild, monkeypatch
    )
    yield instance
    await instance.http_client.aclose()


@pytest.fixture
async def client(bot: YKSBot) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(build_app(bot))) as test_client:
        yield test_client


async def add_clips(db: Database, clip_channel: FakeChannel, *attachments: FakeAttachment) -> None:
    message = clip_channel.post(author=FakeUser(), attachments=list(attachments))
    async with db.session() as session:
        await record_clips(
            session,
            [
                NewClip(
                    clip_channel.guild.id,  # type: ignore[union-attr]
                    clip_channel.id,
                    message.id,
                    a.id,
                    message.author.id,
                    a.filename,
                    a.content_type,
                    a.url,
                )
                for a in attachments
            ],
        )


async def clips_by_name(db: Database) -> dict[str, Clip]:
    async with db.session() as session:
        return {c.filename: c for c in (await session.scalars(select(Clip))).all()}


async def set_clip(db: Database, name: str, **values: Any) -> None:
    async with db.session() as session:
        clip = await session.scalar(select(Clip).where(Clip.filename == name))
        assert clip is not None
        for key, value in values.items():
            setattr(clip, key, value)


async def test_status_page(client: TestClient, db: Database, clip_channel: FakeChannel) -> None:
    await add_clips(db, clip_channel, FakeAttachment("a.mp4"), FakeAttachment("b.mp4"))
    await set_clip(db, "b.mp4", status=ClipStatus.FAILED, last_error="boom")
    response = await client.get("/admin/", headers=as_user()[0])
    assert response.status == 200
    page = await response.text()
    assert "Clip transcription" in page
    assert "Retry 1 failed" in page
    assert "#clips" in page
    assert "user1001" in page  # the signed-in admin


async def test_clip_list_filters_and_searches(
    client: TestClient, db: Database, clip_channel: FakeChannel
) -> None:
    await add_clips(db, clip_channel, FakeAttachment("jingle.mp4"), FakeAttachment("broken.mp4"))
    await set_clip(
        db,
        "jingle.mp4",
        status=ClipStatus.DONE,
        transcription="the kickstarter sucks <b>jingle</b>",
    )
    await set_clip(db, "broken.mp4", status=ClipStatus.FAILED, last_error="ffmpeg exploded")
    headers = as_user()[0]

    async def listing(query: str) -> str:
        response = await client.get(f"/admin/clips?{query}", headers=headers)
        assert response.status == 200
        return await response.text()

    everything = await listing("")
    assert "jingle.mp4" in everything and "broken.mp4" in everything
    assert "<b>jingle</b>" not in everything  # transcripts are escaped
    failed = await listing("status=failed")
    assert "broken.mp4" in failed and "jingle.mp4" not in failed
    assert "Retry all 1 failed" in failed
    searched = await listing("q=kickstarters")  # stemmed full-text search
    assert "jingle.mp4" in searched and "broken.mp4" not in searched
    assert "broken.mp4" in await listing("q=exploded")  # errors are searchable too
    assert "No clips match" in await listing("status=nonsense&deleted=only")


async def test_clip_detail_retry_edit_and_hide(
    client: TestClient, db: Database, clip_channel: FakeChannel
) -> None:
    await add_clips(db, clip_channel, FakeAttachment("x.mp4"))
    await set_clip(db, "x.mp4", status=ClipStatus.FAILED, attempts=3, last_error="nope")
    clip_id = (await clips_by_name(db))["x.mp4"].id
    headers, token = as_user()

    page = await client.get(f"/admin/clips/{clip_id}", headers=headers)
    assert page.status == 200 and "nope" in await page.text()

    response = await client.post(
        f"/admin/clips/{clip_id}/retry",
        data={"csrf": token},
        headers={**headers, "Referer": f"http://test/admin/clips/{clip_id}?flash=old"},
        allow_redirects=False,
    )
    assert response.status == 302
    assert response.headers["Location"].startswith(f"/admin/clips/{clip_id}?flash=Queued")
    clip = (await clips_by_name(db))["x.mp4"]
    assert (clip.status, clip.attempts, clip.last_error) == (ClipStatus.PENDING, 0, None)

    await client.post(
        f"/admin/clips/{clip_id}/transcription",
        data={"csrf": token, "transcription": "  fixed by hand  "},
        headers=headers,
    )
    clip = (await clips_by_name(db))["x.mp4"]
    assert clip.status == ClipStatus.DONE
    assert clip.transcription == "fixed by hand"
    assert clip.transcribed_at is not None

    await client.post(
        f"/admin/clips/{clip_id}/deleted", data={"csrf": token, "deleted": "1"}, headers=headers
    )
    assert (await clips_by_name(db))["x.mp4"].deleted is True
    await client.post(
        f"/admin/clips/{clip_id}/deleted", data={"csrf": token, "deleted": "0"}, headers=headers
    )
    assert (await clips_by_name(db))["x.mp4"].deleted is False


async def test_clips_being_transcribed_are_left_alone(
    client: TestClient, db: Database, clip_channel: FakeChannel
) -> None:
    await add_clips(db, clip_channel, FakeAttachment("busy.mp4"))
    await set_clip(db, "busy.mp4", status=ClipStatus.PROCESSING)
    clip_id = (await clips_by_name(db))["busy.mp4"].id
    headers, token = as_user()
    for action, data in (("retry", {}), ("transcription", {"transcription": "x"})):
        await client.post(
            f"/admin/clips/{clip_id}/{action}", data={"csrf": token, **data}, headers=headers
        )
    clip = (await clips_by_name(db))["busy.mp4"]
    assert clip.status == ClipStatus.PROCESSING and clip.transcription is None


async def test_retry_all_failed(
    client: TestClient, db: Database, clip_channel: FakeChannel
) -> None:
    await add_clips(
        db,
        clip_channel,
        FakeAttachment("f1.mp4"),
        FakeAttachment("f2.mp4"),
        FakeAttachment("ok.mp4"),
    )
    await set_clip(db, "f1.mp4", status=ClipStatus.FAILED, attempts=3)
    await set_clip(db, "f2.mp4", status=ClipStatus.FAILED, attempts=3, deleted=True)
    await set_clip(db, "ok.mp4", status=ClipStatus.DONE, transcription="fine")
    headers, token = as_user()
    response = await client.post(
        "/admin/clips/retry-failed", data={"csrf": token}, headers=headers, allow_redirects=False
    )
    assert "Re-queued+1" in response.headers["Location"]
    statuses = {name: c.status for name, c in (await clips_by_name(db)).items()}
    assert statuses == {
        "f1.mp4": ClipStatus.PENDING,
        "f2.mp4": ClipStatus.FAILED,  # hidden clips stay put
        "ok.mp4": ClipStatus.DONE,
    }


async def test_media_redirects_to_a_fresh_url(
    client: TestClient, db: Database, clip_channel: FakeChannel
) -> None:
    attachment = FakeAttachment("play.mp3", content_type="audio/mpeg")
    await add_clips(db, clip_channel, attachment)
    await set_clip(db, "play.mp3", url="https://cdn.discordapp.com/expired")
    clip_id = (await clips_by_name(db))["play.mp3"].id
    headers = as_user()[0]
    response = await client.get(
        f"/admin/clips/{clip_id}/media", headers=headers, allow_redirects=False
    )
    assert response.status == 302
    assert response.headers["Location"] == attachment.url
    assert (await clips_by_name(db))["play.mp3"].url == attachment.url

    clip_channel.stored.clear()  # the message was deleted on Discord
    response = await client.get(f"/admin/clips/{clip_id}/media", headers=headers)
    assert response.status == 404


async def test_unknown_clip_is_404(client: TestClient) -> None:
    assert (await client.get("/admin/clips/999999", headers=as_user()[0])).status == 404
