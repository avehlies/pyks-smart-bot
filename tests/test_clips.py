"""Phase 5: recording clips, !clip, history backfill (needs Postgres)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from yks.bot.client import YKSBot
from yks.config import Settings
from yks.db import Clip, ClipStatus, Database

from .fakes import BOT_USER_ID, FakeAttachment, FakeChannel, FakeGuild, FakeUser
from .harness import make_bot, patch_context_send, say


@pytest.fixture
def guild() -> FakeGuild:
    return FakeGuild()


@pytest.fixture
def clip_channel(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild, name="clips")


@pytest.fixture
def general(guild: FakeGuild) -> FakeChannel:
    return FakeChannel(guild=guild, name="general")


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


def cog(bot: YKSBot) -> Any:
    return bot.get_cog("Clips")


async def stored(db: Database) -> list[Clip]:
    async with db.session() as session:
        return list((await session.scalars(select(Clip).order_by(Clip.id))).all())


# --- recording ------------------------------------------------------------------------------


async def test_records_audio_and_video_attachments(
    bot: YKSBot, db: Database, clip_channel: FakeChannel
) -> None:
    video = FakeAttachment("bit.mp4", "video/mp4")
    audio = FakeAttachment("bit.ogg", None)  # no content type: guessed from the name
    image = FakeAttachment("pic.png", "image/png")
    message = clip_channel.post("lol", attachments=[video, audio, image])
    await cog(bot).on_message(message)
    await cog(bot).on_message(message)  # seen twice (e.g. backfill): no duplicates
    clips = await stored(db)
    assert [(c.attachment_id, c.filename, c.status) for c in clips] == [
        (video.id, "bit.mp4", ClipStatus.PENDING),
        (audio.id, "bit.ogg", ClipStatus.PENDING),
    ]
    assert clips[0].message_id == message.id
    assert clips[0].guild_id == clip_channel.guild.id
    assert clips[0].transcription is None


async def test_ignores_other_channels_bots_own_posts_and_ignored_user(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, general: FakeChannel
) -> None:
    for message in (
        general.post(attachments=[FakeAttachment()]),
        clip_channel.post(
            author=FakeUser(id=BOT_USER_ID, bot=True), attachments=[FakeAttachment()]
        ),
        clip_channel.post(author=FakeUser(id=789204496097214514), attachments=[FakeAttachment()]),
    ):
        await cog(bot).on_message(message)
    assert await stored(db) == []


async def test_deleted_messages_are_flagged(
    bot: YKSBot, db: Database, clip_channel: FakeChannel
) -> None:
    from types import SimpleNamespace

    first = clip_channel.post(attachments=[FakeAttachment()])
    second = clip_channel.post(attachments=[FakeAttachment()])
    await cog(bot).record([first, second])
    await cog(bot).on_raw_message_delete(
        SimpleNamespace(channel_id=clip_channel.id, message_id=first.id)
    )
    assert [c.deleted for c in await stored(db)] == [True, False]
    await cog(bot).on_raw_bulk_message_delete(
        SimpleNamespace(channel_id=clip_channel.id, message_ids={second.id})
    )
    assert [c.deleted for c in await stored(db)] == [True, True]


# --- !clip ----------------------------------------------------------------------------------


async def test_clip_forwards_a_message_that_is_just_the_clip(
    bot: YKSBot, clip_channel: FakeChannel, general: FakeChannel
) -> None:
    # Even one too big for the bot to upload: forwarding doesn't re-upload anything.
    huge = FakeAttachment("long.mp4", size=50 * 1024 * 1024)
    posted = clip_channel.post(attachments=[huge])
    await cog(bot).record([posted])
    await say(bot, general, "!clip", FakeUser())
    (sent,) = general.sent
    assert sent.kwargs == {"forward": posted}


async def test_clip_reposts_the_file_from_a_message_with_text(
    bot: YKSBot, clip_channel: FakeChannel, general: FakeChannel
) -> None:
    attachment = FakeAttachment("funny.mp4", data=b"video bytes")
    await cog(bot).record([clip_channel.post("lol", attachments=[attachment])])
    await say(bot, general, "!climp", FakeUser())
    (sent,) = general.sent
    assert sent.kwargs["file"].filename == "funny.mp4"
    assert sent.file_bytes == [b"video bytes"]


async def test_clip_reposts_one_of_several_attachments(
    bot: YKSBot, clip_channel: FakeChannel, general: FakeChannel
) -> None:
    video = FakeAttachment("funny.mp4")
    image = FakeAttachment("pic.png", "image/png")
    await cog(bot).record([clip_channel.post(attachments=[video, image])])
    await say(bot, general, "!clip", FakeUser())
    assert [s.kwargs["file"].filename for s in general.sent] == ["funny.mp4"]


async def test_clip_links_files_too_big_to_upload(
    bot: YKSBot, clip_channel: FakeChannel, general: FakeChannel
) -> None:
    huge = FakeAttachment("long.mp4", size=50 * 1024 * 1024)
    await cog(bot).record([clip_channel.post("too long", attachments=[huge])])
    await say(bot, general, "!clip", FakeUser())
    assert [s.content for s in general.sent] == [huge.url]


async def test_clip_skips_deleted_messages(
    bot: YKSBot, db: Database, clip_channel: FakeChannel, general: FakeChannel
) -> None:
    gone = clip_channel.post(attachments=[FakeAttachment("gone.mp4")])
    kept = clip_channel.post(attachments=[FakeAttachment("kept.mp4")])
    await cog(bot).record([gone, kept])
    del clip_channel.stored[gone.id]
    for _ in range(4):  # whichever is picked first, we end up posting the surviving clip
        general.sent.clear()
        await say(bot, general, "!clip", FakeUser())
        assert [s.kwargs["forward"] for s in general.sent] == [kept]


async def test_random_clip_picks_live_clips_uniformly(db: Database) -> None:
    import random

    from yks.db.clips import NewClip, mark_messages_deleted, random_clip, record_clips

    def new(message_id: int) -> NewClip:
        return NewClip(1, 2, message_id, message_id, None, "c.mp4", "video/mp4", "https://x")

    async with db.session() as session:
        await record_clips(session, [new(m) for m in range(1, 41)])
        # Delete a long run in the middle: a live clip right after a gap must not be favoured.
        await mark_messages_deleted(session, range(5, 36))
    rng = random.Random(1)
    picks: dict[int, int] = {}
    for _ in range(900):
        async with db.session() as session:
            clip = await random_clip(session, rng)
        assert clip is not None and not clip.deleted
        picks[clip.message_id] = picks.get(clip.message_id, 0) + 1
    assert set(picks) == {1, 2, 3, 4, 36, 37, 38, 39, 40}
    assert max(picks.values()) < 2 * min(picks.values())  # ~100 each, none ~30x as likely

    async with db.session() as session:  # nearly everything deleted: the fallback finds it
        await mark_messages_deleted(session, [1, 2, 3, 4, 36, 37, 38, 39])
    async with db.session() as session:
        assert (await random_clip(session, rng)).message_id == 40
        await mark_messages_deleted(session, [40])
    async with db.session() as session:
        assert await random_clip(session, rng) is None


async def test_clip_with_no_clips_says_nothing(bot: YKSBot, general: FakeChannel) -> None:
    await say(bot, general, "!clip", FakeUser())
    assert general.sent == []


# --- backfill -------------------------------------------------------------------------------


async def test_backfill_records_history_once(
    bot: YKSBot, db: Database, clip_channel: FakeChannel
) -> None:
    for i in range(250):
        attachments = [FakeAttachment(f"{i}.mp3", "audio/mpeg")] if i % 5 == 0 else []
        clip_channel.post(f"message {i}", attachments=attachments)
    assert await cog(bot).backfill(batch_size=40) == 50
    assert await cog(bot).backfill() == 0
    assert len(await stored(db)) == 50


async def test_backfill_command_is_owner_only(
    bot: YKSBot, clip_channel: FakeChannel, general: FakeChannel
) -> None:
    clip_channel.post(attachments=[FakeAttachment()])
    await say(bot, general, "!backfillclips", FakeUser())
    assert general.sent == []
    await say(bot, general, "!backfillclips", FakeUser(id=bot.settings.owner_id))
    assert [s.content for s in general.sent] == [
        "Backfilling clips from the clip channel history...",
        "Clip backfill done: 1 new clip(s).",
    ]
