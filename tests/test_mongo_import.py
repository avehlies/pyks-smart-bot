"""Phase 9: importing the TypeScript bot's MongoDB data (needs Postgres for the apply tests)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import mongomock
import pytest
from sqlalchemy import func, select

from yks.config import Settings
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
from yks.tools import mongo_import
from yks.tools.mongo_import import ImportConfig, apply_plan, plan_clips, plan_settings

GUILD = 641743927799447553
BOT_USER = 789205632762642493
CLIP_CHANNEL = 1108848589196775526
MAILBAG_CHANNEL = 700000000000000001
KS_CHANNEL = 873238126187917363

# What discord.js v13's JSON.stringify(channel) produced (trimmed to a few keys).
RSS_CHANNEL_JSON = json.dumps(
    {"type": "GUILD_TEXT", "guildId": str(GUILD), "id": "682325261978697732", "name": "new-eps"}
)

GUILD_DOC = {
    "id": str(GUILD),
    "settings": {
        "welcomeMsgDisabled": True,
        "welcomedMembers": ["225822701132972034", "329288617564569602", "not-an-id"],
        "rssChannel": RSS_CHANNEL_JSON,
        "bestEpByUser": json.dumps(
            [["293571605081358336", 42], ["225822701132972034", 7], ["x", 0]]
        ),
        "bestEpTotals": json.dumps([[42, 1], [7, 1]]),  # derived: not imported
        "kickstarters": [json.dumps({"id": "1000000000000000001", "count": 3})],
        "mailbagMessages": [
            json.dumps({"id": "1000000000000000002", "count": 2, "ts": 1729800000000}),
            json.dumps({"id": "1000000000000000003", "count": 0, "ts": 1729800000000}),
        ],
    },
}
BOT_DOC = {
    "id": str(BOT_USER),
    "settings": {
        "latestMainEpTitle": "Episode 412: The Smart Mug For Dogs ",
        "latestBonusEpTitle": "Patreon Exclusive: Mailbag 88",
        "latestEpNum": 412,
    },
}
CLIP_DOCS = [
    {
        "id": "1118513877685116000",
        "url": f"https://media.discordapp.net/attachments/{CLIP_CHANNEL}/1118513877685116958/"
        "videoplayback_2.mp4?ex=6512&is=6511&hm=abc",
        "truncatedUrl": "...",
        "transcription": "Oh no, they did another one.",
    },
    {
        "id": "1118513877685117000",
        "url": f"https://cdn.discordapp.com/attachments/{CLIP_CHANNEL}/1118513877685117111/jf.ogg",
        "transcription": "",
    },
    {
        "id": "1118513877685118000",
        "url": f"https://media.discordapp.net/attachments/{CLIP_CHANNEL}/1118513877685118222/x.mov",
        "deleted": True,
    },
    {"id": "1118513877685119000", "url": "https://example.com/not-discord.mp4"},
]

CONFIG = ImportConfig(
    guild_id=GUILD,
    clip_channel_id=CLIP_CHANNEL,
    mailbag_channel_id=MAILBAG_CHANNEL,
    kickstarter_channel_id=KS_CHANNEL,
)


def full_plan(config: ImportConfig = CONFIG) -> mongo_import.ImportPlan:
    plan = plan_settings([GUILD_DOC, BOT_DOC], config)
    plan_clips(CLIP_DOCS, config, plan)
    return plan


# --- transform (no database) ----------------------------------------------------------------


def test_settings_are_decoded() -> None:
    plan = full_plan()
    assert plan.guild_settings == [
        {"guild_id": GUILD, "welcome_disabled": True, "rss_channel_id": 682325261978697732}
    ]
    assert [w["user_id"] for w in plan.welcomed] == [225822701132972034, 329288617564569602]
    assert sorted((v["user_id"], v["episode"]) for v in plan.votes) == [
        (225822701132972034, 7),
        (293571605081358336, 42),
    ]
    assert {s["key"]: s["value"] for s in plan.bot_state} == {
        "latest_main_title": "Episode 412: The Smart Mug For Dogs",
        "latest_bonus_title": "Patreon Exclusive: Mailbag 88",
        "latest_episode_number": "412",
    }


def test_tallies_get_channels_and_dates() -> None:
    tallies = {t["message_id"]: t for t in full_plan().tallies}
    assert set(tallies) == {1000000000000000001, 1000000000000000002}  # count 0 dropped
    mailbag = tallies[1000000000000000002]
    assert (mailbag["kind"], mailbag["channel_id"], mailbag["count"]) == (
        TallyKind.MAILBAG,
        MAILBAG_CHANNEL,
        2,
    )
    assert mailbag["message_created_at"] == datetime.fromtimestamp(1729800000, tz=UTC)
    kickstarter = tallies[1000000000000000001]
    assert kickstarter["channel_id"] == KS_CHANNEL
    # No timestamp stored for kickstarters: taken from the message ID (a Discord snowflake).
    assert kickstarter["message_created_at"] == mongo_import.snowflake_time(1000000000000000001)
    assert kickstarter["message_created_at"].date().isoformat() == "2022-07-22"


def test_clips_are_parsed_from_their_urls() -> None:
    plan = full_plan()
    clips = {c["message_id"]: c for c in plan.clips}
    assert len(clips) == 3
    first = clips[1118513877685116000]
    assert (first["channel_id"], first["attachment_id"], first["filename"]) == (
        CLIP_CHANNEL,
        1118513877685116958,
        "videoplayback_2.mp4",
    )
    assert first["content_type"] == "video/mp4"
    assert (first["status"], first["transcription"]) == (
        ClipStatus.DONE,
        "Oh no, they did another one.",
    )
    # Empty transcripts (failures in the TS bot) are queued again by default.
    assert clips[1118513877685117000]["status"] == ClipStatus.PENDING
    assert clips[1118513877685118000]["deleted"] is True
    assert any("unrecognised URL" in w for w in plan.warnings)


def test_keep_empty_transcripts() -> None:
    config = ImportConfig(**{**CONFIG.__dict__, "keep_empty_transcripts": True})
    clip = next(c for c in full_plan(config).clips if c["message_id"] == 1118513877685117000)
    assert (clip["status"], clip["transcription"]) == (ClipStatus.DONE, "")


def test_missing_ids_are_reported() -> None:
    config = ImportConfig(
        guild_id=None, clip_channel_id=None, mailbag_channel_id=None, kickstarter_channel_id=None
    )
    plan = full_plan(config)
    assert plan.clips == [] and plan.tallies == []
    assert any("YKS_GUILD_ID" in w for w in plan.warnings)
    assert any("mailbagMessages" in w for w in plan.warnings)


# --- applying to Postgres -------------------------------------------------------------------


async def _counts(db: Database) -> dict[str, int]:
    async with db.session() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (
                GuildSettings,
                WelcomedMember,
                BotState,
                BestEpisodeVote,
                ReactionTally,
                Clip,
            )
        }


async def test_apply_is_idempotent(db: Database) -> None:
    for _ in range(2):
        async with db.session() as session:
            await apply_plan(session, full_plan())
    assert await _counts(db) == {
        "guild_settings": 1,
        "welcomed_members": 2,
        "bot_state": 3,
        "best_episode_votes": 2,
        "reaction_tallies": 2,
        "clips": 3,
    }


async def test_apply_fills_in_transcripts_without_clobbering(db: Database) -> None:
    plan = full_plan()
    already = dict(next(c for c in plan.clips if c["status"] == ClipStatus.DONE))
    async with db.session() as session:
        # The new bot already recorded this clip (pending), and the worker finished another one.
        session.add(Clip(**{**already, "transcription": None, "status": ClipStatus.PENDING}))
        other = next(c for c in plan.clips if c["status"] == ClipStatus.PENDING)
        session.add(
            Clip(**{**other, "transcription": "fresh from the worker", "status": ClipStatus.DONE})
        )
    async with db.session() as session:
        await apply_plan(session, plan)
    async with db.session() as session:
        by_message = {c.message_id: c for c in (await session.scalars(select(Clip))).all()}
    assert by_message[already["message_id"]].transcription == "Oh no, they did another one."
    assert by_message[other["message_id"]].transcription == "fresh from the worker"


# --- reading MongoDB and the CLI --------------------------------------------------------------


@pytest.fixture
def fake_mongo(monkeypatch: pytest.MonkeyPatch) -> mongomock.MongoClient:
    client = mongomock.MongoClient()
    db = client["yks"]
    db["models"].insert_many([dict(GUILD_DOC), dict(BOT_DOC)])
    db["clips"].insert_many([dict(doc) for doc in CLIP_DOCS])
    db["sessions"].insert_one({"id": "x", "session": {}})  # ignored
    monkeypatch.setattr("pymongo.MongoClient", lambda url: client)
    return client


def test_read_mongo(fake_mongo: mongomock.MongoClient) -> None:
    models, clips = mongo_import.read_mongo("mongodb://fake", "yks")
    assert {doc["id"] for doc in models} == {str(GUILD), str(BOT_USER)}
    assert len(clips) == 4
    assert "_id" not in models[0]


def test_cli_dry_run(
    fake_mongo: mongomock.MongoClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        _env_file=None,
        yks_guild_id=GUILD,
        yks_clip_channel_id=CLIP_CHANNEL,
        yks_mailbag_channel_id=MAILBAG_CHANNEL,
        yks_kickstarter_bot_channel_id=KS_CHANNEL,
    )
    monkeypatch.setattr(mongo_import, "get_settings", lambda: settings)
    with caplog.at_level(logging.INFO, logger="yks.import"):
        mongo_import.main(["--mongo-url", "mongodb://fake", "--mongo-db", "yks", "--dry-run"])
    messages = [record.getMessage() for record in caplog.records]
    assert "Would import 3 clips" in messages
    assert "Would import 1 clips with transcripts" in messages
