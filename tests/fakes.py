"""Lightweight stand-ins for discord.py objects, enough to drive the real command pipeline."""

from __future__ import annotations

import io
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import discord
from PIL import Image

_ids = itertools.count(1000)

BOT_USER_ID = 999


def _next_id() -> int:
    return next(_ids)


@dataclass
class Sent:
    content: str | None
    kwargs: dict[str, Any]
    reply_to: int | None = None
    file_bytes: list[bytes] = field(default_factory=list)
    # The message object returned to the bot, so tests can see reactions it added.
    message: Any = None

    def __post_init__(self) -> None:
        # Like discord.py: read each uploaded file, then close it.
        files = [self.kwargs["file"]] if "file" in self.kwargs else self.kwargs.get("files", [])
        for file in files:
            self.file_bytes.append(file.fp.read())
            file.close()


@dataclass(eq=False)
class FakeUser:
    id: int = field(default_factory=_next_id)
    bot: bool = False
    name: str = "user"
    guild_permissions: discord.Permissions = field(default_factory=discord.Permissions.none)

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    def __str__(self) -> str:
        return self.name


class FakeAsset:
    """Stands in for `member.display_avatar`."""

    def __init__(self, color: tuple[int, int, int] = (200, 30, 30)) -> None:
        self.color = color

    def replace(self, **_: Any) -> FakeAsset:
        return self

    async def read(self) -> bytes:
        out = io.BytesIO()
        Image.new("RGB", (64, 64), self.color).save(out, "PNG")
        return out.getvalue()


@dataclass(eq=False)
class FakeMember(FakeUser):
    guild: FakeGuild | None = None
    display_name: str = "New Person"
    display_avatar: FakeAsset = field(default_factory=FakeAsset)


@dataclass(eq=False)
class FakeEmoji:
    name: str
    id: int = field(default_factory=_next_id)

    def __str__(self) -> str:
        return f"<:{self.name}:{self.id}>"


@dataclass(eq=False)
class FakeChannel:
    id: int = field(default_factory=_next_id)
    guild: FakeGuild | None = None
    name: str = "general"
    type: discord.ChannelType | None = None
    sent: list[Sent] = field(default_factory=list)
    # Messages that fetch_message() can find (add with `post()`).
    history: dict[int, FakeMessage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type is None:
            self.type = discord.ChannelType.text if self.guild else discord.ChannelType.private
        if self.guild is not None and self not in self.guild.channels:
            self.guild.channels.append(self)

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    def permissions_for(self, member: Any) -> discord.Permissions:
        return member.guild_permissions

    def post(self, content: str = "", author: FakeUser | None = None, **kwargs: Any) -> FakeMessage:
        """Put a message in this channel's history (as if someone had posted it)."""
        message = FakeMessage(content=content, author=author or FakeUser(), channel=self, **kwargs)
        self.history[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        try:
            return self.history[message_id]
        except KeyError:
            raise discord.NotFound(_FakeResponse(404), "Unknown Message") from None

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        message = FakeMessage(content=content or "", author=FakeUser(id=BOT_USER_ID), channel=self)
        self.sent.append(Sent(content, kwargs, message=message))
        return message

    @property
    def embeds(self) -> list[discord.Embed]:
        """Every embed sent to this channel, in order."""
        out: list[discord.Embed] = []
        for sent in self.sent:
            out += (
                [sent.kwargs["embed"]] if "embed" in sent.kwargs else sent.kwargs.get("embeds", [])
            )
        return out


@dataclass(eq=False)
class FakeGuild:
    id: int = field(default_factory=_next_id)
    emojis: list[FakeEmoji] = field(default_factory=list)
    channels: list[FakeChannel] = field(default_factory=list)
    members: list[FakeMember] = field(default_factory=list)
    system_channel: FakeChannel | None = None

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    def get_member(self, user_id: int) -> FakeMember | None:
        return next((m for m in self.members if m.id == user_id), None)

    async def fetch_member(self, user_id: int) -> FakeMember:
        member = self.get_member(user_id)
        if member is None:
            raise discord.NotFound(_FakeResponse(404), "Unknown Member")
        return member


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Not Found"


@dataclass(eq=False)
class FakeMessage:
    content: str
    author: FakeUser
    channel: FakeChannel
    mentions: list[FakeUser] = field(default_factory=list)
    reactions: list[FakeReaction] = field(default_factory=list)
    attachments: list[Any] = field(default_factory=list)
    embeds: list[discord.Embed] = field(default_factory=list)
    id: int = field(default_factory=_next_id)
    _state: Any = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    edited_at: datetime | None = None

    @property
    def guild(self) -> FakeGuild | None:
        return self.channel.guild

    async def reply(self, content: str | None = None, **kwargs: Any) -> Any:
        self.channel.sent.append(Sent(content, kwargs, reply_to=self.id))

    @property
    def jump_url(self) -> str:
        guild = self.guild.id if self.guild else "@me"
        return f"https://discord.com/channels/{guild}/{self.channel.id}/{self.id}"

    def set_reaction_count(self, name: str, count: int) -> None:
        """Simulate `count` users having reacted with the named emoji."""
        self.reactions = [r for r in self.reactions if r.emoji.name != name]
        if count:
            self.reactions.append(FakeReaction(FakeEmoji(name), count))

    async def add_reaction(self, emoji: Any) -> None:
        for reaction in self.reactions:
            if reaction.emoji is emoji:
                reaction.count += 1
                reaction.me = True
                return
        self.reactions.append(FakeReaction(emoji, 1, me=True))


@dataclass(eq=False)
class FakeReaction:
    emoji: Any
    count: int = 1
    me: bool = False


def sent_files(channel: FakeChannel) -> list[discord.File]:
    return [s.kwargs["file"] for s in channel.sent if "file" in s.kwargs]
