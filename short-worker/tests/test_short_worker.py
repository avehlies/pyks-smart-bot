"""The short worker end to end: scan -> record (needs TEST_DATABASE_URL).

The worker never posts; it decides which shorts the bot may post. "Pending" here means what the
bot's poll would pick up: `announced_at` NULL and a `published_at`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from fake_youtube import FakeYouTube
from yks_short_worker.config import WorkerSettings
from yks_short_worker.health import check
from yks_short_worker.store import ShortStore
from yks_short_worker.worker import Worker
from yks_short_worker.youtube import YouTube

ALL_IDS = {"YWVekO4yGJg", "jZSPxfsgpvs", "yR_-8G1B3R4", "7e_fPLc8cLM"}


@pytest.fixture
def youtube() -> FakeYouTube:
    return FakeYouTube()


@pytest.fixture
def worker(shorts_pool: asyncpg.Pool, youtube: FakeYouTube, tmp_path: Path) -> Worker:
    return Worker(
        store=ShortStore(shorts_pool),
        youtube=YouTube(youtube.client(), handle="h", channel_id="UCxyz"),
        settings=WorkerSettings(_env_file=None, heartbeat_file=str(tmp_path / "heartbeat")),
    )


def now() -> datetime:
    return datetime.now(UTC)


async def rows(pool: asyncpg.Pool) -> dict[str, asyncpg.Record]:
    return {r["video_id"]: r for r in await pool.fetch("SELECT * FROM youtube_shorts")}


async def pending(pool: asyncpg.Pool) -> list[str]:
    """What the bot would post, oldest first."""
    return [
        r["video_id"]
        for r in await pool.fetch(
            "SELECT video_id FROM youtube_shorts WHERE announced_at IS NULL"
            " AND published_at IS NOT NULL ORDER BY published_at, video_id"
        )
    ]


async def test_first_scan_records_the_back_catalogue_quietly(
    worker: Worker, shorts_pool: asyncpg.Pool
) -> None:
    recorded = await worker.scan_once()
    assert set(recorded.new) == ALL_IDS
    found = await rows(shorts_pool)
    assert set(found) == ALL_IDS
    assert all(r["announced_at"] is not None for r in found.values())  # none to be posted
    assert all(r["announced_message_id"] is None for r in found.values())
    assert all(r["title"] for r in found.values())
    assert found["jZSPxfsgpvs"]["published_at"] is not None  # from the feed
    assert found["7e_fPLc8cLM"]["published_at"] is None  # page only
    assert await pending(shorts_pool) == []


async def test_new_short_is_left_for_the_bot(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube
) -> None:
    await worker.scan_once()
    youtube.add_to_feed("NEWshort001", "Brand new", now() - timedelta(minutes=5))
    assert (await worker.scan_once()).new == ["NEWshort001"]
    assert await pending(shorts_pool) == ["NEWshort001"]
    assert (await rows(shorts_pool))["NEWshort001"]["title"] == "Brand new"
    assert (await worker.scan_once()).new == []  # seen already
    assert await pending(shorts_pool) == ["NEWshort001"]  # still the bot's to post


async def test_old_short_found_late_is_marked(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube
) -> None:
    await worker.scan_once()
    youtube.add_to_feed("OLDshort001", "From last year", now() - timedelta(days=300))
    assert (await worker.scan_once()).skipped == 1
    assert await pending(shorts_pool) == []
    assert (await rows(shorts_pool))["OLDshort001"]["announced_at"] is not None


async def test_first_deploy_with_only_the_feed_marks_older_page_shorts(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube
) -> None:
    """The first scan only reads the feed; the next one finds older shorts on the page. They're
    undated, so they mustn't be mistaken for new uploads."""
    youtube.page_status = 500
    await worker.scan_once()
    youtube.page_status = 200
    await worker.scan_once()
    assert await pending(shorts_pool) == []  # undated: the bot won't post them
    assert (await rows(shorts_pool))["7e_fPLc8cLM"]["announced_at"] is None  # waiting for a date
    await shorts_pool.execute(
        "UPDATE youtube_shorts SET first_seen_at = now() - interval '4 days'"
        " WHERE announced_at IS NULL"
    )
    youtube.page_status = youtube.feed_status = 503  # aged out even when YouTube is down
    assert (await worker.scan_once()).skipped == 2
    assert (await rows(shorts_pool))["7e_fPLc8cLM"]["announced_at"] is not None


async def test_page_only_short_becomes_pending_once_the_feed_dates_it(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube
) -> None:
    await worker.scan_once()
    youtube.page = youtube.page.replace('"videoId": "7e_fPLc8cLM"', '"videoId": "NEWshort001"')
    await worker.scan_once()
    assert await pending(shorts_pool) == []  # on the page, not in the feed yet
    youtube.add_to_feed("NEWshort001", "Fresh", now() - timedelta(minutes=1))
    await worker.scan_once()
    assert await pending(shorts_pool) == ["NEWshort001"]


async def test_feed_dating_an_old_page_short_marks_it_at_once(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube
) -> None:
    """Dating and marking happen in one transaction, so the bot never sees it as pending."""
    await worker.scan_once()
    youtube.page = youtube.page.replace('"videoId": "7e_fPLc8cLM"', '"videoId": "OLDshort001"')
    await worker.scan_once()
    youtube.add_to_feed("OLDshort001", "Old", now() - timedelta(days=30))
    assert (await worker.scan_once()).skipped == 1
    assert await pending(shorts_pool) == []


async def test_later_scan_fills_in_dates(
    worker: Worker, shorts_pool: asyncpg.Pool, youtube: FakeYouTube
) -> None:
    youtube.feed_status = 500  # first scan: page only, no dates
    await worker.scan_once()
    assert (await rows(shorts_pool))["jZSPxfsgpvs"]["published_at"] is None
    youtube.feed_status = 200
    await worker.scan_once()
    assert (await rows(shorts_pool))["jZSPxfsgpvs"]["published_at"] is not None


async def test_heartbeat(worker: Worker, tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    assert check(path, 300) is not None
    assert await worker.beat()
    assert check(path, 300) is None
    assert "ago" in (check(path, 300, now=time.time() + 600) or "")
