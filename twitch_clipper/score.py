"""Blend the signals and choose the clip windows."""
from __future__ import annotations

import numpy as np

from .audio import AudioFeatures, excitement, robust_norm, smooth
from .config import ScoreConfig


def blend(feat: AudioFeatures, chat_signal: np.ndarray | None, chat_stats: dict | None,
          cfg: ScoreConfig) -> tuple[np.ndarray, np.ndarray, dict]:
    """Combine audio excitement and chat hype.

    Returns (pick_curve, report_curve, info). `pick_curve` is percentile-clipped
    so one freak spike cannot flatten everything else during peak-finding;
    `report_curve` is scaled against the true maximum so the scores we show
    actually discriminate between the moments we picked.
    """
    audio = excitement(
        feat,
        baseline_seconds=cfg.baseline_seconds,
        loud_weight=cfg.loud_weight,
        hf_weight=cfg.hf_weight,
        dyn_weight=cfg.dyn_weight,
    )
    n = audio.size
    info: dict = {"audio_frames": n}

    usable_chat = (
        chat_signal is not None
        and chat_stats is not None
        and chat_stats.get("msgs_per_min", 0.0) >= cfg.chat_min_rate
        and chat_stats.get("chatters", 0) >= 2
    )
    info["chat_used"] = bool(usable_chat)

    if usable_chat:
        cs = np.asarray(chat_signal, dtype=np.float32)
        if cs.size != n:  # align grids
            cs = np.interp(np.arange(n), np.linspace(0, n - 1, cs.size), cs).astype(np.float32)
        wc, wa = cfg.chat_weight, cfg.audio_weight
        combined = (wc * cs + wa * audio) / max(wc + wa, 1e-9)
        info["chat_weight"] = wc
        info["audio_weight"] = wa
    else:
        combined = audio
        info["chat_weight"] = 0.0
        info["audio_weight"] = 1.0
        if chat_stats is not None:
            info["chat_skip_reason"] = (
                f"only {chat_stats.get('messages', 0)} messages "
                f"({chat_stats.get('msgs_per_min', 0):.1f}/min) from "
                f"{chat_stats.get('chatters', 0)} chatters — using audio only"
            )

    curve = smooth(combined, max(1, int(round(cfg.smooth_seconds / feat.hop))))
    peak = float(curve.max()) if curve.size else 0.0
    report = (curve / peak).astype(np.float32) if peak > 1e-9 else curve
    return robust_norm(curve), report, info


def _snap(t: float, quiet: np.ndarray, hop: float, radius: float,
          lo: float, hi: float) -> float:
    """Nudge a cut point to the quietest frame nearby, staying inside [lo, hi]."""
    n = quiet.size
    i = int(round(t / hop))
    r = int(round(radius / hop))
    a, b = max(0, i - r), min(n, i + r + 1)
    if b <= a:
        return float(np.clip(t, lo, hi))
    local = quiet[a:b]
    best = a + int(np.argmax(local))
    return float(np.clip(best * hop, lo, hi))


def pick_moments(curve: np.ndarray, feat: AudioFeatures, cfg: ScoreConfig,
                 duration: float, quiet: np.ndarray | None = None,
                 report: np.ndarray | None = None) -> list[dict]:
    """Greedy non-overlapping peak selection, best first.

    Ranking blends the peak with the window mean, so a moment that sustains
    its energy beats a single spike surrounded by dead air.
    """
    if report is None:
        report = curve
    hop = feat.hop
    n = curve.size
    if n == 0:
        return []

    head = int(cfg.ignore_head / hop)
    tail = max(0, n - int(cfg.ignore_tail / hop))
    searchable = curve.copy()
    if head > 0:
        searchable[:min(head, n)] = -1.0
    if tail < n:
        searchable[tail:] = -1.0

    # How much each point stands out from the minutes around it. The curve is
    # percentile-normalised, so every VOD spans 0..1 and an absolute threshold
    # cannot tell a dull stream from a great one. Prominence can: a real moment
    # towers over its own neighbourhood, noise does not.
    from .audio import rolling_median
    base_win = max(3, int(cfg.baseline_seconds / max(hop, 1e-6)))
    prominence = curve - rolling_median(curve, base_win)
    valid_prom = prominence[searchable >= 0]
    noise = float(np.median(np.abs(valid_prom - np.median(valid_prom)))) * 1.4826
    gate = (np.median(valid_prom) + cfg.select_k * noise) if (
        cfg.adaptive and valid_prom.size and noise > 1e-9) else -1e9

    order = np.argsort(-searchable)
    chosen: list[dict] = []
    taken: list[tuple[float, float]] = []
    rejected = 0

    for idx in order:
        if searchable[idx] < 0:
            break
        if prominence[idx] < gate and len(chosen) >= cfg.min_clips:
            # ordered by score, so nothing after this clears the bar either
            break
        peak_t = float(idx) * hop
        start = peak_t - cfg.pre_roll
        end = peak_t + cfg.post_roll

        if quiet is not None:
            start = _snap(start, quiet, hop, cfg.snap_window,
                          max(0.0, peak_t - cfg.max_len + 4.0), peak_t - 3.0)
            end = _snap(end, quiet, hop, cfg.snap_window,
                        peak_t + 3.0, min(duration, peak_t + cfg.max_len - 4.0))

        start = max(0.0, start)
        end = min(duration, end)
        if end - start < cfg.min_len:
            # widen symmetrically to reach the minimum length
            need = cfg.min_len - (end - start)
            start = max(0.0, start - need / 2)
            end = min(duration, start + cfg.min_len)
        if end - start > cfg.max_len:
            end = start + cfg.max_len
        if end - start < min(cfg.min_len * 0.6, 8.0):
            continue

        if any(not (end + cfg.min_gap <= s or start - cfg.min_gap >= e)
               for s, e in taken):
            continue

        a, b = int(start / hop), max(int(start / hop) + 1, int(end / hop))
        seg = report[a:min(b, n)]
        peak_score = float(report[idx])
        mean_score = float(seg.mean()) if seg.size else 0.0
        chosen.append({
            "rank": 0,
            "prominence": round(float(prominence[idx]), 4),
            "peak": round(peak_t, 2),
            "start": round(start, 2),
            "end": round(end, 2),
            "duration": round(end - start, 2),
            "score": round(0.65 * peak_score + 0.35 * mean_score, 4),
            "peak_score": round(peak_score, 4),
            "mean_score": round(mean_score, 4),
        })
        taken.append((start, end))
        if len(chosen) >= cfg.count + (4 if cfg.adaptive else 0):
            break

    chosen.sort(key=lambda m: -m["score"])

    if cfg.adaptive and len(chosen) > max(cfg.min_clips, 1):
        # cut on prominence, not score: it separates real moments from the
        # tail far more sharply (0.74 vs 0.39 on real data, where the same
        # boundary in score is only 0.57 vs 0.32)
        chosen = chosen[:_knee(
            [m["prominence"] for m in chosen],
            lo=max(cfg.min_clips, 1), hi=cfg.count)]
    else:
        chosen = chosen[:cfg.count]

    for i, m in enumerate(chosen, 1):
        m["rank"] = i
        m["gate"] = round(float(gate), 4)
    return chosen


def _knee(scores: list[float], *, lo: int, hi: int, min_drop: float = 0.12) -> int:
    """Where does quality fall off a cliff?

    Given scores sorted best-first, find the biggest gap between neighbours and
    cut there. A stream with three standout moments and then a cliff yields
    three clips; one that decays gently yields more. If no gap is meaningful,
    keep everything up to the cap.
    """
    n = len(scores)
    hi = min(hi, n)
    if n <= lo:
        return n
    best_k, best_gap = hi, 0.0
    for k in range(lo, hi):
        gap = scores[k - 1] - scores[k]
        if gap > best_gap:
            best_k, best_gap = k, gap
    return best_k if best_gap >= min_drop else hi
