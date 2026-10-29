"""has_audio: silent videos are skipped instead of crashing faster-whisper."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import av
import numpy as np
from yks_clip_worker.transcriber import WhisperTranscriber, has_audio


def write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        samples = (int(8000 * math.sin(i / 10)) for i in range(1600))
        out.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def write_silent_video(path: Path) -> None:
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        black = np.zeros((32, 32, 3), dtype=np.uint8)
        for _ in range(5):
            frame = av.VideoFrame.from_ndarray(black, format="rgb24")
            container.mux(stream.encode(frame))
        container.mux(stream.encode())


def test_audio_file_has_audio(tmp_path: Path) -> None:
    path = tmp_path / "clip.wav"
    write_wav(path)
    assert has_audio(path)


def test_video_without_audio_track(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    write_silent_video(path)
    assert not has_audio(path)


def test_silent_video_transcribes_to_empty_without_loading_the_model(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    write_silent_video(path)
    transcriber = WhisperTranscriber("tiny.en")
    assert transcriber.transcribe(path) == ""
    assert transcriber._model is None
