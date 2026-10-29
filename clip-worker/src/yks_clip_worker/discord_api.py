"""Minimal, read-only Discord REST client: just enough to refresh attachment URLs.

Discord CDN attachment links are signed and expire (`?ex=...&hm=...`), so a URL stored when
the clip was posted may no longer download. Fetching the message returns fresh links.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/avehlies/pyks-smart-bot, 1.0) yks-clip-worker"


class DiscordError(Exception):
    """The API refused the request (bad token, missing access, repeated rate limits...)."""


class DiscordAPI:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_message(self, channel_id: int, message_id: int) -> dict[str, Any] | None:
        """The message JSON, or None if it (or the channel) no longer exists."""
        url = f"{API_BASE}/channels/{channel_id}/messages/{message_id}"
        for _ in range(5):
            response = await self._http.get(url, headers=self._headers)
            if response.status_code == 429:
                retry_after = float(response.json().get("retry_after", 1.0))
                log.info("Discord rate limit; waiting %.1fs", retry_after)
                await asyncio.sleep(min(retry_after, 60.0))
                continue
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                # Discord's body says why, e.g. {"code": 50001, "message": "Missing Access"}.
                raise DiscordError(
                    f"GET message {message_id} in channel {channel_id} failed: "
                    f"HTTP {response.status_code} {response.text[:200]}"
                )
            return response.json()
        raise DiscordError("Rate limited repeatedly")

    async def fresh_attachment_url(
        self, channel_id: int, message_id: int, attachment_id: int
    ) -> str | None:
        """A current download URL for one attachment, or None if it's gone."""
        message = await self.fetch_message(channel_id, message_id)
        if message is None:
            return None
        for attachment in message.get("attachments", []):
            if int(attachment["id"]) == attachment_id:
                return attachment["url"]
        return None
