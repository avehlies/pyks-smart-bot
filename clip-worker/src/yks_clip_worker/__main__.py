"""Entry point: `yks-clip-worker`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import asyncpg
import httpx

from yks_clip_worker.config import WorkerSettings, get_settings
from yks_clip_worker.discord_api import DiscordAPI
from yks_clip_worker.queue import ClipQueue
from yks_clip_worker.transcriber import WhisperTranscriber
from yks_clip_worker.worker import Worker

log = logging.getLogger("yks_clip_worker")


async def run(settings: WorkerSettings) -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    listen_conn = await asyncpg.connect(settings.database_url)
    discord = DiscordAPI(settings.auth_token)
    http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True)
    worker = Worker(
        queue=ClipQueue(pool),
        discord=discord,
        http=http,
        transcriber=WhisperTranscriber(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            beam_size=settings.whisper_beam_size,
        ),
        settings=settings,
    )
    task = asyncio.create_task(worker.run(listen_conn))
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
        await discord.aclose()
        await listen_conn.close()
        await pool.close()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.auth_token:
        raise SystemExit("AUTH_TOKEN is not set (needed to refresh Discord attachment URLs).")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
