"""Stand-in transcription backends, loaded inside ProcessTranscriber's child process."""

from __future__ import annotations

import os
import time
from pathlib import Path

MODEL_BYTES = 200 * 1024 * 1024


class BigModel:
    """Pretends to load a 200 MB model (and touches every page so it is really resident)."""

    def __init__(self, label: str = "fake") -> None:
        self.label = label
        self.weights = bytearray(MODEL_BYTES)
        for offset in range(0, MODEL_BYTES, 4096):
            self.weights[offset] = 1

    def transcribe(self, path: Path) -> str:
        if path.name == "crash":
            os._exit(1)  # simulate the model process dying
        if path.name == "slow":
            time.sleep(30)
        return f"{self.label}: {path.name} (pid {os.getpid()})"
