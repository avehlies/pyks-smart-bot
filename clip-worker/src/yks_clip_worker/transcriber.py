"""Speech-to-text with faster-whisper (CTranslate2 Whisper; decodes audio/video itself via PyAV)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

log = logging.getLogger(__name__)


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> str:
        """Blocking: return the transcript of an audio/video file ("" if there's no speech)."""
        ...


class WhisperTranscriber:
    """Loads the model on first use (the first run downloads it into the Hugging Face cache)."""

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

                log.info(
                    "Loading Whisper model %s (%s, %s)",
                    self._name,
                    self._device,
                    self._compute_type,
                )
                self._model = WhisperModel(
                    self._name, device=self._device, compute_type=self._compute_type
                )
            return self._model

    def transcribe(self, path: Path) -> str:
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
