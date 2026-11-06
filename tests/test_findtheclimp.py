"""Phase 7: /findtheclimp search, preview and posting (needs Postgres)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import discord
import httpx
import pytest
from sqlalchemy import update

from yks.bot.client import YKSBot
from yks.bot.cogs.findtheclimp import (
    NOT_A_CHOICE,
    PostClipView,
    SearchCache,
    Timings,
    snippet_around,
)
from yks.bot.media import link_is_fresh
from yks.config import Settings
from yks.db import Clip, ClipStatus, Database
from yks.db.clips import NewClip, prefix_tsquery_text, record_clips, search_clips

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


class FakeCDN:
    """Serves attachment bytes by URL: links of messages in the clip channel, plus `extra`."""

    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.extra: dict[str, bytes] = {}
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.extra:
            return httpx.Response(200, content=self.extra[url])
        for message in self.channel.stored.values():
            for attachment in message.attachments:
                if attachment.url == url:
                    return httpx.Response(200, content=attachment.data)
        return httpx.Response(403)  # expired or unknown link


@pytest.fixture
def cdn(clip_channel: FakeChannel) -> FakeCDN:
    return FakeCDN(clip_channel)


@pytest.fixture
async def bot(
    db: Database,
    database_url: str,
    guild: FakeGuild,
    clip_channel: FakeChannel,
    cdn: FakeCDN,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[YKSBot]:
    patch_context_send(monkeypatch)
    settings = Settings(
        _env_file=None, database_url=database_url, yks_clip_channel_id=clip_channel.id
    )
    instance = await make_bot(settings, db)
    instance.get_cog("Tallies").prune_mailbag.cancel()
    monkeypatch.setattr(instance, "get_channel", guild.get_channel)
    await instance.http_client.aclose()
    instance.http_client = httpx.AsyncClient(transport=httpx.MockTransport(cdn.handler))
    yield instance
    await instance.http_client.aclose()


def signed_url(expires: float) -> str:
    return f"https://cdn.discordapp.com/attachments/1/2/clip.mp4?ex={int(expires):x}&hm=abc"


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


async def preview(bot: YKSBot, guild: FakeGuild, clip_id: int, user: FakeUser) -> Any:
    """Run /findtheclimp for a clip; returns the interaction (its first edit is the preview)."""
    interaction = FakeInteraction(user=user, guild=guild)
    await cog(bot).findtheclimp.callback(cog(bot), interaction, str(clip_id))
    return interaction


async def click_post(view: PostClipView, user: FakeUser, guild: FakeGuild) -> FakeInteraction:
    click = FakeInteraction(user=user, guild=guild)
    await view.post.callback(click)
    return click


async def test_command_uses_an_unexpired_stored_link_without_fetching(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild, cdn: FakeCDN
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "jetpack")
    url = signed_url(time.time() + 86400)
    cdn.extra[url] = b"stored link bytes"
    async with db.session() as session:
        await session.execute(update(Clip).where(Clip.id == clip_id).values(url=url))
    clip_channel.stored.clear()  # fetching the message would fail
    user = FakeUser()
    interaction = await preview(bot, guild, clip_id, user)
    assert interaction.edits[0]["content"] == url
    await click_post(interaction.edits[0]["view"], user, guild)
    (posted,) = clip_channel.sent
    assert posted.file_bytes == [b"stored link bytes"]
    assert cdn.requests == [url]


async def test_command_refreshes_a_refused_stored_link(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild, cdn: FakeCDN
) -> None:
    clip_id, attachment = await add_transcribed(db, clip_channel, "jetpack", data=b"jet")
    stale = signed_url(time.time() + 86400)  # looks valid, but the CDN refuses it
    async with db.session() as session:
        await session.execute(update(Clip).where(Clip.id == clip_id).values(url=stale))
    user = FakeUser()
    interaction = await preview(bot, guild, clip_id, user)
    await click_post(interaction.edits[0]["view"], user, guild)
    (posted,) = clip_channel.sent
    assert posted.file_bytes == [b"jet"]
    assert cdn.requests == [stale, attachment.url]
    async with db.session() as session:
        assert (await session.get(Clip, clip_id)).url == attachment.url  # remembered


def test_link_is_fresh() -> None:
    assert link_is_fresh(signed_url(2000), now=1000)
    assert not link_is_fresh(signed_url(1500), now=1000)  # inside the 10 minute margin
    assert not link_is_fresh("https://cdn.discordapp.com/a/b/c.mp4?ex=nothex", now=1000)
    assert not link_is_fresh("https://cdn.discordapp.com/a/b/c.mp4", now=1000)


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


async def test_search_matches_a_half_typed_last_word(
    db: Database, clip_channel: FakeChannel
) -> None:
    sock, _ = await add_transcribed(db, clip_channel, "the self-folding sock is back")
    await add_transcribed(db, clip_channel, "a folding chair")
    async with db.session() as session:
        assert [c.id for c in await search_clips(session, "folding soc")] == [sock]
        assert [c.id for c in await search_clips(session, "self-fold")] == [sock]
        (found,) = await search_clips(session, "sock is ba")
        assert found.transcription == "the self-folding sock is back"


def test_prefix_tsquery_text() -> None:
    assert prefix_tsquery_text("Folding  so") == "folding & so:*"
    assert prefix_tsquery_text("it's a <trap> | !x & y:*") == "it & s & a & trap & x & y:*"
    assert prefix_tsquery_text(" ... ") is None


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


async def test_command_previews_privately_as_a_link(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild, cdn: FakeCDN
) -> None:
    clip_id, attachment = await add_transcribed(db, clip_channel, "the self-folding sock")
    interaction = await preview(bot, guild, clip_id, FakeUser())
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    (edit,) = interaction.edits
    assert edit["content"] == attachment.url  # Discord plays the link; nothing re-uploaded
    assert "attachments" not in edit
    # (discord.py loads extensions as their own module objects, so compare by name)
    assert type(edit["view"]).__name__ == PostClipView.__name__
    labels = [item.label for item in edit["view"].children]
    assert labels == ["Post it", "Don't post it"]
    await edit["view"].download  # the file for posting downloads in the background
    assert cdn.requests == [attachment.url]


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
    interaction = await preview(bot, guild, clip_id, user)
    view: PostClipView = interaction.edits[0]["view"]

    assert await view.interaction_check(FakeInteraction(user=user, guild=guild))
    assert not await view.interaction_check(FakeInteraction(user=FakeUser(), guild=guild))
    click = await click_post(view, user, guild)
    assert click.response.edits == [{"content": "Posting...", "view": None}]
    assert click.edits == [{"content": "Posted."}]
    (posted,) = clip_channel.sent
    assert posted.kwargs["embed"].description == (
        f'Requested by {user.mention} using the `/findtheclimp` command for "jetpack".'
    )
    assert posted.file_bytes == [b"jet"]
    assert cog(bot).search_cache.get(user.id, clip_id) is None
    assert view.download is None  # the view lets go of the file


async def test_post_it_uses_the_finished_background_download(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild, cdn: FakeCDN
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "jetpack", data=b"jet")
    user = FakeUser()
    interaction = await preview(bot, guild, clip_id, user)
    view = interaction.edits[0]["view"]
    await view.download
    clip_channel.stored.clear()  # posting needs neither the message nor the CDN any more
    requests = len(cdn.requests)
    await click_post(view, user, guild)
    (posted,) = clip_channel.sent
    assert posted.file_bytes == [b"jet"]
    assert len(cdn.requests) == requests


async def test_post_it_when_the_clip_is_gone(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "jetpack")
    url = signed_url(time.time() + 86400)  # unexpired, so the preview shows it as is...
    async with db.session() as session:
        await session.execute(update(Clip).where(Clip.id == clip_id).values(url=url))
    clip_channel.stored.clear()  # ...but the message was deleted, so the CDN refuses it
    user = FakeUser()
    interaction = await preview(bot, guild, clip_id, user)
    assert interaction.edits[0]["content"] == url
    view = interaction.edits[0]["view"]
    click = await click_post(view, user, guild)
    assert click.edits == [{"content": "Couldn't post it: the clip seems to be gone."}]
    assert clip_channel.sent == []
    async with db.session() as session:
        assert (await session.get(Clip, clip_id)).deleted is True


async def test_dont_post_it_button_cancels_the_download(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, guild: FakeGuild
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "jetpack")
    user = FakeUser()
    interaction = await preview(bot, guild, clip_id, user)
    view = interaction.edits[0]["view"]
    download = view.download
    click = FakeInteraction(user=user, guild=guild)
    await view.reject.callback(click)
    assert click.response.edits == [
        {"content": "Didn't post. Is that a first for you?", "view": None}
    ]
    with contextlib.suppress(asyncio.CancelledError):
        await download
    assert download.cancelled()
    assert clip_channel.sent == []


def test_timings_report_each_step_and_the_total() -> None:
    ticks = iter([0.0, 0.004, 0.184, 1.5])
    timings = Timings(clock=lambda: next(ticks))
    timings.mark("lookup")
    timings.mark("fetch")
    timings.mark("preview")
    assert str(timings) == "lookup=4ms fetch=180ms preview=1316ms total=1500ms"


async def test_preview_and_post_log_their_timings(
    bot: YKSBot,
    db: Database,
    clip_channel: FakeChannel,
    guild: FakeGuild,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clip_id, _ = await add_transcribed(db, clip_channel, "jetpack", data=b"jet")
    user = FakeUser()
    with caplog.at_level(logging.INFO, logger="yks.bot.cogs.findtheclimp"):
        interaction = await preview(bot, guild, clip_id, user)
        await click_post(interaction.edits[0]["view"], user, guild)
    lines = [r.getMessage() for r in caplog.records]
    (shown,) = [line for line in lines if "preview of clip" in line]
    for step in ("defer=", "lookup=", "fetch=", "preview=", "total="):
        assert step in shown
    (downloaded,) = [line for line in lines if "downloaded clip" in line]
    assert "(3 bytes)" in downloaded and "download=" in downloaded
    (posted,) = [line for line in lines if " posted " in line]
    assert "wait=" in posted and "send=" in posted


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
