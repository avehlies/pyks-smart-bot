"""YouTube Shorts, as recorded by the separate short-worker.

The worker finds shorts and decides which are never to be posted (the back catalogue on its first
scan, old ones): it sets `announced_at` on those. The bot posts the rest. Posting is at most once:
a short is claimed (its `announced_at` set) in its own committed transaction *before* it's posted,
so a second poll, or a second bot process, can't take it too. If the post fails the claim is
released and the next poll tries again; if the bot dies between claiming and posting, that short is
never posted, which beats posting it twice.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yks.db.models import YouTubeShort


async def random_short(session: AsyncSession) -> YouTubeShort | None:
    """A uniformly random short. The table holds at most a few hundred rows, so a sort is fine."""
    return await session.scalar(select(YouTubeShort).order_by(func.random()).limit(1))


async def claim_unannounced(session: AsyncSession) -> YouTubeShort | None:
    """Claim the oldest short waiting to be posted, or None. Commit before posting it.

    Only shorts with a publish date qualify: the worker leaves undated ones (seen on the Shorts
    page but not yet in the RSS feed) waiting until the feed dates them, or skips them. SKIP LOCKED
    lets a concurrent claimer move on to another row instead of waiting for, then re-reading, this
    one; the `announced_at IS NULL` re-check in the UPDATE makes the claim stick either way.
    """
    oldest = (
        select(YouTubeShort.video_id)
        .where(YouTubeShort.announced_at.is_(None), YouTubeShort.published_at.is_not(None))
        .order_by(YouTubeShort.published_at, YouTubeShort.video_id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return await session.scalar(
        update(YouTubeShort)
        .where(YouTubeShort.video_id == oldest, YouTubeShort.announced_at.is_(None))
        .values(announced_at=func.now())
        .returning(YouTubeShort)
    )


async def set_announced_message(session: AsyncSession, video_id: str, message_id: int) -> None:
    await session.execute(
        update(YouTubeShort)
        .where(YouTubeShort.video_id == video_id)
        .values(announced_message_id=message_id)
    )


async def release_claim(session: AsyncSession, video_id: str) -> None:
    """Undo `claim_unannounced` after a failed post, so the next poll tries again."""
    await session.execute(
        update(YouTubeShort)
        .where(YouTubeShort.video_id == video_id, YouTubeShort.announced_message_id.is_(None))
        .values(announced_at=None)
    )
