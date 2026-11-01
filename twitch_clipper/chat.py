"""Turn chat replay into a hype signal.

Two things matter beyond raw message rate: how many *different* people reacted
(one spammer is not a moment), and whether they used the vocabulary people
reach for when something actually happened.
"""
from __future__ import annotations

import re
import numpy as np

# Weighted cues. Values are multipliers on a message's hype contribution.
#
# Matching rules matter here. Short tokens like "w" must match as whole words
# only, or "how"/"wow" score as hype. Longer terms match as prefixes so that
# "lmaooo", "kekwww" and "omegalulw" all count.

# Matched as whole words only.
TOKEN_TERMS: dict[str, float] = {
    "w": 0.5, "ez": 0.7, "gg": 0.8, "rip": 0.7, "sus": 0.6, "huh": 0.5,
    "pog": 1.2, "lol": 0.5, "dub": 0.9, "nani": 1.0, "bruh": 0.9, "wtf": 1.3,
    "omg": 1.2, "holy": 1.3, "jaw": 0.8, "dying": 0.8, "crying": 0.9,
    "throw": 0.9, "thrown": 0.9, "choke": 1.1, "choked": 1.2, "clean": 0.9,
    "nasty": 1.1, "insane": 1.4, "actual": 0.8, "actually": 0.6,
    "unlucky": 0.7, "scuffed": 0.8, "griefing": 0.8, "sheesh": 1.1,
}

# Matched as prefixes (must be >= 4 chars to stay unambiguous).
PREFIX_TERMS: dict[str, float] = {
    "lmfao": 1.2, "lmao": 1.0, "omegalul": 1.6, "kekw": 1.5, "pepelaugh": 1.3,
    "poggers": 1.3, "pogchamp": 1.3, "monkas": 1.1, "copium": 1.0, "sadge": 0.8,
    "cracked": 1.3, "goated": 1.3, "gigachad": 1.2, "clipped": 2.2,
    "clipper": 2.0, "clipping": 2.2, "deadass": 0.6,
}

# Phrases, matched anywhere in the message.
PHRASE_TERMS: dict[str, float] = {
    "clip it": 2.6, "clipit": 2.6, "someone clip": 2.8, "mods clip": 2.8,
    "no way": 1.5, "noway": 1.5, "wait what": 1.4, "lets go": 1.4,
    "letsgo": 1.4, "i cant": 1.2, "icant": 1.2,
}

# "clip" alone is the single strongest cue, but only as a bare word.
CLIP_TOKEN_WEIGHT = 2.2

EMOJI_HYPE = "\U0001F62D\U0001F480\U0001F923\U0001F602\U0001F525\U0001F631\u203C\uFE0F\U0001F92F\U0001F440\U0001F633"

_WORD = re.compile(r"[a-z']+")
_CAPS = re.compile(r"\b[A-Z]{3,}\b")
_LAUGH = re.compile(r"(?:ha){2,}|(?:ah){2,}|(?:je){2,}|(?:hi){3,}")


def message_hype(text: str, emotes: list[str]) -> float:
    """0 for ordinary chatter, higher the more reactive the message is."""
    low = text.lower()
    score = 0.0

    for phrase, weight in PHRASE_TERMS.items():
        if phrase in low:
            score += weight

    tokens = _WORD.findall(low)
    for tok in tokens:
        if tok == "clip":
            score += CLIP_TOKEN_WEIGHT
            continue
        if tok in TOKEN_TERMS:
            score += TOKEN_TERMS[tok]
            continue
        for term, weight in PREFIX_TERMS.items():
            if tok.startswith(term):
                score += weight
                break

    if _LAUGH.search(low):
        score += 0.9
    score += 0.5 * sum(low.count(ch) for ch in EMOJI_HYPE)
    score += 0.4 * len(emotes)
    if _CAPS.search(text):
        score += 0.5
    if "?!" in text or "!!" in text:
        score += 0.4
    return float(min(score, 8.0))


def build_signal(comments: list[dict], duration: float, hop: float,
                 *, window: float = 8.0, unique_damp: float = 0.5,
                 hype_weight: float = 1.4) -> tuple[np.ndarray, dict]:
    """Return a per-frame chat hype array plus some stats about the chat."""
    n = max(1, int(np.ceil(duration / hop)))
    counts = np.zeros(n, dtype=np.float32)
    hype = np.zeros(n, dtype=np.float32)
    # unique-chatter tracking per coarse 1s bucket keeps memory bounded
    buckets: dict[int, set[str]] = {}

    for c in comments:
        t = c.get("t", 0.0)
        if t < 0 or t >= duration:
            continue
        i = min(n - 1, int(t / hop))
        counts[i] += 1.0
        hype[i] += message_hype(c.get("text", ""), c.get("emotes") or [])
        buckets.setdefault(int(t), set()).add(c.get("user", "anon"))

    win = max(1, int(round(window / hop)))
    kernel = np.ones(win, dtype=np.float32)
    pad = win // 2

    def pooled(x: np.ndarray) -> np.ndarray:
        return np.convolve(np.pad(x, (pad, pad), mode="constant"), kernel,
                           mode="same")[pad:pad + n]

    msg_rate = pooled(counts)
    hype_rate = pooled(hype)

    # unique chatters pooled over the same window, on the 1s grid
    secs = max(1, int(np.ceil(duration)))
    uniq_per_sec = np.zeros(secs, dtype=np.float32)
    for s, users in buckets.items():
        if 0 <= s < secs:
            uniq_per_sec[s] = len(users)
    uw = max(1, int(round(window)))
    upad = uw // 2
    uniq_pooled = np.convolve(np.pad(uniq_per_sec, (upad, upad), mode="constant"),
                              np.ones(uw, dtype=np.float32), mode="same")[upad:upad + secs]
    frame_secs = np.clip((np.arange(n) * hop).astype(int), 0, secs - 1)
    uniq_rate = uniq_pooled[frame_secs]

    # damp bursts that come from very few people
    denom = np.maximum(msg_rate, 1.0)
    diversity = np.clip(uniq_rate / denom, 0.0, 1.0)
    damp = (1.0 - unique_damp) + unique_damp * np.sqrt(diversity)

    from .audio import robust_norm, smooth
    raw = (robust_norm(msg_rate) + hype_weight * robust_norm(hype_rate)) / (1.0 + hype_weight)
    signal = smooth(raw * damp, max(1, int(round(2.0 / hop))))

    total = len(comments)
    stats = {
        "messages": total,
        "chatters": len({c.get("user") for c in comments}),
        "msgs_per_min": (total / (duration / 60.0)) if duration > 0 else 0.0,
        "peak_msgs_per_window": float(msg_rate.max()) if n else 0.0,
        "window_seconds": window,
    }
    return robust_norm(signal), stats
