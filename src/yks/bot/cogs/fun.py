"""Hand-written easter eggs: !burgy, !emma, mention greetings, and "the ... store"."""

from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from yks.bot.checks import HOUR, MINUTE, akairo_cooldown
from yks.bot.triggers import Trigger
from yks.bot.utils import bot_is_burgy_day, days_since_burgy_left, find_emoji, local_today

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

EMMA_EMOJIS = ("hornypest", "hornytoo", "emma", "emma2", "emma3")

# Note: matches inside words too ("this" contains "hi"), exactly like the TS bot. It only fires
# when the bot is mentioned, so in practice it means "someone greeted the bot".
GREETING = re.compile(
    r"(hello|hey|hi|howdy|sup|good morning|good afternoon|good evening)", re.IGNORECASE
)
THE_STORE = re.compile(r"\b(a|an|da|the)\b [A-Za-z ]*\bstore\b", re.IGNORECASE)


class Fun(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot

    # --- !burgy -------------------------------------------------------------------------------

    @commands.command(name="burgy", hidden=True)
    @commands.guild_only()
    @akairo_cooldown(MINUTE, relax_when=bot_is_burgy_day)
    async def burgy(self, ctx: commands.Context[Any]) -> None:
        assert ctx.guild is not None
        if not self.bot.guild_gate.try_acquire(ctx.guild.id, "burgy", MINUTE):
            return
        days = days_since_burgy_left(local_today(self.bot.settings.tz))
        await ctx.send(f"Burgy left {days} days ago.")

    # --- !emma --------------------------------------------------------------------------------

    @commands.command(name="emma", hidden=True)
    @commands.guild_only()
    @akairo_cooldown(MINUTE)
    async def emma(self, ctx: commands.Context[Any]) -> None:
        assert ctx.guild is not None
        if not self.bot.guild_gate.try_acquire(ctx.guild.id, "emma", MINUTE):
            return
        found = [emoji for name in EMMA_EMOJIS if (emoji := find_emoji(ctx.guild, name))]
        if found:
            await ctx.send(str(random.choice(found)))

    # --- message triggers -----------------------------------------------------------------------

    async def cog_load(self) -> None:
        self.bot.triggers.add(
            Trigger(
                name="fun:hello",
                pattern=GREETING,
                handler=self._hello,
                cooldown_ms=HOUR,
                condition=self._mentions_bot,
            )
        )
        self.bot.triggers.add(Trigger(name="fun:thestore", pattern=THE_STORE, handler=self._store))

    async def cog_unload(self) -> None:
        self.bot.triggers.remove("fun:hello")
        self.bot.triggers.remove("fun:thestore")

    def _mentions_bot(self, message: discord.Message) -> bool:
        return self.bot.user is not None and any(
            user.id == self.bot.user.id for user in message.mentions
        )

    async def _hello(self, message: discord.Message) -> None:
        await message.channel.send("Can't talk, I'm at work. Let's meet face-to-face.")

    async def _store(self, message: discord.Message) -> None:
        emoji = find_emoji(message.guild, "thestore")
        if emoji is not None:
            await message.add_reaction(emoji)


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Fun(bot))
