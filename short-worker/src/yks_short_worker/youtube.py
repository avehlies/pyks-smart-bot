"""Finding the channel's Shorts, from two public sources:

- the channel's Shorts-only RSS feed (the uploads playlist with a `UUSH` prefix instead of `UC`):
  the newest 15 shorts, with titles and publish dates;
- the channel's Shorts page (`youtube.com/@handle/shorts`): the newest ~48, with titles but no
  dates, read from the `ytInitialData` JSON embedded in the page.

Each scan reads both and merges them, so the worker keeps working if either one breaks (YouTube
changes its page layout now and then). Only the first page of the Shorts tab is read, not the
rest of its infinite scroll.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

# A desktop browser's User-Agent: YouTube serves a stripped-down page to unknown clients.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# Skips the cookie-consent interstitial that YouTube shows some regions instead of the page.
CONSENT_COOKIE = "SOCS=CAI"

_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_INITIAL_DATA = re.compile(
    r"(?:var |window\[\"|window\.)ytInitialData\"?\]?\s*=\s*(\{.+?\});\s*<", re.DOTALL
)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


class ScanError(Exception):
    """Neither source could be read."""


@dataclass(frozen=True)
class Short:
    video_id: str
    title: str | None = None
    published_at: datetime | None = None

    @property
    def url(self) -> str:
        return short_url(self.video_id)


def short_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def feed_url(channel_id: str) -> str:
    playlist = "UUSH" + channel_id.removeprefix("UC")
    return f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist}"


def page_url(handle: str) -> str:
    return f"https://www.youtube.com/@{handle}/shorts"


# --- parsing --------------------------------------------------------------------------------


def parse_feed(xml: str | bytes) -> list[Short]:
    """Shorts in the Atom feed, newest first. Raises ValueError if it isn't a feed."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"not XML: {exc}") from exc
    if root.tag != f"{_ATOM}feed":
        raise ValueError(f"not an Atom feed (root is {root.tag})")
    shorts = []
    for entry in root.iter(f"{_ATOM}entry"):
        video_id = (entry.findtext(f"{_YT}videoId") or "").strip()
        if not _VIDEO_ID.match(video_id):
            continue
        published = entry.findtext(f"{_ATOM}published")
        shorts.append(
            Short(
                video_id=video_id,
                title=(entry.findtext(f"{_ATOM}title") or "").strip() or None,
                published_at=datetime.fromisoformat(published) if published else None,
            )
        )
    return shorts


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _dig(node: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _lockup_short(lockup: dict[str, Any]) -> Short | None:
    """A `shortsLockupViewModel` (the current layout) as a Short."""
    video_id = _dig(lockup, "onTap", "innertubeCommand", "reelWatchEndpoint", "videoId")
    if not isinstance(video_id, str):
        entity = str(lockup.get("entityId", ""))  # "shorts-shelf-item-<id>"
        video_id = entity.removeprefix("shorts-shelf-item-") if entity else None
    if not video_id or not _VIDEO_ID.match(video_id):
        return None
    title = _dig(lockup, "overlayMetadata", "primaryText", "content")
    return Short(video_id, title.strip() if isinstance(title, str) and title.strip() else None)


def _reel_short(reel: dict[str, Any]) -> Short | None:
    """A `reelItemRenderer` (the older layout) as a Short."""
    video_id = reel.get("videoId")
    if not isinstance(video_id, str) or not _VIDEO_ID.match(video_id):
        return None
    title = _dig(reel, "headline", "simpleText")
    return Short(video_id, title.strip() if isinstance(title, str) and title.strip() else None)


def parse_page(html: str) -> list[Short]:
    """Shorts listed on the channel's Shorts page, newest first. Raises ValueError if the page has
    no `ytInitialData`. A page with the data but no shorts gives an empty list."""
    match = _INITIAL_DATA.search(html)
    if match is None:
        raise ValueError("no ytInitialData in the page (consent page or a new layout?)")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"ytInitialData isn't JSON: {exc}") from exc
    shorts: list[Short] = []
    for node in _walk(data):
        found = None
        if isinstance(node.get("shortsLockupViewModel"), dict):
            found = _lockup_short(node["shortsLockupViewModel"])
        elif isinstance(node.get("reelItemRenderer"), dict):
            found = _reel_short(node["reelItemRenderer"])
        if found is not None:
            shorts.append(found)
    return dedupe(shorts)


def dedupe(shorts: Iterable[Short]) -> list[Short]:
    """Keep the first of each video id, filling in a missing title or date from later copies."""
    merged: dict[str, Short] = {}
    for short in shorts:
        seen = merged.get(short.video_id)
        if seen is None:
            merged[short.video_id] = short
        else:
            merged[short.video_id] = Short(
                short.video_id,
                seen.title or short.title,
                seen.published_at or short.published_at,
            )
    return list(merged.values())


# --- fetching -------------------------------------------------------------------------------


def describe(exc: Exception) -> str:
    """One line for the log (httpx's messages run to several)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url}"
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


class YouTube:
    def __init__(self, http: httpx.AsyncClient, *, handle: str, channel_id: str) -> None:
        self._http = http
        self._handle = handle
        self._channel_id = channel_id

    async def _get(self, url: str) -> str:
        response = await self._http.get(
            url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": CONSENT_COOKIE,
            },
        )
        response.raise_for_status()
        return response.text

    async def from_feed(self) -> list[Short]:
        return parse_feed(await self._get(feed_url(self._channel_id)))

    async def from_page(self) -> list[Short]:
        return parse_page(await self._get(page_url(self._handle)))

    async def scan(self) -> list[Short]:
        """Shorts from both sources, merged (the feed's dates win). Raises ScanError only if both
        fail; one failing is logged as a warning."""
        results = await asyncio.gather(self.from_feed(), self.from_page(), return_exceptions=True)
        found: list[Short] = []
        errors = []
        for name, result in zip(("RSS feed", "Shorts page"), results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, httpx.HTTPError | ValueError):
                    raise result
                log.warning("Couldn't read the %s: %s", name, describe(result))
                errors.append(f"{name}: {describe(result)}")
            else:
                found.extend(result)
        if len(errors) == len(results):
            raise ScanError("; ".join(errors))
        return dedupe(found)
