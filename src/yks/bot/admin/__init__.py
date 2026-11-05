"""Web admin panel, mounted at /admin on the bot's HTTP server."""

from __future__ import annotations

import logging
import secrets
import time
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from yks.bot.admin import auth, views

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"


def _ago(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    seconds = int(((now or datetime.now(UTC)) - value).total_seconds())
    suffix = "ago" if seconds >= 0 else "from now"
    seconds = abs(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} {suffix}"
    return f"{seconds}s {suffix}"


def _local(value: datetime | None, tz: tzinfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else "—"


def make_templates() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["ago"] = _ago
    env.filters["local"] = _local
    env.filters["timestamp"] = lambda ts: datetime.fromtimestamp(ts, UTC)
    return env


def build_admin_app(bot: YKSBot) -> web.Application:
    secret = bot.settings.admin_session_secret
    if not secret:
        secret = secrets.token_urlsafe(32)
        if bot.settings.admin_enabled:
            log.warning("ADMIN_SESSION_SECRET is not set; admin sign-ins end on restart.")
    app = web.Application(middlewares=[auth.auth_middleware], client_max_size=256 * 1024)
    app[auth.BOT_KEY] = bot
    app[auth.SIGNER_KEY] = auth.Signer(secret)
    app[views.ENV_KEY] = make_templates()
    app[views.STARTED_KEY] = time.time()
    app.router.add_get("/login", auth.login)
    app.router.add_get("/callback", auth.callback)
    app.router.add_post("/logout", auth.logout)
    app.router.add_get("/signed-out", auth.signed_out)
    views.setup_routes(app)
    return app
