"""!listen: play a podcast episode in a voice channel, with a live progress embed.

One session per guild. Controls: `!listen pause|play|stop`, or the ⏸ ⏹ ▶️ reactions on the
"Now playing" message (stop asks for confirmation).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from yks.bot.episodes import MAIN_ART, MAIN_COLOR
from yks.bot.listen import (
    TrackedSource,
    parse_duration_ms,
    progress_field,
    select_episode,
    short_time,
    title_fields,
)
from yks.feeds import Episode, FeedError

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

PAUSE, STOP, PLAY = "⏸", "⏹", "▶️"
CONFIRM, CANCEL = "✅", "❌"
PROGRESS_EVERY = 10.0  # seconds between embed updates
MIN_PLAYED_MS = 2000  # shorter than this counts as "failed to start"
STOP_CONFIRM_TIMEOUT = 10.0
START_ACTIONS = ("play", "random", "url")


def _emoji_key(emoji: Any) -> str:
    return str(emoji).replace("️", "")  # ignore the emoji variation selector


@dataclass(eq=False)
class ListenSession:
    guild_id: int
    voice: discord.VoiceClient
    text_channel: Any
    episode: Episode
    duration_ms: int
    temp_path: Path
    source: TrackedSource | None = None
    embed: discord.Embed | None = None
    now_playing: discord.Message | None = None
    status: discord.Message | None = None
    progress_task: asyncio.Task[None] | None = None
    error: Exception | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def position_ms(self) -> int:
        return self.source.position_ms if self.source else 0


class Listen(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot
        self.sessions: dict[int, ListenSession] = {}

    async def cog_unload(self) -> None:
        for session in list(self.sessions.values()):
            session.voice.stop()

    # Overridable in tests (real playback needs ffmpeg and a voice connection).
    def make_source(self, path: Path) -> discord.AudioSource:
        return discord.FFmpegOpusAudio(str(path))

    # --- replies --------------------------------------------------------------------------------

    async def respond(self, session: ListenSession, content: str) -> None:
        """One status message per session (a reply to "Now playing"), edited in place."""
        try:
            if session.status is not None:
                await session.status.edit(content=content)
            elif session.now_playing is not None:
                session.status = await session.now_playing.reply(content)
            else:
                await session.text_channel.send(content)
        except discord.HTTPException:
            log.warning("Could not post listen status: %s", content)

    # --- the command --------------------------------------------------------------------------

    @commands.command(name="listen")
    @commands.guild_only()
    async def listen(
        self, ctx: commands.Context[Any], action: str = "play", *, episode: str = "0"
    ) -> None:
        assert ctx.guild is not None
        action = action.lower()
        session = self.sessions.get(ctx.guild.id)

        if session is None:
            if action not in START_ACTIONS:
                await ctx.send("Nothing playing.")
                return
            if action == "url" and ctx.author.id != self.bot.owner_id:
                return  # arbitrary URLs are owner-only
            await self.start(ctx, action, episode.strip())
            return

        number = int(episode) if episode.strip().isdigit() else 0
        voice = session.voice
        if action in ("random", "url"):
            await self.respond(session, "Stop the current episode first.")
        elif action == "play":
            if voice.is_paused() and number == 0:
                self.resume(session)
                await self.respond(session, "Resuming.")
            else:
                await self.respond(session, "Stop the current episode first.")
        elif action == "pause":
            if voice.is_playing():
                voice.pause()
                await self.respond(session, f"Paused at {short_time(session.position_ms)}.")
        elif action == "stop":
            voice.stop()
        else:
            await self.respond(session, "Not a valid option for the command `!listen`.")

    def resume(self, session: ListenSession) -> None:
        session.voice.resume()

    # --- starting playback ----------------------------------------------------------------------

    async def _choose(self, ctx: commands.Context[Any], action: str, arg: str) -> Episode | None:
        if action == "url":
            return Episode(
                title=f"Episode 0: {arg}", link=arg, audio_url=arg, published=None,
                summary="", image_url=None, duration=None,
            )  # fmt: skip
        try:
            feed = await self.bot.feeds.get(self.bot.settings.main_feed_rss)
        except FeedError:
            await ctx.send("Couldn't reach the podcast feed right now. Try again in a bit.")
            return None
        number = int(arg) if arg.isdigit() else 0
        selection = select_episode(feed, action=action, number=number)
        if selection.episode is None:
            await ctx.send(f"Couldn't find episode {selection.requested or number}.")
        return selection.episode

    async def start(self, ctx: commands.Context[Any], action: str, arg: str) -> None:
        assert ctx.guild is not None
        episode = await self._choose(ctx, action, arg)
        if episode is None:
            return
        if not episode.audio_url:
            await ctx.send(f"Couldn't find audio for {episode.title}.")
            return
        voice_state = getattr(ctx.author, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if channel is None:
            await ctx.send("Please join a voice channel first.")
            return
        if ctx.guild.id in self.sessions:  # someone else started one meanwhile
            return

        try:
            log.info("Joining voice channel %s...", channel.name)
            voice = await channel.connect(timeout=30, self_deaf=True)
        except Exception:
            log.exception("Failed to join voice channel %s", channel.id)
            await ctx.send("Failed to join the voice channel")
            return

        fd, name = tempfile.mkstemp(prefix=f"yks-listen-{secrets.token_hex(4)}-", suffix=".audio")
        os.close(fd)
        session = ListenSession(
            guild_id=ctx.guild.id,
            voice=voice,
            text_channel=ctx.channel,
            episode=episode,
            duration_ms=parse_duration_ms(episode.duration),
            temp_path=Path(name),
        )
        self.sessions[ctx.guild.id] = session

        # Download first (instead of streaming) so pausing can't drop the connection.
        with contextlib.suppress(discord.HTTPException):
            await ctx.reply("Downloading episode...")
        try:
            await self._download(episode.audio_url, session.temp_path)
        except Exception:
            log.exception("Failed to download %s", episode.audio_url)
            await ctx.send("Could not download audio. Check bot logs.")
            await self._teardown(session)
            return

        loop = asyncio.get_running_loop()
        session.source = TrackedSource(self.make_source(session.temp_path))

        def after(error: Exception | None) -> None:  # called from the audio thread
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._finished(session, error)))

        voice.play(session.source, after=after)
        log.info("Playing %s in %s", episode.title, channel.name)

        session.embed = self._embed(session, channel.name)
        try:
            session.now_playing = await ctx.send(embed=session.embed)
            for emoji in (PAUSE, STOP, PLAY):
                await session.now_playing.add_reaction(emoji)
        except discord.HTTPException:
            log.warning("Could not post the Now playing message")
        session.progress_task = asyncio.create_task(self._progress(session))

    async def _download(self, url: str, path: Path) -> None:
        async with self.bot.http_client.stream("GET", url, timeout=60.0) as response:
            response.raise_for_status()
            with path.open("wb") as out:
                async for chunk in response.aiter_bytes():
                    out.write(chunk)

    def _embed(self, session: ListenSession, channel_name: str) -> discord.Embed:
        label, rest = title_fields(session.episode.title)
        embed = discord.Embed(color=MAIN_COLOR, title=f"Now playing in {channel_name}")
        embed.set_thumbnail(url=MAIN_ART)
        embed.add_field(name=label, value=rest, inline=False)
        name, value = progress_field(0, session.duration_ms)
        embed.add_field(name=name, value=value, inline=False)
        return embed

    async def _progress(self, session: ListenSession) -> None:
        while not session.finished.is_set():
            await asyncio.sleep(PROGRESS_EVERY)
            if session.embed is None or session.now_playing is None:
                continue
            name, value = progress_field(session.position_ms, session.duration_ms)
            session.embed.set_field_at(1, name=name, value=value, inline=False)
            with contextlib.suppress(discord.HTTPException):
                await session.now_playing.edit(embed=session.embed)

    # --- finishing ------------------------------------------------------------------------------

    async def _finished(self, session: ListenSession, error: Exception | None) -> None:
        if session.finished.is_set():
            return
        if error is not None:
            log.error("Audio player error: %s", error)
            await self.respond(session, "Playback error.")
        elif session.position_ms < MIN_PLAYED_MS:
            await self.respond(
                session, "Playback failed to start (stream may be unavailable or invalid)."
            )
        else:
            await self.respond(session, "Finished playing episode.")
        await self._teardown(session)

    async def _teardown(self, session: ListenSession) -> None:
        session.finished.set()
        if session.progress_task is not None:
            session.progress_task.cancel()
        with contextlib.suppress(Exception):
            await session.voice.disconnect(force=True)
        with contextlib.suppress(OSError):
            session.temp_path.unlink(missing_ok=True)
        if self.sessions.get(session.guild_id) is session:
            del self.sessions[session.guild_id]

    # --- reaction controls ----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.abc.User) -> None:
        if user.bot:
            return
        message = reaction.message
        session = self.sessions.get(message.guild.id) if message.guild else None
        if session is None or session.now_playing is None or message.id != session.now_playing.id:
            return
        key = _emoji_key(reaction.emoji)
        if key == _emoji_key(PLAY) and session.voice.is_paused():
            self.resume(session)
            await self.respond(session, "Resuming.")
        elif key == _emoji_key(PAUSE) and session.voice.is_playing():
            session.voice.pause()
            await self.respond(session, f"Paused at {short_time(session.position_ms)}.")
        elif key == _emoji_key(STOP) and (session.voice.is_playing() or session.voice.is_paused()):
            if await self._confirm_stop(message.channel, user):
                with contextlib.suppress(discord.HTTPException):
                    await message.reply(f"Stopped by {user.mention}")
                session.voice.stop()

    async def _confirm_stop(self, channel: Any, user: discord.abc.User) -> bool:
        prompt = await channel.send("Are you sure you want to stop?")
        try:
            await prompt.add_reaction(CONFIRM)
            await prompt.add_reaction(CANCEL)

            def check(reaction: discord.Reaction, reactor: discord.abc.User) -> bool:
                return (
                    reaction.message.id == prompt.id
                    and reactor.id == user.id
                    and _emoji_key(reaction.emoji) in (_emoji_key(CONFIRM), _emoji_key(CANCEL))
                )

            reaction, _ = await self.bot.wait_for(
                "reaction_add", check=check, timeout=STOP_CONFIRM_TIMEOUT
            )
            return _emoji_key(reaction.emoji) == _emoji_key(CONFIRM)
        except TimeoutError:
            return False
        finally:
            with contextlib.suppress(discord.HTTPException):
                await prompt.delete()


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Listen(bot))
