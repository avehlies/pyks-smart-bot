"""!help [kickstarters|listen|other]"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from yks.bot.checks import MINUTE, akairo_cooldown

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

GREEN = 0x83C133

KICKSTARTER_COMMANDS = (
    (
        "`!topkickstarters` / `!topks`",
        "List the top 10 AI-generated kickstarters, ranked by :this: reactions.",
    ),
)

LISTEN_COMMANDS = (
    ("`!listen`", "Listen to the latest episode of the main feed."),
    ("`!listen play <episode number>`", "Listen to a specific episode of the main feed."),
    ("`!listen random`", "Listen to a random episode of the main feed."),
    ("`!listen pause`", "Pause the current episode being played."),
    ("`!listen stop`", "Stop the current episode being played."),
)

HELP_COMMANDS = (
    ("`!help kickstarters`", "List commands for the AI-generated kickstarters"),
    ("`!help listen`", "List all commands for listening to the podcast in voice chat"),
    ("`!help other`", "List almost all other commands"),
)


def other_commands(clip_channel: str) -> tuple[tuple[str, str], ...]:
    return (
        (
            "`!latest <feed>`",
            "List the latest episodes.\nThe argument `<feed>` is optional and can be "
            "`main`, `bonus`, or `both`.",
        ),
        ("`!today`", "List episodes released on today's date in previous years."),
        (
            "`!best <episode number>`",
            "Let everyone know your favorite episode of the main feed.\nYou can change this "
            "later.\n_It goes by the episode number listed in the title_.",
        ),
        ("`!clip` / `!climp`", f"Grab a random clip that has been posted in {clip_channel}."),
        (
            "`!topmail`",
            "List the top messages posted in the mailbag channel within the last 30 days.",
        ),
    )


def build_help_embed(category: str, *, clip_channel_id: int | None) -> discord.Embed:
    clip_channel = f"<#{clip_channel_id}>" if clip_channel_id else "the clip channel"
    fields = {
        "kickstarters": KICKSTARTER_COMMANDS,
        "listen": LISTEN_COMMANDS,
        "other": other_commands(clip_channel),
    }.get(category.lower(), HELP_COMMANDS)
    embed = discord.Embed(color=GREEN)
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    return embed


class Help(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot

    @commands.command(name="help")
    @commands.guild_only()
    @akairo_cooldown(MINUTE, rate=10)
    async def help(self, ctx: commands.Context[Any], category: str = "none") -> None:
        embed = build_help_embed(category, clip_channel_id=self.bot.settings.yks_clip_channel_id)
        await ctx.send(embed=embed)


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Help(bot))
