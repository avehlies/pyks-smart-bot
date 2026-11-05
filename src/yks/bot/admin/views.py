"""Admin pages: bot status and the clip transcription queue."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import discord
from aiohttp import web
from jinja2 import Environment
from yarl import URL

from yks.bot.admin.auth import ADMIN_KEY, BOT_KEY, COOKIE_PATH, csrf_token
from yks.db import Clip, ClipStatus
from yks.db import admin as queries
from yks.db.state import get_all_state
from yks.log import recent_logs

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

ENV_KEY = web.AppKey("admin_templates", Environment)
STARTED_KEY = web.AppKey("admin_started", float)
PAGE_SIZE = 50
LOG_LINES = 150


def _bot(request: web.Request) -> YKSBot:
    return request.app[BOT_KEY]  # type: ignore[return-value]


def render(request: web.Request, template: str, **context: Any) -> web.Response:
    page = (
        request.app[ENV_KEY]
        .get_template(template)
        .render(
            user=request.get(ADMIN_KEY),
            csrf=csrf_token(request),
            root=COOKIE_PATH,
            path=request.path,
            flash=request.query.get("flash"),
            tz=_bot(request).settings.tz,
            **context,
        )
    )
    return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


def back(request: web.Request, default: str, flash: str) -> web.HTTPFound:
    """Redirect after a POST to the admin page the form was on, with a one-line message."""
    referer = URL(request.headers.get("Referer", ""))
    target = referer if referer.path.startswith(COOKIE_PATH + "/") else URL(default)
    query = {k: v for k, v in target.query.items() if k != "flash"}
    return web.HTTPFound(str(URL.build(path=target.path, query={**query, "flash": flash})))


# --- status -----------------------------------------------------------------------------------


async def status(request: web.Request) -> web.Response:
    bot = _bot(request)
    async with bot.db.session() as session:
        stats = await queries.queue_stats(session)
        state = await get_all_state(session)
    clips_cog: Any = bot.get_cog("Clips")
    backfill = getattr(clips_cog, "_backfill", None)
    latency = bot.latency
    return render(
        request,
        "status.html",
        bot_user=bot.user,
        ready=bot.is_ready(),
        latency_ms=None if latency != latency or latency == float("inf") else latency * 1000,
        guilds=sorted(bot.guilds, key=lambda g: g.name.lower()),
        cogs=sorted(bot.cogs),
        started=datetime.fromtimestamp(request.app[STARTED_KEY], UTC),
        stats=stats,
        now=datetime.now(UTC),
        backfill_running=backfill is not None and not backfill.done(),
        clip_channel=bot.get_channel(bot.settings.yks_clip_channel_id or 0),
        state=state,
        logs=list(recent_logs.lines)[-LOG_LINES:][::-1],
    )


async def start_backfill(request: web.Request) -> web.Response:
    clips_cog: Any = _bot(request).get_cog("Clips")
    if clips_cog is None:
        raise back(request, f"{COOKIE_PATH}/", "The Clips cog isn't loaded.")
    started = clips_cog.start_backfill()
    log.info("admin: %s started a clip backfill", request[ADMIN_KEY].name)
    message = "Backfill started." if started else "A backfill is already running."
    raise back(request, f"{COOKIE_PATH}/", message)


# --- clips ------------------------------------------------------------------------------------


def _filters(request: web.Request) -> queries.ClipFilter:
    raw_status = request.query.get("status", "")
    try:
        clip_status = ClipStatus(raw_status) if raw_status else None
    except ValueError:
        clip_status = None
    deleted = request.query.get("deleted", "hide")
    if deleted not in queries.DELETED_FILTERS:
        deleted = "hide"
    return queries.ClipFilter(clip_status, deleted, request.query.get("q", "")[:200])


async def clips(request: web.Request) -> web.Response:
    filters = _filters(request)
    try:
        page = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page = 1
    async with _bot(request).db.session() as session:
        rows, total = await queries.list_clips(
            session, filters, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE
        )
        stats = await queries.queue_stats(session)

    def page_url(number: int) -> str:
        query = {k: v for k, v in request.query.items() if k not in ("page", "flash")}
        return f"{COOKIE_PATH}/clips?" + urlencode({**query, "page": number})

    return render(
        request,
        "clips.html",
        clips=rows,
        total=total,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        page_url=page_url,
        filters=filters,
        statuses=list(ClipStatus),
        stats=stats,
    )


async def _clip_or_404(request: web.Request) -> Clip:
    try:
        clip_id = int(request.match_info["clip_id"])
    except ValueError:
        raise web.HTTPNotFound() from None
    async with _bot(request).db.session() as session:
        clip = await session.get(Clip, clip_id)
    if clip is None:
        raise web.HTTPNotFound(text="No such clip.")
    return clip


async def clip_detail(request: web.Request) -> web.Response:
    clip = await _clip_or_404(request)
    bot = _bot(request)
    author = bot.get_user(clip.author_id) if clip.author_id else None
    return render(
        request,
        "clip.html",
        clip=clip,
        author=author,
        jump_url=f"https://discord.com/channels/{clip.guild_id}/{clip.channel_id}/{clip.message_id}",
    )


async def clip_media(request: web.Request) -> web.Response:
    """Redirect to a fresh CDN URL for the clip (stored Discord URLs expire after a day)."""
    clip = await _clip_or_404(request)
    bot = _bot(request)
    channel: Any = bot.get_channel(clip.channel_id)
    if channel is None or not hasattr(channel, "fetch_message"):
        raise web.HTTPNotFound(text="The clip's channel isn't visible to the bot.")
    try:
        message = await channel.fetch_message(clip.message_id)
    except discord.NotFound:
        raise web.HTTPNotFound(text="The Discord message was deleted.") from None
    except discord.HTTPException:
        raise web.HTTPBadGateway(text="Discord didn't answer.") from None
    attachment = next((a for a in message.attachments if a.id == clip.attachment_id), None)
    if attachment is None:
        raise web.HTTPNotFound(text="The attachment was removed from the message.")
    if attachment.url != clip.url:
        async with bot.db.session() as session:
            await queries.update_url(session, clip.id, attachment.url)
    raise web.HTTPFound(attachment.url)


async def clip_retry(request: web.Request) -> web.Response:
    clip = await _clip_or_404(request)
    async with _bot(request).db.session() as session:
        count = await queries.retry_clips(session, [clip.id])
    log.info("admin: %s re-queued clip %s", request[ADMIN_KEY].name, clip.id)
    raise back(
        request,
        f"{COOKIE_PATH}/clips/{clip.id}",
        "Queued for transcription." if count else "It's being transcribed right now.",
    )


async def retry_failed(request: web.Request) -> web.Response:
    async with _bot(request).db.session() as session:
        count = await queries.retry_clips(session)
    log.info("admin: %s re-queued %d failed clip(s)", request[ADMIN_KEY].name, count)
    raise back(request, f"{COOKIE_PATH}/clips", f"Re-queued {count} failed clip(s).")


async def clip_transcription(request: web.Request) -> web.Response:
    clip = await _clip_or_404(request)
    text = str((await request.post()).get("transcription", ""))
    async with _bot(request).db.session() as session:
        saved = await queries.set_transcription(session, clip.id, text)
    log.info("admin: %s edited the transcript of clip %s", request[ADMIN_KEY].name, clip.id)
    raise back(
        request,
        f"{COOKIE_PATH}/clips/{clip.id}",
        "Transcript saved." if saved else "It's being transcribed right now; try again shortly.",
    )


async def clip_deleted(request: web.Request) -> web.Response:
    clip = await _clip_or_404(request)
    deleted = (await request.post()).get("deleted") == "1"
    async with _bot(request).db.session() as session:
        await queries.set_deleted(session, clip.id, deleted)
    log.info(
        "admin: %s %s clip %s", request[ADMIN_KEY].name, "hid" if deleted else "restored", clip.id
    )
    raise back(
        request, f"{COOKIE_PATH}/clips/{clip.id}", "Clip hidden." if deleted else "Clip restored."
    )


def setup_routes(app: web.Application) -> None:
    app.router.add_get("/", status)
    app.router.add_post("/backfill", start_backfill)
    app.router.add_get("/clips", clips)
    app.router.add_post("/clips/retry-failed", retry_failed)
    app.router.add_get(r"/clips/{clip_id:\d+}", clip_detail)
    app.router.add_get(r"/clips/{clip_id:\d+}/media", clip_media)
    app.router.add_post(r"/clips/{clip_id:\d+}/retry", clip_retry)
    app.router.add_post(r"/clips/{clip_id:\d+}/transcription", clip_transcription)
    app.router.add_post(r"/clips/{clip_id:\d+}/deleted", clip_deleted)
