from __future__ import annotations

from collections import Counter
from datetime import date

import pytest

from yks.bot.simple_commands import SIMPLE_COMMANDS, File, RandomFile
from yks.bot.utils import days_since_burgy_left, find_emoji, is_burgy_day
from yks.config import DEFAULT_ASSETS_DIR

from .fakes import FakeEmoji, FakeGuild

# Discord's upload limit for servers without boosts.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _files() -> list[File]:
    out: list[File] = []
    for spec in SIMPLE_COMMANDS:
        if isinstance(spec.response, File):
            out.append(spec.response)
        elif isinstance(spec.response, RandomFile):
            out.extend(spec.response.items)
    return out


def test_ids_and_aliases_unique() -> None:
    ids = Counter(spec.id for spec in SIMPLE_COMMANDS)
    assert [i for i, n in ids.items() if n > 1] == []
    aliases = Counter(alias.lower() for spec in SIMPLE_COMMANDS for alias in spec.aliases)
    assert [a for a, n in aliases.items() if n > 1] == []


@pytest.mark.parametrize("item", _files(), ids=lambda f: f.path)
def test_asset_exists_and_is_uploadable(item: File) -> None:
    path = DEFAULT_ASSETS_DIR / item.path
    assert path.is_file()
    assert path.stat().st_size < MAX_UPLOAD_BYTES


def test_burgy_dates() -> None:
    assert is_burgy_day(date(2030, 4, 7))
    assert not is_burgy_day(date(2030, 4, 8))
    assert days_since_burgy_left(date(2021, 4, 8)) == 1


def test_find_emoji_prefers_exact_then_partial() -> None:
    guild = FakeGuild(emojis=[FakeEmoji("emma2"), FakeEmoji("Emma")])
    assert find_emoji(guild, "emma").name == "Emma"  # type: ignore[arg-type,union-attr]
    assert find_emoji(guild, "mma2").name == "emma2"  # type: ignore[arg-type,union-attr]
    assert find_emoji(guild, "nope") is None  # type: ignore[arg-type]
    assert find_emoji(None, "emma") is None
