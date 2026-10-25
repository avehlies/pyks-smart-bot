"""Lightweight stand-ins for discord.py objects, enough to drive the real command pipeline."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import discord

_ids = itertools.count(1000)

BOT_USER_ID = 999


@dataclass
class Sent:
    content: str | None
    kwargs: dict[str, Any]
    reply_to: int | None = None


@dataclass(eq=False)
class FakeUser:
    id: int = field(default_factory=lambda: next(_ids))
    bot: bool = False
    name: str = "user"

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    def __str__(self) -> str:
        return self.name


@dataclass(eq=False)
class FakeEmoji:
    name: str
    id: int = field(default_factory=lambda: next(_ids))

    def __str__(self) -> str:
        return f"<:{self.name}:{self.id}>"


@dataclass(eq=False)
class FakeGuild:
    id: int = field(default_factory=lambda: next(_ids))
    emojis: list[FakeEmoji] = field(default_factory=list)


@dataclass(eq=False)
class FakeChannel:
    id: int = field(default_factory=lambda: next(_ids))
    guild: FakeGuild | None = None
    sent: list[Sent] = field(default_factory=list)

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        self.sent.append(Sent(content, kwargs))
        return None

    def typing(self) -> Any:  # pragma: no cover - not used yet
        raise NotImplementedError


@dataclass(eq=False)
class FakeMessage:
    content: str
    author: FakeUser
    channel: FakeChannel
    mentions: list[FakeUser] = field(default_factory=list)
    reactions: list[Any] = field(default_factory=list)
    attachments: list[Any] = field(default_factory=list)
    id: int = field(default_factory=lambda: next(_ids))
    _state: Any = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    edited_at: datetime | None = None

    @property
    def guild(self) -> FakeGuild | None:
        return self.channel.guild

    async def reply(self, content: str | None = None, **kwargs: Any) -> Any:
        self.channel.sent.append(Sent(content, kwargs, reply_to=self.id))

    async def add_reaction(self, emoji: Any) -> None:
        self.reactions.append(emoji)


def sent_files(channel: FakeChannel) -> list[discord.File]:
    return [s.kwargs["file"] for s in channel.sent if "file" in s.kwargs]
