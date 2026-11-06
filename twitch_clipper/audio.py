"""Audio excitement features, streamed straight out of ffmpeg.

We never load the whole VOD into memory: ffmpeg decodes to mono 16 kHz PCM on
stdout and we reduce it to a handful of numbers per analysis frame.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SR = 16000
FFT = 2048


@dataclass
class AudioFeatures:
    hop: float
    rms_db: np.ndarray      # loudness per frame
    hf: np.ndarray          # high-frequency energy ratio (shouting/laughter)
    times: np.ndarray
    flux: np.ndarray        # positive spectral change in the gunfire band
    crest: np.ndarray       # peak-to-RMS; impulsive sounds score high

    def __len__(self) -> int:
        return int(self.rms_db.shape[0])


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def extract(path: Path, hop: float = 0.25) -> AudioFeatures:
    hop_n = max(1, int(round(SR * hop)))
    cmd = ["ffmpeg", "-v", "error", "-i", str(path),
           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    window = np.hanning(FFT).astype(np.float32)
    freqs = np.fft.rfftfreq(FFT, 1.0 / SR)
    hi_band = (freqs >= 2000) & (freqs <= 7500)
    all_band = freqs >= 50
    # gunshots are impulsive and broadband; voiced speech is not
    shot_band = (freqs >= 1500) & (freqs <= 7800)

    rms_list: list[float] = []
    hf_list: list[float] = []
    flux_list: list[float] = []
    crest_list: list[float] = []
    prev_mag: np.ndarray | None = None
    tail = np.zeros(0, dtype=np.float32)
    read_bytes = hop_n * 2 * 64  # ~16s of audio per read

    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(read_bytes)
        if not buf:
            break
        samples = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
        data = np.concatenate([tail, samples]) if tail.size else samples
        n_full = (data.size - FFT) // hop_n + 1 if data.size >= FFT else 0
        if n_full > 0:
            idx = np.arange(n_full) * hop_n
            # frame the signal without copying it n times where possible
            frames = np.lib.stride_tricks.sliding_window_view(data, FFT)[idx]
            rms = np.sqrt(np.maximum((frames ** 2).mean(axis=1), 1e-12))
            rms_list.extend((20.0 * np.log10(rms)).tolist())
            mag = np.abs(np.fft.rfft(frames * window, axis=1))
            spec = mag ** 2
            total = spec[:, all_band].sum(axis=1) + 1e-12
            hf_list.extend((spec[:, hi_band].sum(axis=1) / total).tolist())

            # spectral flux: only energy that ARRIVES counts, so sustained
            # speech scores low while each gunshot onset spikes
            band = mag[:, shot_band]
            prev = prev_mag if prev_mag is not None else band[0:1]
            stacked = np.vstack([prev, band[:-1]]) if band.shape[0] > 1 else prev
            diff = np.maximum(band - stacked, 0.0)
            flux_list.extend(diff.sum(axis=1).tolist())
            prev_mag = band[-1:].copy()

            peak = np.abs(frames).max(axis=1)
            crest_list.extend((peak / np.maximum(rms, 1e-6)).tolist())
            consumed = idx[-1] + hop_n
            data = data[consumed:]
        tail = data
    proc.stdout.close()
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    proc.wait()
    if not rms_list:
        raise RuntimeError(f"no audio decoded from {path}: {err[-500:]}")

    rms_db = np.asarray(rms_list, dtype=np.float32)
    hf = np.asarray(hf_list, dtype=np.float32)
    n = rms_db.size
    flux = np.asarray(flux_list, dtype=np.float32)[:n]
    crest = np.asarray(crest_list, dtype=np.float32)[:n]
    if flux.size < n:
        flux = np.pad(flux, (0, n - flux.size), mode="edge")
    if crest.size < n:
        crest = np.pad(crest, (0, n - crest.size), mode="edge")
    times = np.arange(n, dtype=np.float32) * hop
    return AudioFeatures(hop=hop, rms_db=rms_db, hf=hf, times=times,
                         flux=flux, crest=crest)


def rolling_median(x: np.ndarray, win: int) -> np.ndarray:
    """Rolling median with edge padding, computed on a decimated grid."""
    n = x.size
    win = max(3, min(win | 1, n if n % 2 else n - 1))
    if win >= n:
        return np.full(n, float(np.median(x)), dtype=np.float32)
    step = max(1, win // 8)
    centers = np.arange(0, n, step)
    half = win // 2
    pad = np.pad(x, (half, half), mode="edge")
    sampled = np.empty(centers.size, dtype=np.float32)
    for i, c in enumerate(centers):
        sampled[i] = np.median(pad[c:c + win])
    if centers.size == 1:
        return np.full(n, float(sampled[0]), dtype=np.float32)
    return np.interp(np.arange(n), centers, sampled).astype(np.float32)


def smooth(x: np.ndarray, win: int) -> np.ndarray:
    win = max(1, int(win))
    if win <= 1 or x.size == 0:
        return x.astype(np.float32)
    k = np.ones(win, dtype=np.float32) / win
    pad = win // 2
    return np.convolve(np.pad(x, (pad, pad), mode="edge"), k, mode="same")[pad:pad + x.size]


def robust_norm(x: np.ndarray, lo_pct: float = 20.0, hi_pct: float = 99.0) -> np.ndarray:
    """Scale to roughly 0..1 using percentiles so a single spike can't flatten it."""
    if x.size == 0:
        return x
    lo = float(np.percentile(x, lo_pct))
    hi = float(np.percentile(x, hi_pct))
    if hi - lo < 1e-9:
        return np.zeros_like(x)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def excitement(feat: AudioFeatures, *, baseline_seconds: float = 90.0,
               loud_weight: float = 0.6, hf_weight: float = 0.25,
               dyn_weight: float = 0.15) -> np.ndarray:
    """How much louder / sharper / peakier than this streamer's own baseline."""
    win = max(3, int(baseline_seconds / max(feat.hop, 1e-6)))
    loud = feat.rms_db - rolling_median(feat.rms_db, win)
    hf = feat.hf - rolling_median(feat.hf, win)
    # local dynamics: bursty audio (laughing, shouting) vs steady narration
    two_sec = max(2, int(2.0 / max(feat.hop, 1e-6)))
    local_mean = smooth(feat.rms_db, two_sec)
    dyn = smooth(np.abs(feat.rms_db - local_mean), two_sec)

    score = (loud_weight * robust_norm(loud)
             + hf_weight * robust_norm(hf)
             + dyn_weight * robust_norm(dyn))
    total = max(loud_weight + hf_weight + dyn_weight, 1e-9)
    return (score / total).astype(np.float32)


def silence_profile(feat: AudioFeatures) -> np.ndarray:
    """Per-frame quietness, used to snap clip boundaries to natural pauses."""
    return (-robust_norm(feat.rms_db)).astype(np.float32) + 1.0


def combat_intensity(feat: AudioFeatures, *, baseline_seconds: float = 90.0,
                     flux_weight: float = 0.6, crest_weight: float = 0.4,
                     burst_seconds: float = 2.5) -> np.ndarray:
    """How much of a firefight is happening, as opposed to someone talking.

    Gunfire is a train of sharp broadband transients: each shot dumps energy
    across 1.5-8 kHz in a few milliseconds. Spectral flux counts only energy
    that newly arrives, so a sustained voice barely registers while every
    shot spikes it. Crest factor separates them further — impulsive audio has
    a far higher peak-to-RMS ratio than speech at the same loudness.
    """
    win = max(3, int(baseline_seconds / max(feat.hop, 1e-6)))
    flux = feat.flux - rolling_median(feat.flux, win)
    crest = feat.crest - rolling_median(feat.crest, win)

    # a firefight is a burst of these, not one tick, so pool over a window
    burst = max(1, int(burst_seconds / max(feat.hop, 1e-6)))
    flux_b = smooth(np.maximum(flux, 0.0), burst)
    crest_b = smooth(np.maximum(crest, 0.0), burst)

    score = flux_weight * robust_norm(flux_b) + crest_weight * robust_norm(crest_b)
    return (score / max(flux_weight + crest_weight, 1e-9)).astype(np.float32)
