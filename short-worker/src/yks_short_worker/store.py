"""The `youtube_shorts` table (created by the bot's Alembic migrations; the shared contract)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import asyncpg

from yks_short_worker.youtube import Short


@dataclass(frozen=True)
class Pending:
    """A short that hasn't been posted (or skipped) yet."""

    video_id: str
    title: str | None
    published_at: datetime | None
    first_seen_at: datetime


class ShortStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def ping(self) -> bool:
        return await self.pool.fetchval("SELECT 1") == 1

    async def record(self, shorts: Sequence[Short]) -> list[str]:
        """Insert shorts not seen before and fill in missing titles/dates on known ones. Returns
        the new video ids.

        On the very first scan (an empty table) everything found is marked as already announced:
        that's the back catalogue, not news.
        """
        if not shorts:
            return []
        async with self.pool.acquire() as conn, conn.transaction():
            # Serialise concurrent workers, so two can't both think the table is empty.
            await conn.execute("LOCK TABLE youtube_shorts IN SHARE ROW EXCLUSIVE MODE")
            first_scan = not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM youtube_shorts)")
            rows = await conn.fetch(
                """
                INSERT INTO youtube_shorts AS s (video_id, title, published_at, announced_at)
                SELECT v.video_id, v.title, v.published_at,
                       CASE WHEN $4::boolean THEN now() END
                FROM unnest($1::text[], $2::text[], $3::timestamptz[])
                     AS v(video_id, title, published_at)
                ON CONFLICT (video_id) DO UPDATE SET
                    title = coalesce(EXCLUDED.title, s.title),
                    published_at = coalesce(s.published_at, EXCLUDED.published_at)
                RETURNING video_id, (xmax = 0) AS inserted
                """,
                [s.video_id for s in shorts],
                [s.title for s in shorts],
                [s.published_at for s in shorts],
                first_scan,
            )
        return [row["video_id"] for row in rows if row["inserted"]]

    async def pending(self) -> list[Pending]:
        """Shorts ready to be posted, oldest first.

        Only shorts with a publish date (from the RSS feed) qualify. One seen only on the Shorts
        page waits for the feed to date it: without a date there's no telling a new short from an
        old one that the first scan missed (say the page was down then, and the feed only lists
        the newest 15). New shorts always show up in the feed, so they get a date soon enough.
        """
        rows = await self.pool.fetch(
            """
            SELECT video_id, title, published_at, first_seen_at FROM youtube_shorts
            WHERE announced_at IS NULL AND published_at IS NOT NULL
            ORDER BY published_at, video_id
            """
        )
        return [Pending(**dict(row)) for row in rows]

    async def mark_announced(self, video_id: str, message_id: int | None) -> None:
        await self.pool.execute(
            """
            UPDATE youtube_shorts SET announced_at = now(), announced_message_id = $2
            WHERE video_id = $1
            """,
            video_id,
            message_id,
        )

    async def skip_older_than(self, age: timedelta) -> int:
        """Mark pending shorts older than `age` as dealt with, without posting them: dated ones
        published that long ago, and undated ones the feed hasn't dated in that long."""
        result = await self.pool.execute(
            """
            UPDATE youtube_shorts SET announced_at = now()
            WHERE announced_at IS NULL
              AND coalesce(published_at, first_seen_at) < now() - $1::interval
            """,
            age,
        )
        return int(result.split()[-1])
