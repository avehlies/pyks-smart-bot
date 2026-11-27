"""The short worker end to end: scan -> record -> post (needs TEST_DATABASE_URL)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from fake_youtube import FakeDiscord, FakeYouTube
from yks_short_worker.config import WorkerSettings
from yks_short_worker.discord_api import DiscordAPI
from yks_short_worker.health import check
from yks_short_worker.store import ShortStore
from yks_short_worker.worker import Worker
from yks_short_worker.youtube import YouTube

CHANNEL = 4242


@pytest.fixture
def youtube() -> FakeYouTube:
    return FakeYouTube()


@pytest.fixture
def discord() -> FakeDiscord:
    return FakeDiscord()


def make_worker(
    pool: asyncpg.Pool, youtube: FakeYouTube, discord: FakeDiscord, tmp_path: Path, **settings
) -> Worker:
    config = WorkerSettings(
        _env_file=None,
        **{
            "yks_shorts_channel_id": CHANNEL,
            "heartbeat_file": str(tmp_path / "heartbeat"),
            **settings,
        },
    )
    return Worker(
        store=ShortStore(pool),
        youtube=YouTube(youtube.client(), handle="h", channel_id="UCxyz"),
        discord=DiscordAPI("token", discord.client()),
        settings=config,
    )


@pytest.fixture
def worker(
    shorts_pool: asyncpg.Pool, youtube: FakeYouTube, discord: FakeDiscord, tmp_path: Path
) -> Worker:
    return make_worker(shorts_pool, youtube, discord, tmp_path)


def now() -> datetime:
    return datetime.now(UTC)


async def rows(pool: asyncpg.Pool) -> dict[str, asyncpg.Record]:
    return {r["video_id"]: r for r in await pool.fetch("SELECT * FROM youtube_shorts")}


async def test_first_scan_records_the_back_catalogue_quietly(
    worker: Worker, shorts_pool: asyncpg.Pool, discord: FakeDiscord
) -> None:
    assert await worker.scan_once() == []
    assert discord.posted == []
    found = await rows(shorts_pool)
    assert set(found) == {"YWVekO4yGJg", "jZSPxfsgpvs", "yR_-8G1B3R4", "7e_fPLc8cLM"}
    assert all(r["announced_at"] is not None for r in found.values())
    assert all(r["title"] for r in found.values())
    assert found["jZSPxfsgpvs"]["published_at"] is not None  # from the feed
    assert found["7e_fPLc8cLM"]["published_at"] is None  # page only


async def test_new_short_is_posted_once(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube, discord: FakeDiscord
) -> None:
    await worker.scan_once()
    youtube.add_to_feed("NEWshort001", "Brand new", now() - timedelta(minutes=5))
    assert await worker.scan_once() == ["NEWshort001"]
    assert discord.posted == [
        (
            CHANNEL,
            {
                "content": "New short! https://www.youtube.com/shorts/NEWshort001",
                "allowed_mentions": {"parse": []},
            },
        )
    ]
    row = (await rows(shorts_pool))["NEWshort001"]
    assert row["title"] == "Brand new"
    assert row["announced_message_id"] == 9001
    assert await worker.scan_once() == []  # not again
    assert len(discord.posted) == 1


async def test_old_short_found_late_is_not_posted(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube, discord: FakeDiscord
) -> None:
    await worker.scan_once()
    youtube.add_to_feed("OLDshort001", "From last year", now() - timedelta(days=300))
    assert await worker.scan_once() == []
    assert discord.posted == []
    assert (await rows(shorts_pool))["OLDshort001"]["announced_at"] is not None


async def test_failed_post_is_retried(
    worker: Worker, youtube: FakeYouTube, discord: FakeDiscord
) -> None:
    await worker.scan_once()
    youtube.add_to_feed("NEWshort001", "One", now() - timedelta(minutes=10))
    youtube.add_to_feed("NEWshort002", "Two", now() - timedelta(minutes=5))
    discord.status = 403
    assert await worker.scan_once() == []
    discord.status = 200
    assert await worker.scan_once() == ["NEWshort001", "NEWshort002"]  # oldest first


async def test_without_a_channel_shorts_wait(
    shorts_pool: asyncpg.Pool, youtube: FakeYouTube, discord: FakeDiscord, tmp_path: Path
) -> None:
    worker = make_worker(shorts_pool, youtube, discord, tmp_path, yks_shorts_channel_id=None)
    await worker.scan_once()
    youtube.add_to_feed("NEWshort001", "Waiting", now())
    assert await worker.scan_once() == []
    assert (await rows(shorts_pool))["NEWshort001"]["announced_at"] is None


async def test_scan_failure_still_retries_pending_posts(
    worker: Worker, youtube: FakeYouTube, discord: FakeDiscord
) -> None:
    await worker.scan_once()
    youtube.add_to_feed("NEWshort001", "One", now())
    discord.status = 500
    await worker.scan_once()
    youtube.feed_status = youtube.page_status = 503
    discord.status = 200
    assert await worker.scan_once() == ["NEWshort001"]


async def test_later_scan_fills_in_dates(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube
) -> None:
    youtube.feed_status = 500  # first scan: page only, no dates
    await worker.scan_once()
    assert (await rows(shorts_pool))["jZSPxfsgpvs"]["published_at"] is None
    youtube.feed_status = 200
    await worker.scan_once()
    assert (await rows(shorts_pool))["jZSPxfsgpvs"]["published_at"] is not None


async def test_first_deploy_with_only_the_feed_posts_nothing_later(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube, discord: FakeDiscord
) -> None:
    """The first scan only reads the feed; the next one finds older shorts on the page. They're
    undated, so they mustn't be mistaken for new uploads."""
    youtube.page_status = 500
    await worker.scan_once()
    youtube.page_status = 200
    assert await worker.scan_once() == []
    assert discord.posted == []
    waiting = (await rows(shorts_pool))["7e_fPLc8cLM"]
    assert waiting["announced_at"] is None  # waiting for a date from the feed
    await shorts_pool.execute(
        "UPDATE youtube_shorts SET first_seen_at = now() - interval '4 days'"
        " WHERE announced_at IS NULL"
    )
    assert await worker.scan_once() == []
    assert (await rows(shorts_pool))["7e_fPLc8cLM"]["announced_at"] is not None  # skipped
    assert discord.posted == []


async def test_page_only_short_is_posted_once_the_feed_dates_it(
    worker: Worker, youtube: FakeYouTube, discord: FakeDiscord
) -> None:
    await worker.scan_once()
    page = youtube.page
    youtube.page = page.replace('"videoId": "7e_fPLc8cLM"', '"videoId": "NEWshort001"')
    assert await worker.scan_once() == []  # on the page, not in the feed yet
    youtube.add_to_feed("NEWshort001", "Fresh", now() - timedelta(minutes=1))
    assert await worker.scan_once() == ["NEWshort001"]
    assert discord.contents == ["New short! https://www.youtube.com/shorts/NEWshort001"]


async def test_heartbeat(worker: Worker, tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    assert check(path, 300) is not None
    assert await worker.beat()
    assert check(path, 300) is None
    assert "ago" in (check(path, 300, now=time.time() + 600) or "")
