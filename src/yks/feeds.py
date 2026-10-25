"""Podcast RSS feeds: fetching, parsing, and a short-lived cache.

The TS bot re-downloaded both feeds on every command. Here each URL is fetched at most once per
`ttl` seconds, and concurrent requests for the same URL share one download.
"""

from __future__ import annotations

import asyncio
import calendar
import html
import logging
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
import httpx

log = logging.getLogger(__name__)

USER_AGENT = "YKS-Smart-Bot/2 (+https://github.com/avehlies/pyks-smart-bot)"

# "Episode 123: ..." / "Ep 12: ..." (same alternatives as the TS bot).
_EPISODE_NUMBER = re.compile(r"\b(?:Episode|Ep)\.? ?(\d+)", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t]+")


class FeedError(Exception):
    """The feed could not be fetched or parsed."""


def normalize_title(title: str) -> str:
    """Compare titles the way the TS bot did (`normalize().trim()`)."""
    return unicodedata.normalize("NFC", title).strip()


@dataclass(frozen=True)
class Episode:
    title: str
    link: str | None
    audio_url: str | None
    published: datetime | None
    summary: str
    image_url: str | None
    duration: str | None

    @property
    def number(self) -> int | None:
        match = _EPISODE_NUMBER.search(self.title)
        return int(match.group(1)) if match else None

    def is_episode(self, number: int) -> bool:
        """True if the title is "Ep N: ..." or "Episode N: ..." (how !best finds titles)."""
        return f"Ep {number}:" in self.title or f"Episode {number}:" in self.title


@dataclass(frozen=True)
class Feed:
    title: str
    episodes: tuple[Episode, ...]

    @property
    def latest(self) -> Episode | None:
        return self.episodes[0] if self.episodes else None

    def find_episode(self, number: int) -> Episode | None:
        return next((ep for ep in self.episodes if ep.is_episode(number)), None)

    def titles(self) -> set[str]:
        return {normalize_title(ep.title) for ep in self.episodes}


def _plain_text(value: str) -> str:
    """HTML description -> plain text, like rss-parser's `contentSnippet`."""
    text = re.sub(r"<br\s*/?>|</p>", "\n", value, flags=re.IGNORECASE)
    text = html.unescape(_TAGS.sub("", text))
    lines = [_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _published(entry: feedparser.FeedParserDict) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)


def _episode(entry: feedparser.FeedParserDict) -> Episode:
    audio = next(
        (
            e.get("href")
            for e in entry.get("enclosures", [])
            if str(e.get("type", "")).startswith("audio")
        ),
        None,
    ) or next((e.get("href") for e in entry.get("enclosures", [])), None)
    content = entry.get("content") or []
    description = entry.get("summary") or (content[0].get("value") if content else "") or ""
    image = entry.get("image") or {}
    return Episode(
        title=str(entry.get("title", "")).strip(),
        link=entry.get("link"),
        audio_url=audio,
        published=_published(entry),
        summary=_plain_text(description),
        image_url=image.get("href") if isinstance(image, dict) else None,
        duration=entry.get("itunes_duration"),
    )


def parse_feed(data: bytes | str) -> Feed:
    parsed = feedparser.parse(data)
    if parsed.get("bozo") and not parsed.get("entries"):
        raise FeedError(f"Could not parse feed: {parsed.get('bozo_exception')}")
    return Feed(
        title=str(parsed.feed.get("title", "")).strip(),
        episodes=tuple(_episode(entry) for entry in parsed.entries),
    )


class FeedClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        ttl: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[float, Feed]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, url: str) -> Feed:
        """Return the parsed feed, from cache if fresh. Raises FeedError."""
        if not url:
            raise FeedError("Feed URL is not configured")
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self._cache.get(url)
            if cached and self._clock() - cached[0] < self._ttl:
                return cached[1]
            try:
                response = await self._http.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise FeedError(f"Could not fetch feed: {exc}") from exc
            feed = parse_feed(response.content)
            self._cache[url] = (self._clock(), feed)
            return feed


def make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=20.0, follow_redirects=True
    )
