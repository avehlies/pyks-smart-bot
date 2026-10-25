"""Message triggers: Akairo's regex commands.

A trigger fires when a message (that isn't a command) matches its pattern anywhere in the
content. Like Akairo, every matching trigger runs, each with its own per-user cooldown.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import discord

from yks.bot.checks import DEFAULT_COOLDOWN_MS, MessageCooldown

log = logging.getLogger(__name__)

Handler = Callable[[discord.Message], Awaitable[Any]]
Condition = Callable[[discord.Message], bool]


@dataclass(eq=False)
class Trigger:
    name: str
    pattern: re.Pattern[str]
    handler: Handler
    cooldown_ms: int = DEFAULT_COOLDOWN_MS
    rate: int = 1
    guild_only: bool = True
    # Checked before the cooldown is spent (e.g. "the bot was mentioned").
    condition: Condition | None = None
    _cooldown: MessageCooldown = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._cooldown = MessageCooldown(self.cooldown_ms, self.rate)

    def matches(self, message: discord.Message) -> bool:
        if self.guild_only and message.guild is None:
            return False
        if not self.pattern.search(message.content):
            return False
        return self.condition is None or self.condition(message)


class TriggerRegistry:
    def __init__(self) -> None:
        self._triggers: dict[str, Trigger] = {}

    def add(self, trigger: Trigger) -> None:
        if trigger.name in self._triggers:
            raise ValueError(f"Duplicate trigger name: {trigger.name}")
        self._triggers[trigger.name] = trigger

    def remove(self, name: str) -> None:
        self._triggers.pop(name, None)

    def __iter__(self) -> Iterator[Trigger]:
        return iter(list(self._triggers.values()))

    def __len__(self) -> int:
        return len(self._triggers)

    async def dispatch(self, message: discord.Message, *, owner_id: int | None) -> list[str]:
        """Run every matching trigger whose cooldown allows it. Returns the names that ran."""
        ran: list[str] = []
        for trigger in self:
            if not trigger.matches(message):
                continue
            if not trigger._cooldown.consume(message, owner_id=owner_id):
                log.debug("Trigger %s on cooldown for %s", trigger.name, message.author.id)
                continue
            try:
                await trigger.handler(message)
            except Exception:
                log.exception("Trigger %s failed", trigger.name)
            ran.append(trigger.name)
        return ran
