"""The `youtube_shorts` table (created by the bot's Alembic migrations; the shared contract).

The worker records shorts; the bot posts them. A row with `announced_at` NULL and a
`published_at` is one the bot will post (see the bot's yks/db/shorts.py), so the worker sets
`announced_at` on rows that must never be posted, in the same transaction that inserts or dates
them. Otherwise the bot could see them in between.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

import asyncpg

from yks_short_worker.youtube import Short


@dataclass(frozen=True)
class Recorded:
    new: list[str]  # video ids not seen before
    skipped: int  # shorts marked as never to be posted (first scan, too old, never dated)


class ShortStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def ping(self) -> bool:
        return await self.pool.fetchval("SELECT 1") == 1

    async def record(self, shorts: Sequence[Short], *, max_age: timedelta) -> Recorded:
        """Insert shorts not seen before, fill in missing titles/dates on known ones, and mark
        the ones the bot mustn't post, all in one transaction.

        - On the very first scan (an empty table) everything found is marked: that's the back
          catalogue, not news. Nothing is posted on a first deploy.
        - A short published more than `max_age` ago is marked: old news, found late.
        - A short with no date (seen only on the Shorts page) is left for the RSS feed to date,
          and marked if that hasn't happened `max_age` after it was first seen. Without a date, a
          new short can't be told apart from an old one the first scan missed.

        Runs the marking even when the scan found nothing, so undated shorts still age out.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            # Serialise concurrent workers, so two can't both think the table is empty.
            await conn.execute("LOCK TABLE youtube_shorts IN SHARE ROW EXCLUSIVE MODE")
            new: list[str] = []
            if shorts:
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
                new = [row["video_id"] for row in rows if row["inserted"]]
            result = await conn.execute(
                """
                UPDATE youtube_shorts SET announced_at = now()
                WHERE announced_at IS NULL
                  AND coalesce(published_at, first_seen_at) < now() - $1::interval
                """,
                max_age,
            )
        return Recorded(new=new, skipped=int(result.split()[-1]))
