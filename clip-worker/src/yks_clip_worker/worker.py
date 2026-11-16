"""The worker loop: wait for clips, then refresh URL -> download -> transcribe -> store."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import asyncpg
import httpx

from yks_clip_worker.config import WorkerSettings
from yks_clip_worker.discord_api import DiscordAPI, DiscordError
from yks_clip_worker.media import DownloadError, download, link_is_fresh
from yks_clip_worker.queue import NOTIFY_CHANNEL, ClipQueue, Job
from yks_clip_worker.transcriber import Transcriber

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        *,
        queue: ClipQueue,
        discord: DiscordAPI,
        http: httpx.AsyncClient,
        transcriber: Transcriber,
        settings: WorkerSettings,
    ) -> None:
        self.queue = queue
        self.discord = discord
        self.http = http
        self.transcriber = transcriber
        self.settings = settings
        self._wake = asyncio.Event()
        self._last_progress = time.monotonic()  # the loop went round, or a clip finished

    def wake(self, *_: object) -> None:
        """asyncpg NOTIFY callback (connection, pid, channel, payload)."""
        self._wake.set()

    async def _claim(self) -> Job | None:
        """The next new clip; failing that, an old clip to re-transcribe with this model."""
        job = await self.queue.claim()
        if job is None and self.settings.retranscribe:
            job = await self.queue.claim_redo(
                max_attempts=self.settings.max_attempts,
                stale_after=timedelta(minutes=self.settings.stale_after_minutes),
            )
        return job

    async def process_one(self) -> bool:
        """Handle the next ready clip. Returns False if the queue was empty."""
        job = await self._claim()
        if job is None:
            return False
        log.info(
            "%s clip %s (%s), attempt %d",
            "Re-transcribing" if job.redo else "Transcribing",
            job.id,
            job.filename,
            job.attempts,
        )
        try:
            await self._process(job)
        except asyncio.CancelledError:
            await asyncio.shield(self.queue.release(job))
            raise
        except Exception as exc:  # anything unexpected: count it as a failed attempt
            log.exception("Clip %s failed", job.id)
            await self.queue.fail(
                job, f"{type(exc).__name__}: {exc}", max_attempts=self.settings.max_attempts
            )
        return True

    async def _process(self, job: Job) -> None:
        with tempfile.TemporaryDirectory(prefix="yks-clip-") as tmp:
            try:
                path = await self._download(job, Path(tmp))
            except DownloadError as exc:
                status = await self.queue.fail(
                    job, str(exc), max_attempts=self.settings.max_attempts, permanent=exc.permanent
                )
                log.warning("Clip %s: %s (%s)", job.id, exc, status)
                return
            if path is None:
                return  # already recorded (deleted, or a Discord API error)
            text = await self.transcriber.transcribe(path)
        await self.queue.complete(job, text)
        log.info("Clip %s done: %d characters", job.id, len(text))

    async def _download(self, job: Job, directory: Path) -> Path | None:
        """Download the clip. Returns None if the job was already recorded as deleted/failed.

        The stored CDN link is used as is while its signature is still valid (most clips are
        transcribed minutes after they're posted), which saves a Discord API call per clip. The
        message is only fetched for a fresh link when the stored one has expired or is refused.
        """
        max_bytes = self.settings.max_download_mb * 1024 * 1024
        if link_is_fresh(job.url):
            try:
                return await download(self.http, job.url, directory, max_bytes=max_bytes)
            except DownloadError as exc:
                if not exc.gone:
                    raise
                log.info("Clip %s: stored link refused (%s); refreshing it.", job.id, exc)

        try:
            url = await self.discord.fresh_attachment_url(
                job.channel_id, job.message_id, job.attachment_id
            )
        except (DiscordError, httpx.HTTPError) as exc:
            status = await self.queue.fail(job, str(exc), max_attempts=self.settings.max_attempts)
            log.warning("Clip %s: Discord API error (%s): %s", job.id, status, exc)
            return None
        if url is None:
            log.info("Clip %s: message or attachment deleted; skipping.", job.id)
            await self.queue.mark_deleted(job)
            return None
        if url != job.url:
            await self.queue.update_url(job, url)
        return await download(self.http, url, directory, max_bytes=max_bytes)

    async def drain(self) -> int:
        """Process clips until none are ready. Returns how many were handled."""
        handled = 0
        while await self.process_one():
            handled += 1
            self._last_progress = time.monotonic()
        return handled

    def _stall_limit(self) -> float:
        return self.settings.transcribe_timeout_seconds + self.settings.stall_margin_seconds

    def stalled(self) -> bool:
        """Has the worker been stuck on something for much longer than any clip should take?"""
        if self.settings.transcribe_timeout_seconds <= 0:
            return False  # no limit on a clip, so no way to tell
        return time.monotonic() - self._last_progress > self._stall_limit()

    async def beat(self) -> bool:
        """Touch the heartbeat file if the worker is healthy. Returns whether it did."""
        if self.stalled():
            log.warning("No progress for over %.0fs; not reporting healthy.", self._stall_limit())
            return False
        try:
            await asyncio.wait_for(self.queue.ping(), timeout=10)
        except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            log.warning("Database check failed (%r); not reporting healthy.", exc)
            return False
        self._touch_heartbeat()
        return True

    # A file in /tmp: these are too quick to bother a thread with.
    def _touch_heartbeat(self) -> None:
        Path(self.settings.heartbeat_file).touch()

    def _remove_heartbeat(self) -> None:
        with contextlib.suppress(OSError):
            Path(self.settings.heartbeat_file).unlink(missing_ok=True)

    async def heartbeat(self) -> None:
        """Run `beat` every HEARTBEAT_INTERVAL_SECONDS, for the Docker healthcheck."""
        while True:
            try:
                await self.beat()
            except OSError as exc:  # can't write the file
                log.warning("Could not write the heartbeat file: %s", exc)
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)

    async def run(self, listen_conn: asyncpg.Connection) -> None:
        """Run forever: drain the queue, then sleep until NOTIFY or the poll interval."""
        await listen_conn.add_listener(NOTIFY_CHANNEL, self.wake)
        stale = timedelta(minutes=self.settings.stale_after_minutes)
        log.info("Clip worker ready; queue: %s", await self.queue.counts())
        if self.settings.retranscribe and (
            backlog := await self.queue.redo_backlog(max_attempts=self.settings.max_attempts)
        ):
            log.info("%d clip(s) to re-transcribe with %s.", backlog, self.queue.model)
        heartbeat = asyncio.create_task(self.heartbeat())
        try:
            while True:
                self._wake.clear()
                self._last_progress = time.monotonic()
                if reclaimed := await self.queue.reclaim_stale(stale):
                    log.info("Put %d stuck clip(s) back in the queue.", reclaimed)
                await self.drain()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.settings.poll_interval_seconds
                    )
        finally:
            heartbeat.cancel()
            self._remove_heartbeat()  # a stopped worker isn't healthy
