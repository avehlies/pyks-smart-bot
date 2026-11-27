"""YouTube Shorts: `!short` (a random one) and posting new ones in the shorts channel.

The separate short-worker application (see short-worker/README.md) finds the shorts and records
them in `youtube_shorts`; the bot never talks to YouTube, and the worker never talks to Discord.
Every POLL_SECONDS the bot posts shorts the worker recorded but nobody has announced yet. Each one
is claimed in the database before it's posted, so it's never posted twice (see yks.db.shorts).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from yks.bot.checks import SECOND, akairo_cooldown
from yks.db.models import YouTubeShort
from yks.db.shorts import claim_unannounced, random_short, release_claim, set_announced_message

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

NO_SHORTS = "No shorts found yet."
POLL_SECONDS = 60
# At most this many shorts per poll; more wait for the next one (rate limits, and a safety net).
MAX_PER_POLL = 5


def announcement(short: YouTubeShort) -> str:
    # Discord unfurls the link into the title and thumbnail; no need to repeat the title.
    return f"New short! {short.url}"


class Shorts(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if self.bot.settings.yks_shorts_channel_id is not None:
            self.announce_new.start()
        else:
            log.warning("YKS_SHORTS_CHANNEL_ID not set; new shorts won't be posted.")

    async def cog_unload(self) -> None:
        self.announce_new.cancel()

    async def cog_check(self, ctx: commands.Context[Any]) -> bool:  # type: ignore[override]
        """Only in the shorts channel, when one is configured. Elsewhere `!short` is ignored
        (a failed check is silent, and runs before the cooldown, so it doesn't use it up)."""
        channel_id = self.bot.settings.yks_shorts_channel_id
        return channel_id is None or ctx.channel.id == channel_id

    # --- !short -------------------------------------------------------------------------------

    @commands.command(name="short", aliases=["shorts"])
    @commands.guild_only()
    @akairo_cooldown(10 * SECOND)
    async def short(self, ctx: commands.Context[Any]) -> None:
        async with self.bot.db.session() as session:
            picked = await random_short(session)
        await ctx.reply(picked.url if picked else NO_SHORTS)

    # --- posting new shorts -------------------------------------------------------------------

    @tasks.loop(seconds=POLL_SECONDS)
    async def announce_new(self) -> None:
        try:
            await self.announce_pending()
        except Exception:
            log.exception("Posting new shorts failed")

    @announce_new.before_loop
    async def _before_announce(self) -> None:
        await self.bot.wait_until_ready()

    async def announce_pending(self) -> list[str]:
        """Post shorts that haven't been announced yet, oldest first. Returns the video ids
        posted. Stops at the first failure: the same problem (permissions, Discord being down)
        would hit the rest too, and they're all tried again next poll."""
        channel_id = self.bot.settings.yks_shorts_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        if channel is None or not hasattr(channel, "send"):
            if channel_id is not None:
                log.warning("Shorts channel %s not found; not posting new shorts.", channel_id)
            return []
        posted: list[str] = []
        for _ in range(MAX_PER_POLL):
            # Claimed and committed before posting: nothing else can post this short now.
            async with self.bot.db.session() as session:
                short = await claim_unannounced(session)
            if short is None:
                break
            try:
                message = await channel.send(announcement(short))
            except discord.HTTPException as exc:
                if exc.status >= 500:
                    # Discord may have posted it anyway: don't risk a second post.
                    log.error(
                        "Discord error posting short %s (HTTP %s); it may not have been posted, "
                        "and won't be retried.",
                        short.video_id,
                        exc.status,
                    )
                    break
                # Refused outright (missing permissions, ...): nothing was posted, so retry later.
                log.error(
                    "Couldn't post short %s in channel %s (HTTP %s: %s); will retry.",
                    short.video_id,
                    channel_id,
                    exc.status,
                    exc.text,
                )
                async with self.bot.db.session() as session:
                    await release_claim(session, short.video_id)
                break
            # Anything else (a timeout, a dropped connection) propagates with the claim kept, for
            # the same reason as a 5xx.
            async with self.bot.db.session() as session:
                await set_announced_message(session, short.video_id, message.id)
            log.info("Posted short %s (%s)", short.video_id, short.title)
            posted.append(short.video_id)
        return posted


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Shorts(bot))
