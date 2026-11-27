"""Minimal Discord REST client: just enough to post a message as the bot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/avehlies/pyks-smart-bot, 1.0) yks-short-worker"


class DiscordError(Exception):
    """The API refused the request (bad token, missing access, repeated rate limits...)."""


class DiscordAPI:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_message(self, channel_id: int, content: str) -> dict[str, Any]:
        """Post `content` (no pings) and return the created message's JSON."""
        url = f"{API_BASE}/channels/{channel_id}/messages"
        body = {"content": content, "allowed_mentions": {"parse": []}}
        for _ in range(5):
            response = await self._http.post(url, headers=self._headers, json=body)
            if response.status_code == 429:
                retry_after = float(response.json().get("retry_after", 1.0))
                log.info("Discord rate limit; waiting %.1fs", retry_after)
                await asyncio.sleep(min(retry_after, 60.0))
                continue
            if response.status_code >= 400:
                # Discord's body says why, e.g. {"code": 50001, "message": "Missing Access"}.
                raise DiscordError(
                    f"POST message in channel {channel_id} failed: "
                    f"HTTP {response.status_code} {response.text[:200]}"
                )
            return response.json()
        raise DiscordError("Rate limited repeatedly")
