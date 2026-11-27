"""!short: a random YouTube Short from the show's channel.

The shorts are found and announced by the separate short-worker application (see
short-worker/README.md); the bot never talks to YouTube, it only reads the `youtube_shorts` table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from discord.ext import commands

from yks.bot.checks import SECOND, akairo_cooldown
from yks.db.shorts import random_short

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

NO_SHORTS = "No shorts found yet."


class Shorts(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context[Any]) -> bool:  # type: ignore[override]
        """Only in the shorts channel, when one is configured. Elsewhere `!short` is ignored
        (a failed check is silent, and runs before the cooldown, so it doesn't use it up)."""
        channel_id = self.bot.settings.yks_shorts_channel_id
        return channel_id is None or ctx.channel.id == channel_id

    @commands.command(name="short", aliases=["shorts"])
    @commands.guild_only()
    @akairo_cooldown(10 * SECOND)
    async def short(self, ctx: commands.Context[Any]) -> None:
        async with self.bot.db.session() as session:
            picked = await random_short(session)
        await ctx.reply(picked.url if picked else NO_SHORTS)


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Shorts(bot))
