"""Phase 8: !listen voice playback, with fake voice connections and audio."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from yks.bot.client import YKSBot
from yks.bot.listen import (
    TrackedSource,
    colon_time,
    parse_duration_ms,
    progress_field,
    select_episode,
    short_time,
    title_fields,
)
from yks.config import Settings
from yks.db import Database
from yks.feeds import FeedClient, parse_feed

from .fakes import FakeAudio, FakeChannel, FakeGuild, FakeMember, FakeUser, FakeVoiceChannel
from .harness import make_bot, patch_context_send, say

DATA = Path(__file__).parent / "data"
MAIN_URL = "https://feeds.test/main"
AUDIO = b"ID3 fake mp3"


# --- pure helpers ---------------------------------------------------------------------------


def test_durations_and_times() -> None:
    assert parse_duration_ms("01:12:34") == (3600 + 12 * 60 + 34) * 1000
    assert parse_duration_ms("58:01") == (58 * 60 + 1) * 1000
    assert parse_duration_ms("3600") == 3_600_000
    assert parse_duration_ms(None) == 0
    assert parse_duration_ms("soon") == 0
    assert [colon_time(ms) for ms in (0, 65_000, 3_723_000)] == ["0:00", "1:05", "1:02:03"]
    assert [short_time(ms) for ms in (45_000, 83_456, 3_723_000)] == ["45s", "1m 23s", "1h 2m 3s"]


def test_progress_field() -> None:
    assert progress_field(0, 60_000) == ("Progress (0:00 / 1:00)", "\\|🟢" + "-" * 24 + "\\|")
    name, value = progress_field(30_000, 60_000)
    assert name == "Progress (0:30 / 1:00)"
    assert value == "\\|||" + "-" * 13 + "||🟢" + "-" * 11 + "\\|"  # ceil(50 / 4) = 13 played
    assert progress_field(999_999, 60_000)[1].count("-") == 24  # never past the end
    assert progress_field(5_000, 0)[0] == "Progress (0:05 / ?)"


def test_title_fields() -> None:
    assert title_fields("Episode 412: The Smart Mug For Dogs") == (
        "Episode 412",
        "The Smart Mug For Dogs",
    )
    assert title_fields("Ep 410: The Jetpack") == ("Episode 410", "The Jetpack")
    assert title_fields("Live in Nashville") == ("Now playing", "Live in Nashville")


def test_select_episode() -> None:
    feed = parse_feed((DATA / "main_feed.xml").read_bytes())
    assert select_episode(feed, action="play", number=0).episode.title.startswith("Episode 412")
    assert select_episode(feed, action="play", number=101).episode.title.startswith("Episode 101")
    missing = select_episode(feed, action="play", number=999)
    assert (missing.episode, missing.requested) == (None, 999)
    picks = {
        select_episode(feed, action="random", number=0, rng=random.Random(seed)).episode.title
        for seed in range(30)
    }
    # Random picks only numbered episodes (never the "Bonus: Live in Nashville" item).
    assert picks <= {
        ep.title for ep in feed.episodes if ":" in ep.title and "Bonus" not in ep.title
    }
    assert len(picks) > 1


def test_tracked_source_counts_frames() -> None:
    source = TrackedSource(FakeAudio(frames=3))
    while source.read():
        pass
    assert source.position_ms == 60
    source.cleanup()
    assert source.inner.cleaned_up


# --- the cog --------------------------------------------------------------------------------


@pytest.fixture
def guild() -> FakeGuild:
    return FakeGuild()


@pytest.fixture
def channel(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild, name="podcast-chat")


@pytest.fixture
def voice_channel() -> FakeVoiceChannel:
    return FakeVoiceChannel(name="Listening Party")


@pytest.fixture
def listener(guild: FakeGuild, voice_channel: FakeVoiceChannel) -> FakeMember:
    member = FakeMember(guild=guild)
    member.voice = SimpleNamespace(channel=voice_channel)  # type: ignore[attr-defined]
    return member


@pytest.fixture
async def bot(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[YKSBot]:
    patch_context_send(monkeypatch)
    settings = Settings(_env_file=None, main_feed_rss=MAIN_URL)
    db = Database(settings.database_url)  # unused
    instance = await make_bot(settings, db)
    instance.get_cog("Feeds").watch_feeds.cancel()
    instance.get_cog("Tallies").prune_mailbag.cancel()

    async def fetch(url: str) -> bytes:
        assert url == MAIN_URL
        return (DATA / "main_feed.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/broken.mp3"):
            return httpx.Response(500)
        return httpx.Response(200, content=AUDIO)

    await instance.http_client.aclose()
    instance.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    instance.feeds = FeedClient(fetch)
    listen = instance.get_cog("Listen")
    monkeypatch.setattr(listen, "make_source", lambda path: FakeAudio(frames=500))
    yield instance
    await instance.http_client.aclose()
    await db.dispose()


def cog(bot: YKSBot) -> Any:
    return bot.get_cog("Listen")


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def contents(channel: FakeChannel) -> list[str | None]:
    return [s.content for s in channel.sent]


async def test_needs_a_voice_channel(bot: YKSBot, channel: FakeChannel, guild: FakeGuild) -> None:
    await say(bot, channel, "!listen", FakeMember(guild=guild))
    assert contents(channel) == ["Please join a voice channel first."]


async def test_nothing_playing(bot: YKSBot, channel: FakeChannel, listener: FakeMember) -> None:
    for action in ("pause", "stop", "bogus"):
        await say(bot, channel, f"!listen {action}", FakeUser())
    assert contents(channel) == ["Nothing playing."] * 3


async def test_unknown_episode(bot: YKSBot, channel: FakeChannel, listener: FakeMember) -> None:
    await say(bot, channel, "!listen play 999", listener)
    assert contents(channel) == ["Couldn't find episode 999."]


async def test_play_session_lifecycle(
    bot: YKSBot, channel: FakeChannel, guild: FakeGuild, listener: FakeMember,
    voice_channel: FakeVoiceChannel,
) -> None:  # fmt: skip
    await say(bot, channel, "!listen play 410", listener)
    session = cog(bot).sessions[guild.id]
    (voice,) = voice_channel.clients
    assert voice.is_playing()
    assert session.temp_path.read_bytes() == AUDIO
    downloading, now_playing = channel.sent
    assert downloading.content == "Downloading episode..."
    embed = now_playing.kwargs["embed"]
    assert embed.title == "Now playing in Listening Party"
    assert (embed.fields[0].name, embed.fields[0].value) == ("Episode 410", "The Jetpack")
    assert embed.fields[1].name == "Progress (0:00 / 58:01)"
    assert [r.emoji for r in now_playing.message.reactions] == ["⏸", "⏹", "▶️"]

    for _ in range(150):  # 3 seconds of audio
        session.source.read()
    await say(bot, channel, "!listen random", FakeUser())
    await say(bot, channel, "!listen pause", FakeUser())
    assert voice.is_paused()
    await say(bot, channel, "!listen play", FakeUser())
    assert voice.is_playing()
    await say(bot, channel, "!listen dance", FakeUser())
    status = session.status
    assert [e["content"] for e in status.edits] == [
        "Paused at 3s.",
        "Resuming.",
        "Not a valid option for the command `!listen`.",
    ]
    assert status.content == "Not a valid option for the command `!listen`."

    await say(bot, channel, "!listen stop", FakeUser())
    await settle()
    assert status.content == "Finished playing episode."
    assert guild.id not in cog(bot).sessions
    assert not voice.connected
    assert not session.temp_path.exists()


async def test_quick_end_reports_failure(
    bot: YKSBot, channel: FakeChannel, guild: FakeGuild, listener: FakeMember,
    voice_channel: FakeVoiceChannel,
) -> None:  # fmt: skip
    await say(bot, channel, "!listen", listener)
    session = cog(bot).sessions[guild.id]
    voice_channel.clients[0].finish(None)  # ended before 2 s of audio
    await settle()
    assert channel.sent[-1].content == (
        "Playback failed to start (stream may be unavailable or invalid)."
    )
    assert session.finished.is_set()


async def test_player_error(
    bot: YKSBot, channel: FakeChannel, guild: FakeGuild, listener: FakeMember,
    voice_channel: FakeVoiceChannel,
) -> None:  # fmt: skip
    await say(bot, channel, "!listen", listener)
    voice_channel.clients[0].finish(RuntimeError("ffmpeg died"))
    await settle()
    assert channel.sent[-1].content == "Playback error."
    assert guild.id not in cog(bot).sessions


async def test_download_failure(
    bot: YKSBot, channel: FakeChannel, guild: FakeGuild, listener: FakeMember,
    voice_channel: FakeVoiceChannel,
) -> None:  # fmt: skip
    await say(bot, channel, "!listen url https://cdn.test/broken.mp3", FakeUser())  # not owner
    assert channel.sent == [] and voice_channel.clients == []
    owner = FakeMember(id=bot.settings.owner_id, guild=guild)
    owner.voice = SimpleNamespace(channel=voice_channel)  # type: ignore[attr-defined]
    await say(bot, channel, "!listen url https://cdn.test/broken.mp3", owner)
    assert contents(channel) == [
        "Downloading episode...",
        "Could not download audio. Check bot logs.",
    ]
    assert not voice_channel.clients[0].connected
    assert guild.id not in cog(bot).sessions


async def test_voice_connect_failure(
    bot: YKSBot, channel: FakeChannel, guild: FakeGuild, listener: FakeMember,
    voice_channel: FakeVoiceChannel,
) -> None:  # fmt: skip
    voice_channel.fail = True
    await say(bot, channel, "!listen", listener)
    assert contents(channel) == ["Failed to join the voice channel"]
    assert guild.id not in cog(bot).sessions


async def test_reaction_controls(
    bot: YKSBot, channel: FakeChannel, guild: FakeGuild, listener: FakeMember,
    voice_channel: FakeVoiceChannel, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    await say(bot, channel, "!listen", listener)
    session = cog(bot).sessions[guild.id]
    voice = voice_channel.clients[0]
    message = session.now_playing
    user = FakeUser(name="dave")

    def reaction(emoji: str, on: Any = message) -> SimpleNamespace:
        return SimpleNamespace(emoji=emoji, message=on)

    await cog(bot).on_reaction_add(reaction("⏸"), FakeUser(bot=True))  # bots ignored
    assert voice.is_playing()
    await cog(bot).on_reaction_add(reaction("⏸"), user)
    assert voice.is_paused()
    await cog(bot).on_reaction_add(reaction("▶"), user)  # without the variation selector
    assert voice.is_playing()

    # Stop asks first; declining (or no answer) keeps playing.
    answers = iter(["❌", "✅"])

    async def wait_for(event: str, *, check: Any, **_: Any) -> Any:
        prompt = channel.sent[-1].message
        answer = SimpleNamespace(emoji=next(answers), message=prompt)
        assert check(answer, user)
        assert not check(answer, FakeUser())  # only the person who asked can answer
        return answer, user

    monkeypatch.setattr(bot, "wait_for", wait_for)
    await cog(bot).on_reaction_add(reaction("⏹"), user)
    assert voice.is_playing()
    assert channel.sent[-1].content == "Are you sure you want to stop?"
    assert channel.sent[-1].message.deleted
    await cog(bot).on_reaction_add(reaction("⏹"), user)
    await settle()
    assert f"Stopped by {user.mention}" in contents(channel)
    assert guild.id not in cog(bot).sessions


async def test_sessions_are_per_guild(
    bot: YKSBot, channel: FakeChannel, guild: FakeGuild, listener: FakeMember
) -> None:
    await say(bot, channel, "!listen", listener)
    other_guild = FakeGuild()
    other_channel = FakeChannel(guild=other_guild)
    other = FakeMember(guild=other_guild)
    other.voice = SimpleNamespace(channel=FakeVoiceChannel())  # type: ignore[attr-defined]
    await say(bot, other_channel, "!listen play 101", other)
    assert set(cog(bot).sessions) == {guild.id, other_guild.id}
