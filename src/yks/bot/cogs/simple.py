"""Registers the config-driven commands from `yks.bot.simple_commands`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from yks.bot.checks import DEFAULT_COOLDOWN_MS, akairo_cooldown
from yks.bot.simple_commands import SIMPLE_COMMANDS, SimpleCommand, run_simple_command
from yks.bot.triggers import Trigger

if TYPE_CHECKING:
    from yks.bot.client import YKSBot


def _build_command(bot: YKSBot, spec: SimpleCommand) -> commands.Command[Any, ..., Any]:
    async def callback(ctx: commands.Context[Any]) -> None:
        await run_simple_command(
            spec, ctx.message, gate=bot.guild_gate, assets_dir=bot.settings.assets_dir
        )

    command: commands.Command[Any, ..., Any] = commands.Command(
        callback, name=spec.aliases[0], aliases=list(spec.aliases[1:]), hidden=True
    )
    if spec.cooldown_ms is not None:
        command = akairo_cooldown(spec.cooldown_ms, spec.rate, relax_when=spec.relax_cooldown_when)(
            command
        )
    return command


def _build_trigger(bot: YKSBot, spec: SimpleCommand) -> Trigger:
    assert spec.regex is not None

    async def handler(message: discord.Message) -> None:
        await run_simple_command(
            spec, message, gate=bot.guild_gate, assets_dir=bot.settings.assets_dir
        )

    return Trigger(
        name=f"simple:{spec.id}",
        pattern=spec.regex,
        handler=handler,
        cooldown_ms=spec.cooldown_ms if spec.cooldown_ms is not None else DEFAULT_COOLDOWN_MS,
        rate=spec.rate,
        guild_only=spec.require_guild,
    )


class SimpleCommands(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot
        self._command_names: list[str] = []
        self._trigger_names: list[str] = []

    async def cog_load(self) -> None:
        for spec in SIMPLE_COMMANDS:
            if spec.aliases:
                command = _build_command(self.bot, spec)
                self.bot.add_command(command)
                self._command_names.append(command.name)
            else:
                trigger = _build_trigger(self.bot, spec)
                self.bot.triggers.add(trigger)
                self._trigger_names.append(trigger.name)

    async def cog_unload(self) -> None:
        for name in self._command_names:
            self.bot.remove_command(name)
        for name in self._trigger_names:
            self.bot.triggers.remove(name)


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(SimpleCommands(bot))
