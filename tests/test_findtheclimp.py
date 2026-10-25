"""Phase 7: /findtheclimp search, preview and posting (needs Postgres)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import discord
import pytest
from sqlalchemy import update

from yks.bot.client import YKSBot
from yks.bot.cogs.findtheclimp import NOT_A_CHOICE, PostClipView, SearchCache, snippet_around
from yks.config import Settings
from yks.db import Clip, ClipStatus, Database
from yks.db.clips import NewClip, record_clips, search_clips

from .fakes import FakeAttachment, FakeChannel, FakeGuild, FakeUser
from .harness import make_bot, patch_context_send, say


@dataclass
class FakeInteractionResponse:
    deferred: dict[str, Any] | None = None
    edits: list[dict[str, Any]] = field(default_factory=list)

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = kwargs

    async def edit_message(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


@dataclass
class FakeInteraction:
    user: FakeUser
    guild: FakeGuild
    response: FakeInteractionResponse = field(default_factory=FakeInteractionResponse)
    edits: list[dict[str, Any]] = field(default_factory=list)

    async def edit_original_response(self, **kwargs: Any) -> None:
        for file in kwargs.get("attachments", []):
            kwargs.setdefault("file_bytes", []).append(file.fp.read())
            file.close()
        self.edits.append(kwargs)


@pytest.fixture
def guild() -> FakeGuild:
    return FakeGuild()


@pytest.fixture
def clip_channel(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild, name="clips")


@pytest.fixture
async def bot(
    db: Database,
    database_url: str,
    guild: FakeGuild,
    clip_channel: FakeChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[YKSBot]:
    patch_context_send(monkeypatch)
    settings = Settings(
        _env_file=None, database_url=database_url, yks_clip_channel_id=clip_channel.id
    )
    instance = await make_bot(settings, db)
    instance.get_cog("Tallies").prune_mailbag.cancel()
    monkeypatch.setattr(instance, "get_channel", guild.get_channel)
    yield instance
    await instance.http_client.aclose()


async def add_transcribed(
    db: Database, channel: FakeChannel, text: str, *, data: bytes = b"clip"
) -> tuple[int, FakeAttachment]:
    attachment = FakeAttachment("clip.mp4", data=data)
    message = channel.post(attachments=[attachment])
    async with db.session() as session:
        await record_clips(
            session,
            [
                NewClip(
                    guild_id=channel.guild.id,
                    channel_id=channel.id,
                    message_id=message.id,
                    attachment_id=attachment.id,
                    author_id=None,
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    url=attachment.url,
                )
            ],
        )
        await session.execute(
            update(Clip)
            .where(Clip.attachment_id == attachment.id)
            .values(status=ClipStatus.DONE, transcription=text)
        )
        clip_id = (
            (
                await session.execute(
                    Clip.__table__.select().where(Clip.attachment_id == attachment.id)
                )
            )
            .one()
            .id
        )
    return clip_id, attachment


def cog(bot: YKSBot) -> Any:
    return bot.get_cog("FindTheClimp")


# --- pure helpers ---------------------------------------------------------------------------


def test_snippet_centres_on_the_phrase() -> None:
    text = "a" * 100 + " oh no they did another " + "b" * 100
    snippet = snippet_around(text, "they did")
    assert "they did" in snippet
    assert snippet.startswith("...") and snippet.endswith("...")
    assert len(snippet) == 94 + 6  # within Discord's 100-character limit
    assert snippet_around("short", "x") == "short"
    assert snippet_around("x" * 200, "missing") == "x" * 94 + "..."


def test_search_cache_expires() -> None:
    now = [0.0]
    cache = SearchCache(ttl=10, clock=lambda: now[0])
    cache.put(1, 5, "jetpack")
    assert cache.get(1, 5) == "jetpack"
    assert cache.get(2, 5) is None
    now[0] = 11
    assert cache.get(1, 5) is None


# --- search ---------------------------------------------------------------------------------


async def test_search_ranks_and_falls_back_to_substring(
    db: Database, clip_channel: FakeChannel
) -> None:
    jetpack, _ = await add_transcribed(db, clip_channel, "the jetpack is a jetpack for jetpacking")
    mention, _ = await add_transcribed(db, clip_channel, "I saw a jetpack once")
    stopwords, _ = await add_transcribed(db, clip_channel, "well it is what it is")
    async with db.session() as session:
        assert [c.id for c in await search_clips(session, "jetpacks")] == [jetpack, mention]
        # All stop words: full-text search finds nothing, the substring match does.
        assert [c.id for c in await search_clips(session, "it is what it is")] == [stopwords]
        assert await search_clips(session, "zeppelin") == []


async def test_search_skips_untranscribed_and_deleted(
    db: Database, clip_channel: FakeChannel
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "ghost bongo")
    async with db.session() as session:
        await session.execute(update(Clip).where(Clip.id == clip_id).values(deleted=True))
        assert await search_clips(session, "bongo") == []


# --- autocomplete ---------------------------------------------------------------------------


async def test_autocomplete(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild
) -> None:
    await add_transcribed(db, clip_channel, "Oh no, they did another one")
    newest, _ = await add_transcribed(db, clip_channel, "oh no, they did another one")  # dupe
    user = FakeUser()
    interaction = FakeInteraction(user=user, guild=guild)

    short = await cog(bot).findtheclimp_autocomplete(interaction, "oh")
    assert [(c.name, c.value) for c in short] == [("Enter a search term.", NOT_A_CHOICE)]

    choices = await cog(bot).findtheclimp_autocomplete(interaction, "another one")
    assert [(c.name, c.value) for c in choices] == [("oh no, they did another one", str(newest))]
    assert cog(bot).search_cache.get(user.id, int(choices[0].value)) == "another one"

    none = await cog(bot).findtheclimp_autocomplete(interaction, "zeppelin")
    assert [c.name for c in none] == ["No results."]


# --- the command and its buttons ------------------------------------------------------------


async def test_command_previews_privately(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "the self-folding sock", data=b"sock clip")
    user = FakeUser()
    interaction = FakeInteraction(user=user, guild=guild)
    await cog(bot).findtheclimp.callback(cog(bot), interaction, str(clip_id))
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    (edit,) = interaction.edits
    assert edit["file_bytes"] == [b"sock clip"]
    # (discord.py loads extensions as their own module objects, so compare by name)
    assert type(edit["view"]).__name__ == PostClipView.__name__
    labels = [item.label for item in edit["view"].children]
    assert labels == ["Post it", "Don't post it"]


@pytest.mark.parametrize("value", ["-", "jetpack"])
async def test_command_needs_an_autocomplete_choice(
    bot: YKSBot, guild: FakeGuild, value: str
) -> None:
    interaction = FakeInteraction(user=FakeUser(), guild=guild)
    await cog(bot).findtheclimp.callback(cog(bot), interaction, value)
    assert interaction.edits == [{"content": "Please wait for the autocomplete options!"}]


async def test_command_with_deleted_clip(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "gone")
    clip_channel.stored.clear()
    interaction = FakeInteraction(user=FakeUser(), guild=guild)
    await cog(bot).findtheclimp.callback(cog(bot), interaction, str(clip_id))
    assert interaction.edits == [{"content": "Something went wrong."}]
    async with db.session() as session:
        assert (await session.get(Clip, clip_id)).deleted is True


async def test_post_it_button(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "jetpack", data=b"jet")
    user = FakeUser()
    cog(bot).search_cache.put(user.id, clip_id, "jetpack")
    interaction = FakeInteraction(user=user, guild=guild)
    await cog(bot).findtheclimp.callback(cog(bot), interaction, str(clip_id))
    view: PostClipView = interaction.edits[0]["view"]

    click = FakeInteraction(user=user, guild=guild)
    assert await view.interaction_check(click)
    assert not await view.interaction_check(FakeInteraction(user=FakeUser(), guild=guild))
    await view.post.callback(click)
    assert click.response.edits == [{"content": "Posted.", "attachments": [], "view": None}]
    (posted,) = clip_channel.sent
    assert posted.kwargs["embed"].description == (
        f'Requested by {user.mention} using the `/findtheclimp` command for "jetpack".'
    )
    assert posted.file_bytes == [b"jet"]
    assert cog(bot).search_cache.get(user.id, clip_id) is None


async def test_dont_post_it_button(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "jetpack")
    user = FakeUser()
    interaction = FakeInteraction(user=user, guild=guild)
    await cog(bot).findtheclimp.callback(cog(bot), interaction, str(clip_id))
    click = FakeInteraction(user=user, guild=guild)
    await interaction.edits[0]["view"].reject.callback(click)
    assert click.response.edits == [
        {"content": "Didn't post. Is that a first for you?", "attachments": [], "view": None}
    ]
    assert clip_channel.sent == []


async def test_sync_is_owner_only(
    bot: YKSBot, guild: FakeGuild, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[object] = []

    async def fake_sync(*, guild: object = None) -> list[str]:
        synced.append(guild)
        return ["findtheclimp"]

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    monkeypatch.setattr(bot.tree, "copy_global_to", lambda *, guild: None)
    channel = FakeChannel(guild=guild)
    await say(bot, channel, "!sync", FakeUser())
    assert synced == []
    owner = FakeUser(id=bot.settings.owner_id)
    await say(bot, channel, "!sync", owner)
    await say(bot, channel, "!sync here", owner)
    assert synced == [None, guild]
    assert [s.content for s in channel.sent] == [
        "Synced 1 command(s) globally.",
        "Synced 1 command(s) to this server.",
    ]
    assert discord  # module used for type references
