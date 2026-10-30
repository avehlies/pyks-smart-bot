"""Podcast feed commands (!latest, !today, !best) and the new-episode watcher."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any, Literal

import discord
from discord.ext import commands, tasks
from sqlalchemy import select

from yks.bot.checks import MINUTE, akairo_cooldown
from yks.bot.episodes import (
    ANNOUNCEMENT_EMOJIS,
    MAIN_COLOR,
    bonus_latest_embed,
    chunk_embeds,
    episodes_on_this_day,
    main_latest_embed,
    this_day_embed,
)
from yks.bot.utils import find_emoji, local_today
from yks.db import GuildSettings
from yks.db.state import (
    LATEST_BONUS_TITLE,
    LATEST_EPISODE_NUMBER,
    LATEST_MAIN_TITLE,
    get_state,
    set_state,
)
from yks.db.votes import get_vote, set_vote, vote_totals
from yks.feeds import Feed, FeedError, normalize_title

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

log = logging.getLogger(__name__)

Which = Literal["main", "bonus", "both"]
FEED_CHOICES: dict[str, Which] = {
    "main": "main",
    "bonus": "bonus",
    "premium": "bonus",
    "patreon": "bonus",
    "both": "both",
}
POLL_MINUTES = 10
FEED_UNAVAILABLE = "Couldn't reach the podcast feed right now. Try again in a bit."


def rank_totals(
    totals: list[tuple[int, int]], rng: random.Random | None = None
) -> list[tuple[int, int]]:
    """Sort (episode, votes) by votes, descending, breaking ties randomly ("no bias for ties!")."""
    rng = rng or random.Random()
    shuffled = list(totals)
    rng.shuffle(shuffled)
    return sorted(shuffled, key=lambda item: item[1], reverse=True)  # stable: keeps shuffle order


class Feeds(commands.Cog):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot
        self._presence_title: str | None = None

    async def cog_load(self) -> None:
        if self.bot.settings.main_feed_rss or self.bot.settings.bonus_feed_rss:
            self.watch_feeds.start()
        else:
            log.warning("MAIN_FEED_RSS / BONUS_FEED_RSS not set; new-episode watcher disabled.")

    async def cog_unload(self) -> None:
        self.watch_feeds.cancel()

    # --- feed access ----------------------------------------------------------------------------

    async def _feed(self, url: str) -> Feed | None:
        try:
            return await self.bot.feeds.get(url)
        except FeedError as exc:
            log.warning("Feed unavailable (%s): %s", url[:60], exc)
            return None

    async def main_feed(self) -> Feed | None:
        return await self._feed(self.bot.settings.main_feed_rss)

    async def bonus_feed(self) -> Feed | None:
        return await self._feed(self.bot.settings.bonus_feed_rss)

    async def latest_embeds(self, which: Which) -> list[discord.Embed]:
        # The main feed is needed even for "bonus": bonus episodes that are also public are skipped.
        main, bonus = await asyncio.gather(
            self.main_feed(), self.bonus_feed() if which != "main" else _none()
        )
        embeds: list[discord.Embed | None] = []
        if which in ("main", "both") and main:
            embeds.append(main_latest_embed(main))
        if which in ("bonus", "both") and bonus:
            embeds.append(bonus_latest_embed(bonus, main))
        return [e for e in embeds if e is not None]

    # --- !latest ------------------------------------------------------------------------------

    @commands.command(name="latest", aliases=["eps", "recent"])
    @commands.guild_only()
    async def latest(self, ctx: commands.Context[Any], feed: str = "both") -> None:
        which = FEED_CHOICES.get(feed.lower(), "both")
        embeds = await self.latest_embeds(which)
        if not embeds:
            await ctx.send(FEED_UNAVAILABLE)
            return
        for embed in embeds:
            await ctx.send(embed=embed)

    # --- !today -------------------------------------------------------------------------------

    @commands.command(name="today")
    @commands.guild_only()
    @akairo_cooldown(MINUTE)
    async def today(self, ctx: commands.Context[Any]) -> None:
        main, bonus = await asyncio.gather(self.main_feed(), self.bonus_feed())
        if main is None and bonus is None:
            await ctx.send(FEED_UNAVAILABLE)
            return
        tz = self.bot.settings.tz
        found = episodes_on_this_day(local_today(tz), tz, main, bonus)
        if not found:
            await ctx.send("Nothing happened on this date.")
            return
        for batch in chunk_embeds(this_day_embed(item, tz) for item in found):
            await ctx.send(embeds=batch)

    # --- !best --------------------------------------------------------------------------------

    @commands.command(name="best")
    @commands.guild_only()
    @akairo_cooldown(30 * MINUTE)
    async def best(self, ctx: commands.Context[Any], episode: str = "") -> None:
        assert ctx.guild is not None
        number = int(episode) if episode.strip().lstrip("-").isdigit() else None
        main = await self.main_feed()

        prefix = ""
        if number is not None:
            if number < 1:
                return
            if number == 101:
                await ctx.send(
                    "We already know Episode 101 is the best. This is for all the other ones."
                )
                return
            if not await self._episode_exists(number, main):
                await ctx.send("Not an episode.")
                return
            async with self.bot.db.session() as session:
                previous = await get_vote(session, ctx.guild.id, ctx.author.id)
                if previous == number:
                    await ctx.reply("No change.")
                    return
                await set_vote(session, ctx.guild.id, ctx.author.id, number)
            if previous is not None:
                prefix = f"You thought **Episode {previous}** was the best. Now...\n"
            prefix += f"You think **Episode {number}** is the best."

        async with self.bot.db.session() as session:
            ranked = rank_totals(await vote_totals(session, ctx.guild.id))[:10]
        embed = discord.Embed(color=MAIN_COLOR, title=f"Top {len(ranked)} Episodes")
        for place, (ep_number, votes) in enumerate(ranked, start=1):
            found = main.find_episode(ep_number) if main else None
            title = found.title if found else "title not found"
            embed.add_field(
                name=f"**{place}.** {title}"[:256],
                value=f"{votes} vote{'s' if votes != 1 else ''}",
                inline=False,
            )
        await ctx.reply(content=prefix or None, embed=embed)

    async def _episode_exists(self, number: int, main: Feed | None) -> bool:
        """Is `number` at or below the newest known episode number? Refreshes from the feed."""
        async with self.bot.db.session() as session:
            stored = await get_state(session, LATEST_EPISODE_NUMBER)
            latest = int(stored) if stored and stored.isdigit() else 0
            if number <= latest:
                return True
            # Maybe a new episode came out (and it's really good)...
            numbers = [ep.number for ep in main.episodes if ep.number] if main else []
            if numbers and max(numbers) > latest:
                latest = max(numbers)
                await set_state(session, LATEST_EPISODE_NUMBER, str(latest))
        return number <= latest

    # --- new-episode watcher --------------------------------------------------------------------

    @tasks.loop(minutes=POLL_MINUTES)
    async def watch_feeds(self) -> None:
        try:
            await self.check_feeds()
        except Exception:
            log.exception("Feed check failed")

    @watch_feeds.before_loop
    async def _before_watch(self) -> None:
        await self.bot.wait_until_ready()

    async def check_feeds(self) -> list[Which]:
        """Compare feed heads with what was last seen; announce new episodes. Returns what was
        announced. The first run on an empty database only records the current titles."""
        main, bonus = await asyncio.gather(self.main_feed(), self.bonus_feed())
        announced: list[Which] = []
        async with self.bot.db.session() as session:
            seen_main = await get_state(session, LATEST_MAIN_TITLE)
            seen_bonus = await get_state(session, LATEST_BONUS_TITLE)

        if main and main.latest:
            title = normalize_title(main.latest.title)
            if title != seen_main:
                async with self.bot.db.session() as session:
                    await set_state(session, LATEST_MAIN_TITLE, title)
                if seen_main is None:
                    log.info("Recorded current main episode: %s", title)
                else:
                    log.info("New main episode!\n  Previous: %s\n  Latest: %s", seen_main, title)
                    await self.announce("main")
                    announced.append("main")
                seen_main = title

        if bonus and bonus.latest:
            title = normalize_title(bonus.latest.title)
            if title != self._presence_title:
                await self.bot.change_presence(
                    status=discord.Status.dnd,
                    activity=discord.Activity(type=discord.ActivityType.listening, name=title),
                )
                self._presence_title = title
            if title != seen_bonus:
                async with self.bot.db.session() as session:
                    await set_state(session, LATEST_BONUS_TITLE, title)
                if seen_bonus is None:
                    log.info("Recorded current bonus episode: %s", title)
                elif title != seen_main:  # public episodes also land in the bonus feed
                    log.info("New bonus episode!\n  Previous: %s\n  Latest: %s", seen_bonus, title)
                    await self.announce("bonus")
                    announced.append("bonus")
        return announced

    async def announce(self, which: Which) -> None:
        """Post the latest episode to every guild's configured RSS channel, with reactions."""
        embeds = await self.latest_embeds(which)
        if not embeds:
            return
        async with self.bot.db.session() as session:
            channel_ids = (
                await session.scalars(
                    select(GuildSettings.rss_channel_id).where(
                        GuildSettings.rss_channel_id.is_not(None)
                    )
                )
            ).all()
        for channel_id in channel_ids:
            channel = self.bot.get_channel(channel_id)
            if channel is None or not hasattr(channel, "send"):
                log.warning("RSS channel %s not found; skipping announcement.", channel_id)
                continue
            for embed in embeds:
                try:
                    message = await channel.send(embed=embed)
                except discord.HTTPException:
                    log.exception("Could not announce in channel %s", channel_id)
                    break
                await self._react(message, getattr(channel, "guild", None))

    async def _react(self, message: discord.Message, guild: discord.Guild | None) -> None:
        for name in ANNOUNCEMENT_EMOJIS:
            emoji = find_emoji(guild, name)
            if emoji is None:
                continue
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                log.warning("Could not add reaction %s", name)


async def _none() -> None:
    return None


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(Feeds(bot))
