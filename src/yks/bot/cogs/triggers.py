"""Runs message triggers (regex easter eggs) on messages that aren't commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from yks.bot.client import YKSBot


class Triggers(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.author.id in self.bot.settings.blocked_user_ids:
            return
        if not message.content:
            return
        # Akairo only checked regex commands when the message wasn't a valid prefix command.
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        await self.bot.triggers.dispatch(message, owner_id=self.bot.owner_id)


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Triggers(bot))
