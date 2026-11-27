"""youtube shorts

The `youtube_shorts` table: filled by the short-worker, read by the bot's `!short`.

Revision ID: 3a9d7e1c5b42
Revises: 8b3f6c2d4e71
Create Date: 2026-11-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3a9d7e1c5b42"
down_revision: str | None = "8b3f6c2d4e71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "youtube_shorts",
        sa.Column("video_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("announced_message_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("video_id", name=op.f("pk_youtube_shorts")),
    )


def downgrade() -> None:
    op.drop_table("youtube_shorts")
