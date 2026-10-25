"""ProcessTranscriber: the model lives in a child process that exits when idle (real processes)."""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest
from yks_clip_worker.transcriber import BackendSpec, ProcessTranscriber

SPEC = BackendSpec(factory="fake_backend:BigModel", kwargs=(("label", "test"),))


def rss_mb(pid: int) -> float:
    with open(f"/proc/{pid}/status") as status:
        for line in status:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/status") as status:
            return "zombie" not in status.read()
    except FileNotFoundError:
        return False


def child_pid(text: str) -> int:
    return int(text.rsplit("pid ", 1)[1].rstrip(")"))


async def wait_until(condition, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


pytestmark = pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux /proc")


async def test_model_memory_lives_in_the_child_and_is_freed_when_idle() -> None:
    transcriber = ProcessTranscriber(SPEC, idle_seconds=0.5)
    parent_before = rss_mb(os.getpid())
    try:
        text = await transcriber.transcribe(Path("clip.mp4"))
        assert text.startswith("test: clip.mp4")
        pid = child_pid(text)
        assert transcriber.loaded
        assert rss_mb(pid) > 180  # the 200 MB "model" is in the child...
        assert rss_mb(os.getpid()) - parent_before < 50  # ...not in the worker itself

        # Busy: a second clip reuses the same child (no reload).
        assert child_pid(await transcriber.transcribe(Path("next.mp4"))) == pid

        # Idle: the child exits and its memory goes back to the OS.
        await wait_until(lambda: not transcriber.loaded)
        await wait_until(lambda: not alive(pid))

        # The next clip starts a fresh child.
        new_pid = child_pid(await transcriber.transcribe(Path("later.mp4")))
        assert new_pid != pid
        assert transcriber.loaded
    finally:
        await transcriber.aclose()
    assert not transcriber.loaded


async def test_zero_idle_keeps_the_model_loaded() -> None:
    transcriber = ProcessTranscriber(SPEC, idle_seconds=0)
    try:
        pid = child_pid(await transcriber.transcribe(Path("a.mp4")))
        await asyncio.sleep(0.6)
        assert transcriber.loaded and alive(pid)
    finally:
        await transcriber.aclose()


async def test_a_crashed_child_is_replaced() -> None:
    transcriber = ProcessTranscriber(SPEC, idle_seconds=60)
    try:
        with pytest.raises(BrokenProcessPool):
            await transcriber.transcribe(Path("crash"))
        assert not transcriber.loaded
        assert (await transcriber.transcribe(Path("ok.mp4"))).startswith("test: ok.mp4")
    finally:
        await transcriber.aclose()


async def test_close_stops_a_long_transcription_quickly() -> None:
    transcriber = ProcessTranscriber(SPEC, idle_seconds=60)
    await transcriber.transcribe(Path("warm-up.mp4"))
    job = asyncio.create_task(transcriber.transcribe(Path("slow")))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    await transcriber.aclose()
    assert time.monotonic() - started < 5  # didn't wait for the 30 s "transcription"
    job.cancel()
    with pytest.raises((asyncio.CancelledError, BrokenProcessPool)):
        await job


async def test_whisper_factory_builds_the_spec() -> None:
    transcriber = ProcessTranscriber.whisper(
        "base.en", device="cpu", compute_type="int8", beam_size=3, idle_seconds=120
    )
    assert transcriber._spec.factory == "yks_clip_worker.transcriber:WhisperTranscriber"
    assert dict(transcriber._spec.kwargs) == {
        "model": "base.en",
        "device": "cpu",
        "compute_type": "int8",
        "beam_size": 3,
    }
    assert not transcriber.loaded  # nothing starts until the first clip
