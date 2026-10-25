import asyncio

import asyncpg
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from yks.db import Clip, ClipStatus, Database, ReactionTally, TallyKind


def make_clip(**overrides: object) -> Clip:
    values: dict[str, object] = {
        "guild_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "attachment_id": 4,
        "filename": "clip.mp4",
        "content_type": "video/mp4",
        "url": "https://cdn.discordapp.com/attachments/2/4/clip.mp4?ex=1",
    }
    values.update(overrides)
    return Clip(**values)


async def test_ping(db: Database) -> None:
    assert await db.ping()


async def test_clip_defaults_and_search(db: Database) -> None:
    async with db.session() as session:
        session.add(make_clip(transcription="Ohh no, they did another one"))
        session.add(make_clip(attachment_id=5, transcription="cunch wrap supreme"))

    async with db.session() as session:
        clip = await session.scalar(select(Clip).where(Clip.attachment_id == 4))
        assert clip is not None
        assert clip.status is ClipStatus.PENDING
        assert clip.attempts == 0
        assert clip.deleted is False

        query = func.websearch_to_tsquery("english", "another one")
        hits = (await session.scalars(select(Clip).where(Clip.search.op("@@")(query)))).all()
        assert [hit.attachment_id for hit in hits] == [4]


async def test_clip_unique_per_attachment(db: Database) -> None:
    async with db.session() as session:
        session.add(make_clip())
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            session.add(make_clip())


async def test_insert_sends_notify(db: Database, database_url: str) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    conn = await asyncpg.connect(database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.add_listener("clips_new", lambda *args: received.put_nowait(args[3]))
        async with db.session() as session:
            clip = make_clip()
            session.add(clip)
            await session.flush()
            clip_id = clip.id
        assert await asyncio.wait_for(received.get(), timeout=5) == str(clip_id)
    finally:
        await conn.close()


async def test_reaction_tally_count_cannot_go_negative(db: Database) -> None:
    from datetime import UTC, datetime

    with pytest.raises(IntegrityError):
        async with db.session() as session:
            session.add(
                ReactionTally(
                    message_id=1,
                    kind=TallyKind.MAILBAG,
                    guild_id=1,
                    channel_id=2,
                    count=-1,
                    message_created_at=datetime.now(UTC),
                )
            )
