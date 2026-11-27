"""The worker loop: every SCAN_INTERVAL_MINUTES, scan YouTube, record, post what's new."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from pathlib import Path

from yks_short_worker.config import WorkerSettings
from yks_short_worker.discord_api import DiscordAPI, DiscordError
from yks_short_worker.store import Pending, ShortStore
from yks_short_worker.youtube import ScanError, YouTube, short_url

log = logging.getLogger(__name__)


def announcement(short: Pending) -> str:
    # Discord unfurls the link into the title and thumbnail; no need to repeat the title.
    return f"New short! {short_url(short.video_id)}"


class Worker:
    def __init__(
        self,
        *,
        store: ShortStore,
        youtube: YouTube,
        discord: DiscordAPI,
        settings: WorkerSettings,
    ) -> None:
        self.store = store
        self.youtube = youtube
        self.discord = discord
        self.settings = settings
        self._warned_no_channel = False

    async def scan_once(self) -> list[str]:
        """Scan, record and announce. Returns the video ids posted."""
        try:
            shorts = await self.youtube.scan()
        except ScanError as exc:
            log.error("YouTube scan failed: %s", exc)
            shorts = []
        new = await self.store.record(shorts)
        if new:
            log.info("Found %d new short(s): %s", len(new), ", ".join(new))
        return await self.announce()

    async def announce(self) -> list[str]:
        """Post every pending short that's recent enough; quietly mark older ones as done.

        A short whose post fails stays pending and is tried again on the next scan."""
        skipped = await self.store.skip_older_than(
            timedelta(hours=self.settings.announce_max_age_hours)
        )
        if skipped:
            log.info("Recorded %d older short(s) without posting them.", skipped)
        pending = await self.store.pending()
        channel_id = self.settings.yks_shorts_channel_id
        if not pending:
            return []
        if channel_id is None:
            if not self._warned_no_channel:
                log.warning("YKS_SHORTS_CHANNEL_ID is not set; new shorts aren't being posted.")
                self._warned_no_channel = True
            return []
        posted = []
        for short in pending:
            try:
                message = await self.discord.send_message(channel_id, announcement(short))
            except DiscordError as exc:
                log.error("Couldn't post short %s: %s", short.video_id, exc)
                break  # the same problem (permissions, token) would hit the rest too
            await self.store.mark_announced(short.video_id, int(message["id"]))
            log.info("Posted short %s (%s)", short.video_id, short.title)
            posted.append(short.video_id)
        return posted

    async def run(self) -> None:
        log.info(
            "Short worker ready; scanning @%s every %g minutes, posting in channel %s.",
            self.settings.youtube_handle,
            self.settings.scan_interval_minutes,
            self.settings.yks_shorts_channel_id or "(none: YKS_SHORTS_CHANNEL_ID not set)",
        )
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            while True:
                try:
                    await self.scan_once()
                except Exception:  # the database or Discord had a hiccup: try again next time
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
