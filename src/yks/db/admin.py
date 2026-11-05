"""Clip queries for the web admin panel: queue health, browsing, and fixing clips by hand."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, false, func, or_, select, text, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from yks.db.models import Clip, ClipStatus

# The worker LISTENs here (see the initial migration); a NOTIFY wakes it without waiting to poll.
NOTIFY_CHANNEL = "clips_new"


@dataclass(frozen=True)
class QueueStats:
    by_status: dict[str, int]  # clips that aren't deleted, per status
    deleted: int
    oldest_ready: datetime | None  # created_at of the oldest pending clip that may run now
    oldest_processing: datetime | None  # claimed_at of the longest-running claim
    last_transcribed: datetime | None

    @property
    def total(self) -> int:
        return sum(self.by_status.values())


async def queue_stats(session: AsyncSession) -> QueueStats:
    rows = await session.execute(
        select(Clip.status, func.count()).where(Clip.deleted.is_(False)).group_by(Clip.status)
    )
    by_status = {status.value: 0 for status in ClipStatus}
    by_status.update({str(status): count for status, count in rows.all()})
    stats = (
        await session.execute(
            select(
                func.count().filter(Clip.deleted.is_(True)),
                func.min(Clip.created_at).filter(
                    Clip.status == ClipStatus.PENDING,
                    Clip.deleted.is_(False),
                    or_(Clip.retry_at.is_(None), Clip.retry_at <= func.now()),
                ),
                func.min(Clip.claimed_at).filter(Clip.status == ClipStatus.PROCESSING),
                func.max(Clip.transcribed_at),
            )
        )
    ).one()
    return QueueStats(by_status, *stats)


DELETED_FILTERS = ("hide", "only", "all")


@dataclass(frozen=True)
class ClipFilter:
    status: ClipStatus | None = None
    deleted: str = "hide"  # one of DELETED_FILTERS
    query: str = ""

    def apply(self, stmt: Select[tuple[Clip]]) -> Select[tuple[Clip]]:
        if self.status is not None:
            stmt = stmt.where(Clip.status == self.status)
        if self.deleted == "hide":
            stmt = stmt.where(Clip.deleted.is_(False))
        elif self.deleted == "only":
            stmt = stmt.where(Clip.deleted.is_(True))
        if phrase := self.query.strip():
            escaped = phrase.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(
                or_(
                    Clip.search.op("@@")(func.websearch_to_tsquery("english", phrase)),
                    Clip.filename.ilike(f"%{escaped}%", escape="\\"),
                    Clip.last_error.ilike(f"%{escaped}%", escape="\\"),
                    Clip.id == int(phrase) if phrase.isdigit() else false(),
                    Clip.message_id == int(phrase) if phrase.isdigit() else false(),
                )
            )
        return stmt


async def list_clips(
    session: AsyncSession, filters: ClipFilter, *, offset: int = 0, limit: int = 50
) -> tuple[list[Clip], int]:
    """A page of clips, newest first, and how many match in all."""
    total = await session.scalar(filters.apply(select(func.count()).select_from(Clip)))
    page = filters.apply(select(Clip)).order_by(Clip.id.desc()).offset(offset).limit(limit)
    return list((await session.scalars(page)).all()), total or 0


async def _notify_worker(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": NOTIFY_CHANNEL})


async def retry_clips(session: AsyncSession, clip_ids: list[int] | None = None) -> int:
    """Put clips back in the queue with a fresh set of attempts.

    `None` retries every failed clip that isn't deleted. Clips being transcribed right now are
    left alone.
    """
    where = (
        [Clip.status == ClipStatus.FAILED, Clip.deleted.is_(False)]
        if clip_ids is None
        else [Clip.id.in_(clip_ids), Clip.status != ClipStatus.PROCESSING]
    )
    result = await session.execute(
        update(Clip)
        .where(*where)
        .values(
            status=ClipStatus.PENDING,
            attempts=0,
            last_error=None,
            retry_at=None,
            claimed_at=None,
            deleted=False,
        )
    )
    count = result.rowcount or 0
    if count:
        await _notify_worker(session)
    return count


async def set_transcription(session: AsyncSession, clip_id: int, transcription: str) -> bool:
    """Replace a clip's transcript by hand; the clip counts as transcribed from now on."""
    result = await session.execute(
        update(Clip)
        .where(Clip.id == clip_id, Clip.status != ClipStatus.PROCESSING)
        .values(
            transcription=transcription.strip(),
            status=ClipStatus.DONE,
            last_error=None,
            retry_at=None,
            transcribed_at=func.now(),
        )
    )
    return bool(result.rowcount)


async def set_deleted(session: AsyncSession, clip_id: int, deleted: bool) -> bool:
    """Hide a clip from !clip and search (or bring it back). Discord is not touched."""
    result = await session.execute(
        update(Clip).where(Clip.id == clip_id).values(deleted=true() if deleted else false())
    )
    return bool(result.rowcount)


async def update_url(session: AsyncSession, clip_id: int, url: str) -> None:
    await session.execute(update(Clip).where(Clip.id == clip_id).values(url=url))
