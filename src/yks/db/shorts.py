"""YouTube Shorts, as recorded by the separate short-worker. The bot only reads them."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yks.db.models import YouTubeShort


async def random_short(session: AsyncSession) -> YouTubeShort | None:
    """A uniformly random short. The table holds at most a few hundred rows, so a sort is fine."""
    return await session.scalar(select(YouTubeShort).order_by(func.random()).limit(1))
