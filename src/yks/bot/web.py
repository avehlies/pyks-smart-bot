"""HTTP API served by the bot: `POST /api/share-clip` (used by im-at.work) and `GET /healthz`."""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

import discord
from aiohttp import web

from yks.bot.media import download_payload

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

BOT_KEY = web.AppKey("bot", object)
CLIPS_SITE = "https://im-at.work/clips/"


def _check_basic_auth(header: str | None, username: str, password: str) -> str | None:
    """Returns an error message, or None if the credentials match."""
    if not header or not header.startswith("Basic "):
        return "Missing Authorization Header"
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ").strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return "Invalid credentials."
    user, _, secret = decoded.partition(":")
    ok_user = hmac.compare_digest(user.encode(), username.encode())
    ok_pass = hmac.compare_digest(secret.encode(), password.encode())
    return None if ok_user and ok_pass else "Invalid credentials."


async def share_clip(request: web.Request) -> web.Response:
    bot: YKSBot = request.app[BOT_KEY]  # type: ignore[assignment]
    settings = bot.settings
    if not (settings.bot_auth_username and settings.bot_auth_password):
        return web.json_response({"message": "The share-clip API is not configured."}, status=503)
    error = _check_basic_auth(
        request.headers.get("Authorization"), settings.bot_auth_username, settings.bot_auth_password
    )
    if error:
        return web.json_response({"message": error}, status=401)

    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"message": "Expected a JSON object."}, status=400)
    user_id, page, url = (str(body.get(key) or "").strip() for key in ("userId", "page", "url"))
    if not (user_id and page and url):
        log.error("Missing params on share-clip request.")
        return web.json_response({"message": "userId, page and url are required."}, status=400)
    if not user_id.isdigit():
        return web.json_response({"message": "userId must be a Discord user ID."}, status=400)
    if urlparse(url).scheme != "https":
        return web.json_response({"message": "url must be an https URL."}, status=400)

    channel = bot.get_channel(settings.yks_clip_channel_id or 0)
    if channel is None or not hasattr(channel, "send"):
        return web.json_response({"message": "Clip channel not found."}, status=503)

    payload = await download_payload(bot.http_client, url, getattr(channel, "guild", None))
    embed = discord.Embed(
        description=f"<@{user_id}> shared a clip from {CLIPS_SITE}{quote(page, safe='/')}"
    )
    try:
        await channel.send(embed=embed, **payload)
    except discord.HTTPException:
        log.exception("share-clip: could not post to the clip channel")
        return web.json_response({"message": "Could not post the clip."}, status=502)
    log.info("share-clip: posted a clip for user %s", user_id)
    return web.json_response({"ok": True})


async def healthz(request: web.Request) -> web.Response:
    bot: YKSBot = request.app[BOT_KEY]  # type: ignore[assignment]
    return web.json_response({"ok": True, "discord": bot.is_ready()})


def build_app(bot: YKSBot) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app[BOT_KEY] = bot
    app.router.add_post("/api/share-clip", share_clip)
    app.router.add_get("/healthz", healthz)
    return app


async def start_web(bot: YKSBot, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    log.info("HTTP API listening on port %d", port)
    return runner
