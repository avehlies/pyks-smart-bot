"""Database schema.

Discord IDs (snowflakes) are stored as BIGINT. Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _now_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GuildSettings(Base):
    """Per-guild toggles. Replaces Akairo's `welcomeMsgDisabled` and `rssChannel` settings."""

    __tablename__ = "guild_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    welcome_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    rss_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WelcomedMember(Base):
    """Members who already got the welcome image, so rejoining doesn't trigger it again."""

    __tablename__ = "welcomed_members"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    welcomed_at: Mapped[datetime] = _now_column()


class BotState(Base):
    """Small global key/value store (latest episode titles, latest episode number)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BestEpisodeVote(Base):
    """One `!best` vote per member per guild. Totals are computed with GROUP BY."""

    __tablename__ = "best_episode_votes"
    __table_args__ = (CheckConstraint("episode > 0", name="episode_positive"),)

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    voted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TallyKind(enum.StrEnum):
    MAILBAG = "mailbag"
    KICKSTARTER = "kickstarter"


class ReactionTally(Base):
    """`:this:` reaction counts for mailbag messages and generated kickstarters."""

    __tablename__ = "reaction_tallies"
    __table_args__ = (
        CheckConstraint("count >= 0", name="count_non_negative"),
        Index("ix_reaction_tallies_kind_count", "kind", "guild_id", "count"),
    )

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    kind: Mapped[TallyKind] = mapped_column(
        Enum(
            TallyKind,
            name="kind",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    message_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClipStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # transcription disabled or not applicable


class Clip(Base):
    """One row per audio/video attachment posted in the clip channel.

    The bot inserts `pending` rows; the separate clip-worker application claims them,
    transcribes, and marks them `done`/`failed` (it uses this table directly, with SQL).
    A trigger (see the initial migration) sends `NOTIFY clips_new, '<id>'` on every insert so
    the worker picks new clips up immediately.
    """

    __tablename__ = "clips"
    __table_args__ = (
        UniqueConstraint("message_id", "attachment_id", name="uq_clips_message_attachment"),
        Index("ix_clips_status", "status", "id"),
        Index("ix_clips_search", "search", postgresql_using="gin"),
        # Substring search (/findtheclimp's ILIKE fallback). Needs the pg_trgm extension.
        Index(
            "ix_clips_transcription_trgm",
            "transcription",
            postgresql_using="gin",
            postgresql_ops={"transcription": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    attachment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_id: Mapped[int | None] = mapped_column(BigInteger)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    # Last known (signed, expiring) CDN URL. Always refresh via the API before downloading.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    transcription: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ClipStatus] = mapped_column(
        Enum(
            ClipStatus,
            name="status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=ClipStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Not before this time (backoff after a failed attempt). NULL = as soon as possible.
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transcribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The Whisper model that wrote `transcription` (e.g. "small.en"). The worker re-transcribes
    # `done` clips whose model differs from its own. NULL means unknown, and is left alone.
    transcribed_model: Mapped[str | None] = mapped_column(Text)

    search: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(transcription, ''))", persisted=True),
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()


class YouTubeShort(Base):
    """One row per YouTube Short on the show's channel.

    The separate short-worker application finds shorts and fills this table (with SQL); it posts
    each new one in the shorts channel and sets `announced_at`. The bot only reads it (`!short`).
    """

    __tablename__ = "youtube_shorts"

    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    # From the channel's RSS feed; NULL for shorts only seen on the channel's Shorts page.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = _now_column()
    # Set once the short has been dealt with: posted (`announced_message_id` is the message), or
    # skipped (found on the first scan, or already old when found).
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    announced_message_id: Mapped[int | None] = mapped_column(BigInteger)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/shorts/{self.video_id}"
