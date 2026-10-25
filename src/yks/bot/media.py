"""Re-posting clip media: upload the file when it fits, otherwise link to it."""

from __future__ import annotations

import io
import logging
import mimetypes
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

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


def filename_from_url(url: str, default: str = "clip") -> str:
    return PurePosixPath(urlparse(url).path).name or default


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


async def download_payload(
    http: httpx.AsyncClient, url: str, guild: discord.Guild | None
) -> dict[str, Any]:
    """Like `attachment_payload`, for an arbitrary https URL (the share-clip API)."""
    limit = upload_limit(guild)
    try:
        async with http.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > limit:
                return {"content": url}
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > limit:
                    return {"content": url}
    except httpx.HTTPError:
        log.warning("Could not download %s; linking instead", url[:100])
        return {"content": url}
    return {"file": discord.File(io.BytesIO(bytes(data)), filename=filename_from_url(url))}
