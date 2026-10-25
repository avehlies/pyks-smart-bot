"""Member welcome image and guild settings: !welcome, !rssChannel, !newmember."""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from yks.bot.welcome_image import render_welcome_image
from yks.db import guilds

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

# Besides administrators, this user may set the RSS channel (from the TS bot).
RSS_CHANNEL_EDITOR_ID = 329288617564569602

# Verbatim from the TypeScript bot.
WELCOME_GREETINGS = (
    "You don't have to be insane to post here, but it's a \"good to have.\"",
    "I give it a month.",
    (
        "Make yourself at home.\nOh ok, you're going straight for the nasty channel. Ah! Well. "
        "Nevertheless,"
    ),
    "It's not too late to just turn around and walk away. No one would blame you.",
    (
        "Grab an empty chair in the circle. We're just about to start sharing how YKS ruined "
        "our lives."
    ),
    "If you need to know what episode something happened in, ask vinny.",
    "If you see JF or DB in here, avert your eyes from their posts as a sign of respect.",
    "Vote for your favorite episode # using the command `!best <episode number>`",
    (
        "Coming in on a bad note. Don't know ya and don't know if I care to given who ya sight "
        "as a source. Name's Saley, the GodKing. I've been accused of being a bit of a "
        "peacekeeper and ring leader of our little rag tag group. Ninja is a trusted second, "
        "and Roman is my official go to boy on vibes. Just as a fair warning, don't bring that "
        "name up again. You won't like what you get. That being said, strike 1, and welcome to "
        "the group."
    ),
)

_CHANNEL_MENTION = re.compile(r"^<#(\d+)>$")


def resolve_channel(guild: discord.Guild, argument: str) -> Any | None:
    """Find a channel by mention, ID, or (case-insensitive) name, like Akairo's `channel` type."""
    argument = argument.strip()
    match = _CHANNEL_MENTION.match(argument)
    if match or argument.isdigit():
        return guild.get_channel(int(match.group(1) if match else argument))
    name = argument.removeprefix("#").lower()
    return next((c for c in guild.channels if c.name.lower() == name), None)


def can_edit_rss_channel(ctx: commands.Context[Any]) -> bool:
    if ctx.guild is None:
        return False
    if ctx.author.id == RSS_CHANNEL_EDITOR_ID:
        return True
    return ctx.permissions.administrator


class Welcome(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot

    # --- member join ----------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.welcome(member)

    async def welcome(self, member: discord.Member) -> bool:
        """Post the welcome image for a member, once per guild. Returns True if posted."""
        guild = member.guild
        channel = guild.system_channel
        async with self.bot.db.session() as session:
            if await guilds.is_welcome_disabled(session, guild.id):
                return False
            if not await guilds.mark_welcomed(session, guild.id, member.id):
                log.info("Already welcomed %s in %s; skipping.", member.id, guild.id)
                return False
        if channel is None:
            log.info("Guild %s has no system channel; not welcoming %s.", guild.id, member.id)
            return False

        avatar = await member.display_avatar.replace(format="png", size=256).read()
        assets = self.bot.settings.assets_dir
        image = await asyncio.to_thread(
            render_welcome_image,
            member.display_name,
            avatar,
            background_path=assets / "jf-blessing.png",
            font_path=assets / "fonts" / "DejaVuSans.ttf",
        )
        await channel.send(
            content=f"Welcome, {member.mention}! {random.choice(WELCOME_GREETINGS)}",
            file=discord.File(io.BytesIO(image), filename="welcome-image.png"),
        )
        return True

    # --- !welcome -------------------------------------------------------------------------------

    @commands.command(name="welcome")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def toggle_welcome(self, ctx: commands.Context[Any]) -> None:
        assert ctx.guild is not None
        async with self.bot.db.session() as session:
            disabled = await guilds.toggle_welcome(session, ctx.guild.id)
        await ctx.send(f"Welcome message {'disabled' if disabled else 'enabled'}.")

    # --- !rssChannel ----------------------------------------------------------------------------

    @commands.command(name="rsschannel")
    @commands.guild_only()
    @commands.check(can_edit_rss_channel)
    async def rss_channel(self, ctx: commands.Context[Any], *, channel: str = "") -> None:
        assert ctx.guild is not None
        target = resolve_channel(ctx.guild, channel) if channel else None
        if target is None:
            await ctx.send("That channel does not exist.")
            return
        if getattr(target, "type", None) is not discord.ChannelType.text:
            await ctx.reply(f"{target.mention} is not a text channel. No change.")
            return
        async with self.bot.db.session() as session:
            await guilds.set_rss_channel(session, ctx.guild.id, target.id)
        await target.send(f"RSS feed updates will come to {target.mention}.")

    # --- !newmember (owner only) ------------------------------------------------------------------

    @commands.command(name="newmember", hidden=True)
    @commands.is_owner()
    async def new_member(self, ctx: commands.Context[Any], member_id: int) -> None:
        """Re-run the welcome for a member of the YKS guild (for testing)."""
        guild_id = self.bot.settings.yks_guild_id
        guild = self.bot.get_guild(guild_id) if guild_id else None
        if guild is None:
            return
        member = guild.get_member(member_id)
        if member is None:
            try:
                member = await guild.fetch_member(member_id)
            except discord.HTTPException:
                return
        await self.welcome(member)


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Welcome(bot))
