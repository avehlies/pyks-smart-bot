"""The clip worker: queue semantics and the refresh -> download -> transcribe pipeline."""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest
from yks_clip_worker.config import WorkerSettings, asyncpg_dsn
from yks_clip_worker.discord_api import DiscordAPI
from yks_clip_worker.queue import ClipQueue
from yks_clip_worker.transcriber import WhisperTranscriber
from yks_clip_worker.worker import Worker

CHANNEL = 111
OLD_URL = "https://cdn.discordapp.com/attachments/111/{a}/clip.mp4?ex=old"
FRESH_URL = "https://cdn.discordapp.com/attachments/111/{a}/clip.mp4?ex=fresh"


async def insert_clip(
    pool: asyncpg.Pool, message_id: int, attachment_id: int, **extra: object
) -> int:
    columns = {
        "guild_id": 1,
        "channel_id": CHANNEL,
        "message_id": message_id,
        "attachment_id": attachment_id,
        "filename": "clip.mp4",
        "url": OLD_URL.format(a=attachment_id),
        **extra,
    }
    names = ", ".join(columns)
    params = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    return await pool.fetchval(
        f"INSERT INTO clips ({names}) VALUES ({params}) RETURNING id",
        *columns.values(),
    )


async def clip_row(pool: asyncpg.Pool, clip_id: int) -> asyncpg.Record:
    return await pool.fetchrow("SELECT * FROM clips WHERE id = $1", clip_id)


class FakeDiscord:
    """Serves the Discord API (messages) and the CDN (file bytes)."""

    def __init__(self) -> None:
        self.messages: dict[int, list[int]] = {}  # message id -> attachment ids
        self.cdn_status = 200
        self.cdn_body = b"\x00fake media bytes"
        self.rate_limit_once = False
        self.api_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://discord.com/api/v10/channels/"):
            self.api_calls += 1
            assert request.headers["Authorization"] == "Bot test-token"
            if self.rate_limit_once:
                self.rate_limit_once = False
                return httpx.Response(429, json={"retry_after": 0.01})
            message_id = int(url.rsplit("/", 1)[1])
            if message_id not in self.messages:
                return httpx.Response(404, json={"message": "Unknown Message"})
            attachments = [
                {"id": str(a), "url": FRESH_URL.format(a=a)} for a in self.messages[message_id]
            ]
            return httpx.Response(200, json={"id": str(message_id), "attachments": attachments})
        if "ex=fresh" in url:
            return httpx.Response(self.cdn_status, content=self.cdn_body)
        return httpx.Response(403)  # expired link


class FakeTranscriber:
    def __init__(self, text: str = "oh no they did another one") -> None:
        self.text = text
        self.paths: list[Path] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def transcribe(self, path: Path) -> str:
        assert path.read_bytes() == b"\x00fake media bytes"  # noqa: ASYNC240 - tiny test file
        self.paths.append(path)
        if self.gate is not None:  # block until the test lets it finish
            self.gate.set()
            await asyncio.sleep(60)
        if self.error:
            raise self.error
        return self.text


@pytest.fixture
def fake_discord() -> FakeDiscord:
    return FakeDiscord()


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
async def worker(
    pool: asyncpg.Pool, fake_discord: FakeDiscord, transcriber: FakeTranscriber
) -> AsyncIterator[Worker]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_discord.handler))
    settings = WorkerSettings(_env_file=None, auth_token="test-token", max_attempts=3)
    yield Worker(
        queue=ClipQueue(pool),
        discord=DiscordAPI("test-token", http),
        http=http,
        transcriber=transcriber,
        settings=settings,
    )
    await http.aclose()


# --- queue --------------------------------------------------------------------------------


async def test_claim_order_and_filters(pool: asyncpg.Pool) -> None:
    queue = ClipQueue(pool)
    oldest = await insert_clip(pool, 1, 1)
    await insert_clip(pool, 2, 2, deleted=True)
    await insert_clip(pool, 3, 3, status="done")
    later = await insert_clip(pool, 4, 4)
    await pool.execute("UPDATE clips SET retry_at = now() + interval '1 hour' WHERE id = $1", later)
    newest = await insert_clip(pool, 5, 5)

    first, second, third = await queue.claim(), await queue.claim(), await queue.claim()
    assert (first.id, second.id, third) == (newest, oldest, None)  # newest first
    assert first.attempts == 1
    assert (await clip_row(pool, first.id))["status"] == "processing"


async def test_concurrent_claims_never_share_a_clip(pool: asyncpg.Pool) -> None:
    for i in range(10):
        await insert_clip(pool, i, i)
    queue = ClipQueue(pool)
    jobs = await asyncio.gather(*(queue.claim() for _ in range(12)))
    ids = [job.id for job in jobs if job]
    assert len(ids) == 10
    assert len(set(ids)) == 10


async def test_fail_backs_off_then_gives_up(pool: asyncpg.Pool) -> None:
    queue = ClipQueue(pool)
    clip_id = await insert_clip(pool, 1, 1)
    for attempt in (1, 2):
        job = await queue.claim()
        assert job.attempts == attempt
        assert await queue.fail(job, "boom", max_attempts=3) == "pending"
        row = await clip_row(pool, clip_id)
        assert row["retry_at"] is not None
        await pool.execute("UPDATE clips SET retry_at = now() WHERE id = $1", clip_id)
    job = await queue.claim()
    assert await queue.fail(job, "boom", max_attempts=3) == "failed"
    row = await clip_row(pool, clip_id)
    assert (row["status"], row["last_error"], row["retry_at"]) == ("failed", "boom", None)


async def test_release_and_reclaim(pool: asyncpg.Pool) -> None:
    queue = ClipQueue(pool)
    clip_id = await insert_clip(pool, 1, 1)
    await queue.release(await queue.claim())
    row = await clip_row(pool, clip_id)
    assert (row["status"], row["attempts"]) == ("pending", 0)

    await queue.claim()
    await pool.execute(
        "UPDATE clips SET claimed_at = now() - interval '2 hours' WHERE id = $1", clip_id
    )
    assert await queue.reclaim_stale(timedelta(minutes=30)) == 1
    assert (await clip_row(pool, clip_id))["status"] == "pending"


# --- pipeline -------------------------------------------------------------------------------


async def test_transcribes_with_a_fresh_url(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord, transcriber: FakeTranscriber
) -> None:
    clip_id = await insert_clip(pool, 10, 20)
    fake_discord.messages[10] = [20]
    assert await worker.process_one() is True
    row = await clip_row(pool, clip_id)
    assert row["status"] == "done"
    assert row["transcription"] == "oh no they did another one"
    assert row["url"] == FRESH_URL.format(a=20)
    assert row["transcribed_at"] is not None
    assert not transcriber.paths[0].exists()  # temp file cleaned up
    assert await worker.process_one() is False


async def test_full_text_search_sees_the_transcript(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord
) -> None:
    clip_id = await insert_clip(pool, 10, 20)
    fake_discord.messages[10] = [20]
    await worker.drain()
    hit = await pool.fetchval(
        "SELECT id FROM clips WHERE search @@ websearch_to_tsquery('english', 'another')"
    )
    assert hit == clip_id


async def test_empty_transcript_is_done(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord, transcriber: FakeTranscriber
) -> None:
    transcriber.text = ""
    clip_id = await insert_clip(pool, 10, 20)
    fake_discord.messages[10] = [20]
    await worker.drain()
    row = await clip_row(pool, clip_id)
    assert (row["status"], row["transcription"]) == ("done", "")


@pytest.mark.parametrize("attachments", [None, [99]])  # message gone / attachment removed
async def test_deleted_messages_are_skipped(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord, attachments: list[int] | None
) -> None:
    clip_id = await insert_clip(pool, 10, 20)
    if attachments is not None:
        fake_discord.messages[10] = attachments
    await worker.drain()
    row = await clip_row(pool, clip_id)
    assert (row["status"], row["deleted"]) == ("skipped", True)


async def test_download_errors_retry_later(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord
) -> None:
    clip_id = await insert_clip(pool, 10, 20)
    fake_discord.messages[10] = [20]
    fake_discord.cdn_status = 503
    await worker.drain()  # one attempt, then it waits for retry_at
    row = await clip_row(pool, clip_id)
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert "503" in row["last_error"]


async def test_too_big_fails_permanently(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord
) -> None:
    worker.settings = worker.settings.model_copy(update={"max_download_mb": 0})
    clip_id = await insert_clip(pool, 10, 20)
    fake_discord.messages[10] = [20]
    await worker.drain()
    row = await clip_row(pool, clip_id)
    assert row["status"] == "failed"
    assert "MAX_DOWNLOAD_MB" in row["last_error"]


async def test_transcriber_errors_are_recorded(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord, transcriber: FakeTranscriber
) -> None:
    transcriber.error = RuntimeError("decoder exploded")
    clip_id = await insert_clip(pool, 10, 20)
    fake_discord.messages[10] = [20]
    await worker.drain()
    row = await clip_row(pool, clip_id)
    assert row["status"] == "pending"
    assert row["last_error"] == "RuntimeError: decoder exploded"


async def test_rate_limits_are_waited_out(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord
) -> None:
    fake_discord.rate_limit_once = True
    clip_id = await insert_clip(pool, 10, 20)
    fake_discord.messages[10] = [20]
    await worker.drain()
    assert (await clip_row(pool, clip_id))["status"] == "done"
    assert fake_discord.api_calls == 2


async def test_cancel_mid_job_hands_the_clip_back(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord, transcriber: FakeTranscriber
) -> None:
    clip_id = await insert_clip(pool, 10, 20)
    fake_discord.messages[10] = [20]
    transcriber.gate = asyncio.Event()  # transcribe() sets it, then hangs
    task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(transcriber.gate.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await clip_row(pool, clip_id)
    assert (row["status"], row["attempts"]) == ("pending", 0)


async def test_run_wakes_on_notify(
    worker: Worker, pool: asyncpg.Pool, fake_discord: FakeDiscord, worker_dsn: str
) -> None:
    worker.settings = worker.settings.model_copy(update={"poll_interval_seconds": 120})
    listen = await asyncpg.connect(worker_dsn)
    task = asyncio.create_task(worker.run(listen))
    try:
        await asyncio.sleep(0.2)  # let it drain the (empty) queue and start waiting
        fake_discord.messages[10] = [20]
        clip_id = await insert_clip(pool, 10, 20)  # the trigger sends NOTIFY clips_new
        for _ in range(100):
            if (await clip_row(pool, clip_id))["status"] == "done":
                break
            await asyncio.sleep(0.05)
        assert (await clip_row(pool, clip_id))["status"] == "done"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await listen.close()


# --- config and the Whisper wrapper -----------------------------------------------------------


def test_dsn_accepts_the_bots_url() -> None:
    assert asyncpg_dsn("postgresql+asyncpg://u:p@db/yks") == "postgresql://u:p@db/yks"
    assert asyncpg_dsn("postgres://u:p@db/yks") == "postgresql://u:p@db/yks"


def test_whisper_transcriber_joins_segments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    class FakeModel:
        def __init__(self, name: str, *, device: str, compute_type: str) -> None:
            calls["init"] = (name, device, compute_type)

        def transcribe(self, path: str, **kwargs: object):
            calls["kwargs"] = kwargs
            segments = (
                types.SimpleNamespace(text=t) for t in (" Oh no, ", "they did ", " another.")
            )
            return segments, None

    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel)
    )
    transcriber = WhisperTranscriber("tiny.en", compute_type="int8")
    assert transcriber.transcribe(tmp_path / "x.mp4") == "Oh no, they did another."
    assert calls["init"] == ("tiny.en", "cpu", "int8")
    assert calls["kwargs"]["language"] == "en"
    assert calls["kwargs"]["vad_filter"] is True
    transcriber.transcribe(tmp_path / "y.mp4")  # model is loaded once
