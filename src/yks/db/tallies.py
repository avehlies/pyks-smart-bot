""":this: reaction tallies for mailbag messages and generated kickstarters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yks.db.models import ReactionTally, TallyKind


async def set_tally(
    session: AsyncSession,
    *,
    kind: TallyKind,
    guild_id: int,
    channel_id: int,
    message_id: int,
    message_created_at: datetime,
    count: int,
) -> None:
    """Store the current reaction count for a message (removing it at zero)."""
    if count <= 0:
        await delete_tally(session, message_id)
        return
    stmt = (
        insert(ReactionTally)
        .values(
            message_id=message_id,
            kind=kind,
            guild_id=guild_id,
            channel_id=channel_id,
            count=count,
            message_created_at=message_created_at,
        )
        .on_conflict_do_update(index_elements=[ReactionTally.message_id], set_={"count": count})
    )
    await session.execute(stmt)


async def delete_tally(session: AsyncSession, message_id: int) -> None:
    await session.execute(delete(ReactionTally).where(ReactionTally.message_id == message_id))


async def top_tallies(
    session: AsyncSession,
    kind: TallyKind,
    guild_id: int,
    *,
    since: datetime | None = None,
    limit: int = 50,
) -> Sequence[ReactionTally]:
    """Most-reacted messages first (older messages win ties)."""
    stmt = select(ReactionTally).where(
        ReactionTally.kind == kind, ReactionTally.guild_id == guild_id
    )
    if since is not None:
        stmt = stmt.where(ReactionTally.message_created_at >= since)
    stmt = stmt.order_by(ReactionTally.count.desc(), ReactionTally.message_id).limit(limit)
    return (await session.scalars(stmt)).all()


async def prune_older_than(session: AsyncSession, kind: TallyKind, cutoff: datetime) -> int:
    """Delete tallies for messages posted before `cutoff`. Returns how many were removed."""
    result = await session.execute(
        delete(ReactionTally).where(
            ReactionTally.kind == kind, ReactionTally.message_created_at < cutoff
        )
    )
    return result.rowcount or 0
