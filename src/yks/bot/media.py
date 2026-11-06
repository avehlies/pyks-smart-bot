"""Re-posting clip media: upload the file when it fits, otherwise link to it."""

from __future__ import annotations

import io
import logging
import mimetypes
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import discord
import httpx

log = logging.getLogger(__name__)

# Discord's upload limit for servers without boosts (used when no guild is known).
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024


def upload_limit(guild: discord.Guild | None) -> int:
    return getattr(guild, "filesize_limit", None) or DEFAULT_UPLOAD_LIMIT


def is_audio_or_video(content_type: str | None, filename: str) -> bool:
    kind = content_type or mimetypes.guess_type(filename)[0] or ""
    return kind.split("/")[0] in ("audio", "video")


async def attachment_payload(
    attachment: discord.Attachment, guild: discord.Guild | None
) -> dict[str, Any]:
    """`send()` kwargs that re-post an attachment: the file itself, or its URL if too big.

    Clips uploaded by Nitro users can be far bigger than the bot is allowed to upload.
    """
    if attachment.size <= upload_limit(guild):
        try:
            return {"file": await attachment.to_file()}
        except discord.HTTPException:
            log.warning("Could not download attachment %s; linking instead", attachment.id)
    return {"content": attachment.url}


@dataclass(frozen=True)
class ClipMedia:
    """An attachment downloaded once, to be posted more than once (a preview, then the post).

    `data` is None when the file was too big to upload (or couldn't be downloaded): it's linked.
    """

    filename: str
    url: str
    data: bytes | None

    def payload(self, guild: discord.Guild | None) -> dict[str, Any]:
        """`send()` kwargs for this media in `guild` (whose upload limit may be smaller)."""
        if self.data is not None and len(self.data) <= upload_limit(guild):
            return {"file": discord.File(io.BytesIO(self.data), filename=self.filename)}
        return {"content": self.url}


# Statuses the Discord CDN gives for an expired (or removed) attachment link.
GONE_STATUSES = frozenset({403, 404, 410})


class LinkGone(Exception):
    """The CDN refused the link: its signature expired or the file was removed."""


def link_is_fresh(url: str, *, margin: float = 600.0, now: float | None = None) -> bool:
    """Will this signed Discord CDN link still work for at least `margin` more seconds?

    Discord links carry their expiry as `ex=<hex unix time>`. A link without one is not fresh.
    """
    values = parse_qs(urlparse(url).query).get("ex")
    try:
        expires = float(int(values[0], 16)) if values else None
    except ValueError:
        expires = None
    current = time.time() if now is None else now
    return expires is not None and expires - margin > current


async def download_media(
    http: httpx.AsyncClient, url: str, filename: str, *, max_bytes: int
) -> ClipMedia:
    """Download a clip to re-post it. Too big to upload (or a failed download): it's linked.

    Raises LinkGone if the CDN refuses the link, so the caller can get a fresh one.
    """
    try:
        async with http.stream("GET", url) as response:
            if response.status_code in GONE_STATUSES:
                raise LinkGone(f"HTTP {response.status_code}")
            response.raise_for_status()
            if int(response.headers.get("content-length") or 0) > max_bytes:
                return ClipMedia(filename=filename, url=url, data=None)
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return ClipMedia(filename=filename, url=url, data=None)
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        log.warning("Could not download %s (%s); linking instead", filename, exc)
        return ClipMedia(filename=filename, url=url, data=None)
    return ClipMedia(filename=filename, url=url, data=b"".join(chunks))
