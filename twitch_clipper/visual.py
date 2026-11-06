"""Check what a candidate moment actually looks like before clipping it.

Audio alone cannot tell a firefight from someone talking over a pause menu —
on a real VOD the loudest moment of the stream turned out to be twenty
seconds of the settings screen. Decoding a few seconds at thumbnail size is
cheap and settles it: a paused game barely changes between frames, and
gameplay never stops changing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

W, H, FPS = 160, 90, 4
STILL_THRESHOLD = 1.0   # mean abs pixel delta below this = a frozen frame


def motion_stats(path: Path, *, start: float | None = None,
                 duration: float | None = None) -> dict | None:
    """Return motion statistics for a clip, or None if it can't be read."""
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.2f}"]
    cmd += ["-vf", f"fps={FPS},scale={W}:{H}", "-pix_fmt", "gray",
            "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (W * H)
    if n < 3:
        return None
    frames = (np.frombuffer(raw[: n * W * H], dtype=np.uint8)
              .reshape(n, H * W).astype(np.float32))
    d = np.abs(np.diff(frames, axis=0)).mean(axis=1)
    return {
        "frames": int(n),
        "motion": round(float(np.median(d)), 3),
        "motion_p90": round(float(np.percentile(d, 90)), 3),
        "still_frac": round(float((d < STILL_THRESHOLD).mean()), 3),
    }


def is_static(stats: dict | None, max_still_frac: float = 0.40,
              min_motion: float = 1.5) -> bool:
    """A menu, a frozen screen, an AFK shot — anything not worth clipping."""
    if stats is None:
        return False  # can't tell; don't throw the candidate away
    return (stats["still_frac"] > max_still_frac
            or stats["motion"] < min_motion)
