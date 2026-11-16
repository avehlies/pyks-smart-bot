"""clip transcribed_model

Records which Whisper model wrote each transcript, so the clip worker can re-transcribe clips
made with an older model (replacing `clip-worker/scripts/requeue-clips.sh`).

Existing transcripts are left as NULL ("unknown"), which the worker never redoes on its own: the
migration can't tell which model wrote them. Label them by hand (see the clip worker's README).

`attempts` is reset on `done` clips because it now counts re-transcription attempts there; before,
it was just left over from the first transcription.

Revision ID: 8b3f6c2d4e71
Revises: 5d2e8a1f9c37
Create Date: 2026-11-16 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b3f6c2d4e71"
down_revision: str | None = "5d2e8a1f9c37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("transcribed_model", sa.Text(), nullable=True))
    op.execute("UPDATE clips SET attempts = 0 WHERE status = 'done'")


def downgrade() -> None:
    op.drop_column("clips", "transcribed_model")
