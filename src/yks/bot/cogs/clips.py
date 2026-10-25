"""Clips: record audio/video posted in the clip channel, !clip / !climp, and history backfill.

Transcription is not done here; the separate clip-worker service picks up new rows.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from yks.bot.checks import MINUTE, akairo_cooldown
from yks.bot.media import attachment_payload, is_audio_or_video
from yks.db.clips import NewClip, mark_messages_deleted, random_clip, record_clips

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

# Never recorded as clips (from the TS bot).
IGNORED_AUTHOR_IDS = frozenset({789204496097214514})
# !clip retries this many random picks when it lands on deleted messages.
MAX_PICKS = 10


def clips_in(message: discord.Message) -> list[NewClip]:
    """The audio/video attachments of a message, as rows to record."""
    if message.guild is None:
        return []
    return [
        NewClip(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            attachment_id=attachment.id,
            author_id=message.author.id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            url=attachment.url,
        )
        for attachment in message.attachments
        if is_audio_or_video(attachment.content_type, attachment.filename)
    ]


class Clips(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot
        self._backfill: asyncio.Task[int] | None = None
        self._gathered_on_start = False

    @property
    def clip_channel_id(self) -> int | None:
        return self.bot.settings.yks_clip_channel_id

    def _should_record(self, message: discord.Message) -> bool:
        return (
            message.guild is not None
            and message.channel.id == self.clip_channel_id
            and message.author.id not in IGNORED_AUTHOR_IDS
            and (self.bot.user is None or message.author.id != self.bot.user.id)
        )

    async def record(self, messages: Iterable[discord.Message]) -> int:
        rows = [clip for m in messages if self._should_record(m) for clip in clips_in(m)]
        if not rows:
            return 0
        async with self.bot.db.session() as session:
            return await record_clips(session, rows)

    # --- listeners --------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if self._should_record(message) and message.attachments:
            added = await self.record([message])
            if added:
                log.info("Recorded %d new clip(s) from message %s", added, message.id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.channel_id == self.clip_channel_id:
            async with self.bot.db.session() as session:
                await mark_messages_deleted(session, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.channel_id == self.clip_channel_id:
            async with self.bot.db.session() as session:
                await mark_messages_deleted(session, payload.message_ids)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # "Break in case of emergency" from the TS bot: GATHER_ALL_CLIPS=true backfills on start.
        if self.bot.settings.gather_all_clips and not self._gathered_on_start:
            self._gathered_on_start = True
            self.start_backfill()

    # --- !clip --------------------------------------------------------------------------------

    @commands.command(
        name="clip", aliases=["clips", "randomclip", "climp", "climps", "randomclimp"]
    )
    @commands.guild_only()
    @akairo_cooldown(MINUTE)
    async def clip(self, ctx: commands.Context[Any]) -> None:
        for _ in range(MAX_PICKS):
            async with self.bot.db.session() as session:
                picked = await random_clip(session)
            if picked is None:
                return
            channel = self.bot.get_channel(picked.channel_id)
            if channel is None:
                return
            try:
                message = await channel.fetch_message(picked.message_id)  # type: ignore[union-attr]
            except discord.NotFound:
                async with self.bot.db.session() as session:
                    await mark_messages_deleted(session, [picked.message_id])
                continue
            attachment = next(
                (a for a in message.attachments if a.id == picked.attachment_id), None
            )
            if attachment is None:  # the attachment was removed from the message
                continue
            await ctx.send(**await attachment_payload(attachment, ctx.guild))
            return

    # --- backfill -----------------------------------------------------------------------------

    def start_backfill(self) -> bool:
        if self._backfill is not None and not self._backfill.done():
            return False
        self._backfill = asyncio.create_task(self.backfill())
        return True

    async def backfill(self, batch_size: int = 100) -> int:
        """Walk the whole clip channel history and record any clips that were missed."""
        channel = self.bot.get_channel(self.clip_channel_id or 0)
        if channel is None or not hasattr(channel, "history"):
            log.warning("Clip channel not found; nothing to backfill.")
            return 0
        log.info("Backfilling clips from #%s...", getattr(channel, "name", channel.id))
        added = seen = 0
        batch: list[discord.Message] = []
        async for message in channel.history(limit=None):
            seen += 1
            batch.append(message)
            if len(batch) >= batch_size:
                added += await self.record(batch)
                batch.clear()
        added += await self.record(batch)
        log.info("Clip backfill done: %d new clip(s) from %d message(s).", added, seen)
        return added

    @commands.command(name="backfillclips", hidden=True)
    @commands.is_owner()
    async def backfill_command(self, ctx: commands.Context[Any]) -> None:
        if not self.start_backfill():
            await ctx.send("A clip backfill is already running.")
            return
        await ctx.send("Backfilling clips from the clip channel history...")
        assert self._backfill is not None
        added = await asyncio.shield(self._backfill)
        await ctx.send(f"Clip backfill done: {added} new clip(s).")


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Clips(bot))
