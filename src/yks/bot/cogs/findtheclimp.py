"""/findtheclimp: search clip transcripts, preview the clip privately, then post it (or not).

Transcripts are written by the separate clip-worker service; this cog only reads them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from yks.bot.media import ClipMedia, LinkGone, download_media, link_is_fresh, upload_limit
from yks.db.clips import get_clip, mark_messages_deleted, search_clips, set_clip_url

if TYPE_CHECKING:
    from yks.bot.client import YKSBot
    from yks.db import Clip

log = logging.getLogger(__name__)

COMMAND_NAME = "findtheclimp"
MIN_SEARCH_LENGTH = 3
MAX_CHOICES = 20
SNIPPET_LENGTH = 94  # + "..." on both ends = Discord's 100-character choice limit
SEARCH_CACHE_TTL = 10 * 60
VIEW_TIMEOUT = 10 * 60
NOT_A_CHOICE = "-"  # value for hint "choices"; run() treats it as "use the autocomplete"


def snippet_around(text: str, phrase: str, max_len: int = SNIPPET_LENGTH) -> str:
    """A `max_len` window of `text` centred on the first match of `phrase`, with ellipses."""
    if len(text) <= max_len:
        return text
    index = text.lower().find(phrase.lower()) if phrase.strip() else -1
    if index == -1:
        return text[:max_len] + "..."
    center = index + len(phrase) / 2
    start = max(0, min(int(center - max_len / 2), len(text) - max_len))
    result = text[start : start + max_len]
    if start > 0:
        result = "..." + result
    if start + max_len < len(text):
        result += "..."
    return result


class SearchCache:
    """Remembers what each user searched for, keyed by the clip they then picked.

    The slash command's option value is the clip ID, so this is how the posted message can say
    'for "<phrase>"'.
    """

    def __init__(
        self, ttl: float = SEARCH_CACHE_TTL, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[tuple[int, int], tuple[float, str]] = {}

    def put(self, user_id: int, clip_id: int, phrase: str) -> None:
        now = self._clock()
        self._entries[(user_id, clip_id)] = (now + self._ttl, phrase)
        if len(self._entries) > 5000:
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}

    def get(self, user_id: int, clip_id: int) -> str | None:
        entry = self._entries.get((user_id, clip_id))
        if entry is None or entry[0] < self._clock():
            self._entries.pop((user_id, clip_id), None)
            return None
        return entry[1]

    def discard(self, user_id: int, clip_id: int) -> None:
        self._entries.pop((user_id, clip_id), None)


class Timings:
    """How long each step of a request took, for one log line ("lookup=4ms fetch=180ms ...")."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._start = self._last = clock()
        self.steps: list[tuple[str, float]] = []

    def mark(self, step: str) -> None:
        """Record the time since the previous mark (or the start) as `step`."""
        now = self._clock()
        self.steps.append((step, (now - self._last) * 1000))
        self._last = now

    def __str__(self) -> str:
        parts = [f"{name}={ms:.0f}ms" for name, ms in self.steps]
        parts.append(f"total={(self._last - self._start) * 1000:.0f}ms")
        return " ".join(parts)


def build_choices(clips: list[Clip], phrase: str) -> list[app_commands.Choice[str]]:
    """Up to 20 choices, one per distinct transcript (the same clip is often posted twice)."""
    seen: set[str] = set()
    choices: list[app_commands.Choice[str]] = []
    for clip in clips:
        key = (clip.transcription or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        choices.append(
            app_commands.Choice(
                name=snippet_around(clip.transcription or "", phrase), value=str(clip.id)
            )
        )
        if len(choices) == MAX_CHOICES:
            break
    return choices


class PostClipView(discord.ui.View):
    """'Post it' / 'Don't post it' under the private preview.

    The preview is just the clip's link, so it shows up at once. The file for the public post is
    downloaded in the background meanwhile (`download`), so "Post it" rarely has to wait for it.
    """

    def __init__(
        self,
        cog: FindTheClimp,
        *,
        requester_id: int,
        clip_id: int,
        phrase: str | None,
        download: asyncio.Task[ClipMedia | None],
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.requester_id = requester_id
        self.clip_id = clip_id
        self.phrase = phrase
        self.download: asyncio.Task[ClipMedia | None] | None = download
        self.message: discord.InteractionMessage | None = None

    def _release(self, *, cancel: bool) -> asyncio.Task[ClipMedia | None] | None:
        """Finish with the view. Returns the download (cancelled if `cancel`: not posting)."""
        self.stop()
        download, self.download = self.download, None
        if cancel and download is not None:
            download.cancel()
        self.cog.search_cache.discard(self.requester_id, self.clip_id)
        return download

    async def on_timeout(self) -> None:
        self._release(cancel=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester_id

    @discord.ui.button(label="Post it", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        download = self._release(cancel=False)
        await interaction.response.edit_message(content="Posting...", view=None)
        posted = download is not None and await self.cog.post_clip(
            interaction.user, download, self.phrase
        )
        await interaction.edit_original_response(
            content="Posted." if posted else "Couldn't post it: the clip seems to be gone."
        )

    @discord.ui.button(label="Don't post it", style=discord.ButtonStyle.danger)
    async def reject(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        self._release(cancel=True)
        await interaction.response.edit_message(
            content="Didn't post. Is that a first for you?", view=None
        )


class FindTheClimp(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot
        self.search_cache = SearchCache()

    # --- the slash command ----------------------------------------------------------------------

    @app_commands.command(name=COMMAND_NAME, description="Looking for the Climp")
    @app_commands.describe(search="Phrase to search for")
    @app_commands.guild_only()
    async def findtheclimp(self, interaction: discord.Interaction, search: str) -> None:
        timings = Timings()
        await interaction.response.defer(ephemeral=True, thinking=True)
        timings.mark("defer")
        if not search.isdigit():
            await interaction.edit_original_response(
                content="Please wait for the autocomplete options!"
            )
            return
        clip_id = int(search)
        clip = await self._live_clip(clip_id, timings)
        url = await self._link(clip, timings) if clip else None
        if clip is None or url is None:
            await interaction.edit_original_response(content="Something went wrong.")
            log.info("/%s clip %s not found: %s", COMMAND_NAME, clip_id, timings)
            return
        # The preview is the link (Discord plays it inline): no download and re-upload to wait
        # for. The file for the public post downloads in the background meanwhile.
        download = asyncio.create_task(self._load_media(clip, url))
        view = PostClipView(
            self,
            requester_id=interaction.user.id,
            clip_id=clip_id,
            phrase=self.search_cache.get(interaction.user.id, clip_id),
            download=download,
        )
        await interaction.edit_original_response(content=url, view=view)
        timings.mark("preview")
        log.info("/%s preview of clip %s: %s", COMMAND_NAME, clip_id, timings)

    @findtheclimp.autocomplete("search")
    async def findtheclimp_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        phrase = current.strip()
        if len(phrase) < MIN_SEARCH_LENGTH:
            return [app_commands.Choice(name="Enter a search term.", value=NOT_A_CHOICE)]
        timings = Timings()
        try:
            async with self.bot.db.session() as session:
                clips = await search_clips(session, phrase)
        except Exception:
            log.exception("Clip search failed for %r", phrase)
            return []
        timings.mark("search")
        choices = build_choices(clips, phrase)
        log.info(
            "/%s autocomplete %r: %d choice(s) from %d clip(s): %s",
            COMMAND_NAME,
            phrase,
            len(choices),
            len(clips),
            timings,
        )
        if not choices:
            return [app_commands.Choice(name="No results.", value=NOT_A_CHOICE)]
        for choice in choices:
            self.search_cache.put(interaction.user.id, int(choice.value), phrase)
        return choices

    # --- helpers --------------------------------------------------------------------------------

    async def _live_clip(self, clip_id: int, timings: Timings) -> Clip | None:
        async with self.bot.db.session() as session:
            clip = await get_clip(session, clip_id)
        timings.mark("lookup")
        return None if clip is None or clip.deleted else clip

    async def _link(self, clip: Clip, timings: Timings) -> str | None:
        """A working link for the clip. The stored one is used while its signature is valid,
        which skips a Discord API call; otherwise the message is fetched for a new one."""
        if link_is_fresh(clip.url):
            return clip.url
        return await self._fresh_link(clip, timings)

    async def _fresh_link(self, clip: Clip, timings: Timings) -> str | None:
        """A newly signed link for the clip, from its Discord message (None if it's gone)."""
        channel = self.bot.get_channel(clip.channel_id)
        if channel is None:
            return None
        try:
            message = await channel.fetch_message(clip.message_id)  # type: ignore[union-attr]
        except discord.NotFound:
            async with self.bot.db.session() as session:
                await mark_messages_deleted(session, [clip.message_id])
            return None
        finally:
            timings.mark("fetch")
        attachment = next((a for a in message.attachments if a.id == clip.attachment_id), None)
        if attachment is None:
            return None
        if attachment.url != clip.url:
            async with self.bot.db.session() as session:
                await set_clip_url(session, clip.id, attachment.url)
            clip.url = attachment.url
        return attachment.url

    def _clip_channel(self) -> Any | None:
        return self.bot.get_channel(self.bot.settings.yks_clip_channel_id or 0)

    async def _load_media(self, clip: Clip, url: str) -> ClipMedia | None:
        """Download the clip for posting (or settle for linking it, if it's too big to upload).
        None if it no longer exists. Runs in the background while the user looks at the preview.
        """
        timings = Timings()
        guild = getattr(self._clip_channel(), "guild", None)
        try:
            media = await self._download(clip, url, guild)
        except LinkGone:
            timings.mark("download")
            # The link was refused: expired early, or the message is gone. Ask Discord once.
            fresh = await self._fresh_link(clip, timings)
            media = None
            if fresh is not None and fresh != url:
                with contextlib.suppress(LinkGone):
                    media = await self._download(clip, fresh, guild)
        timings.mark("download")
        size = "gone" if media is None else media.size_label
        log.info("/%s downloaded clip %s (%s): %s", COMMAND_NAME, clip.id, size, timings)
        return media

    async def _download(self, clip: Clip, url: str, guild: discord.Guild | None) -> ClipMedia:
        return await download_media(
            self.bot.http_client, url, clip.filename, max_bytes=upload_limit(guild)
        )

    async def post_clip(
        self,
        user: discord.abc.User,
        download: asyncio.Task[ClipMedia | None],
        phrase: str | None,
    ) -> bool:
        """Post the previewed clip to the clip channel, credited to the user who found it.
        Returns False if it couldn't (the clip is gone)."""
        channel = self._clip_channel()
        if channel is None:
            return False
        timings = Timings()
        try:
            media = await download
        except Exception:
            log.exception("Downloading a clip to post failed")
            media = None
        timings.mark("wait")  # how long the background download still had to go
        if media is None:
            return False
        payload = media.payload(getattr(channel, "guild", None))
        searched = f' for "{phrase}"' if phrase else ""
        credit = f"Requested by {user.mention} using the `/{COMMAND_NAME}` command{searched}."
        embed = discord.Embed(description=credit)
        await channel.send(embed=embed, **payload)
        timings.mark("send")
        log.info("/%s posted %s (%s): %s", COMMAND_NAME, media.filename, media.size_label, timings)
        return True

    # --- registering slash commands ---------------------------------------------------------------

    @commands.command(name="sync", hidden=True)
    @commands.is_owner()
    async def sync(self, ctx: commands.Context[Any], scope: str = "global") -> None:
        """`!sync` registers slash commands globally (can take a while to show up);
        `!sync here` copies them to this server instantly, for testing."""
        if scope == "here" and ctx.guild is not None:
            self.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await self.bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"Synced {len(synced)} command(s) to this server.")
            return
        synced = await self.bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} command(s) globally.")


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(FindTheClimp(bot))
