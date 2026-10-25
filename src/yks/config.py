"""Runtime configuration, read from environment variables (and a `.env` file if present).

Variable names match the TypeScript bot so an existing `.env` mostly carries over;
`DATABASE_URL` replaces the old `MONGO_*` variables.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _blank_to_none(value: Any) -> Any:
    """Treat `FOO=` (empty) the same as an unset variable for optional fields."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalId = Annotated[int | None, BeforeValidator(_blank_to_none)]


def normalize_database_url(url: str) -> str:
    """Accept the usual `postgres://` / `postgresql://` forms and force the asyncpg driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Discord
    auth_token: str = ""
    owner_id: int = 293571605081358336
    command_prefix: str = "!"
    # Comma-separated user IDs the bot ignores entirely (the old "blacklist" inhibitor).
    blocked_user_ids: Annotated[frozenset[int], NoDecode] = frozenset()

    # Server / channel IDs
    yks_guild_id: OptionalId = None
    yks_clip_channel_id: OptionalId = None
    yks_mailbag_channel_id: OptionalId = None
    yks_kickstarter_bot_channel_id: OptionalId = None

    # Podcast feeds
    main_feed_rss: str = ""
    bonus_feed_rss: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://yks:yks@localhost:5432/yks"

    # HTTP API (/api/share-clip)
    port: int = 3000
    bot_auth_username: str = ""
    bot_auth_password: str = ""

    # Clip worker
    transcribe_clips: bool = False
    whisper_model: str = "tiny.en"

    log_level: str = Field(default="INFO")

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("blocked_user_ids", mode="before")
    @classmethod
    def _parse_id_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            return frozenset(int(part) for part in value.replace(" ", "").split(",") if part)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
