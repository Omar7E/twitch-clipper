"""Word-level transcription for the caption track (faster-whisper, CPU-friendly).

Only the chosen windows get transcribed, not the whole VOD.
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

_MODEL_CACHE: dict[tuple[str, str], object] = {}


class TranscribeUnavailable(RuntimeError):
    pass


def slice_wav(src: Path, start: float, end: float, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}",
           "-t", f"{max(end - start, 0.1):.3f}", "-i", str(src),
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(f"audio slice failed: {proc.stderr[-400:]}")
    return dest


def _load(model_size: str, compute_type: str = "int8"):
    key = (model_size, compute_type)
    if key not in _MODEL_CACHE:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise TranscribeUnavailable(
                "faster-whisper is not installed — run `pip install faster-whisper`, "
                "or pass --no-captions to skip the caption track"
            ) from e
        _MODEL_CACHE[key] = WhisperModel(model_size, device="cpu",
                                         compute_type=compute_type)
    return _MODEL_CACHE[key]


def read_wav_mono16k(path: Path) -> "np.ndarray":
    """Decode our own 16 kHz mono PCM wav with the stdlib.

    faster-whisper would otherwise hand the path to PyAV, whose newer releases
    dropped the `metadata_errors` kwarg it passes. We already produce exactly
    the format whisper wants, so decode it here and skip PyAV entirely.
    """
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise RuntimeError(f"{path}: expected 16-bit PCM")
        frames = w.readframes(w.getnframes())
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
    return np.ascontiguousarray(data)


def transcribe_words(wav: Path, *, model_size: str = "small",
                     language: str | None = None) -> list[dict]:
    """Return [{word, start, end}] with times relative to the start of `wav`."""
    model = _load(model_size)
    audio = read_wav_mono16k(wav)
    segments, _info = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 350},
        beam_size=5,
        condition_on_previous_text=False,
    )
    words: list[dict] = []
    for seg in segments:
        for w in (seg.words or []):
            text = (w.word or "").strip()
            if not text:
                continue
            start = float(w.start)
            end = max(float(w.end), start + 0.08)
            words.append({"word": text, "start": round(start, 3), "end": round(end, 3)})
    words.sort(key=lambda w: w["start"])
    # clean overlaps so caption timing never runs backwards
    for a, b in zip(words, words[1:]):
        if a["end"] > b["start"]:
            a["end"] = round(max(a["start"] + 0.05, b["start"]), 3)
    return words


def group_lines(words: list[dict], *, per_line: int = 3, max_chars: int = 26,
                max_gap: float = 0.7) -> list[list[dict]]:
    """Chunk words into short caption lines that read fast on a phone."""
    lines: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            chars = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            if gap > max_gap or len(cur) >= per_line or chars > max_chars:
                lines.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines
