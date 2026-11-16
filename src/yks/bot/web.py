"""HTTP server run by the bot: `GET /healthz`, used by the Docker healthcheck."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

# How long the bot may be off the Discord gateway before it counts as unhealthy. Covers startup and
# discord.py's own reconnects, which normally take seconds.
DISCONNECTED_GRACE_SECONDS = 120.0


class Health:
    """Is the bot connected to Discord? Tracks how long it hasn't been.

    `is_ready()` alone isn't enough: discord.py only clears it on close, so it stays true while
    the gateway connection is down and being retried. The websocket must be open too.
    """

    def __init__(
        self,
        bot: YKSBot,
        *,
        grace: float = DISCONNECTED_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._grace = grace
        self._clock = clock
        self._down_since: float | None = clock()  # not connected yet at startup

    def connected(self) -> bool:
        ws = getattr(self._bot, "ws", None)
        return self._bot.is_ready() and ws is not None and ws.open

    def check(self) -> tuple[bool, dict[str, Any]]:
        """(healthy, details for the response body)."""
        now = self._clock()
        connected = self.connected()
        if connected:
            self._down_since = None
        elif self._down_since is None:
            self._down_since = now
        down_for = 0.0 if self._down_since is None else now - self._down_since
        healthy = down_for <= self._grace
        if not healthy:
            log.warning("Not connected to Discord for %.0fs; reporting unhealthy.", down_for)
        return healthy, {"ok": healthy, "discord": connected, "disconnected_for": round(down_for)}


HEALTH_KEY = web.AppKey("health", Health)


async def healthz(request: web.Request) -> web.Response:
    healthy, body = request.app[HEALTH_KEY].check()
    return web.json_response(body, status=200 if healthy else 503)


def build_app(bot: YKSBot, *, health: Health | None = None) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app[HEALTH_KEY] = health or Health(bot)
    app.router.add_get("/healthz", healthz)
    return app


async def start_web(bot: YKSBot, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    log.info("HTTP server listening on port %d", port)
    return runner
