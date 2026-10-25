"""One-time import of the TypeScript bot's MongoDB data into Postgres.

Reads the Akairo `MongooseProvider` collection (`models`: one `{id, settings}` document per guild,
plus one for the bot user) and the `clips` collection, converts the JSON-in-strings formats, and
upserts everything. Safe to run more than once: re-running updates rows instead of duplicating.

    uv run --extra migrate yks-import-mongo --mongo-url "mongodb+srv://..." --dry-run
    uv run --extra migrate yks-import-mongo --mongo-url "mongodb+srv://..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yks.config import Settings, get_settings
from yks.db import (
    BestEpisodeVote,
    BotState,
    Clip,
    ClipStatus,
    Database,
    GuildSettings,
    ReactionTally,
    TallyKind,
    WelcomedMember,
)
from yks.db.state import LATEST_BONUS_TITLE, LATEST_EPISODE_NUMBER, LATEST_MAIN_TITLE

log = logging.getLogger("yks.import")

DISCORD_EPOCH_MS = 1420070400000
_ATTACHMENT_PATH = re.compile(r"/attachments/(\d+)/(\d+)/([^/?#]+)")
_BOT_STATE_KEYS = {
    "latestMainEpTitle": LATEST_MAIN_TITLE,
    "latestBonusEpTitle": LATEST_BONUS_TITLE,
    "latestEpNum": LATEST_EPISODE_NUMBER,
}


def snowflake_time(snowflake: int) -> datetime:
    return datetime.fromtimestamp(((snowflake >> 22) + DISCORD_EPOCH_MS) / 1000, tz=UTC)


def _json(value: Any) -> Any:
    """Akairo values were often JSON strings (sometimes double-encoded); decode until not a str."""
    for _ in range(3):
        if not isinstance(value, str):
            return value
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return value


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ImportPlan:
    guild_settings: list[dict[str, Any]] = field(default_factory=list)
    welcomed: list[dict[str, Any]] = field(default_factory=list)
    bot_state: list[dict[str, Any]] = field(default_factory=list)
    votes: list[dict[str, Any]] = field(default_factory=list)
    tallies: list[dict[str, Any]] = field(default_factory=list)
    clips: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "guild settings": len(self.guild_settings),
            "welcomed members": len(self.welcomed),
            "bot state values": len(self.bot_state),
            "best-episode votes": len(self.votes),
            "reaction tallies": len(self.tallies),
            "clips": len(self.clips),
            "clips with transcripts": sum(c["status"] == ClipStatus.DONE for c in self.clips),
        }


@dataclass(frozen=True)
class ImportConfig:
    guild_id: int | None
    clip_channel_id: int | None
    mailbag_channel_id: int | None
    kickstarter_channel_id: int | None
    # The TS bot stored "" both for "no speech" and for "transcription failed". By default those
    # clips are queued again (the worker refreshes their URLs); set this to keep them as done.
    keep_empty_transcripts: bool = False


def plan_settings(docs: Iterable[Mapping[str, Any]], config: ImportConfig) -> ImportPlan:
    plan = ImportPlan()
    for doc in docs:
        owner_id = _int(doc.get("id"))
        settings = doc.get("settings") or {}
        if owner_id is None or not isinstance(settings, Mapping):
            plan.warnings.append(f"Skipped settings document with id {doc.get('id')!r}")
            continue

        # The bot user's own document: latest episode titles / number.
        for old_key, new_key in _BOT_STATE_KEYS.items():
            if settings.get(old_key) not in (None, ""):
                plan.bot_state.append({"key": new_key, "value": str(settings[old_key]).strip()})

        row: dict[str, Any] = {}
        if "welcomeMsgDisabled" in settings:
            row["welcome_disabled"] = bool(settings["welcomeMsgDisabled"])
        if settings.get("rssChannel"):
            channel = _json(settings["rssChannel"])
            channel_id = _int(channel.get("id")) if isinstance(channel, Mapping) else _int(channel)
            if channel_id:
                row["rss_channel_id"] = channel_id
        if row:
            plan.guild_settings.append({"guild_id": owner_id, **row})

        for member_id in settings.get("welcomedMembers") or []:
            if (user_id := _int(member_id)) is not None:
                plan.welcomed.append({"guild_id": owner_id, "user_id": user_id})

        votes = _json(settings.get("bestEpByUser"))
        if isinstance(votes, list):
            for entry in votes:
                if isinstance(entry, list) and len(entry) == 2:
                    user_id, episode = _int(entry[0]), _int(entry[1])
                    if user_id is not None and episode is not None and episode > 0:
                        plan.votes.append(
                            {"guild_id": owner_id, "user_id": user_id, "episode": episode}
                        )

        for kind, key, channel_id in (
            (TallyKind.MAILBAG, "mailbagMessages", config.mailbag_channel_id),
            (TallyKind.KICKSTARTER, "kickstarters", config.kickstarter_channel_id),
        ):
            for raw in settings.get(key) or []:
                item = _json(raw)
                message_id = _int(item.get("id")) if isinstance(item, Mapping) else None
                count = _int(item.get("count")) if isinstance(item, Mapping) else None
                if message_id is None or not count or count < 1:
                    continue
                if channel_id is None:
                    plan.warnings.append(f"No channel ID configured for {key}; skipped")
                    break
                ts = _int(item.get("ts"))
                created = (
                    datetime.fromtimestamp(ts / 1000, tz=UTC) if ts else snowflake_time(message_id)
                )
                plan.tallies.append(
                    {
                        "message_id": message_id,
                        "kind": kind,
                        "guild_id": owner_id,
                        "channel_id": channel_id,
                        "count": count,
                        "message_created_at": created,
                    }
                )
    return plan


def plan_clips(docs: Iterable[Mapping[str, Any]], config: ImportConfig, plan: ImportPlan) -> None:
    if config.guild_id is None:
        plan.warnings.append("YKS_GUILD_ID is not set; clips were not imported")
        return
    for doc in docs:
        message_id = _int(doc.get("id"))
        url = str(doc.get("url") or doc.get("truncatedUrl") or "")
        match = _ATTACHMENT_PATH.search(url)
        if message_id is None or not match:
            plan.warnings.append(f"Skipped clip {doc.get('id')!r}: unrecognised URL {url[:80]!r}")
            continue
        channel_id, attachment_id, filename = int(match[1]), int(match[2]), match[3]
        transcription = doc.get("transcription")
        has_text = isinstance(transcription, str) and transcription.strip() != ""
        done = has_text or (transcription == "" and config.keep_empty_transcripts)
        plan.clips.append(
            {
                "guild_id": config.guild_id,
                "channel_id": channel_id or config.clip_channel_id,
                "message_id": message_id,
                "attachment_id": attachment_id,
                "filename": filename,
                "content_type": mimetypes.guess_type(filename)[0],
                "url": url,
                "deleted": bool(doc.get("deleted")),
                "transcription": transcription if done else None,
                "status": ClipStatus.DONE if done else ClipStatus.PENDING,
            }
        )


async def apply_plan(session: AsyncSession, plan: ImportPlan) -> None:
    """Upsert everything in the plan."""
    for row in plan.guild_settings:
        update = {k: v for k, v in row.items() if k != "guild_id"}
        await session.execute(
            insert(GuildSettings)
            .values(**row)
            .on_conflict_do_update(index_elements=[GuildSettings.guild_id], set_=update)
        )
    if plan.welcomed:
        await session.execute(insert(WelcomedMember).values(plan.welcomed).on_conflict_do_nothing())
    for row in plan.bot_state:
        await session.execute(
            insert(BotState)
            .values(**row)
            .on_conflict_do_update(index_elements=[BotState.key], set_={"value": row["value"]})
        )
    for row in plan.votes:
        await session.execute(
            insert(BestEpisodeVote)
            .values(**row)
            .on_conflict_do_update(
                index_elements=[BestEpisodeVote.guild_id, BestEpisodeVote.user_id],
                set_={"episode": row["episode"]},
            )
        )
    for row in plan.tallies:
        await session.execute(
            insert(ReactionTally)
            .values(**row)
            .on_conflict_do_update(
                index_elements=[ReactionTally.message_id], set_={"count": row["count"]}
            )
        )
    for start in range(0, len(plan.clips), 500):
        stmt = insert(Clip).values(plan.clips[start : start + 500])
        # Keep existing rows, but bring in a transcript if Postgres doesn't have one yet.
        stmt = stmt.on_conflict_do_update(
            constraint="uq_clips_message_attachment",
            set_={
                "transcription": stmt.excluded.transcription,
                "status": stmt.excluded.status,
                "deleted": Clip.deleted | stmt.excluded.deleted,
            },
            where=(stmt.excluded.status == ClipStatus.DONE)
            & Clip.status.in_([ClipStatus.PENDING, ClipStatus.FAILED]),
        )
        await session.execute(stmt)


def read_mongo(url: str, database: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch the `models` and `clips` collections (needs the `migrate` extra: pymongo)."""
    try:
        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SystemExit("pymongo is needed: run with `uv run --extra migrate ...`") from exc
    client: MongoClient[dict[str, Any]] = MongoClient(url)
    try:
        db = client[database] if database else client.get_default_database()
        return list(db["models"].find({}, {"_id": 0})), list(db["clips"].find({}, {"_id": 0}))
    finally:
        client.close()


async def run_import(
    settings: Settings, models: list[dict[str, Any]], clips: list[dict[str, Any]], *, dry_run: bool,
    keep_empty_transcripts: bool = False,
) -> ImportPlan:  # fmt: skip
    config = ImportConfig(
        guild_id=settings.yks_guild_id,
        clip_channel_id=settings.yks_clip_channel_id,
        mailbag_channel_id=settings.yks_mailbag_channel_id,
        kickstarter_channel_id=settings.yks_kickstarter_bot_channel_id,
        keep_empty_transcripts=keep_empty_transcripts,
    )
    plan = plan_settings(models, config)
    plan_clips(clips, config, plan)
    if not dry_run:
        db = Database(settings.database_url)
        try:
            async with db.session() as session:
                await apply_plan(session, plan)
        finally:
            await db.dispose()
    return plan


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mongo-url", default=os.environ.get("MONGO_URL"), help="or MONGO_URL")
    parser.add_argument("--mongo-db", default=os.environ.get("MONGO_DB"), help="or MONGO_DB")
    parser.add_argument("--dry-run", action="store_true", help="show what would be imported")
    parser.add_argument(
        "--keep-empty-transcripts",
        action="store_true",
        help="keep clips whose TS transcript was empty as done (default: queue them again)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.mongo_url:
        parser.error("--mongo-url (or MONGO_URL) is required")

    models, clips = read_mongo(args.mongo_url, args.mongo_db)
    log.info("Read %d settings document(s) and %d clip(s) from MongoDB.", len(models), len(clips))
    plan = asyncio.run(
        run_import(
            get_settings(), models, clips, dry_run=args.dry_run,
            keep_empty_transcripts=args.keep_empty_transcripts,
        )
    )  # fmt: skip
    for warning in dict.fromkeys(plan.warnings):
        log.warning("warning: %s", warning)
    verb = "Would import" if args.dry_run else "Imported"
    for name, count in plan.summary().items():
        log.info("%s %d %s", verb, count, name)


if __name__ == "__main__":
    main()
