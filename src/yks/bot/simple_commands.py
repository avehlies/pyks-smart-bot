"""Config-driven commands (port of `commands/config/simpleCommands.ts`).

Each entry is triggered either by `!alias` (a normal prefix command) or by a regex anywhere in a
message (an easter egg), and answers with a fixed response. Cooldowns are per user; `guild_gate_ms`
additionally limits the command to once per guild per window, whoever triggers it.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import discord

from yks.bot.checks import DAY, HOUR, MINUTE, GuildGate
from yks.bot.utils import bot_is_burgy_day


@dataclass(frozen=True)
class Text:
    content: str
    reply: bool = False


@dataclass(frozen=True)
class Embed:
    embed: dict[str, Any]


@dataclass(frozen=True)
class File:
    path: str  # relative to the assets directory
    name: str | None = None


@dataclass(frozen=True)
class RandomFile:
    items: tuple[File, ...]


@dataclass(frozen=True)
class RandomText:
    items: tuple[str, ...]
    reply: bool = False


Response = Text | Embed | File | RandomFile | RandomText


@dataclass(frozen=True)
class SimpleCommand:
    id: str
    response: Response
    aliases: tuple[str, ...] = ()
    regex: re.Pattern[str] | None = None
    cooldown_ms: int | None = None  # None: the 1 s default
    rate: int = 1
    guild_gate_ms: int | None = None
    require_guild: bool = False
    skip_user_ids: frozenset[int] = field(default_factory=frozenset)
    # Returns True when the cooldown should drop to the 1 s default (e.g. April 7 for !mug).
    relax_cooldown_when: Callable[[Any], bool] | None = None

    def __post_init__(self) -> None:
        if bool(self.aliases) == bool(self.regex):
            raise ValueError(f"{self.id}: needs exactly one of aliases or regex")


# Long copypastas, verbatim from the TypeScript bot.
_AMOGUS = (
    "Hey everybody, for real, playing to frustrate each other is not a fun way to play "
    "because we're all on the same team and that team is to have fun together and to make "
    "it fun for all our audiences. And so when people make plays just to frustrate each "
    "other and just to troll each other, there's enough of that in the world today, of "
    "people trolling each other just to be mean and to be hurtful, and if we're gonna play "
    "in this space together we need to do it because we want each other to have fun and "
    "not because we're trying to frustrate each other, cause there's enough frustrating "
    "things in the world right now and there's enough we can't control, and one of the "
    "things we can control is that everyone is here to have fun and not waste each others' "
    "time and so when we make decisions that are meant to troll each other, that's "
    "something that bad people do."
)

_GABE = (
    "Really wanted to like this podcast, the hosts are pretty funny and I'm a big fan of "
    "the South. But at this point I'm gonna cancel my Patreon; the constant one-sided "
    "trashing of conservatives is just too much. It would be one thing if the jabs were "
    "funny, but it's usually they're more along the lines of \"some guy was talking to me "
    "about Alex Jones and I was like, I don't want to have this conversation\". Just "
    "blatant unfunny virtue signaling to the presumably overwhelmingly liberal "
    "audience.\n\nHeading back to Hollywood Handbook-- even though everyone knows they're "
    "liberal California elites, they're able to restrain themselves from flinging shit "
    "long enough to record an hour-long podcast every week. Hope you guys know that by "
    "demonizing your political opponents, you're deepening the divide in America, and when "
    "Americans commit acts of terror, it's because of this environment that you're helping "
    "to perpetuate."
)

# Easter eggs that echo a word back, at most once per 3 hours per guild.
_ECHO_GATE = 3 * HOUR

SIMPLE_COMMANDS: tuple[SimpleCommand, ...] = (
    SimpleCommand(
        id="feet",
        aliases=("feet",),
        cooldown_ms=HOUR,
        guild_gate_ms=_ECHO_GATE,
        response=Text("Is that you, Quentin?", reply=True),
        require_guild=True,
    ),
    *(
        SimpleCommand(
            id=word,
            regex=re.compile(rf"^{word}$", re.IGNORECASE),
            cooldown_ms=HOUR,
            guild_gate_ms=_ECHO_GATE,
            response=Text(word, reply=True),
            require_guild=True,
        )
        for word in ("fubfay", "garm", "gorb", "grog", "cunky", "bustard")
    ),
    SimpleCommand(
        id="cunch",
        regex=re.compile(r"cunch", re.IGNORECASE),
        cooldown_ms=HOUR,
        guild_gate_ms=_ECHO_GATE,
        response=Text("cunch wrap supreme", reply=True),
        require_guild=True,
    ),
    SimpleCommand(
        id="eve",
        aliases=("eve",),
        cooldown_ms=MINUTE,
        guild_gate_ms=MINUTE,
        response=Text("Who?", reply=True),
        require_guild=True,
    ),
    SimpleCommand(
        id="garth",
        aliases=("garth", "slickstuff", "coolstuff", "rawstuff", "neatstuff"),
        cooldown_ms=HOUR,
        response=Text(
            "https://cdn.discordapp.com/attachments/1108848589196775526/1118513877685116958/videoplayback_2.mp4"
        ),
    ),
    SimpleCommand(
        id="barnes",
        aliases=("barnes",),
        cooldown_ms=MINUTE,
        guild_gate_ms=MINUTE,
        response=File("barmes.jpg"),
        require_guild=True,
    ),
    SimpleCommand(
        id="amogus",
        aliases=("amogus", "frustrating"),
        cooldown_ms=MINUTE,
        guild_gate_ms=DAY,
        response=Text(_AMOGUS),
        require_guild=True,
    ),
    SimpleCommand(
        id="bongo",
        aliases=("bongo",),
        cooldown_ms=MINUTE,
        response=Text("https://ghostbongo.bigcartel.com"),
    ),
    SimpleCommand(
        id="bo",
        aliases=("bo",),
        cooldown_ms=HOUR,
        response=Text("<@225822701132972034> can you handle this please.", reply=True),
    ),
    SimpleCommand(
        id="crash",
        aliases=("crash", "carcrash", "rearend", "rearended", "dashcam"),
        cooldown_ms=MINUTE,
        response=Text("https://www.youtube.com/watch?v=aTBHQXzt_C0&feature=youtu.be"),
    ),
    SimpleCommand(
        id="ftp",
        aliases=("ftp",),
        cooldown_ms=MINUTE,
        response=Text(
            "https://discord.com/channels/641743927799447553/682325261978697732/1249099267927183391"
        ),
    ),
    SimpleCommand(
        id="music",
        aliases=("music", "soundtrack", "songs", "soundcloud"),
        cooldown_ms=MINUTE,
        response=Text("https://soundcloud.com/ykspod"),
        require_guild=True,
    ),
    SimpleCommand(
        id="sixpack",
        aliases=("sixpack", "sixpacks", "6pack", "6packs", "kaggle"),
        cooldown_ms=MINUTE,
        response=Text("https://www.kaggle.com/officerbribe/yks-six-pack"),
        require_guild=True,
    ),
    SimpleCommand(
        id="usenet",
        aliases=("usenet", "stealing"),
        cooldown_ms=MINUTE,
        response=Text(
            "https://discord.com/channels/641743927799447553/682325261978697732/1140406217836081222"
        ),
    ),
    SimpleCommand(
        id="mailbag",
        aliases=("mailbag", "address", "pubox"),
        cooldown_ms=MINUTE,
        response=Embed(
            {
                "color": 0x83C133,
                "title": "P.U. Box",
                "description": "540 W Main St #209\nGallatin, TN\n37066\nUSA",
                "footer": {"text": "Remember to put an embarrassing name on the package"},
            }
        ),
        require_guild=True,
    ),
    SimpleCommand(
        id="sporange",
        regex=re.compile(r"sporange", re.IGNORECASE),
        cooldown_ms=MINUTE,
        guild_gate_ms=MINUTE,
        response=Text("it's a new kind of orange", reply=True),
        require_guild=True,
    ),
    SimpleCommand(
        id="staysippin",
        aliases=("staysippin", "staysipping", "twodrinks"),
        cooldown_ms=MINUTE,
        rate=4,
        guild_gate_ms=MINUTE,
        response=RandomFile(
            tuple(
                File(f"staysippin/{name}")
                for name in (
                    "8chbein.png",
                    "adhd_md.jpg",
                    "heycraisins.jpg",
                    "jf.jpg",
                    "jf-nyc.png",
                    "jf-time-magazine.jpg",
                    "kristin.jpg",
                    "mrs-craisins.jpg",
                    "YoungOnion.png",
                )
            )
        ),
        require_guild=True,
    ),
    SimpleCommand(
        id="gabe",
        aliases=("gabe", "terrorists", "thesouth", "cancel", "virtuesignal"),
        cooldown_ms=MINUTE,
        guild_gate_ms=DAY,
        response=Text(_GABE),
        require_guild=True,
    ),
    SimpleCommand(
        id="mug",
        aliases=("mug", "burgymug"),
        cooldown_ms=MINUTE,
        relax_cooldown_when=bot_is_burgy_day,
        guild_gate_ms=MINUTE,
        response=RandomFile(
            tuple(
                File(path) for path in ("mug1.png", "mug2.jpg", "mug3.png", "mug4.jpg", "mug5.jpg")
            )
        ),
        require_guild=True,
    ),
)


def _discord_file(item: File, assets_dir: Path) -> discord.File:
    path = assets_dir / item.path
    return discord.File(path, filename=item.name or path.name)


async def send_response(response: Response, message: discord.Message, assets_dir: Path) -> None:
    match response:
        case Text(content, reply):
            await (message.reply(content) if reply else message.channel.send(content))
        case Embed(embed):
            await message.channel.send(embed=discord.Embed.from_dict(embed))
        case File():
            await message.channel.send(file=_discord_file(response, assets_dir))
        case RandomFile(items):
            await message.channel.send(file=_discord_file(random.choice(items), assets_dir))
        case RandomText(items, reply):
            content = random.choice(items)
            await (message.reply(content) if reply else message.channel.send(content))


async def run_simple_command(
    spec: SimpleCommand, message: discord.Message, *, gate: GuildGate, assets_dir: Path
) -> bool:
    """Apply the command's guards, then respond. Returns True if a response was sent."""
    if message.author.id in spec.skip_user_ids:
        return False
    if spec.require_guild and message.guild is None:
        return False
    if spec.guild_gate_ms is not None:
        if message.guild is None:
            return False
        if not gate.try_acquire(message.guild.id, spec.id, spec.guild_gate_ms):
            return False
    await send_response(spec.response, message, assets_dir)
    return True
