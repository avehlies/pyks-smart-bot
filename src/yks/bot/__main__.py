from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from yks.bot.client import YKSBot
from yks.bot.web import start_web
from yks.config import Settings, get_settings
from yks.db import Database
from yks.log import setup_logging

log = logging.getLogger("yks")


def _log_unhandled(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    # Equivalent of the TS bot's process-level `unhandledRejection` listener.
    log.error(
        "Unhandled error in event loop: %s",
        context.get("message"),
        exc_info=context.get("exception"),
    )


async def run(settings: Settings) -> None:
    asyncio.get_running_loop().set_exception_handler(_log_unhandled)
    db = Database(settings.database_url)
    try:
        await db.ping()
        log.info("Connected to the database.")
        async with YKSBot(settings, db) as bot:
            web_runner = await start_web(bot, settings.port)
            try:
                await bot.start(settings.auth_token)
            finally:
                await web_runner.cleanup()
    finally:
        await db.dispose()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.auth_token:
        raise SystemExit("AUTH_TOKEN is not set.")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
