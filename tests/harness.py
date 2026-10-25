"""Builds an offline bot and feeds it fake messages through the real discord.py pipeline."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from discord.ext import commands

from yks.bot.client import YKSBot
from yks.config import Settings
from yks.db import Database

from .fakes import BOT_USER_ID, FakeChannel, FakeMessage, FakeUser


def patch_context_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ctx.send to the fake channel (the real one needs a gateway connection)."""

    async def ctx_send(self: commands.Context[Any], content: Any = None, **kwargs: Any) -> Any:
        return await self.channel.send(content, **kwargs)

    async def ctx_reply(self: commands.Context[Any], content: Any = None, **kwargs: Any) -> Any:
        return await self.message.reply(content, **kwargs)

    monkeypatch.setattr(commands.Context, "send", ctx_send)
    monkeypatch.setattr(commands.Context, "reply", ctx_reply)


async def make_bot(settings: Settings, db: Database) -> YKSBot:
    bot = YKSBot(settings, db)
    bot._connection.user = SimpleNamespace(id=BOT_USER_ID, mention=f"<@{BOT_USER_ID}>")
    await bot._async_setup_hook()  # attaches the event loop, as login() would
    await bot.setup_hook()
    return bot


async def say(bot: YKSBot, channel: FakeChannel, content: str, author: FakeUser, **kw: Any) -> None:
    """Deliver a message the way discord.py does: commands, then on_message listeners."""
    message = FakeMessage(content=content, author=author, channel=channel, **kw)
    await bot.process_commands(message)  # type: ignore[arg-type]
    await bot.get_cog("Triggers").on_message(message)  # type: ignore[union-attr]
