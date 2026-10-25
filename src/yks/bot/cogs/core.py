"""Error handling and owner diagnostics."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from discord.ext import commands

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

# Failures the TypeScript bot ignored silently (Akairo emitted events nobody listened to).
SILENT_ERRORS = (
    commands.CommandNotFound,
    commands.CommandOnCooldown,
    commands.CheckFailure,  # also covers missing permissions, owner-only, guild-only
    commands.UserInputError,
)


class Core(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_command_error(
        self, ctx: commands.Context[Any], error: commands.CommandError
    ) -> None:
        if isinstance(error, SILENT_ERRORS):
            log.debug(
                "Ignored %s for %r from %s", type(error).__name__, ctx.invoked_with, ctx.author
            )
            return
        original = getattr(error, "original", error)
        log.error(
            "Command %r failed (author=%s, guild=%s)",
            ctx.command.qualified_name if ctx.command else ctx.invoked_with,
            ctx.author.id,
            ctx.guild.id if ctx.guild else None,
            exc_info=original,
        )

    @commands.command(name="ping", hidden=True)
    @commands.is_owner()
    async def ping(self, ctx: commands.Context[Any]) -> None:
        """Owner-only health check: gateway latency and database round-trip."""
        started = time.perf_counter()
        try:
            db_ok = await self.bot.db.ping()
        except Exception as exc:  # report, don't crash
            log.exception("Database ping failed")
            db_status = f"error ({type(exc).__name__})"
        else:
            db_status = f"ok ({(time.perf_counter() - started) * 1000:.0f} ms)" if db_ok else "bad"
        await ctx.reply(f"Pong. Gateway {self.bot.latency * 1000:.0f} ms, database {db_status}.")


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Core(bot))
