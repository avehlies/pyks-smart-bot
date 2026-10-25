"""Phase 2: member welcome, !welcome, !rssChannel, !newmember (needs TEST_DATABASE_URL)."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator

import discord
import pytest
from PIL import Image

from yks.bot.client import YKSBot
from yks.bot.cogs.welcome import RSS_CHANNEL_EDITOR_ID, WELCOME_GREETINGS
from yks.config import Settings
from yks.db import Database, GuildSettings, guilds

from .fakes import FakeAsset, FakeChannel, FakeGuild, FakeMember, FakeUser, sent_files
from .harness import make_bot, patch_context_send, say

ADMIN = discord.Permissions(administrator=True)
MODERATOR = discord.Permissions(manage_roles=True)


@pytest.fixture
async def bot(
    db: Database, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[YKSBot]:
    patch_context_send(monkeypatch)
    yield await make_bot(Settings(_env_file=None, database_url=database_url), db)


@pytest.fixture
def guild() -> FakeGuild:
    g = FakeGuild()
    g.system_channel = FakeChannel(guild=g, name="welcome")
    return g


@pytest.fixture
def channel(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild, name="general")


def welcome_cog(bot: YKSBot):
    return bot.get_cog("Welcome")


# --- database helpers -----------------------------------------------------------------------


async def test_toggle_welcome_round_trip(db: Database) -> None:
    async with db.session() as session:
        assert await guilds.is_welcome_disabled(session, 1) is False
        assert await guilds.toggle_welcome(session, 1) is True
        assert await guilds.is_welcome_disabled(session, 1) is True
        assert await guilds.toggle_welcome(session, 1) is False
        assert await guilds.is_welcome_disabled(session, 2) is False


async def test_mark_welcomed_only_once(db: Database) -> None:
    async with db.session() as session:
        assert await guilds.mark_welcomed(session, 1, 10) is True
        assert await guilds.mark_welcomed(session, 1, 10) is False
        assert await guilds.mark_welcomed(session, 2, 10) is True  # per guild


async def test_rss_channel_upsert_keeps_other_settings(db: Database) -> None:
    async with db.session() as session:
        await guilds.toggle_welcome(session, 1)
        await guilds.set_rss_channel(session, 1, 555)
        row = await session.get(GuildSettings, 1)
        assert row is not None
        assert (row.rss_channel_id, row.welcome_disabled) == (555, True)


# --- member join ----------------------------------------------------------------------------


async def test_member_join_posts_image_once(bot: YKSBot, guild: FakeGuild) -> None:
    member = FakeMember(guild=guild, display_name="Andrew", display_avatar=FakeAsset((0, 0, 255)))
    await welcome_cog(bot).on_member_join(member)
    (sent,) = guild.system_channel.sent
    assert sent.content.startswith(f"Welcome, {member.mention}! ")
    assert sent.content.removeprefix(f"Welcome, {member.mention}! ") in WELCOME_GREETINGS
    (file,) = sent_files(guild.system_channel)
    assert file.filename == "welcome-image.png"
    image = Image.open(io.BytesIO(sent.file_bytes[0]))
    assert image.size == (1000, 1000)
    assert image.getpixel((550, 120))[:3] == (0, 0, 255)  # avatar centre
    assert image.getpixel((452, 22))[3] == 0  # outside the avatar circle: transparent

    await welcome_cog(bot).on_member_join(member)  # rejoin: no second welcome
    assert len(guild.system_channel.sent) == 1


async def test_member_join_respects_toggle(bot: YKSBot, guild: FakeGuild, db: Database) -> None:
    async with db.session() as session:
        await guilds.toggle_welcome(session, guild.id)
    await welcome_cog(bot).on_member_join(FakeMember(guild=guild))
    assert guild.system_channel.sent == []


async def test_member_join_without_system_channel(bot: YKSBot) -> None:
    guild = FakeGuild()
    assert await welcome_cog(bot).welcome(FakeMember(guild=guild)) is False


# --- !welcome -------------------------------------------------------------------------------


async def test_welcome_toggle_command(bot: YKSBot, channel: FakeChannel) -> None:
    # Two different moderators: the same user would hit the 1 s default cooldown.
    await say(bot, channel, "!welcome", FakeUser(guild_permissions=MODERATOR))
    await say(bot, channel, "!welcome", FakeUser(guild_permissions=MODERATOR))
    assert [s.content for s in channel.sent] == [
        "Welcome message disabled.",
        "Welcome message enabled.",
    ]


async def test_welcome_toggle_needs_manage_roles(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!welcome", FakeUser())
    assert channel.sent == []


# --- !rssChannel ----------------------------------------------------------------------------


async def test_rss_channel_by_mention(
    bot: YKSBot, guild: FakeGuild, channel: FakeChannel, db: Database
) -> None:
    news = FakeChannel(guild=guild, name="new-episodes")
    await say(bot, channel, f"!rssChannel {news.mention}", FakeUser(guild_permissions=ADMIN))
    assert [s.content for s in news.sent] == [f"RSS feed updates will come to {news.mention}."]
    async with db.session() as session:
        assert await guilds.get_rss_channel_id(session, guild.id) == news.id


async def test_rss_channel_by_name_for_editor(
    bot: YKSBot, guild: FakeGuild, channel: FakeChannel
) -> None:
    news = FakeChannel(guild=guild, name="New-Episodes")
    await say(bot, channel, "!rsschannel #new-episodes", FakeUser(id=RSS_CHANNEL_EDITOR_ID))
    assert len(news.sent) == 1


async def test_rss_channel_rejects_missing_and_voice(
    bot: YKSBot, guild: FakeGuild, channel: FakeChannel
) -> None:
    voice = FakeChannel(guild=guild, name="hangout", type=discord.ChannelType.voice)
    for args in ("#nope", "", voice.mention):
        await say(bot, channel, f"!rssChannel {args}", FakeUser(guild_permissions=ADMIN))
    assert [s.content for s in channel.sent] == [
        "That channel does not exist.",
        "That channel does not exist.",
        f"{voice.mention} is not a text channel. No change.",
    ]
    assert channel.sent[2].reply_to is not None


async def test_rss_channel_needs_admin(bot: YKSBot, guild: FakeGuild, channel: FakeChannel) -> None:
    news = FakeChannel(guild=guild, name="news")
    await say(bot, channel, f"!rssChannel {news.mention}", FakeUser(guild_permissions=MODERATOR))
    assert news.sent == [] and channel.sent == []


# --- !newmember -----------------------------------------------------------------------------


async def test_newmember_is_owner_only_and_welcomes(
    bot: YKSBot, guild: FakeGuild, channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = FakeMember(guild=guild)
    guild.members.append(member)
    bot.settings = bot.settings.model_copy(update={"yks_guild_id": guild.id})
    monkeypatch.setattr(bot, "get_guild", lambda gid: guild if gid == guild.id else None)

    await say(bot, channel, f"!newmember {member.id}", FakeUser())
    assert guild.system_channel.sent == []
    await say(bot, channel, f"!newmember {member.id}", FakeUser(id=bot.settings.owner_id))
    assert len(guild.system_channel.sent) == 1
    await say(bot, channel, "!newmember 123", FakeUser(id=bot.settings.owner_id))  # unknown
    assert len(guild.system_channel.sent) == 1
