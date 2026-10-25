"""The transcription queue: the `clips` table, used through plain SQL.

Contract with the bot (schema owned by the bot's Alembic migrations):
* The bot inserts rows with `status = 'pending'`; a trigger sends `NOTIFY clips_new, '<id>'`.
* The worker moves a row `pending -> processing -> done | failed`, or marks it `deleted` +
  `skipped` if the Discord message is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import asyncpg

NOTIFY_CHANNEL = "clips_new"


@dataclass(frozen=True)
class Job:
    id: int
    channel_id: int
    message_id: int
    attachment_id: int
    url: str
    filename: str
    attempts: int  # including this one


def backoff(attempts: int) -> timedelta:
    """Wait 1, 4, 9, ... minutes after the 1st, 2nd, 3rd failure."""
    return timedelta(minutes=attempts * attempts)


class ClipQueue:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def reclaim_stale(self, older_than: timedelta) -> int:
        """Put back clips left in `processing` (worker crashed or was killed mid-job)."""
        result = await self.pool.execute(
            """
            UPDATE clips SET status = 'pending', claimed_at = NULL
            WHERE status = 'processing' AND claimed_at < now() - $1::interval
            """,
            older_than,
        )
        return int(result.split()[-1])

    async def claim(self) -> Job | None:
        """Take the newest ready clip, so fresh posts are searchable quickly even during a
        big backfill. SKIP LOCKED lets several workers run side by side."""
        row = await self.pool.fetchrow(
            """
            UPDATE clips
            SET status = 'processing', claimed_at = now(), attempts = attempts + 1
            WHERE id = (
                SELECT id FROM clips
                WHERE status = 'pending' AND NOT deleted
                  AND (retry_at IS NULL OR retry_at <= now())
                ORDER BY id DESC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, channel_id, message_id, attachment_id, url, filename, attempts
            """
        )
        return Job(**dict(row)) if row else None

    async def complete(self, job: Job, transcription: str) -> None:
        await self.pool.execute(
            """
            UPDATE clips SET status = 'done', transcription = $2, transcribed_at = now(),
                claimed_at = NULL, retry_at = NULL, last_error = NULL
            WHERE id = $1
            """,
            job.id,
            transcription,
        )

    async def fail(
        self, job: Job, error: str, *, max_attempts: int, permanent: bool = False
    ) -> str:
        """Record a failed attempt: retry later with backoff, or give up. Returns the new status."""
        status = "failed" if permanent or job.attempts >= max_attempts else "pending"
        await self.pool.execute(
            """
            UPDATE clips SET status = $2::varchar, last_error = $3, claimed_at = NULL,
                retry_at = CASE WHEN $2::varchar = 'pending' THEN now() + $4::interval END
            WHERE id = $1
            """,
            job.id,
            status,
            error[:2000],
            backoff(job.attempts),
        )
        return status

    async def release(self, job: Job) -> None:
        """Hand a job back untouched (shutting down mid-job): this attempt doesn't count."""
        await self.pool.execute(
            """
            UPDATE clips SET status = 'pending', claimed_at = NULL,
                attempts = greatest(attempts - 1, 0)
            WHERE id = $1 AND status = 'processing'
            """,
            job.id,
        )

    async def mark_deleted(self, job: Job) -> None:
        await self.pool.execute(
            """
            UPDATE clips SET deleted = true, status = 'skipped', claimed_at = NULL,
                last_error = 'Discord message or attachment no longer exists'
            WHERE id = $1
            """,
            job.id,
        )

    async def update_url(self, job: Job, url: str) -> None:
        await self.pool.execute("UPDATE clips SET url = $2 WHERE id = $1", job.id, url)

    async def counts(self) -> dict[str, int]:
        rows = await self.pool.fetch("SELECT status, count(*) AS n FROM clips GROUP BY status")
        return {row["status"]: row["n"] for row in rows}
