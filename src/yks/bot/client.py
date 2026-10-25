from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from yks.bot.checks import DefaultCooldown, GuildGate
from yks.bot.triggers import TriggerRegistry
from yks.config import Settings
from yks.db import Database

log = logging.getLogger(__name__)

# Cogs loaded at startup. Each phase adds its own.
EXTENSIONS: tuple[str, ...] = (
    "yks.bot.cogs.core",
    "yks.bot.cogs.triggers",
    "yks.bot.cogs.simple",
    "yks.bot.cogs.fun",
    "yks.bot.cogs.help",
)


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()  # guilds, messages, reactions, voice states, ...
    intents.members = True  # welcome messages
    intents.message_content = True  # prefix commands and easter-egg regexes
    return intents


class YKSBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or(settings.command_prefix),
            case_insensitive=True,
            intents=build_intents(),
            owner_id=settings.owner_id,
            help_command=None,  # replaced by the bot's own !help
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
        )
        self.settings = settings
        self.db = db
        self.guild_gate = GuildGate()
        self.triggers = TriggerRegistry()
        self.add_check(DefaultCooldown())
        self.add_check(self._not_blocked)

    async def _not_blocked(self, ctx: commands.Context[Any]) -> bool:
        return ctx.author.id not in self.settings.blocked_user_ids

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded extension %s", extension)

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (%s) in %d guild(s)", self.user, self.user.id, len(self.guilds))
