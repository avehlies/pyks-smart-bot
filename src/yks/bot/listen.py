"""Helpers for !listen: episode selection, durations, the progress bar, and position tracking."""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass

import discord

from yks.feeds import Episode, Feed

FRAME_MS = 20  # discord.py sends one 20 ms Opus frame per AudioSource.read()
PROGRESS_SLOTS = 24
PROGRESS_TRACK = "-" * PROGRESS_SLOTS

_NUMBERED = re.compile(r" [0-9]+:")  # "Episode 412: ..." (skips bonus items in the main feed)
_EPISODE_LABEL = re.compile(r"\b(?:Episode|Ep)\.? ?(\d+)", re.IGNORECASE)


def numbered_episodes(feed: Feed) -> list[Episode]:
    return [ep for ep in feed.episodes if _NUMBERED.search(ep.title)]


@dataclass(frozen=True)
class Selection:
    episode: Episode | None
    requested: int | None = None  # the number asked for (for "Couldn't find episode N.")


def select_episode(
    feed: Feed, *, action: str, number: int, rng: random.Random | None = None
) -> Selection:
    """Pick the episode for `!listen play [n]` / `!listen random`."""
    episodes = numbered_episodes(feed)
    if not episodes:
        return Selection(None, number or None)
    if action == "random":
        return Selection((rng or random).choice(episodes))
    if number > 0:
        found = next((ep for ep in episodes if f" {number}:" in ep.title), None)
        return Selection(found, number)
    return Selection(episodes[0])


def parse_duration_ms(value: str | None) -> int:
    """iTunes durations: "HH:MM:SS", "MM:SS" or plain seconds. 0 if unknown."""
    if not value:
        return 0
    try:
        total = 0
        for part in value.strip().split(":"):
            total = total * 60 + int(float(part))
    except ValueError:
        return 0
    return total * 1000


def colon_time(ms: int) -> str:
    """pretty-ms `colonNotation`: 0:00, 1:05, 1:02:03."""
    seconds = max(0, ms) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def short_time(ms: int) -> str:
    """pretty-ms style, to the second: 45s, 1m 23s, 1h 2m 3s."""
    seconds = max(0, ms) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{hours}h"] if hours else []
    if hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def progress_field(position_ms: int, duration_ms: int) -> tuple[str, str]:
    """(name, value) for the embed's progress field. The played part is hidden in a spoiler."""
    total = colon_time(duration_ms) if duration_ms else "?"
    name = f"Progress ({colon_time(position_ms)} / {total})"
    filled = 0
    if duration_ms > 0:
        filled = min(PROGRESS_SLOTS, math.ceil(100 * position_ms / duration_ms / 4))
    played = PROGRESS_TRACK[:filled]
    value = "\\|" + (f"||{played}||" if played else "") + "🟢" + PROGRESS_TRACK[filled:] + "\\|"
    return name, value


def title_fields(title: str) -> tuple[str, str]:
    """("Episode 412", "The Smart Mug For Dogs") from "Episode 412: The Smart Mug For Dogs"."""
    match = _EPISODE_LABEL.search(title)
    if not match:
        return "Now playing", title or "."
    rest = (title[: match.start()] + title[match.end() :]).replace(":", " ")
    return f"Episode {match.group(1)}", " ".join(rest.split()) or "."


class TrackedSource(discord.AudioSource):
    """Wraps an audio source and counts frames, to know the playback position."""

    def __init__(self, inner: discord.AudioSource) -> None:
        self.inner = inner
        self.frames = 0

    @property
    def position_ms(self) -> int:
        return self.frames * FRAME_MS

    def read(self) -> bytes:
        data = self.inner.read()
        if data:
            self.frames += 1
        return data

    def is_opus(self) -> bool:
        return self.inner.is_opus()

    def cleanup(self) -> None:
        self.inner.cleanup()
