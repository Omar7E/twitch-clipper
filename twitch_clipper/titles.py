"""Write the on-screen "what happened" line for each moment.

Default is a heuristic built from the transcript and chat around the peak. If
ANTHROPIC_API_KEY is set, Claude writes a punchier one instead.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter

from .chat import message_hype

_FILLER = {
    "the", "a", "an", "and", "or", "but", "so", "like", "just", "really", "very",
    "i", "im", "you", "he", "she", "it", "we", "they", "is", "was", "are", "be",
    "to", "of", "in", "on", "at", "that", "this", "my", "me", "uh", "um", "yeah",
    "okay", "ok", "oh", "gonna", "wanna", "kinda", "know", "what", "its",
}


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" .,-—")
    return text


def chat_reaction(comments: list[dict], start: float, end: float) -> str:
    """The dominant short reaction in chat during the moment, if there is one."""
    window = [c for c in comments if start <= c.get("t", -1) <= end]
    if not window:
        return ""
    counter: Counter[str] = Counter()
    for c in window:
        txt = (c.get("text") or "").strip()
        if not txt or len(txt) > 24:
            continue
        key = re.sub(r"[^a-z]", "", txt.lower())
        if len(key) < 2:
            continue
        counter[txt.upper() if len(txt) <= 12 else txt] += 1
    if not counter:
        return ""
    top, n = counter.most_common(1)[0]
    return top if n >= max(3, len(window) // 8) else ""


def heuristic_title(words: list[dict], comments: list[dict],
                    start: float, end: float, *, max_chars: int = 34,
                    max_words: int = 6) -> str:
    """Pick the punchiest line of speech, else fall back to chat's reaction."""
    if words:
        # score candidate sentences by information density
        text = " ".join(w["word"] for w in words)
        sentences = [s for s in re.split(r"(?<=[.!?])\s+|\s{2,}", text) if s.strip()]
        if not sentences:
            sentences = [text]
        best, best_score = "", -1.0
        for s in sentences:
            toks = re.findall(r"[A-Za-z']+", s.lower())
            if not toks:
                continue
            content = [t for t in toks if t not in _FILLER and len(t) > 2]
            if not content:
                continue
            density = len(content) / max(len(toks), 1)
            # prefer 3..10 content words, penalise very long rambles
            length_fit = 1.0 - abs(min(len(content), 14) - 6) / 14.0
            score = density * 0.6 + length_fit * 0.4
            if score > best_score:
                best, best_score = s, score
        cand = _clean(best)
        if cand:
            toks = cand.split()
            if len(toks) > max_words:
                cand = " ".join(toks[:max_words])
            if len(cand) > max_chars:
                cut = cand[:max_chars].rsplit(" ", 1)[0]
                cand = cut or cand[:max_chars]
            return cand.rstrip(" ,.-").upper()

    reaction = chat_reaction(comments, start, end)
    if reaction:
        return f"CHAT: {reaction}"[:max_chars].upper()
    return ""


TITLE_BRIEF = """You write the on-screen caption for Twitch highlight clips.

The caption says WHAT HAPPENS, in the third person, like a narrator telling
someone what they are about to watch. It is NOT a quote of what the streamer
said.

Good: {streamer} ACCIDENTALLY KILLS HIS OWN TEAMMATE
Good: CHAT SPAMS L AFTER THIS WHIFF
Good: {streamer} CLUTCHES 1V3 ON 5 HP
Bad:  "I'M REALLY NOT I'M ACTUALLY 55 HP"   (a quote, not a description)
Bad:  FUNNY MOMENT                          (says nothing)

Rules:
- describe the event, third person, present tense
- 3 to 7 words, UPPERCASE
- no emoji, no quotes, no hashtags, no trailing punctuation
- use the streamer's name where it reads naturally
- if the clip is unclear, describe the reaction instead of inventing an event

You get a transcript of what was said and the loudest chat messages. Chat is
often the best clue to what actually happened on screen.
"""


def _claude_titles(items: list[dict], model: str, streamer: str) -> list[str] | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}. said: {it['transcript'][:600] or '(no speech)'}\n"
            f"   chat: {it['chat'][:300] or '(no chat)'}"
        )
    prompt = (
        TITLE_BRIEF.replace("{streamer}", (streamer or "the streamer").upper())
        + "\nWrite one caption per numbered moment. Reply with ONLY a JSON "
          "array of strings, in order.\n\n" + "\n".join(lines)
    )
    body = json.dumps({
        "model": model,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read().decode())
        text = "".join(b.get("text", "") for b in payload.get("content", []))
        match = re.search(r"\[.*\]", text, re.S)
        if not match:
            return None
        titles = json.loads(match.group(0))
        if not isinstance(titles, list):
            return None
        return [str(t).strip().upper()[:60] for t in titles]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return None


def make_titles(moments: list[dict], comments: list[dict], *,
                use_claude: bool = True, streamer: str = "",
                model: str = "claude-sonnet-5-5") -> tuple[list[str], str]:
    """Return (titles, source) where source is 'claude' or 'heuristic'."""
    items = []
    for m in moments:
        words = m.get("words") or []
        transcript = " ".join(w["word"] for w in words)
        window = [c for c in comments if m["start"] <= c.get("t", -1) <= m["end"]]
        window.sort(key=lambda c: -message_hype(c.get("text", ""), c.get("emotes") or []))
        chat = " | ".join((c.get("text") or "")[:40] for c in window[:12])
        items.append({"transcript": transcript, "chat": chat})

    if use_claude and items:
        got = _claude_titles(items, model, streamer)
        if got and len(got) >= len(items):
            return got[:len(items)], "claude"

    return [
        heuristic_title(m.get("words") or [], comments, m["start"], m["end"])
        for m in moments
    ], "heuristic"
