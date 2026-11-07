"""Mask strong language in burnt-in text.

Captions are burnt in, so a slur or an f-bomb on screen is permanent and
will cost reach on every platform that reads frames. Masking happens at
render time, not in the transcript: the words stay intact in moments.json
for titling and searching, and only the drawn text changes.
"""
from __future__ import annotations

import re

# Stems, matched with any suffix (fucking, fucked, bitches...). Order does
# not matter; the longest match at a position wins.
STEMS = [
    "motherfuck", "clusterfuck", "fuck", "shit", "bitch", "cunt", "bollock",
    "bastard", "wank", "twat", "prick", "slut", "whore", "dickhead",
    "arsehole", "asshole", "nigg", "fag", "retard", "pussy", "cock",
]
# Whole words only, so "class" and "scunthorpe" survive.
EXACT = ["ass", "arse", "damn", "goddamn", "piss", "crap", "dick", "hell"]
MILD = {"damn", "crap", "hell"}

_STEM_RE = re.compile(r"\b(" + "|".join(sorted(STEMS, key=len, reverse=True))
                      + r")(\w*)", re.IGNORECASE)
_EXACT_RE = re.compile(r"\b(" + "|".join(sorted(EXACT, key=len, reverse=True))
                       + r")\b", re.IGNORECASE)


def _mask(word: str) -> str:
    """Keep the first letter, hash the rest: fuck -> F###."""
    if len(word) <= 1:
        return word
    return word[0] + "#" * (len(word) - 1)


def censor(text: str, *, include_mild: bool = False) -> str:
    """Mask strong language, preserving length and the leading letter."""
    if not text:
        return text

    def stem_sub(m: re.Match) -> str:
        return _mask(m.group(0))

    def exact_sub(m: re.Match) -> str:
        word = m.group(0)
        if not include_mild and word.lower() in MILD:
            return word
        return _mask(word)

    return _EXACT_RE.sub(exact_sub, _STEM_RE.sub(stem_sub, text))


def censor_words(words: list[dict], *, include_mild: bool = False) -> list[dict]:
    """Copy of the caption words with their text masked."""
    out = []
    for w in words:
        c = dict(w)
        c["word"] = censor(w.get("word", ""), include_mild=include_mild)
        out.append(c)
    return out
