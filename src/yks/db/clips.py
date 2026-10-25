"""Clip queries used by the bot: recording new clips and picking them back out.

Transcription state (`status`, `transcription`, ...) is owned by the separate clip worker; new
rows start as `pending` and the database trigger notifies the worker.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yks.db.models import Clip, ClipStatus


@dataclass(frozen=True)
class NewClip:
    guild_id: int
    channel_id: int
    message_id: int
    attachment_id: int
    author_id: int | None
    filename: str
    content_type: str | None
    url: str


async def record_clips(session: AsyncSession, clips: Iterable[NewClip]) -> int:
    """Insert clips that aren't stored yet. Returns how many were new."""
    rows = [clip.__dict__ for clip in clips]
    if not rows:
        return 0
    stmt = (
        insert(Clip)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_clips_message_attachment")
        .returning(Clip.id)
    )
    return len((await session.scalars(stmt)).all())


async def random_clip(session: AsyncSession) -> Clip | None:
    return await session.scalar(
        select(Clip).where(Clip.deleted.is_(False)).order_by(func.random()).limit(1)
    )


async def get_clip(session: AsyncSession, clip_id: int) -> Clip | None:
    return await session.get(Clip, clip_id)


async def mark_messages_deleted(session: AsyncSession, message_ids: Iterable[int]) -> int:
    ids = list(message_ids)
    if not ids:
        return 0
    result = await session.execute(
        update(Clip).where(Clip.message_id.in_(ids)).values(deleted=True)
    )
    return result.rowcount or 0


async def search_clips(session: AsyncSession, phrase: str, *, limit: int = 60) -> list[Clip]:
    """Transcribed clips matching `phrase`, best match first.

    Uses Postgres full-text search (stemmed, ranked). If that finds nothing, for example when the
    phrase is all stop words ("it is what it is"), fall back to a case-insensitive substring match.
    """
    searchable = (
        Clip.deleted.is_(False),
        Clip.status == ClipStatus.DONE,
        Clip.transcription.is_not(None),
        Clip.transcription != "",
    )
    query = func.websearch_to_tsquery("english", phrase)
    ranked = (
        select(Clip)
        .where(*searchable, Clip.search.op("@@")(query))
        .order_by(func.ts_rank(Clip.search, query).desc(), Clip.id.desc())
        .limit(limit)
    )
    found = list((await session.scalars(ranked)).all())
    if found:
        return found
    escaped = phrase.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    substring = (
        select(Clip)
        .where(*searchable, Clip.transcription.ilike(f"%{escaped}%", escape="\\"))
        .order_by(Clip.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(substring)).all())
