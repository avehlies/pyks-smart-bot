"""Speech-to-text with faster-whisper (CTranslate2 Whisper; decodes audio/video itself via PyAV).

The model runs in a child process (`ProcessTranscriber`) that exits after a period with no clips,
so its memory goes back to the operating system. Simply deleting the model inside a long-lived
process isn't reliable for that: the allocator tends to keep freed memory for reuse.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import multiprocessing
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

log = logging.getLogger(__name__)


class Transcriber(Protocol):
    async def transcribe(self, path: Path) -> str:
        """Return the transcript of an audio/video file ("" if there's no speech)."""
        ...


class SyncTranscriber(Protocol):
    def transcribe(self, path: Path) -> str: ...


def has_audio(path: Path) -> bool:
    """Does the file have at least one audio stream?"""
    import av  # comes with faster-whisper; only needed in the child process

    with av.open(str(path)) as container:
        return len(container.streams.audio) > 0


class WhisperTranscriber:
    """Blocking faster-whisper wrapper. Loads the model on first use (the first run downloads it
    into the Hugging Face cache). Used inside the child process."""

    def __init__(
        self, model: str, *, device: str = "cpu", compute_type: str = "int8", beam_size: int = 5
    ) -> None:
        self._name = model
        self._device = device
        self._compute_type = compute_type
        self._beam_size = beam_size
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()

    def _load(self) -> WhisperModel:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel  # heavy import; only when needed

                started = time.monotonic()
                self._model = WhisperModel(
                    self._name, device=self._device, compute_type=self._compute_type
                )
                log.info(
                    "Loaded Whisper model %s (%s, %s) in %.1fs",
                    self._name,
                    self._device,
                    self._compute_type,
                    time.monotonic() - started,
                )
            return self._model

    def transcribe(self, path: Path) -> str:
        if not has_audio(path):
            # faster-whisper raises IndexError on files with no audio track (e.g. silent videos).
            log.info("%s has no audio track; nothing to transcribe.", path.name)
            return ""
        model = self._load()
        language = "en" if self._name.endswith(".en") else None
        segments, _info = model.transcribe(
            str(path),
            beam_size=self._beam_size,
            language=language,
            vad_filter=True,  # skip silence and music beds
        )
        # `segments` is lazy: iterating it is what actually runs the model.
        return " ".join(segment.text.strip() for segment in segments).strip()


# --- the child process ------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendSpec:
    """How the child process builds its transcriber: "module:callable" plus keyword arguments.
    (A string, so it pickles cleanly into a freshly spawned process.)"""

    factory: str = "yks_clip_worker.transcriber:WhisperTranscriber"
    kwargs: tuple[tuple[str, Any], ...] = ()


_CHILD_BACKEND: SyncTranscriber | None = None


def _child_init(spec: BackendSpec, log_level: int) -> None:
    global _CHILD_BACKEND
    logging.basicConfig(
        level=log_level, format="%(asctime)s %(levelname)-8s %(name)s[child]: %(message)s"
    )
    module_name, _, attr = spec.factory.partition(":")
    factory: Callable[..., SyncTranscriber] = getattr(importlib.import_module(module_name), attr)
    _CHILD_BACKEND = factory(**dict(spec.kwargs))


def _child_transcribe(path: str) -> str:
    assert _CHILD_BACKEND is not None, "child process was not initialised"
    return _CHILD_BACKEND.transcribe(Path(path))


class ProcessTranscriber:
    """Runs the transcriber in one child process, started on demand and stopped when idle.

    * The first clip starts the child (a few seconds: process start + model load).
    * After `idle_seconds` with no clips, the child exits and all of its memory is freed.
      `idle_seconds <= 0` keeps it running for good.
    * If the child crashes, the clip fails (and is retried later); the next clip starts a new child.
    """

    def __init__(self, spec: BackendSpec, *, idle_seconds: float = 300.0) -> None:
        self._spec = spec
        self._idle_seconds = idle_seconds
        self._pool: ProcessPoolExecutor | None = None
        self._idle_timer: asyncio.TimerHandle | None = None
        self._busy = 0

    @classmethod
    def whisper(
        cls,
        model: str,
        *,
        device: str,
        compute_type: str,
        beam_size: int,
        idle_seconds: float,
    ) -> ProcessTranscriber:
        kwargs = {
            "model": model,
            "device": device,
            "compute_type": compute_type,
            "beam_size": beam_size,
        }
        return cls(BackendSpec(kwargs=tuple(kwargs.items())), idle_seconds=idle_seconds)

    @property
    def loaded(self) -> bool:
        """Is the child process (and therefore the model) currently running?"""
        return self._pool is not None

    def _start(self) -> ProcessPoolExecutor:
        log.info("Starting the transcription process.")
        return ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),  # a clean process, no forked state
            initializer=_child_init,
            initargs=(self._spec, logging.getLogger().getEffectiveLevel()),
        )

    async def transcribe(self, path: Path) -> str:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._pool is None:
            self._pool = self._start()
        pool = self._pool
        self._busy += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(
                pool, _child_transcribe, str(path)
            )
        except BrokenProcessPool:
            log.error("The transcription process died; it will be restarted for the next clip.")
            self._stop()
            raise
        finally:
            self._busy -= 1
            self._schedule_unload()

    def _schedule_unload(self) -> None:
        if self._busy or self._pool is None or self._idle_seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        self._idle_timer = loop.call_later(self._idle_seconds, self._unload_if_idle)

    def _unload_if_idle(self) -> None:
        self._idle_timer = None
        if self._busy == 0 and self._pool is not None:
            log.info(
                "No clips for %ds; stopping the transcription process to free its memory.",
                self._idle_seconds,
            )
            self._stop()

    def _stop(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        """Stop the child now (shutdown). A clip in progress is abandoned (and handed back)."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        pool, self._pool = self._pool, None
        if pool is None:
            return
        for process in list(getattr(pool, "_processes", {}).values()):
            process.terminate()  # don't wait for a long transcription to finish
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
