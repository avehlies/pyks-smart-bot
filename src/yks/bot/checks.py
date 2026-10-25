"""Rate limiting that mirrors the TypeScript bot's behaviour.

Akairo had two layers, both reproduced here:

* **Per-user cooldowns.** Each command allows `rate` uses per user within a `ms` window
  (Akairo's `cooldown` + `ratelimit`). Every command without its own cooldown got a 1 s default.
  The owner is exempt.
* **Per-guild gate** (`client.globalRates`). Some commands fire at most once per guild per TTL
  no matter who triggers them. When gated, the command silently does nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from discord.ext import commands

T = TypeVar("T")

DEFAULT_COOLDOWN_MS = 1000

SECOND = 1000
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def _is_owner(ctx: commands.Context[Any]) -> bool:
    owner_id = getattr(ctx.bot, "owner_id", None)
    return owner_id is not None and ctx.author.id == owner_id


def akairo_cooldown(
    ms: int, rate: int = 1, *, relax_when: Callable[[Any], bool] | None = None
) -> Callable[[T], T]:
    """Allow `rate` uses per user every `ms` milliseconds. The bot owner is exempt.

    `relax_when(bot)` returning True swaps in the 1 s default instead (e.g. `!mug` on April 7).
    """
    per = ms / 1000

    def factory(ctx: commands.Context[Any]) -> commands.Cooldown | None:
        if _is_owner(ctx):
            return None
        if relax_when is not None and relax_when(ctx.bot):
            return commands.Cooldown(1, DEFAULT_COOLDOWN_MS / 1000)
        return commands.Cooldown(rate, per)

    return commands.dynamic_cooldown(factory, commands.BucketType.user)


class MessageCooldown:
    """Per-user cooldown for message triggers (regex easter eggs), same semantics as above."""

    def __init__(self, ms: int = DEFAULT_COOLDOWN_MS, rate: int = 1) -> None:
        self._mapping: commands.CooldownMapping[Any] = commands.CooldownMapping.from_cooldown(
            rate, ms / 1000, commands.BucketType.user
        )

    def consume(self, message: Any, *, owner_id: int | None) -> bool:
        """Record a use. Returns False if the user is still cooling down."""
        if owner_id is not None and message.author.id == owner_id:
            return True
        bucket = self._mapping.get_bucket(message)
        return bucket is None or not bucket.update_rate_limit()


class DefaultCooldown:
    """Global check giving every command without its own cooldown Akairo's 1 s default."""

    def __init__(self, ms: int = DEFAULT_COOLDOWN_MS) -> None:
        self._per = ms / 1000
        self._mappings: dict[str, commands.CooldownMapping[Any]] = {}

    def __call__(self, ctx: commands.Context[Any]) -> bool:
        command = ctx.command
        if command is None or command._buckets.valid or _is_owner(ctx):
            return True
        mapping = self._mappings.setdefault(
            command.qualified_name,
            commands.CooldownMapping.from_cooldown(1, self._per, commands.BucketType.user),
        )
        bucket = mapping.get_bucket(ctx.message)
        if bucket is not None and (retry_after := bucket.update_rate_limit()):
            raise commands.CommandOnCooldown(bucket, retry_after, commands.BucketType.user)
        return True


class GuildGate:
    """Lets a keyed action run at most once per guild per TTL (replaces `client.globalRates`)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._expires: dict[tuple[int, str], float] = {}

    def try_acquire(self, guild_id: int, key: str, ttl_ms: int) -> bool:
        """Return True (and start the TTL) if the action may run now, else False."""
        now = self._clock()
        slot = (guild_id, key)
        expires = self._expires.get(slot)
        if expires is not None and expires > now:
            return False
        self._expires[slot] = now + ttl_ms / 1000
        self._prune(now)
        return True

    def _prune(self, now: float) -> None:
        if len(self._expires) > 1024:
            self._expires = {k: v for k, v in self._expires.items() if v > now}
