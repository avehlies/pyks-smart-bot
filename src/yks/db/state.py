"""Global key/value state (latest episode titles and number)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yks.db.models import BotState

LATEST_MAIN_TITLE = "latest_main_title"
LATEST_BONUS_TITLE = "latest_bonus_title"
LATEST_EPISODE_NUMBER = "latest_episode_number"


async def get_state(session: AsyncSession, key: str) -> str | None:
    return await session.scalar(select(BotState.value).where(BotState.key == key))


async def set_state(session: AsyncSession, key: str, value: str) -> None:
    stmt = (
        insert(BotState)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=[BotState.key], set_={"value": value})
    )
    await session.execute(stmt)
