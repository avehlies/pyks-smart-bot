"""Worker configuration, from environment variables (and `.env`)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def asyncpg_dsn(url: str) -> str:
    """Accept the bot's SQLAlchemy URL (`postgresql+asyncpg://`) or a plain Postgres URL."""
    for prefix in ("postgresql+asyncpg://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://yks:yks@localhost:5432/yks"
    # The bot's token: used read-only, to fetch fresh attachment URLs (Discord CDN links expire).
    auth_token: str = ""

    whisper_model: str = "tiny.en"
    whisper_device: str = "cpu"  # or "cuda"
    whisper_compute_type: str = "int8"  # fast on CPU; "float16" on GPU
    # A middle ground: faster than faster-whisper's usual 5 on a CPU, a little more accurate than
    # greedy decoding (1). Lower it for speed, raise it for slightly better transcripts.
    whisper_beam_size: int = 3
    # Threads the model uses on a CPU. 0 is faster-whisper's default (4). More than the machine's
    # cores doesn't help; fewer leaves CPU for the bot and anything else on the host.
    whisper_cpu_threads: int = 0
    # Stop the transcription process (freeing the model's memory) after this many seconds with no
    # clips; the next clip starts it again. 0 keeps the model loaded all the time.
    whisper_idle_unload_seconds: float = 300.0
    # Kill the transcription process if one clip takes longer than this (a hung decoder would
    # otherwise stall the queue for good); the clip counts as a failed attempt. It includes loading
    # the model, and downloading it the first time. 0 waits forever.
    transcribe_timeout_seconds: float = 1800.0

    # When no new clips are waiting, transcribe clips made with a different WHISPER_MODEL again,
    # newest first. They stay searchable by their old transcript until the new one is saved.
    retranscribe: bool = True

    max_attempts: int = 3
    # Seconds between queue checks when no NOTIFY arrives (a safety net; NOTIFY wakes it sooner).
    poll_interval_seconds: float = 60.0
    # A clip stuck in `processing` this long (e.g. the worker was killed) goes back to `pending`.
    stale_after_minutes: float = 30.0
    max_download_mb: int = 500

    # Liveness for the Docker healthcheck (`yks-clip-worker-health`): every interval the worker
    # touches this file, if the database answers and the queue isn't stuck. The check fails when
    # the file is older than HEARTBEAT_MAX_AGE_SECONDS.
    heartbeat_file: str = "/tmp/yks-clip-worker.heartbeat"
    heartbeat_interval_seconds: float = 30.0
    heartbeat_max_age_seconds: float = 120.0
    # The queue counts as stuck when one clip has taken this long beyond TRANSCRIBE_TIMEOUT_SECONDS
    # (the rest is for refreshing the link and downloading).
    stall_margin_seconds: float = 1800.0

    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _dsn(cls, value: str) -> str:
        return asyncpg_dsn(value)


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
