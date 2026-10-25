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
    whisper_beam_size: int = 5
    # Stop the transcription process (freeing the model's memory) after this many seconds with no
    # clips; the next clip starts it again. 0 keeps the model loaded all the time.
    whisper_idle_unload_seconds: float = 300.0

    max_attempts: int = 3
    # Seconds between queue checks when no NOTIFY arrives (a safety net; NOTIFY wakes it sooner).
    poll_interval_seconds: float = 60.0
    # A clip stuck in `processing` this long (e.g. the worker was killed) goes back to `pending`.
    stale_after_minutes: float = 30.0
    max_download_mb: int = 500

    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _dsn(cls, value: str) -> str:
        return asyncpg_dsn(value)


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
