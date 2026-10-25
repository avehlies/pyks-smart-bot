"""End-to-end tests through discord.py's real command pipeline (no Discord connection)."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import date

import pytest

import yks.bot.utils as bot_utils
from yks.bot.client import YKSBot
from yks.config import Settings
from yks.db import Database

from .fakes import BOT_USER_ID, FakeChannel, FakeEmoji, FakeGuild, FakeMessage, FakeUser, sent_files
from .harness import make_bot, patch_context_send, say


@pytest.fixture
async def bot(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[YKSBot]:
    patch_context_send(monkeypatch)
    settings = Settings(_env_file=None, yks_clip_channel_id=4242)
    db = Database(settings.database_url)  # never connected in these tests
    yield await make_bot(settings, db)
    await db.dispose()


@pytest.fixture
def guild() -> FakeGuild:
    return FakeGuild(emojis=[FakeEmoji("thestore"), FakeEmoji("emma2"), FakeEmoji("emma")])


@pytest.fixture
def channel(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild)


def set_today(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    monkeypatch.setattr(bot_utils, "local_today", lambda tz: today)
    # Extensions are loaded by discord.py and not attached to their package; go via sys.modules.
    monkeypatch.setattr(sys.modules["yks.bot.cogs.fun"], "local_today", lambda tz: today)


# --- simple commands ------------------------------------------------------------------------


async def test_alias_reply_and_user_cooldown(bot: YKSBot, channel: FakeChannel) -> None:
    alice = FakeUser()
    await say(bot, channel, "!feet", alice)
    assert [(s.content, s.reply_to is not None) for s in channel.sent] == [
        ("Is that you, Quentin?", True)
    ]
    await say(bot, channel, "!FEET", alice)  # case-insensitive, but on cooldown
    assert len(channel.sent) == 1


async def test_guild_gate_blocks_other_users(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!eve", FakeUser())
    await say(bot, channel, "!eve", FakeUser())  # different user, same guild: gated
    assert [s.content for s in channel.sent] == ["Who?"]
    other = FakeChannel(guild=FakeGuild())
    await say(bot, other, "!eve", FakeUser())
    assert [s.content for s in other.sent] == ["Who?"]


async def test_require_guild_ignores_dms(bot: YKSBot) -> None:
    dm = FakeChannel(guild=None)
    await say(bot, dm, "!music", FakeUser())
    assert dm.sent == []
    await say(bot, dm, "!bongo", FakeUser())  # not guild-only
    assert [s.content for s in dm.sent] == ["https://ghostbongo.bigcartel.com"]


async def test_mention_prefix(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, f"<@{BOT_USER_ID}> bongo", FakeUser())
    assert [s.content for s in channel.sent] == ["https://ghostbongo.bigcartel.com"]


async def test_embed_and_file_responses(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!pubox", FakeUser())
    embed = channel.sent[0].kwargs["embed"]
    assert embed.title == "P.U. Box"
    assert embed.footer.text.startswith("Remember")

    await say(bot, channel, "!barnes", FakeUser())
    (file,) = sent_files(channel)
    assert file.filename == "barmes.jpg"


async def test_staysippin_allows_four_per_minute(bot: YKSBot, guild: FakeGuild) -> None:
    alice = FakeUser()
    channels = [FakeChannel(guild=FakeGuild()) for _ in range(5)]  # avoid the guild gate
    for ch in channels:
        await say(bot, ch, "!twodrinks", alice)
    assert [len(sent_files(ch)) for ch in channels] == [1, 1, 1, 1, 0]


async def test_mug_cooldown_relaxed_on_burgy_day(
    bot: YKSBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = FakeUser()
    set_today(monkeypatch, date(2026, 4, 7))
    for _ in range(2):
        await say(bot, FakeChannel(guild=FakeGuild()), "!mug", alice)
        bot.get_command("mug")._buckets._cache.clear()  # skip past the 1 s default
    # Both went through; on a normal day the second would be on a 60 s cooldown:
    set_today(monkeypatch, date(2026, 4, 8))
    first, second = FakeChannel(guild=FakeGuild()), FakeChannel(guild=FakeGuild())
    await say(bot, first, "!burgymug", alice)
    await say(bot, second, "!burgymug", alice)
    assert len(sent_files(first)) == 1
    assert sent_files(second) == []


async def test_owner_skips_cooldowns(bot: YKSBot) -> None:
    owner = FakeUser(id=bot.settings.owner_id)
    for _ in range(3):
        ch = FakeChannel(guild=FakeGuild())
        await say(bot, ch, "!feet", owner)
        assert len(ch.sent) == 1


async def test_blocked_users_are_ignored(bot: YKSBot, channel: FakeChannel) -> None:
    blocked = FakeUser()
    bot.settings = bot.settings.model_copy(update={"blocked_user_ids": frozenset({blocked.id})})
    await say(bot, channel, "!bongo", blocked)
    await say(bot, channel, "cunch", blocked)
    assert channel.sent == []


async def test_bots_are_ignored(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!bongo", FakeUser(bot=True))
    await say(bot, channel, "garm", FakeUser(bot=True))
    assert channel.sent == []


# --- regex easter eggs ----------------------------------------------------------------------


async def test_echo_word_must_be_whole_message(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "garm garm", FakeUser())
    assert channel.sent == []
    await say(bot, channel, "GARM", FakeUser())
    assert [s.content for s in channel.sent] == ["garm"]


async def test_substring_triggers(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "I could go for a crunchy cunchwrap", FakeUser())
    await say(bot, channel, "is that a sporange?", FakeUser())
    assert [s.content for s in channel.sent] == ["cunch wrap supreme", "it's a new kind of orange"]


async def test_triggers_skip_valid_commands(bot: YKSBot, channel: FakeChannel) -> None:
    # "!help" is a command, so the "the ... store" trigger in its argument is not checked.
    msg = FakeMessage(content="!help the store", author=FakeUser(), channel=channel)
    await bot.process_commands(msg)  # type: ignore[arg-type]
    await bot.get_cog("Triggers").on_message(msg)  # type: ignore[union-attr]
    assert msg.reactions == []


async def test_the_store_reaction(bot: YKSBot, channel: FakeChannel) -> None:
    msg = FakeMessage(content="going to the grocery store", author=FakeUser(), channel=channel)
    await bot.get_cog("Triggers").on_message(msg)  # type: ignore[union-attr]
    assert [e.name for e in msg.reactions] == ["thestore"]


async def test_hello_needs_mention_and_doesnt_burn_cooldown(
    bot: YKSBot, channel: FakeChannel
) -> None:
    alice = FakeUser()
    await say(bot, channel, "hey everyone", alice)  # no mention: nothing, no cooldown used
    assert channel.sent == []
    bot_user = FakeUser(id=BOT_USER_ID, bot=True)
    await say(bot, channel, f"<@{BOT_USER_ID}> hey!", alice, mentions=[bot_user])
    await say(bot, channel, f"<@{BOT_USER_ID}> hello?", alice, mentions=[bot_user])
    assert [s.content for s in channel.sent] == [
        "Can't talk, I'm at work. Let's meet face-to-face."
    ]


# --- hand-written commands ------------------------------------------------------------------


async def test_burgy(bot: YKSBot, channel: FakeChannel, monkeypatch: pytest.MonkeyPatch) -> None:
    set_today(monkeypatch, date(2026, 10, 25))
    await say(bot, channel, "!burgy", FakeUser())
    await say(bot, channel, "!burgy", FakeUser())  # guild gate
    assert [s.content for s in channel.sent] == ["Burgy left 2027 days ago."]


async def test_emma_uses_existing_emojis(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!emma", FakeUser())
    (sent,) = channel.sent
    names = {f"<:{e.name}:{e.id}>" for e in channel.guild.emojis if e.name.startswith("emma")}
    assert sent.content in names


async def test_emma_without_emojis_sends_nothing(bot: YKSBot) -> None:
    ch = FakeChannel(guild=FakeGuild())
    await say(bot, ch, "!emma", FakeUser())
    assert ch.sent == []


@pytest.mark.parametrize(
    ("args", "first_field"),
    [
        ("", "`!help kickstarters`"),
        (" bogus", "`!help kickstarters`"),
        (" kickstarters", "`!topkickstarters` / `!topks`"),
        (" LISTEN", "`!listen`"),
        (" other", "`!latest <feed>`"),
    ],
)
async def test_help_categories(
    bot: YKSBot, channel: FakeChannel, args: str, first_field: str
) -> None:
    await say(bot, channel, f"!help{args}", FakeUser())
    embed = channel.sent[0].kwargs["embed"]
    assert embed.fields[0].name == first_field


async def test_help_mentions_clip_channel(bot: YKSBot, channel: FakeChannel) -> None:
    await say(bot, channel, "!help other", FakeUser())
    values = [f.value for f in channel.sent[0].kwargs["embed"].fields]
    assert any("<#4242>" in v for v in values)


async def test_help_is_guild_only(bot: YKSBot) -> None:
    dm = FakeChannel(guild=None)
    await say(bot, dm, "!help", FakeUser())
    assert dm.sent == []
