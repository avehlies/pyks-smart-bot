"""Clip queries used by the bot: recording new clips and picking them back out.

Transcription state (`status`, `transcription`, ...) is owned by the separate clip worker; new
rows start as `pending` and the database trigger notifies the worker.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from yks.db.models import Clip, ClipStatus


@dataclass(frozen=True)
class NewClip:
    guild_id: int
    channel_id: int
    message_id: int
    attachment_id: int
    author_id: int | None
    filename: str
    content_type: str | None
    url: str


async def record_clips(session: AsyncSession, clips: Iterable[NewClip]) -> int:
    """Insert clips that aren't stored yet. Returns how many were new."""
    rows = [clip.__dict__ for clip in clips]
    if not rows:
        return 0
    stmt = (
        insert(Clip)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_clips_message_attachment")
        .returning(Clip.id)
    )
    return len((await session.scalars(stmt)).all())


# Random ids tried per query by `random_clip` before it falls back to sorting the whole table.
RANDOM_ID_TRIES = 25


async def random_clip(session: AsyncSession, rng: random.Random | None = None) -> Clip | None:
    """A uniformly random clip that isn't deleted.

    `ORDER BY random()` reads and sorts every row. Instead, guess random ids between the lowest
    and highest and take the first guess that is a live clip (rejection sampling, so every clip is
    equally likely despite gaps in the ids). The lookups use the primary key. Only if no guess
    hits, e.g. when most clips are deleted, fall back to the full sort.
    """
    rng = rng or random.Random()
    low, high = (await session.execute(select(func.min(Clip.id), func.max(Clip.id)))).one()
    if low is None:
        return None
    guesses = [rng.randint(low, high) for _ in range(RANDOM_ID_TRIES)]
    hits = {
        clip.id: clip
        for clip in await session.scalars(
            select(Clip).where(Clip.id.in_(set(guesses)), Clip.deleted.is_(False))
        )
    }
    for guess in guesses:  # the first hit in guess order (not the lowest id) keeps it uniform
        if guess in hits:
            return hits[guess]
    return await session.scalar(
        select(Clip).where(Clip.deleted.is_(False)).order_by(func.random()).limit(1)
    )


async def get_clip(session: AsyncSession, clip_id: int) -> Clip | None:
    return await session.get(Clip, clip_id)


async def set_clip_url(session: AsyncSession, clip_id: int, url: str) -> None:
    """Remember a freshly signed CDN link, so the next lookup can skip refreshing it."""
    await session.execute(update(Clip).where(Clip.id == clip_id).values(url=url))


async def mark_messages_deleted(session: AsyncSession, message_ids: Iterable[int]) -> int:
    ids = list(message_ids)
    if not ids:
        return 0
    result = await session.execute(
        update(Clip).where(Clip.message_id.in_(ids)).values(deleted=True)
    )
    return result.rowcount or 0


_WORDS = re.compile(r"[^\W_]+")


def prefix_tsquery_text(phrase: str) -> str | None:
    """`to_tsquery` text that ANDs the phrase's words and treats the last one as a prefix.

    Autocomplete runs on every keystroke, so the last word is usually half typed ("jetpa").
    Only letters and digits are kept, so the result can't contain tsquery syntax.
    """
    words = _WORDS.findall(phrase.lower())
    if not words:
        return None
    return " & ".join([*words[:-1], f"{words[-1]}:*"])


async def search_clips(session: AsyncSession, phrase: str, *, limit: int = 60) -> list[Clip]:
    """Transcribed clips matching `phrase`, best match first.

    Uses Postgres full-text search (stemmed, ranked; the last word may be partly typed). If that
    finds nothing, for example when the phrase is all stop words ("it is what it is"), fall back
    to a case-insensitive substring match (served by a trigram index).

    Only `id` and `transcription` are loaded: this runs on every autocomplete keystroke.
    """
    searchable = (
        Clip.deleted.is_(False),
        Clip.status == ClipStatus.DONE,
        Clip.transcription.is_not(None),
        Clip.transcription != "",
    )
    columns = load_only(Clip.id, Clip.transcription)
    if (text := prefix_tsquery_text(phrase)) is not None:
        query = func.to_tsquery("english", text)
        ranked = (
            select(Clip)
            .options(columns)
            .where(*searchable, Clip.search.op("@@")(query))
            .order_by(func.ts_rank(Clip.search, query).desc(), Clip.id.desc())
            .limit(limit)
        )
        found = list((await session.scalars(ranked)).all())
        if found:
            return found
    escaped = phrase.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    substring = (
        select(Clip)
        .options(columns)
        .where(*searchable, Clip.transcription.ilike(f"%{escaped}%", escape="\\"))
        .order_by(Clip.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(substring)).all())
