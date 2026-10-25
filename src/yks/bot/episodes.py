"""Embeds for podcast episodes (`!latest`, `!today`, new-episode announcements)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

import discord

from yks.feeds import Episode, Feed, normalize_title

MAIN_COLOR = 0x83C133
BONUS_COLOR = 0xDDAF74

MAIN_ART = (
    "https://content.production.cdn.art19.com/images/c8/38/41/df/"
    "c83841df-2683-4baf-8959-28a8e7d66774/"
    "3e98f6d3fffcf5ebd7e02df5609cfe5fe9997e62f24382a26649e59061a6d029a0e16417689b0ccd00f7fc7638344abe1f61bc8d9e3c7235e4e60f43efec8c38.jpeg"
)
BONUS_ART = "https://i.imgur.com/Cmb1RTP.jpg"
OVERCAST_LINK = "https://overcast.fm/itunes1204911385"
APPLE_LINK = "https://podcasts.apple.com/us/podcast/your-kickstarter-sucks/id1204911385"

# Reactions added to new-episode announcements ("oh no, they did another").
ANNOUNCEMENT_EMOJIS = ("1ohno", "2they", "3did", "40another", "yks")

FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
EMBEDS_PER_MESSAGE = 10
CHARS_PER_MESSAGE = 6000


def _clip(text: str, limit: int) -> str:
    """Trim to a Discord field limit the way the TS bot did (`slice(0, limit - 4)...`)."""
    return text if len(text) < limit else f"{text[: limit - 4].rstrip()}..."


def latest_bonus_episode(bonus: Feed, main: Feed | None) -> Episode | None:
    """Newest bonus episode that isn't also in the main feed."""
    main_titles = main.titles() if main else set()
    return next((ep for ep in bonus.episodes if normalize_title(ep.title) not in main_titles), None)


def main_latest_embed(main: Feed) -> discord.Embed | None:
    episode = main.latest
    if episode is None:
        return None
    links = [f"[ART19]({episode.audio_url})"] if episode.audio_url else []
    links += [f"[Overcast]({OVERCAST_LINK})", f"[Apple]({APPLE_LINK})"]
    embed = discord.Embed(color=MAIN_COLOR, title=main.title or None)
    embed.set_thumbnail(url=MAIN_ART)
    embed.add_field(
        name=_clip(episode.title, FIELD_NAME_LIMIT), value="\n".join(links), inline=False
    )
    return embed


def bonus_latest_embed(bonus: Feed, main: Feed | None) -> discord.Embed | None:
    episode = latest_bonus_episode(bonus, main)
    if episode is None:
        return None
    embed = discord.Embed(color=BONUS_COLOR, title=bonus.title or None)
    embed.set_thumbnail(url=BONUS_ART)
    value = f"[Patreon]({episode.link})" if episode.link else "Patreon"
    embed.add_field(name=_clip(episode.title, FIELD_NAME_LIMIT), value=value, inline=False)
    return embed


@dataclass(frozen=True)
class DatedEpisode:
    episode: Episode
    is_bonus: bool


def episodes_on_this_day(
    today: date, tz: ZoneInfo, main: Feed | None, bonus: Feed | None
) -> list[DatedEpisode]:
    """Main episodes, then bonus-only episodes, released on today's month/day in any year."""

    def same_day(episode: Episode) -> bool:
        if episode.published is None:
            return False
        local = episode.published.astimezone(tz)
        return (local.month, local.day) == (today.month, today.day)

    found = [DatedEpisode(ep, False) for ep in (main.episodes if main else ()) if same_day(ep)]
    if bonus is not None:
        main_titles = main.titles() if main else set()
        found += [
            DatedEpisode(ep, True)
            for ep in bonus.episodes
            if same_day(ep) and normalize_title(ep.title) not in main_titles
        ]
    return found


def format_date(day: date) -> str:
    """'Oct 25, 2019' (en-US medium date; avoids the non-portable %-d)."""
    return f"{day:%b} {day.day}, {day.year}"


def this_day_embed(item: DatedEpisode, tz: ZoneInfo) -> discord.Embed:
    episode = item.episode
    assert episode.published is not None
    embed = discord.Embed(
        color=BONUS_COLOR if item.is_bonus else MAIN_COLOR,
        title=format_date(episode.published.astimezone(tz).date()),
    )
    thumbnail = BONUS_ART if item.is_bonus else (episode.image_url or MAIN_ART)
    embed.set_thumbnail(url=thumbnail)
    embed.add_field(
        name=_clip(episode.title, FIELD_NAME_LIMIT),
        value=_clip(episode.summary, FIELD_VALUE_LIMIT) or "No description.",
        inline=False,
    )
    link = episode.link or episode.audio_url
    if link:
        label = "Patreon" if item.is_bonus else "Listen"
        embed.add_field(name="Link", value=f"[{label}]({link})", inline=False)
    return embed


def chunk_embeds(embeds: Iterable[discord.Embed]) -> Iterator[list[discord.Embed]]:
    """Group embeds into messages within Discord's limits (10 embeds, 6000 characters)."""
    batch: list[discord.Embed] = []
    chars = 0
    for embed in embeds:
        size = len(embed)
        if batch and (len(batch) == EMBEDS_PER_MESSAGE or chars + size > CHARS_PER_MESSAGE):
            yield batch
            batch, chars = [], 0
        batch.append(embed)
        chars += size
    if batch:
        yield batch
