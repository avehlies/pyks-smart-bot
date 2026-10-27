"""Real audio decoding through faster-whisper (no model needed).

The worker's other tests fake the transcriber, so they can't notice faster-whisper and PyAV
disagreeing. That's what broke in production: faster-whisper 1.2.x calls
`av.open(..., metadata_errors="ignore")`, which PyAV 19 removed, so every clip failed. These tests
decode real files with faster-whisper's own `decode_audio`, the first step of every transcription.
"""

from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest
from faster_whisper.audio import decode_audio

SECONDS = 2
SAMPLE_RATE = 44100


def write_tone(path: Path, container: str, codec: str) -> None:
    """Encode a 440 Hz tone with PyAV (so CI needs no ffmpeg binary)."""
    with av.open(str(path), mode="w", format=container) as output:
        stream = output.add_stream(codec, rate=SAMPLE_RATE, layout="stereo")
        frame_size = 1024
        total = SECONDS * SAMPLE_RATE
        for start in range(0, total, frame_size):
            count = min(frame_size, total - start)
            t = (np.arange(start, start + count) / SAMPLE_RATE).astype(np.float32)
            mono = (0.3 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)
            samples = np.stack([mono, mono])  # planar float, 2 channels
            frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="stereo")
            frame.sample_rate = SAMPLE_RATE
            frame.pts = start
            frame.time_base = Fraction(1, SAMPLE_RATE)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)


@pytest.mark.parametrize(
    ("filename", "container", "codec"),
    [
        ("clip.m4a", "mp4", "aac"),  # Overcast / phone clips
        ("clip.mov", "mov", "aac"),  # iPhone screen recordings
        ("clip.wav", "wav", "pcm_s16le"),
    ],
)
def test_faster_whisper_decodes_clip_formats(
    tmp_path: Path, filename: str, container: str, codec: str
) -> None:
    path = tmp_path / filename
    write_tone(path, container, codec)
    audio = decode_audio(str(path), sampling_rate=16000)  # what WhisperModel.transcribe() calls
    assert audio.dtype == np.float32
    # ~2 seconds at 16 kHz (encoders add a little padding).
    assert abs(len(audio) - SECONDS * 16000) < 3000
    assert float(np.abs(audio).max()) > 0.1  # real signal, not silence
