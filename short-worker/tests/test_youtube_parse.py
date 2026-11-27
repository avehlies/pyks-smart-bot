"""Reading shorts from the RSS feed and the Shorts page (no database needed)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fake_youtube import DATA, FEED_IDS, PAGE_IDS, FakeYouTube
from yks_short_worker.youtube import (
    ScanError,
    Short,
    YouTube,
    dedupe,
    feed_url,
    page_url,
    parse_feed,
    parse_page,
)


def test_urls() -> None:
    assert feed_url("UCt_yVzAjE5AQUNMlLAw5jtA") == (
        "https://www.youtube.com/feeds/videos.xml?playlist_id=UUSHt_yVzAjE5AQUNMlLAw5jtA"
    )
    assert (
        page_url("YourKickstarterSucks") == "https://www.youtube.com/@YourKickstarterSucks/shorts"
    )
    assert Short("abcdefgh").url == "https://www.youtube.com/shorts/abcdefgh"


def test_parse_feed() -> None:
    shorts = parse_feed((DATA / "shorts_feed.xml").read_bytes())
    assert [s.video_id for s in shorts] == FEED_IDS
    assert shorts[0].title and shorts[0].title.startswith("It\u2019s not an Xmas movie anymore")
    assert shorts[0].published_at == datetime(2026, 11, 13, 22, 1, 43, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["not xml", "<html><body>hi</body></html>"])
def test_parse_feed_rejects_non_feeds(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_feed(bad)


def test_parse_page() -> None:
    shorts = parse_page((DATA / "shorts_page.html").read_text())
    assert [s.video_id for s in shorts] == PAGE_IDS
    assert all(s.title for s in shorts)
    assert all(s.published_at is None for s in shorts)


def test_parse_page_old_layout() -> None:
    html = (
        '<script>var ytInitialData = {"items": [{"reelItemRenderer": {"videoId": "abcdefghijk",'
        ' "headline": {"simpleText": "Old style"}}}]};</script>'
    )
    assert parse_page(html) == [Short("abcdefghijk", "Old style")]


def test_parse_page_without_data_raises() -> None:
    with pytest.raises(ValueError, match="ytInitialData"):
        parse_page("<html><form action='https://consent.youtube.com/save'></form></html>")


def test_dedupe_fills_in_missing_fields() -> None:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    merged = dedupe([Short("aaaaaaaa", None, when), Short("aaaaaaaa", "Title", None)])
    assert merged == [Short("aaaaaaaa", "Title", when)]


def youtube(fake: FakeYouTube) -> YouTube:
    return YouTube(fake.client(), handle="YourKickstarterSucks", channel_id="UCxyz")


async def test_scan_merges_both_sources() -> None:
    fake = FakeYouTube()
    shorts = await youtube(fake).scan()
    assert [s.video_id for s in shorts] == [
        "YWVekO4yGJg",
        "jZSPxfsgpvs",
        "yR_-8G1B3R4",
        "7e_fPLc8cLM",
    ]
    # The overlapping short keeps the feed's date.
    assert shorts[1].published_at is not None
    assert {r.headers["Cookie"] for r in fake.requests} == {"SOCS=CAI"}


async def test_scan_survives_one_source_failing() -> None:
    fake = FakeYouTube()
    fake.page_status = 500
    assert [s.video_id for s in await youtube(fake).scan()] == FEED_IDS
    fake.page_status, fake.feed = 200, "garbage"
    assert [s.video_id for s in await youtube(fake).scan()] == PAGE_IDS


async def test_scan_raises_when_both_fail() -> None:
    fake = FakeYouTube()
    fake.page_status = fake.feed_status = 503
    with pytest.raises(ScanError, match=r"RSS feed.*Shorts page"):
        await youtube(fake).scan()
