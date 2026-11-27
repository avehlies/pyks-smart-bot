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

    # The YouTube channel: its handle for the Shorts page, its id (UC...) for the RSS feed.
    youtube_handle: str = "YourKickstarterSucks"
    youtube_channel_id: str = "UCt_yVzAjE5AQUNMlLAw5jtA"

    scan_interval_minutes: float = 30.0
    # The bot only posts a short if the RSS feed says it was published this recently: the worker
    # marks older ones as not to be posted, and shorts the feed doesn't date within this long of
    # first seeing them. Stops a backlog from flooding the channel.
    announce_max_age_hours: float = 72.0

    # Liveness for the Docker healthcheck (`yks-short-worker-health`): the worker touches this
    # file every interval while the database answers. YouTube being down doesn't count against it.
    heartbeat_file: str = "/tmp/yks-short-worker.heartbeat"
    heartbeat_interval_seconds: float = 60.0
    heartbeat_max_age_seconds: float = 300.0

    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _dsn(cls, value: str) -> str:
        return asyncpg_dsn(value)

    @field_validator("youtube_handle")
    @classmethod
    def _handle(cls, value: str) -> str:
        return value.strip().removeprefix("@")


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
