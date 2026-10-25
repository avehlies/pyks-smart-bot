from types import SimpleNamespace

import pytest
from discord.ext import commands

from yks.bot.checks import DefaultCooldown, GuildGate, akairo_cooldown

OWNER_ID = 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_ctx(command: commands.Command, author_id: int) -> SimpleNamespace:
    author = SimpleNamespace(id=author_id)
    message = SimpleNamespace(author=author, guild=None, channel=SimpleNamespace(id=5))
    return SimpleNamespace(
        command=command, author=author, message=message, bot=SimpleNamespace(owner_id=OWNER_ID)
    )


def test_guild_gate_blocks_until_ttl_expires() -> None:
    clock = FakeClock()
    gate = GuildGate(clock)
    assert gate.try_acquire(10, "burgy", 60_000)
    assert not gate.try_acquire(10, "burgy", 60_000)
    # Other guilds and other keys are independent.
    assert gate.try_acquire(11, "burgy", 60_000)
    assert gate.try_acquire(10, "emma", 60_000)
    clock.now += 59.9
    assert not gate.try_acquire(10, "burgy", 60_000)
    clock.now += 0.2
    assert gate.try_acquire(10, "burgy", 60_000)


async def _noop(ctx: object) -> None:  # pragma: no cover - never invoked
    pass


def test_akairo_cooldown_factory_exempts_owner() -> None:
    command = akairo_cooldown(60_000, rate=4)(commands.command(name="x")(_noop))
    factory = command._buckets._factory
    assert factory(make_ctx(command, OWNER_ID)) is None
    cooldown = factory(make_ctx(command, 2))
    assert cooldown.rate == 4
    assert cooldown.per == 60


def test_default_cooldown_one_use_per_second_per_user() -> None:
    check = DefaultCooldown()
    command = commands.command(name="plain")(_noop)
    assert check(make_ctx(command, 2))
    with pytest.raises(commands.CommandOnCooldown):
        check(make_ctx(command, 2))
    assert check(make_ctx(command, 3))  # a different user is unaffected
    assert check(make_ctx(command, OWNER_ID))
    assert check(make_ctx(command, OWNER_ID))  # owner is never limited


def test_default_cooldown_skips_commands_with_their_own() -> None:
    check = DefaultCooldown()
    command = akairo_cooldown(60_000)(commands.command(name="limited")(_noop))
    assert check(make_ctx(command, 2))
    assert check(make_ctx(command, 2))  # left to the command's own cooldown
