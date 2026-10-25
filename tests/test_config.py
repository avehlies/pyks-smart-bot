import pytest

from yks.config import Settings, normalize_database_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ],
)
def test_normalize_database_url(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


def test_blank_ids_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YKS_GUILD_ID", "")
    monkeypatch.setenv("YKS_CLIP_CHANNEL_ID", "123")
    settings = Settings(_env_file=None)
    assert settings.yks_guild_id is None
    assert settings.yks_clip_channel_id == 123


def test_blocked_user_ids_parse_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOCKED_USER_IDS", "1, 2,3,")
    assert Settings(_env_file=None).blocked_user_ids == frozenset({1, 2, 3})


def test_blocked_user_ids_default_empty() -> None:
    assert Settings(_env_file=None).blocked_user_ids == frozenset()
