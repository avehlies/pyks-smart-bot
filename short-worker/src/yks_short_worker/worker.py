"""The worker loop: every SCAN_INTERVAL_MINUTES, scan YouTube and record what's there.

Posting new shorts in Discord is the bot's job (it polls the table); this app never talks to
Discord.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from pathlib import Path

from yks_short_worker.config import WorkerSettings
from yks_short_worker.store import Recorded, ShortStore
from yks_short_worker.youtube import ScanError, YouTube

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, *, store: ShortStore, youtube: YouTube, settings: WorkerSettings) -> None:
        self.store = store
        self.youtube = youtube
        self.settings = settings

    async def scan_once(self) -> Recorded:
        try:
            shorts = await self.youtube.scan()
        except ScanError as exc:
            log.error("YouTube scan failed: %s", exc)
            shorts = []
        recorded = await self.store.record(
            shorts, max_age=timedelta(hours=self.settings.announce_max_age_hours)
        )
        if recorded.new:
            log.info("Found %d new short(s): %s", len(recorded.new), ", ".join(recorded.new))
        if recorded.skipped:
            log.info("Marked %d short(s) as not to be posted (old or undated).", recorded.skipped)
        return recorded

    async def run(self) -> None:
        log.info(
            "Short worker ready; scanning @%s every %g minutes.",
            self.settings.youtube_handle,
            self.settings.scan_interval_minutes,
        )
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            while True:
                try:
                    await self.scan_once()
                except Exception:  # the database had a hiccup: try again next time
                    log.exception("Scan failed")
                await asyncio.sleep(self.settings.scan_interval_minutes * 60)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def beat(self) -> bool:
        """Touch the heartbeat file if the database answers."""
        try:
            ok = await self.store.ping()
        except Exception as exc:
            log.warning("Heartbeat: database check failed: %s", exc)
            return False
        if ok:
            await asyncio.to_thread(Path(self.settings.heartbeat_file).touch)
        return ok

    async def _heartbeat(self) -> None:
        while True:
            await self.beat()
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)
