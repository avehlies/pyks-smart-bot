"""Feed parsing, the feed cache, and the episode embed helpers (no Discord, no database)."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest

from yks.bot.cogs.feeds import rank_totals
from yks.bot.episodes import (
    chunk_embeds,
    episodes_on_this_day,
    format_date,
    latest_bonus_episode,
    this_day_embed,
)
from yks.feeds import FeedClient, FeedError, parse_feed

DATA = Path(__file__).parent / "data"
CHICAGO = ZoneInfo("America/Chicago")


@pytest.fixture(scope="module")
def main_feed():
    return parse_feed((DATA / "main_feed.xml").read_bytes())


@pytest.fixture(scope="module")
def bonus_feed():
    return parse_feed((DATA / "bonus_feed.xml").read_bytes())


# --- parsing --------------------------------------------------------------------------------


def test_parse_main_feed(main_feed) -> None:
    assert main_feed.title == "Your Kickstarter Sucks"
    assert [ep.number for ep in main_feed.episodes] == [412, None, 410, 101, 1]
    latest = main_feed.latest
    assert latest.title == "Episode 412: The Smart Mug For Dogs"
    assert latest.audio_url == "https://rss.art19.com/episodes/412.mp3"
    assert latest.image_url == "https://content.production.cdn.art19.com/images/412.jpeg"
    assert latest.duration == "01:12:34"
    assert latest.published == datetime(2026, 10, 19, 10, tzinfo=UTC)
    assert latest.summary == "Jack & Dave review a mug.\nPlus: a dog."


def test_find_episode_matches_ep_and_episode_prefixes(main_feed) -> None:
    assert main_feed.find_episode(410).title == "Ep 410: The Jetpack"
    assert main_feed.find_episode(101).title == "Episode 101: The Best One"
    assert main_feed.find_episode(41) is None  # "Ep 410:" must not match 41
    assert main_feed.find_episode(999) is None


def test_garbage_is_a_feed_error() -> None:
    with pytest.raises(FeedError):
        parse_feed(b"<html><body>502 Bad Gateway")


def test_latest_bonus_skips_episodes_also_in_main(main_feed, bonus_feed) -> None:
    assert latest_bonus_episode(bonus_feed, main_feed).title == "Patreon Exclusive: Mailbag 88"
    # Without the main feed to compare against, the newest bonus item wins.
    assert latest_bonus_episode(bonus_feed, None).title == "Bonus: Live in Nashville"


# --- !today helpers -------------------------------------------------------------------------


def test_episodes_on_this_day_uses_local_dates(main_feed, bonus_feed) -> None:
    found = episodes_on_this_day(date(2026, 10, 25), CHICAGO, main_feed, bonus_feed)
    assert [(item.episode.title, item.is_bonus) for item in found] == [
        ("Episode 101: The Best One", False),
        # The cross-posted Ep 101 is skipped; "Late Night" was Oct 24 in Chicago.
        ("Patreon Exclusive: Halloween Special", True),
    ]
    utc = episodes_on_this_day(date(2026, 10, 25), ZoneInfo("UTC"), main_feed, bonus_feed)
    assert "Patreon Exclusive: Late Night" in [item.episode.title for item in utc]


def test_this_day_embed(main_feed, bonus_feed) -> None:
    main_item, bonus_item = episodes_on_this_day(date(2026, 10, 25), CHICAGO, main_feed, bonus_feed)
    embed = this_day_embed(main_item, CHICAGO)
    assert embed.title == "Oct 25, 2019"
    assert embed.thumbnail.url == "https://content.production.cdn.art19.com/images/101.jpeg"
    assert [f.name for f in embed.fields] == ["Episode 101: The Best One", "Link"]
    assert embed.fields[1].value.startswith("[Listen](")
    bonus = this_day_embed(bonus_item, CHICAGO)
    assert bonus.fields[1].value == "[Patreon](https://www.patreon.com/posts/halloween-3)"


def test_format_date() -> None:
    assert format_date(date(2019, 10, 5)) == "Oct 5, 2019"


def test_chunk_embeds_respects_count_and_size() -> None:
    small = [discord.Embed(title=f"e{i}") for i in range(12)]
    assert [len(b) for b in chunk_embeds(small)] == [10, 2]
    big = [discord.Embed(description="x" * 2500) for _ in range(5)]
    assert [len(b) for b in chunk_embeds(big)] == [2, 2, 1]  # 6000-character limit


def test_rank_totals_sorts_and_breaks_ties_randomly() -> None:
    totals = [(1, 2), (2, 5), (3, 2), (4, 1)]
    orders = {tuple(rank_totals(totals, random.Random(seed))) for seed in range(20)}
    assert all(order[0] == (2, 5) and order[-1] == (4, 1) for order in orders)
    assert len(orders) == 2  # the tie between episodes 1 and 3 goes both ways


# --- FeedClient -----------------------------------------------------------------------------


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_feed_client_caches_and_expires() -> None:
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, content=(DATA / "main_feed.xml").read_bytes())

    clock = Clock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FeedClient(http, ttl=15, clock=clock)
        feeds = await asyncio.gather(*(client.get("https://feeds.test/main") for _ in range(5)))
        assert hits == 1  # concurrent requests share one download
        assert all(f is feeds[0] for f in feeds)
        clock.now = 14
        await client.get("https://feeds.test/main")
        assert hits == 1
        clock.now = 16
        await client.get("https://feeds.test/main")
        assert hits == 2


async def test_feed_client_errors() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as http:
        client = FeedClient(http)
        with pytest.raises(FeedError):
            await client.get("https://feeds.test/main")
        with pytest.raises(FeedError):
            await client.get("")  # not configured
