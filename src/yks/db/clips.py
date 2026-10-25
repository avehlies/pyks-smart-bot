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

from yks.db.models import Clip


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
