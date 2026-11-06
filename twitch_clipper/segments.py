"""Build a longer clip out of several pieces, dropping the dead air between.

A 20-second window straight off the peak is too short to carry a story: it
opens mid-event and ends before the reaction. Widening it to a minute drags
in the boring parts. So take the wide window, keep only the stretches worth
watching, and cut them together.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np


def plan_segments(curve: np.ndarray, hop: float, window_start: float,
                  window_end: float, *, target: float = 35.0,
                  min_segment: float = 3.5, merge_gap: float = 1.2,
                  max_segments: int = 5,
                  quiet: np.ndarray | None = None) -> list[tuple[float, float]]:
    """Pick the stretches of [window_start, window_end] worth keeping.

    Returns absolute (start, end) pairs in VOD time, in order, totalling
    roughly `target` seconds. Falls back to the whole window when it cannot
    do better than that.
    """
    a, b = int(window_start / hop), int(window_end / hop)
    a, b = max(0, a), min(curve.size, b)
    if b - a < 4:
        return [(window_start, window_end)]
    seg = curve[a:b]
    span = (b - a) * hop
    if span <= target * 1.1:
        return [(window_start, window_end)]

    # Choose the threshold that keeps closest to `target` seconds.
    #
    # A quantile does not work here: the curve is bimodal (a flat baseline
    # plus bursts), so the quantile lands exactly on the baseline value and
    # `seg >= thresh` then matches every frame, silently keeping the whole
    # window. Walk the distinct values downwards instead and take the one
    # whose kept duration is nearest the target without overshooting it.
    thresh = None
    best_err = float("inf")
    for t in np.unique(seg)[::-1]:
        kept_sec = float((seg >= t).sum()) * hop
        if kept_sec > target * 1.25:
            break
        err = abs(kept_sec - target)
        if err < best_err:
            thresh, best_err = float(t), err
    if thresh is None:
        return [(window_start, window_end)]

    above = seg >= thresh
    runs: list[list[int]] = []
    start = None
    for i, on in enumerate(above):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(above)])
    if not runs:
        return [(window_start, window_end)]

    # bridge short gaps: a half-second lull inside an event is not a cut
    gap_frames = max(1, int(merge_gap / hop))
    merged: list[list[int]] = [runs[0]]
    for r in runs[1:]:
        if r[0] - merged[-1][1] <= gap_frames:
            merged[-1][1] = r[1]
        else:
            merged.append(r)

    min_frames = max(2, int(min_segment / hop))
    kept = [r for r in merged if r[1] - r[0] >= min_frames]
    if not kept:
        kept = [max(merged, key=lambda r: r[1] - r[0])]

    # if there are too many pieces, keep the strongest ones
    if len(kept) > max_segments:
        kept = sorted(kept, key=lambda r: -float(seg[r[0]:r[1]].mean()))[:max_segments]
        kept.sort(key=lambda r: r[0])

    # trim back to target, weakest piece first, never below one segment
    def total(rs):
        return sum((r[1] - r[0]) * hop for r in rs)

    while total(kept) > target * 1.25 and len(kept) > 1:
        weakest = min(kept, key=lambda r: float(seg[r[0]:r[1]].mean()))
        kept.remove(weakest)

    out: list[tuple[float, float]] = []
    for r in kept:
        s = window_start + r[0] * hop
        e = window_start + r[1] * hop
        if quiet is not None:
            s = _snap(s, quiet, hop, 0.8, window_start, e - min_segment)
            e = _snap(e, quiet, hop, 0.8, s + min_segment, window_end)
        out.append((round(s, 2), round(e, 2)))
    return out or [(window_start, window_end)]


def _snap(t: float, quiet: np.ndarray, hop: float, radius: float,
          lo: float, hi: float) -> float:
    if hi <= lo:
        return float(np.clip(t, min(lo, hi), max(lo, hi)))
    i = int(round(t / hop)); r = int(round(radius / hop))
    x, y = max(0, i - r), min(quiet.size, i + r + 1)
    if y <= x:
        return float(np.clip(t, lo, hi))
    best = x + int(np.argmax(quiet[x:y]))
    return float(np.clip(best * hop, lo, hi))


def remap_words(words: list[dict], segments: list[tuple[float, float]],
                window_start: float) -> list[dict]:
    """Move caption timings onto the cut timeline, dropping excised words."""
    out: list[dict] = []
    offset = 0.0
    for s, e in segments:
        rel_s, rel_e = s - window_start, e - window_start
        for w in words:
            if w["start"] >= rel_s and w["end"] <= rel_e:
                out.append({
                    "word": w["word"],
                    "start": round(offset + (w["start"] - rel_s), 3),
                    "end": round(offset + (w["end"] - rel_s), 3),
                })
        offset += rel_e - rel_s
    out.sort(key=lambda w: w["start"])
    return out


def build_cut(src: Path, dest: Path, segments: list[tuple[float, float]],
              window_start: float, *, quiet: bool = True) -> Path:
    """Concatenate the kept segments of `src` into one file.

    Each segment is extracted on its own and the parts are joined with the
    concat demuxer. The obvious one-pass alternative - a filter_complex of
    parallel trim/atrim branches feeding concat - runs the machine out of
    memory and gets the process killed: `trim` does not seek, so every
    branch decodes the whole input, and concat needs the first branch's
    frames while the later ones buffer theirs. On an 85s 1080p60 source with
    five segments that is gigabytes of buffered video, and ffmpeg dies with
    no message on stderr. Seeking per segment keeps it to one decode each.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(segments) <= 1:
        return src

    tmp = dest.parent / f".{dest.stem}_parts"
    tmp.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for i, (s_, e_) in enumerate(segments):
            rs = max(0.0, s_ - window_start)
            dur = max(0.05, e_ - s_)
            part = tmp / f"{i:02d}.mp4"
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
                   "-ss", f"{rs:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                   "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                   "-avoid_negative_ts", "make_zero", str(part)]
            proc = subprocess.run(cmd, capture_output=quiet, text=True)
            if proc.returncode != 0 or not part.exists():
                raise RuntimeError(
                    f"segment {i} ({rs:.1f}s +{dur:.1f}s) failed "
                    f"(exit {proc.returncode}):\n{(proc.stderr or '')[-600:]}")
            parts.append(part)

        listing = tmp / "parts.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
               "-f", "concat", "-safe", "0", "-i", str(listing),
               "-c", "copy", "-movflags", "+faststart", str(dest)]
        proc = subprocess.run(cmd, capture_output=quiet, text=True,
                              cwd=str(tmp))
        if proc.returncode != 0 or not dest.exists():
            raise RuntimeError(
                f"joining {len(parts)} segments failed "
                f"(exit {proc.returncode}):\n{(proc.stderr or '')[-600:]}")
    finally:
        for f in tmp.glob("*"):
            f.unlink(missing_ok=True)
        tmp.rmdir()
    return dest
