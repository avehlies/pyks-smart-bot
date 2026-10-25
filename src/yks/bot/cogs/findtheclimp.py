"""/findtheclimp: search clip transcripts, preview the clip privately, then post it (or not).

Transcripts are written by the separate clip-worker service; this cog only reads them.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from yks.bot.media import attachment_payload
from yks.db.clips import get_clip, mark_messages_deleted, search_clips

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
    """'Post it' / 'Don't post it' under the private preview."""

    def __init__(
        self, cog: FindTheClimp, *, requester_id: int, clip_id: int, phrase: str | None
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.requester_id = requester_id
        self.clip_id = clip_id
        self.phrase = phrase
        self.message: discord.InteractionMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester_id

    @discord.ui.button(label="Post it", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content="Posted.", attachments=[], view=None)
        await self.cog.post_clip(interaction.user, self.clip_id, self.phrase)
        self.cog.search_cache.discard(self.requester_id, self.clip_id)

    @discord.ui.button(label="Don't post it", style=discord.ButtonStyle.danger)
    async def reject(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Didn't post. Is that a first for you?", attachments=[], view=None
        )
        self.cog.search_cache.discard(self.requester_id, self.clip_id)


class FindTheClimp(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot
        self.search_cache = SearchCache()

    # --- the slash command ----------------------------------------------------------------------

    @app_commands.command(name=COMMAND_NAME, description="Looking for the Climp")
    @app_commands.describe(search="Phrase to search for")
    @app_commands.guild_only()
    async def findtheclimp(self, interaction: discord.Interaction, search: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not search.isdigit():
            await interaction.edit_original_response(
                content="Please wait for the autocomplete options!"
            )
            return
        clip_id = int(search)
        found = await self._clip_attachment(clip_id)
        if found is None:
            await interaction.edit_original_response(content="Something went wrong.")
            return
        _clip, attachment = found
        payload = await attachment_payload(attachment, interaction.guild)
        view = PostClipView(
            self,
            requester_id=interaction.user.id,
            clip_id=clip_id,
            phrase=self.search_cache.get(interaction.user.id, clip_id),
        )
        if "file" in payload:
            await interaction.edit_original_response(attachments=[payload["file"]], view=view)
        else:
            await interaction.edit_original_response(content=payload["content"], view=view)

    @findtheclimp.autocomplete("search")
    async def findtheclimp_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        phrase = current.strip()
        if len(phrase) < MIN_SEARCH_LENGTH:
            return [app_commands.Choice(name="Enter a search term.", value=NOT_A_CHOICE)]
        try:
            async with self.bot.db.session() as session:
                clips = await search_clips(session, phrase)
        except Exception:
            log.exception("Clip search failed for %r", phrase)
            return []
        choices = build_choices(clips, phrase)
        if not choices:
            return [app_commands.Choice(name="No results.", value=NOT_A_CHOICE)]
        for choice in choices:
            self.search_cache.put(interaction.user.id, int(choice.value), phrase)
        return choices

    # --- helpers --------------------------------------------------------------------------------

    async def _clip_attachment(self, clip_id: int) -> tuple[Clip, discord.Attachment] | None:
        async with self.bot.db.session() as session:
            clip = await get_clip(session, clip_id)
        if clip is None or clip.deleted:
            return None
        channel = self.bot.get_channel(clip.channel_id)
        if channel is None:
            return None
        try:
            message = await channel.fetch_message(clip.message_id)  # type: ignore[union-attr]
        except discord.NotFound:
            async with self.bot.db.session() as session:
                await mark_messages_deleted(session, [clip.message_id])
            return None
        attachment = next((a for a in message.attachments if a.id == clip.attachment_id), None)
        return (clip, attachment) if attachment else None

    async def post_clip(self, user: discord.abc.User, clip_id: int, phrase: str | None) -> bool:
        """Post the clip to the clip channel, credited to the user who found it."""
        channel = self.bot.get_channel(self.bot.settings.yks_clip_channel_id or 0)
        found = await self._clip_attachment(clip_id)
        if channel is None or found is None:
            return False
        _clip, attachment = found
        payload = await attachment_payload(attachment, getattr(channel, "guild", None))
        searched = f' for "{phrase}"' if phrase else ""
        credit = f"Requested by {user.mention} using the `/{COMMAND_NAME}` command{searched}."
        embed = discord.Embed(description=credit)
        await channel.send(embed=embed, **payload)  # type: ignore[union-attr]
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
