"""Small helpers shared by cogs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import discord

# Burgy left on 2021-04-07; every April 7 the !mug / !burgy cooldowns are lifted.
BURGY_LEFT = date(2021, 4, 7)


def local_today(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def is_burgy_day(today: date) -> bool:
    return (today.month, today.day) == (BURGY_LEFT.month, BURGY_LEFT.day)


def bot_is_burgy_day(bot: Any) -> bool:
    """`relax_when` hook for cooldowns: is it April 7 in the bot's timezone?"""
    return is_burgy_day(local_today(bot.settings.tz))


def days_since_burgy_left(today: date) -> int:
    return (today - BURGY_LEFT).days


def find_emoji(guild: discord.Guild | None, name: str) -> discord.Emoji | None:
    """Find a guild emoji by name, like Akairo's `resolveEmoji`.

    Exact (case-insensitive) name first, then the first emoji whose name contains `name`.
    """
    if guild is None:
        return None
    wanted = name.lower()
    partial: discord.Emoji | None = None
    for emoji in guild.emojis:
        emoji_name = emoji.name.lower()
        if emoji_name == wanted:
            return emoji
        if partial is None and wanted in emoji_name:
            partial = emoji
    return partial
