"""Entry point: `yks-short-worker`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import asyncpg
import httpx

from yks_short_worker.config import WorkerSettings, get_settings
from yks_short_worker.store import ShortStore
from yks_short_worker.worker import Worker
from yks_short_worker.youtube import YouTube

log = logging.getLogger("yks_short_worker")


async def run(settings: WorkerSettings) -> None:
    # command_timeout: a query that hangs (e.g. a dropped connection) raises instead of stalling.
    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=2, command_timeout=60
    )
    http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    worker = Worker(
        store=ShortStore(pool),
        youtube=YouTube(
            http, handle=settings.youtube_handle, channel_id=settings.youtube_channel_id
        ),
        settings=settings,
    )
    task = asyncio.create_task(worker.run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        log.info("Shutting down.")
    finally:
        await http.aclose()
        await pool.close()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
