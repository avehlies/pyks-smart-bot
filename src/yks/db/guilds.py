"""Queries for per-guild settings and the welcomed-members list.

Each function is a single statement, so concurrent events (two members joining at once, two
admins toggling) can't race.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yks.db.models import GuildSettings, WelcomedMember


async def is_welcome_disabled(session: AsyncSession, guild_id: int) -> bool:
    disabled = await session.scalar(
        select(GuildSettings.welcome_disabled).where(GuildSettings.guild_id == guild_id)
    )
    return bool(disabled)


async def toggle_welcome(session: AsyncSession, guild_id: int) -> bool:
    """Flip the welcome-message setting. Returns True if it is now *disabled*."""
    stmt = (
        insert(GuildSettings)
        .values(guild_id=guild_id, welcome_disabled=True)  # first toggle disables it
        .on_conflict_do_update(
            index_elements=[GuildSettings.guild_id],
            set_={"welcome_disabled": ~GuildSettings.welcome_disabled},
        )
        .returning(GuildSettings.welcome_disabled)
    )
    return bool(await session.scalar(stmt))


async def get_rss_channel_id(session: AsyncSession, guild_id: int) -> int | None:
    return await session.scalar(
        select(GuildSettings.rss_channel_id).where(GuildSettings.guild_id == guild_id)
    )


async def set_rss_channel(session: AsyncSession, guild_id: int, channel_id: int | None) -> None:
    stmt = (
        insert(GuildSettings)
        .values(guild_id=guild_id, rss_channel_id=channel_id)
        .on_conflict_do_update(
            index_elements=[GuildSettings.guild_id], set_={"rss_channel_id": channel_id}
        )
    )
    await session.execute(stmt)


async def mark_welcomed(session: AsyncSession, guild_id: int, user_id: int) -> bool:
    """Record that a member was welcomed. Returns False if they already had been."""
    stmt = (
        insert(WelcomedMember)
        .values(guild_id=guild_id, user_id=user_id)
        .on_conflict_do_nothing()
        .returning(WelcomedMember.user_id)
    )
    return (await session.scalar(stmt)) is not None
