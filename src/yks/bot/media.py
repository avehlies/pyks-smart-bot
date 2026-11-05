"""Re-posting clip media: upload the file when it fits, otherwise link to it."""

from __future__ import annotations

import logging
import mimetypes
from typing import Any

import discord

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
