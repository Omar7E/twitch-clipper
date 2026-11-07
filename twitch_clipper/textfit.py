"""Measure and wrap text the way it will actually render.

Wrapping by character count is wrong: character width varies, so a 24-char
line in a bold face at 80px can be 1023px wide and run off a 1080px frame.
These helpers measure the real font and wrap to a pixel budget.
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from .config import FONT_DIR


@lru_cache(maxsize=16)
def font_path(family: str, bold: bool, file_name: str = "") -> str | None:
    """Locate a font file so its text can be measured."""
    if file_name:
        cand = FONT_DIR / file_name
        if cand.exists():
            return str(cand)
    query = f"{family}:bold" if bold else family
    out = subprocess.run(["fc-match", "-f", "%{file}", query],
                         capture_output=True, text=True)
    p = Path(out.stdout.strip()) if out.returncode == 0 else None
    return str(p) if p and p.exists() else None


@lru_cache(maxsize=4096)
def _text_width(path: str, size: int, text: str) -> float:
    from PIL import ImageFont
    return float(ImageFont.truetype(path, size).getlength(text))


def measure(text: str, family: str, size: int, *, bold: bool = True,
            file_name: str = "") -> tuple[float, float]:
    """(width, line height) in pixels, with a rough fallback."""
    path = font_path(family, bold, file_name)
    if path is None:
        return len(text) * size * 0.55, size * 1.2
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(path, size)
        asc, desc = font.getmetrics()
        return _text_width(path, size, text), float(asc + desc)
    except Exception:
        return len(text) * size * 0.55, size * 1.2


def wrap_to_width(text: str, family: str, size: int, max_px: float, *,
                  bold: bool = True, file_name: str = "",
                  max_lines: int = 3) -> list[str]:
    """Break text into lines that each fit inside `max_px`."""
    words = text.split()
    if not words:
        return [""]
    path = font_path(family, bold, file_name)

    def width(s: str) -> float:
        if path is None:
            return len(s) * size * 0.55
        return _text_width(path, size, s)

    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and width(trial) > max_px:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)

    # too many lines means the text is too long for the budget: shrink by
    # merging is not possible, so let the caller decide via the line count
    return lines[:max_lines] if len(lines) > max_lines else lines
