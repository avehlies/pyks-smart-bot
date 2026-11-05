"""Admin sign-in: Discord OAuth2 (`identify` scope) and an HMAC-signed session cookie.

Who counts as an admin is checked on every request, not only at sign-in, so removing someone
from ADMIN_USER_IDS or taking away their Manage Server permission locks them out at once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

import httpx
from aiohttp import web

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
SESSION_COOKIE = "yks_admin"
STATE_COOKIE = "yks_admin_state"
SESSION_SECONDS = 7 * 24 * 3600
STATE_SECONDS = 600
COOKIE_PATH = "/admin"


@dataclass(frozen=True)
class AdminUser:
    id: int
    name: str
    avatar_url: str | None
    expires: int


class Signer:
    """Signs small JSON payloads: `<base64 json>.<base64 hmac>`."""

    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(("yks-admin:" + secret).encode()).digest()

    def _mac(self, purpose: str, body: bytes) -> bytes:
        return hmac.new(self._key, purpose.encode() + b"\0" + body, hashlib.sha256).digest()

    def sign(self, purpose: str, data: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
        mac = base64.urlsafe_b64encode(self._mac(purpose, body))
        return f"{body.decode()}.{mac.decode()}"

    def unsign(self, purpose: str, value: str | None) -> dict[str, Any] | None:
        if not value or "." not in value:
            return None
        body, _, mac = value.encode().partition(b".")
        try:
            expected = base64.urlsafe_b64encode(self._mac(purpose, body))
            if not hmac.compare_digest(mac, expected):
                return None
            data = json.loads(base64.urlsafe_b64decode(body))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            return None
        return data

    def csrf_token(self, session_cookie: str) -> str:
        return hmac.new(self._key, b"csrf\0" + session_cookie.encode(), "sha256").hexdigest()


def redirect_uri(bot: YKSBot) -> str:
    return bot.settings.admin_base_url.rstrip("/") + COOKIE_PATH + "/callback"


def client_id(bot: YKSBot) -> int | None:
    return bot.settings.admin_client_id or bot.application_id


def _secure(bot: YKSBot) -> bool:
    return bot.settings.admin_base_url.startswith("https://")


def safe_next(value: str | None) -> str:
    """Only redirect back into the admin panel after signing in (no open redirects)."""
    if not value:
        return COOKIE_PATH + "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith(COOKIE_PATH + "/"):
        return COOKIE_PATH + "/"
    return value


def is_admin(bot: YKSBot, user_id: int) -> bool:
    settings = bot.settings
    if user_id == settings.owner_id or user_id in settings.admin_user_ids:
        return True
    guild = bot.get_guild(settings.yks_guild_id or 0)
    member = guild.get_member(user_id) if guild is not None else None
    return member is not None and member.guild_permissions.manage_guild


def current_user(request: web.Request) -> AdminUser | None:
    signer: Signer = request.app[SIGNER_KEY]
    data = signer.unsign("session", request.cookies.get(SESSION_COOKIE))
    if data is None:
        return None
    try:
        return AdminUser(int(data["uid"]), str(data["name"]), data.get("avatar"), int(data["exp"]))
    except (KeyError, ValueError, TypeError):
        return None


def csrf_token(request: web.Request) -> str:
    return request.app[SIGNER_KEY].csrf_token(request.cookies.get(SESSION_COOKIE, ""))


SIGNER_KEY = web.AppKey("admin_signer", Signer)
BOT_KEY = web.AppKey("admin_bot", object)
ADMIN_KEY = web.RequestKey("admin_user", AdminUser)

PUBLIC_PATHS = {"/login", "/callback", "/logout", "/signed-out"}


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    bot: YKSBot = request.app[BOT_KEY]  # type: ignore[assignment]
    path = request.path.removeprefix(COOKIE_PATH)
    if not bot.settings.admin_enabled:
        return web.Response(
            status=503,
            text="The admin panel is off. Set ADMIN_BASE_URL and ADMIN_CLIENT_SECRET to use it.",
        )
    if path in PUBLIC_PATHS:
        return await handler(request)
    user = current_user(request)
    if user is None:
        if request.method != "GET":
            raise web.HTTPUnauthorized(text="Signed out.")
        raise web.HTTPFound(f"{COOKIE_PATH}/login?" + urlencode({"next": request.path_qs}))
    if not is_admin(bot, user.id):
        raise web.HTTPForbidden(text=f"{user.name} is not an admin of this bot.")
    if request.method not in ("GET", "HEAD"):
        form = await request.post()
        if not hmac.compare_digest(str(form.get("csrf", "")), csrf_token(request)):
            raise web.HTTPForbidden(text="Bad or missing form token. Reload the page and retry.")
    request[ADMIN_KEY] = user
    return await handler(request)


async def login(request: web.Request) -> web.Response:
    bot: YKSBot = request.app[BOT_KEY]  # type: ignore[assignment]
    app_id = client_id(bot)
    if app_id is None:
        return web.Response(status=503, text="The bot isn't connected yet; try again shortly.")
    state = secrets.token_urlsafe(24)
    signer: Signer = request.app[SIGNER_KEY]
    cookie = signer.sign(
        "state",
        {
            "state": state,
            "next": safe_next(request.query.get("next")),
            "exp": int(time.time()) + STATE_SECONDS,
        },
    )
    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri(bot),
            "response_type": "code",
            "scope": "identify",
            "state": state,
        }
    )
    response = web.HTTPFound(f"{DISCORD_AUTHORIZE}?{query}")
    response.set_cookie(
        STATE_COOKIE,
        cookie,
        max_age=STATE_SECONDS,
        path=COOKIE_PATH,
        httponly=True,
        secure=_secure(bot),
        samesite="Lax",
    )
    raise response


async def _fetch_discord_user(bot: YKSBot, code: str) -> dict[str, Any]:
    token = await bot.http_client.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(bot),
        },
        auth=(str(client_id(bot)), bot.settings.admin_client_secret),
    )
    token.raise_for_status()
    access_token = token.json()["access_token"]
    me = await bot.http_client.get(
        f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {access_token}"}
    )
    me.raise_for_status()
    return me.json()


async def callback(request: web.Request) -> web.Response:
    bot: YKSBot = request.app[BOT_KEY]  # type: ignore[assignment]
    signer: Signer = request.app[SIGNER_KEY]
    state = signer.unsign("state", request.cookies.get(STATE_COOKIE))
    if state is None or not hmac.compare_digest(
        str(state.get("state")), request.query.get("state", "")
    ):
        raise web.HTTPBadRequest(text="Sign-in expired or was tampered with. Try again.")
    code = request.query.get("code")
    if not code:
        raise web.HTTPBadRequest(text=f"Discord sign-in failed: {request.query.get('error', '?')}")
    try:
        profile = await _fetch_discord_user(bot, code)
    except (httpx.HTTPError, KeyError, ValueError):
        log.exception("admin: Discord OAuth exchange failed")
        raise web.HTTPBadGateway(text="Could not reach Discord to finish signing in.") from None
    user_id = int(profile["id"])
    name = profile.get("global_name") or profile.get("username") or str(user_id)
    if not is_admin(bot, user_id):
        log.warning("admin: refused sign-in by %s (%s)", name, user_id)
        raise web.HTTPForbidden(text=f"{name} is not an admin of this bot.")
    avatar = profile.get("avatar")
    session = signer.sign(
        "session",
        {
            "uid": str(user_id),
            "name": name,
            "avatar": f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=64"
            if avatar
            else None,
            "exp": int(time.time()) + SESSION_SECONDS,
        },
    )
    log.info("admin: %s (%s) signed in", name, user_id)
    response = web.HTTPFound(safe_next(state.get("next")))
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=SESSION_SECONDS,
        path=COOKIE_PATH,
        httponly=True,
        secure=_secure(bot),
        samesite="Lax",
    )
    response.del_cookie(STATE_COOKIE, path=COOKIE_PATH)
    raise response


async def logout(request: web.Request) -> web.Response:
    # A POST with the CSRF token, so another site can't sign you out.
    user = current_user(request)
    form = await request.post()
    if user is not None and not hmac.compare_digest(str(form.get("csrf", "")), csrf_token(request)):
        raise web.HTTPForbidden(text="Bad or missing form token.")
    response = web.HTTPFound(COOKIE_PATH + "/signed-out")
    response.del_cookie(SESSION_COOKIE, path=COOKIE_PATH)
    raise response


async def signed_out(request: web.Request) -> web.Response:
    return web.Response(
        text=f'Signed out. <a href="{COOKIE_PATH}/">Sign in again</a>', content_type="text/html"
    )
