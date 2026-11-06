"""clip transcription trigram index

Indexes `clips.transcription` for `ILIKE '%...%'`, the substring fallback of /findtheclimp's
search, which otherwise reads every transcript. pg_trgm ships with Postgres and is a trusted
extension, so the database owner can create it.

Revision ID: 5d2e8a1f9c37
Revises: 0c4342cb8e82
Create Date: 2026-11-06 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5d2e8a1f9c37"
down_revision: str | None = "0c4342cb8e82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_clips_transcription_trgm",
        "clips",
        ["transcription"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"transcription": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_clips_transcription_trgm", table_name="clips", postgresql_using="gin")
    # The extension is left installed: other objects may use it.
