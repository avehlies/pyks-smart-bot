"""HTTP server run by the bot: `GET /healthz`, used by the Docker healthcheck."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

BOT_KEY = web.AppKey("bot", object)


async def healthz(request: web.Request) -> web.Response:
    bot: YKSBot = request.app[BOT_KEY]  # type: ignore[assignment]
    return web.json_response({"ok": True, "discord": bot.is_ready()})


def build_app(bot: YKSBot) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app[BOT_KEY] = bot
    app.router.add_get("/healthz", healthz)
    return app


async def start_web(bot: YKSBot, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    log.info("HTTP server listening on port %d", port)
    return runner
