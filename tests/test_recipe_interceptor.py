from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from yks.bot.client import YKSBot
from yks.bot.cogs.recipe_interceptor import rewrite_recipe_urls
from yks.config import Settings
from yks.db import Database

from .fakes import FakeChannel, FakeGuild, FakeMessage, FakeUser
from .harness import make_bot

FOOD_CHANNEL_ID = 682325132127371341


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "try this https://cooking.nytimes.com/recipes/1234-pie",
            ["https://nyetcook.ing/cooking.nytimes.com/recipes/1234-pie"],
        ),
        (
            "<https://www.bonappetit.com/recipe/soup?x=1>",
            ["https://nyetcook.ing/www.bonappetit.com/recipe/soup?x=1"],
        ),
        ("https://bonappetit.com", ["https://nyetcook.ing/bonappetit.com"]),
        (
            "https://bonappetit.com/a and https://cooking.nytimes.com/b https://bonappetit.com/a",
            ["https://nyetcook.ing/bonappetit.com/a", "https://nyetcook.ing/cooking.nytimes.com/b"],
        ),
        ("already done https://nyetcook.ing/cooking.nytimes.com/recipes/1", []),
        ("https://www.nytimes.com/2026/news.html", []),
        ("https://bonappetit.com.evil.example/recipe", []),
        ("cooking.nytimes.com with no scheme", []),
    ],
)
def test_rewrite_recipe_urls(content: str, expected: list[str]) -> None:
    assert rewrite_recipe_urls(content) == expected


@pytest.fixture
async def bot() -> AsyncIterator[YKSBot]:
    settings = Settings(_env_file=None, yks_food_channel_id=FOOD_CHANNEL_ID)
    db = Database(settings.database_url)  # never connected
    yield await make_bot(settings, db)
    await db.dispose()


async def deliver(bot: YKSBot, channel: FakeChannel, content: str, author: FakeUser) -> None:
    message = FakeMessage(content=content, author=author, channel=channel)
    await bot.get_cog("recipe-interceptor").on_message(message)  # type: ignore[union-attr]


async def test_replies_in_the_food_channel(bot: YKSBot) -> None:
    channel = FakeChannel(id=FOOD_CHANNEL_ID, guild=FakeGuild())
    await deliver(bot, channel, "https://cooking.nytimes.com/recipes/1-x", FakeUser())
    [sent] = channel.sent
    assert sent.content == "https://nyetcook.ing/cooking.nytimes.com/recipes/1-x"
    assert sent.reply_to is not None


async def test_ignores_other_channels_and_bots(bot: YKSBot) -> None:
    other = FakeChannel(guild=FakeGuild())
    await deliver(bot, other, "https://cooking.nytimes.com/recipes/1-x", FakeUser())
    assert other.sent == []
    food = FakeChannel(id=FOOD_CHANNEL_ID, guild=FakeGuild())
    await deliver(bot, food, "https://cooking.nytimes.com/recipes/1-x", FakeUser(bot=True))
    await deliver(bot, food, "no links here", FakeUser())
    assert food.sent == []
