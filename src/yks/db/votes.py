"""`!best` episode votes: one per member per guild."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yks.db.models import BestEpisodeVote


async def get_vote(session: AsyncSession, guild_id: int, user_id: int) -> int | None:
    return await session.scalar(
        select(BestEpisodeVote.episode).where(
            BestEpisodeVote.guild_id == guild_id, BestEpisodeVote.user_id == user_id
        )
    )


async def set_vote(session: AsyncSession, guild_id: int, user_id: int, episode: int) -> None:
    stmt = (
        insert(BestEpisodeVote)
        .values(guild_id=guild_id, user_id=user_id, episode=episode)
        .on_conflict_do_update(
            index_elements=[BestEpisodeVote.guild_id, BestEpisodeVote.user_id],
            set_={"episode": episode, "voted_at": func.now()},
        )
    )
    await session.execute(stmt)


async def vote_totals(session: AsyncSession, guild_id: int) -> list[tuple[int, int]]:
    """(episode, votes) for every episode with at least one vote, in no particular order."""
    rows = await session.execute(
        select(BestEpisodeVote.episode, func.count())
        .where(BestEpisodeVote.guild_id == guild_id)
        .group_by(BestEpisodeVote.episode)
    )
    return [(episode, count) for episode, count in rows.all()]
