"""Phase 3 end to end: !latest, !today, !best and the new-episode watcher (needs Postgres)."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from yks.bot.client import YKSBot
from yks.bot.cogs.feeds import FEED_UNAVAILABLE
from yks.config import Settings
from yks.db import Database, guilds
from yks.feeds import FeedClient

from .fakes import FakeChannel, FakeEmoji, FakeGuild, FakeUser
from .harness import make_bot, patch_context_send, say

DATA = Path(__file__).parent / "data"
MAIN_URL = "https://feeds.test/main"
BONUS_URL = "https://feeds.test/bonus"

NEW_MAIN_ITEM = """
    <item>
      <title>Episode 413: The Self-Folding Sock</title>
      <link>https://art19.com/shows/your-kickstarter-sucks/episodes/413</link>
      <pubDate>Mon, 26 Oct 2026 10:00:00 -0000</pubDate>
      <description>Socks.</description>
      <enclosure url="https://rss.art19.com/episodes/413.mp3" type="audio/mpeg" length="1"/>
    </item>"""


class FeedServer:
    """Serves the fixture feeds; tests can edit them or make them fail."""

    def __init__(self) -> None:
        self.feeds = {
            MAIN_URL: (DATA / "main_feed.xml").read_text(),
            BONUS_URL: (DATA / "bonus_feed.xml").read_text(),
        }
        self.down = False

    def add_item(self, url: str, item_xml: str) -> None:
        feed = self.feeds[url]
        first = feed.index("<item>")
        self.feeds[url] = feed[:first] + item_xml.strip() + "\n    " + feed[first:]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, text=self.feeds[str(request.url)])


@pytest.fixture
def server() -> FeedServer:
    return FeedServer()


@pytest.fixture
async def bot(
    db: Database, database_url: str, server: FeedServer, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[YKSBot]:
    patch_context_send(monkeypatch)
    settings = Settings(
        _env_file=None, database_url=database_url, main_feed_rss=MAIN_URL, bonus_feed_rss=BONUS_URL
    )
    instance = await make_bot(settings, db)
    instance.get_cog("Feeds").watch_feeds.cancel()  # tests drive check_feeds() directly
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    instance.feeds = FeedClient(http, ttl=0)  # always re-read, so edits show up
    monkeypatch.setattr(instance, "change_presence", AsyncMock())
    yield instance
    await http.aclose()
    await instance.http_client.aclose()


@pytest.fixture
def guild() -> FakeGuild:
    names = ("1ohno", "2they", "3did", "40another")  # no "yks" emoji: it is skipped
    return FakeGuild(emojis=[FakeEmoji(name) for name in names])


@pytest.fixture
def channel(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild)


def set_today(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    monkeypatch.setattr(sys.modules["yks.bot.cogs.feeds"], "local_today", lambda tz: today)


# --- !latest --------------------------------------------------------------------------------


async def test_latest_both(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!latest", FakeUser())
    main, bonus = channel.embeds
    assert main.title == "Your Kickstarter Sucks"
    assert main.fields[0].name == "Episode 412: The Smart Mug For Dogs"
    assert "[ART19](https://rss.art19.com/episodes/412.mp3)" in main.fields[0].value
    assert bonus.title == "Your Kickstarter Sucks (Patreon)"
    assert bonus.fields[0].name == "Patreon Exclusive: Mailbag 88"  # "Live in Nashville" is public


@pytest.mark.parametrize(
    ("arg", "titles"),
    [
        ("main", ["Your Kickstarter Sucks"]),
        ("patreon", ["Your Kickstarter Sucks (Patreon)"]),
        ("PREMIUM", ["Your Kickstarter Sucks (Patreon)"]),
        ("nonsense", ["Your Kickstarter Sucks", "Your Kickstarter Sucks (Patreon)"]),
    ],
)
async def test_latest_feed_choice(
    bot: YKSBot, channel: FakeChannel, arg: str, titles: list[str]
) -> None:
    await say(bot, channel, f"!eps {arg}", FakeUser())
    assert [e.title for e in channel.embeds] == titles


async def test_latest_when_feeds_are_down(
    bot: YKSBot, channel: FakeChannel, server: FeedServer
) -> None:
    server.down = True
    await say(bot, channel, "!latest", FakeUser())
    assert [s.content for s in channel.sent] == [FEED_UNAVAILABLE]


# --- !today ---------------------------------------------------------------------------------


async def test_today(bot: YKSBot, channel: FakeChannel, monkeypatch: pytest.MonkeyPatch) -> None:
    set_today(monkeypatch, date(2026, 10, 25))
    await say(bot, channel, "!today", FakeUser())
    assert [e.title for e in channel.embeds] == ["Oct 25, 2019", "Oct 25, 2021"]
    assert len(channel.sent) == 1  # both in one message


async def test_today_nothing(
    bot: YKSBot, channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_today(monkeypatch, date(2026, 3, 3))
    await say(bot, channel, "!today", FakeUser())
    assert [s.content for s in channel.sent] == ["Nothing happened on this date."]


# --- !best ----------------------------------------------------------------------------------


async def test_best_vote_and_list(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!best 410", FakeUser())
    await say(bot, channel, "!best 410", FakeUser())
    await say(bot, channel, "!best 1", FakeUser())
    await say(bot, channel, "!best", FakeUser())  # list only
    first, _, _, listing = channel.sent
    assert first.content == "You think **Episode 410** is the best."
    assert first.reply_to is not None
    assert listing.content is None
    embed = listing.kwargs["embed"]
    assert embed.title == "Top 2 Episodes"
    assert [(f.name, f.value) for f in embed.fields] == [
        ("**1.** Ep 410: The Jetpack", "2 votes"),
        ("**2.** Episode 1: Pilot", "1 vote"),
    ]


async def test_best_change_and_no_change(bot: YKSBot, channel: FakeChannel) -> None:
    owner = FakeUser(id=bot.settings.owner_id)  # the owner skips the 30-minute cooldown
    await say(bot, channel, "!best 410", owner)
    await say(bot, channel, "!best 410", owner)
    await say(bot, channel, "!best 101", owner)
    await say(bot, channel, "!best 1", owner)
    assert [s.content for s in channel.sent] == [
        "You think **Episode 410** is the best.",
        "No change.",
        "We already know Episode 101 is the best. This is for all the other ones.",
        "You thought **Episode 410** was the best. Now...\nYou think **Episode 1** is the best.",
    ]
    assert [f.value for f in channel.sent[-1].kwargs["embed"].fields] == ["1 vote"]


async def test_best_rejects_bad_numbers(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!best 9999", FakeUser())
    await say(bot, channel, "!best 0", FakeUser())
    assert [s.content for s in channel.sent] == ["Not an episode."]


async def test_best_unknown_title_still_counts(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!best 411", FakeUser())  # not in the feed, but <= latest (412)
    (field,) = channel.sent[0].kwargs["embed"].fields
    assert field.name == "**1.** title not found"


async def test_best_votes_are_per_guild(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!best 410", FakeUser())
    other = FakeChannel(guild=FakeGuild())
    await say(bot, other, "!best", FakeUser())
    assert other.sent[0].kwargs["embed"].title == "Top 0 Episodes"


# --- watcher --------------------------------------------------------------------------------


@pytest.fixture
async def rss_channel(
    bot: YKSBot, guild: FakeGuild, db: Database, monkeypatch: pytest.MonkeyPatch
) -> FakeChannel:
    target = FakeChannel(guild=guild, name="new-episodes")
    async with db.session() as session:
        await guilds.set_rss_channel(session, guild.id, target.id)
    monkeypatch.setattr(bot, "get_channel", lambda cid: target if cid == target.id else None)
    return target


async def test_first_check_records_without_announcing(
    bot: YKSBot, rss_channel: FakeChannel
) -> None:
    assert await bot.get_cog("Feeds").check_feeds() == []
    assert rss_channel.sent == []
    presence = bot.change_presence.await_args.kwargs
    assert presence["activity"].name == "Bonus: Live in Nashville"
    assert await bot.get_cog("Feeds").check_feeds() == []  # nothing new


async def test_new_main_episode_is_announced(
    bot: YKSBot, rss_channel: FakeChannel, server: FeedServer
) -> None:
    feeds = bot.get_cog("Feeds")
    await feeds.check_feeds()
    server.add_item(MAIN_URL, NEW_MAIN_ITEM)
    assert await feeds.check_feeds() == ["main"]
    (sent,) = rss_channel.sent
    assert sent.kwargs["embed"].fields[0].name == "Episode 413: The Self-Folding Sock"
    assert [r.emoji.name for r in sent.message.reactions] == ["1ohno", "2they", "3did", "40another"]


async def test_new_bonus_episode_is_announced_unless_public(
    bot: YKSBot, rss_channel: FakeChannel, server: FeedServer
) -> None:
    feeds = bot.get_cog("Feeds")
    await feeds.check_feeds()

    # A new main episode cross-posted to Patreon: announced once (as main), not twice.
    server.add_item(MAIN_URL, NEW_MAIN_ITEM)
    server.add_item(BONUS_URL, NEW_MAIN_ITEM)
    assert await feeds.check_feeds() == ["main"]

    exclusive = NEW_MAIN_ITEM.replace("Episode 413: The Self-Folding Sock", "Patreon Exclusive: 89")
    server.add_item(BONUS_URL, exclusive)
    assert await feeds.check_feeds() == ["bonus"]
    assert rss_channel.embeds[-1].fields[0].name == "Patreon Exclusive: 89"
    assert bot.change_presence.await_args.kwargs["activity"].name == "Patreon Exclusive: 89"


async def test_check_survives_feed_outage(
    bot: YKSBot, rss_channel: FakeChannel, server: FeedServer
) -> None:
    feeds = bot.get_cog("Feeds")
    await feeds.check_feeds()
    server.down = True
    assert await feeds.check_feeds() == []
    server.down = False
    server.add_item(MAIN_URL, NEW_MAIN_ITEM)
    assert await feeds.check_feeds() == ["main"]
