"""!short and posting new shorts, from the table the short-worker fills (needs Postgres)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from sqlalchemy import select

from yks.bot.client import YKSBot
from yks.bot.cogs.shorts import MAX_PER_POLL, NO_SHORTS
from yks.config import Settings
from yks.db import Database, YouTubeShort

from .fakes import FakeChannel, FakeGuild, FakeUser
from .harness import make_bot, patch_context_send, say


@pytest.fixture
def guild() -> FakeGuild:
    return FakeGuild()


@pytest.fixture
def shorts_channel(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild, name="yks-shit")


@pytest.fixture
async def bot(
    db: Database, database_url: str, shorts_channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[YKSBot]:
    patch_context_send(monkeypatch)
    settings = Settings(
        _env_file=None, database_url=database_url, yks_shorts_channel_id=shorts_channel.id
    )
    instance = await make_bot(settings, db)
    instance.get_cog("Tallies").prune_mailbag.cancel()
    instance.get_cog("Shorts").announce_new.cancel()  # tests drive announce_pending() directly
    monkeypatch.setattr(
        instance, "get_channel", lambda cid: shorts_channel if cid == shorts_channel.id else None
    )
    yield instance
    await instance.http_client.aclose()


async def add_shorts(db: Database, *video_ids: str) -> None:
    async with db.session() as session:
        session.add_all(YouTubeShort(video_id=v, title=f"Short {v}") for v in video_ids)


async def test_short_posts_a_random_short(
    bot: YKSBot, db: Database, shorts_channel: FakeChannel
) -> None:
    await add_shorts(db, "aaaaaaaaaaa", "bbbbbbbbbbb")
    owner = FakeUser(id=bot.settings.owner_id)  # skips the cooldown
    for _ in range(10):
        await say(bot, shorts_channel, "!short", owner)
    urls = {s.content for s in shorts_channel.sent}
    assert urls <= {
        "https://www.youtube.com/shorts/aaaaaaaaaaa",
        "https://www.youtube.com/shorts/bbbbbbbbbbb",
    }
    assert len(shorts_channel.sent) == 10
    assert all(s.reply_to is not None for s in shorts_channel.sent)  # replies to the asker


async def test_short_with_no_shorts(bot: YKSBot, shorts_channel: FakeChannel) -> None:
    await say(bot, shorts_channel, "!short", FakeUser())
    assert [s.content for s in shorts_channel.sent] == [NO_SHORTS]
    assert shorts_channel.sent[0].reply_to is not None


async def test_short_is_ignored_in_other_channels(
    bot: YKSBot, db: Database, guild: FakeGuild, shorts_channel: FakeChannel
) -> None:
    await add_shorts(db, "aaaaaaaaaaa")
    general = FakeChannel(guild=guild, name="general")
    user = FakeUser()
    await say(bot, general, "!short", user)
    assert general.sent == []
    # ...and doesn't use up the cooldown.
    await say(bot, shorts_channel, "!short", user)
    assert len(shorts_channel.sent) == 1


async def test_short_works_anywhere_without_a_channel(
    db: Database, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_context_send(monkeypatch)
    bot = await make_bot(Settings(_env_file=None, database_url=database_url), db)
    bot.get_cog("Tallies").prune_mailbag.cancel()
    assert not bot.get_cog("Shorts").announce_new.is_running()  # no channel: no poller
    await add_shorts(db, "aaaaaaaaaaa")
    channel = FakeChannel(guild=FakeGuild())
    await say(bot, channel, "!short", FakeUser())
    assert [s.content for s in channel.sent] == ["https://www.youtube.com/shorts/aaaaaaaaaaa"]
    await bot.http_client.aclose()


def test_help_lists_short() -> None:
    from yks.bot.cogs.help import build_help_embed

    embed = build_help_embed("other", clip_channel_id=None, shorts_channel_id=672146620275621918)
    assert any(f.name == "`!short`" and "<#672146620275621918>" in f.value for f in embed.fields)


# --- posting new shorts ---------------------------------------------------------------------


def ago(**kwargs: float) -> datetime:
    return datetime.now(UTC) - timedelta(**kwargs)


async def add_pending(db: Database, video_id: str, published: datetime | None = None) -> None:
    """A short the worker recorded and left for the bot to post."""
    async with db.session() as session:
        session.add(YouTubeShort(video_id=video_id, title=video_id, published_at=published))


async def stored(db: Database) -> dict[str, YouTubeShort]:
    async with db.session() as session:
        return {s.video_id: s for s in (await session.scalars(select(YouTubeShort))).all()}


def cog(bot: YKSBot) -> Any:
    return bot.get_cog("Shorts")


async def test_posts_new_shorts_once_oldest_first(
    bot: YKSBot, db: Database, shorts_channel: FakeChannel
) -> None:
    await add_pending(db, "newer000001", ago(minutes=1))
    await add_pending(db, "older000001", ago(minutes=30))
    assert await cog(bot).announce_pending() == ["older000001", "newer000001"]
    assert [s.content for s in shorts_channel.sent] == [
        "New short! https://www.youtube.com/shorts/older000001",
        "New short! https://www.youtube.com/shorts/newer000001",
    ]
    assert all(s.reply_to is None for s in shorts_channel.sent)  # a plain post
    rows = await stored(db)
    assert rows["older000001"].announced_at is not None
    assert rows["older000001"].announced_message_id == shorts_channel.sent[0].message.id
    assert await cog(bot).announce_pending() == []  # never again
    assert len(shorts_channel.sent) == 2


async def test_concurrent_polls_never_post_a_short_twice(
    bot: YKSBot, db: Database, shorts_channel: FakeChannel
) -> None:
    for n in range(4):
        await add_pending(db, f"short00000{n}", ago(minutes=10 - n))
    results = await asyncio.gather(*(cog(bot).announce_pending() for _ in range(4)))
    posted = [video_id for result in results for video_id in result]
    assert sorted(posted) == [f"short00000{n}" for n in range(4)]
    contents = [s.content for s in shorts_channel.sent]
    assert len(contents) == len(set(contents)) == 4


async def test_skips_undated_and_already_announced_shorts(
    bot: YKSBot, db: Database, shorts_channel: FakeChannel
) -> None:
    await add_pending(db, "undated0001", None)  # waiting for the RSS feed to date it
    async with db.session() as session:
        session.add(
            YouTubeShort(video_id="seeded00001", published_at=ago(days=1), announced_at=ago(days=1))
        )
    assert await cog(bot).announce_pending() == []
    assert shorts_channel.sent == []


async def test_at_most_a_few_per_poll(bot: YKSBot, db: Database) -> None:
    for n in range(MAX_PER_POLL + 2):
        await add_pending(db, f"burst{n:06d}", ago(minutes=60 - n))
    assert len(await cog(bot).announce_pending()) == MAX_PER_POLL
    assert len(await cog(bot).announce_pending()) == 2


def failing_send(status: int) -> Any:
    async def send(content: Any = None, **kwargs: Any) -> Any:
        response = SimpleNamespace(status=status, reason="nope")
        raise discord.HTTPException(response, {"code": 0, "message": "nope"})

    return send


async def test_refused_post_is_retried(
    bot: YKSBot, db: Database, shorts_channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_pending(db, "short000001", ago(minutes=1))
    real_send = shorts_channel.send
    monkeypatch.setattr(shorts_channel, "send", failing_send(403))  # e.g. missing permissions
    assert await cog(bot).announce_pending() == []
    assert (await stored(db))["short000001"].announced_at is None  # released
    monkeypatch.setattr(shorts_channel, "send", real_send)
    assert await cog(bot).announce_pending() == ["short000001"]


async def test_discord_server_error_is_not_retried(
    bot: YKSBot, db: Database, shorts_channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5xx might still have posted the message: retrying could post it twice."""
    await add_pending(db, "short000001", ago(minutes=1))
    real_send = shorts_channel.send
    monkeypatch.setattr(shorts_channel, "send", failing_send(503))
    assert await cog(bot).announce_pending() == []
    monkeypatch.setattr(shorts_channel, "send", real_send)
    assert await cog(bot).announce_pending() == []
    row = (await stored(db))["short000001"]
    assert row.announced_at is not None and row.announced_message_id is None


async def test_missing_channel_claims_nothing(
    bot: YKSBot, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_pending(db, "short000001", ago(minutes=1))
    monkeypatch.setattr(bot, "get_channel", lambda cid: None)
    assert await cog(bot).announce_pending() == []
    assert (await stored(db))["short000001"].announced_at is None
