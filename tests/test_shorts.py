"""!short: a random YouTube Short from the table the short-worker fills (needs Postgres)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from yks.bot.client import YKSBot
from yks.bot.cogs.shorts import NO_SHORTS
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
    await add_shorts(db, "aaaaaaaaaaa")
    channel = FakeChannel(guild=FakeGuild())
    await say(bot, channel, "!short", FakeUser())
    assert [s.content for s in channel.sent] == ["https://www.youtube.com/shorts/aaaaaaaaaaa"]
    await bot.http_client.aclose()


def test_help_lists_short() -> None:
    from yks.bot.cogs.help import build_help_embed

    embed = build_help_embed("other", clip_channel_id=None, shorts_channel_id=672146620275621918)
    assert any(f.name == "`!short`" and "<#672146620275621918>" in f.value for f in embed.fields)
