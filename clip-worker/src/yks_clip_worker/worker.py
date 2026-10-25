"""The worker loop: wait for clips, then refresh URL -> download -> transcribe -> store."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from datetime import timedelta
from pathlib import Path

import asyncpg
import httpx

from yks_clip_worker.config import WorkerSettings
from yks_clip_worker.discord_api import DiscordAPI, DiscordError
from yks_clip_worker.media import DownloadError, download
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

    def wake(self, *_: object) -> None:
        """asyncpg NOTIFY callback (connection, pid, channel, payload)."""
        self._wake.set()

    async def process_one(self) -> bool:
        """Handle the next ready clip. Returns False if the queue was empty."""
        job = await self.queue.claim()
        if job is None:
            return False
        log.info("Transcribing clip %s (%s), attempt %d", job.id, job.filename, job.attempts)
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
        try:
            url = await self.discord.fresh_attachment_url(
                job.channel_id, job.message_id, job.attachment_id
            )
        except (DiscordError, httpx.HTTPError) as exc:
            status = await self.queue.fail(job, str(exc), max_attempts=self.settings.max_attempts)
            log.warning("Clip %s: Discord API error (%s): %s", job.id, status, exc)
            return
        if url is None:
            log.info("Clip %s: message or attachment deleted; skipping.", job.id)
            await self.queue.mark_deleted(job)
            return
        if url != job.url:
            await self.queue.update_url(job, url)

        with tempfile.TemporaryDirectory(prefix="yks-clip-") as tmp:
            try:
                path = await download(
                    self.http, url, Path(tmp), max_bytes=self.settings.max_download_mb * 1024 * 1024
                )
            except DownloadError as exc:
                status = await self.queue.fail(
                    job, str(exc), max_attempts=self.settings.max_attempts, permanent=exc.permanent
                )
                log.warning("Clip %s: %s (%s)", job.id, exc, status)
                return
            text = await asyncio.to_thread(self.transcriber.transcribe, path)
        await self.queue.complete(job, text)
        log.info("Clip %s done: %d characters", job.id, len(text))

    async def drain(self) -> int:
        """Process clips until none are ready. Returns how many were handled."""
        handled = 0
        while await self.process_one():
            handled += 1
        return handled

    async def run(self, listen_conn: asyncpg.Connection) -> None:
        """Run forever: drain the queue, then sleep until NOTIFY or the poll interval."""
        await listen_conn.add_listener(NOTIFY_CHANNEL, self.wake)
        stale = timedelta(minutes=self.settings.stale_after_minutes)
        log.info("Clip worker ready; queue: %s", await self.queue.counts())
        while True:
            self._wake.clear()
            if reclaimed := await self.queue.reclaim_stale(stale):
                log.info("Put %d stuck clip(s) back in the queue.", reclaimed)
            await self.drain()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.settings.poll_interval_seconds
                )
