"""Build the ASS overlay: word-popping captions, the title card, follow banner.

libass renders this in one pass inside the ffmpeg graph, which is why the
captions can animate per word without us touching individual frames.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

from .censor import censor, censor_words
from .config import RenderConfig
from .transcribe import group_lines


def ass_color(hex_rgb: str, alpha: float = 0.0) -> str:
    """#RRGGBB -> &HAABBGGRR (ASS is BGR, alpha 00 = opaque)."""
    s = hex_rgb.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"bad colour {hex_rgb!r}, expected #RRGGBB")
    r, g, b = s[0:2], s[2:4], s[4:6]
    a = f"{int(round(max(0.0, min(1.0, alpha)) * 255)):02X}"
    return f"&H{a}{b}{g}{r}".upper()


def ass_tag_color(hex_rgb: str) -> str:
    """#RRGGBB -> &HBBGGRR& for inline override tags (no alpha, closing &)."""
    s = hex_rgb.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"bad colour {hex_rgb!r}, expected #RRGGBB")
    return f"&H{s[4:6]}{s[2:4]}{s[0:2]}&".upper()


def ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
            .replace("\n", "\\N"))


@dataclass
class Geometry:
    width: int
    height: int
    clip_top: int
    clip_height: int
    game_top: int
    game_height: int

    @property
    def clip_bottom(self) -> int:
        return self.clip_top + self.clip_height


def geometry(cfg: RenderConfig, with_gameplay: bool = True) -> Geometry:
    w, h = cfg.width, cfg.height
    if not with_gameplay:
        # no filler: the clip owns the whole canvas, so there is no seam
        return Geometry(w, h, clip_top=0, clip_height=h, game_top=0, game_height=0)
    clip_h = int(round(h * cfg.clip_frac)) // 2 * 2
    clip_h = max(2, min(h - 2, clip_h))
    game_h = h - clip_h
    if cfg.layout == "gameplay-top":
        return Geometry(w, h, clip_top=game_h, clip_height=clip_h,
                        game_top=0, game_height=game_h)
    if cfg.layout == "gameplay-bottom":
        return Geometry(w, h, clip_top=0, clip_height=clip_h,
                        game_top=clip_h, game_height=game_h)
    raise ValueError(f"unknown layout {cfg.layout!r} "
                     "(expected gameplay-top or gameplay-bottom)")


def _styles(cfg: RenderConfig) -> list[str]:
    white = ass_color("#FFFFFF")
    black = ass_color("#000000")
    title_bg = ass_color(cfg.title_bg, 1.0 - cfg.title_opacity)
    follow_bg = ass_color(cfg.follow_bg, 0.08)
    fields = ("Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
              "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,"
              "Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
              "MarginV,Encoding")
    cap_border = 3 if cfg.caption_style == "boxed" else 1
    cap_outline = 10 if cfg.caption_style == "boxed" else 7
    rows = [
        f"Format: {fields}",
        # captions
        f"Style: Cap,{cfg.font},{cfg.caption_size},{white},{white},"
        f"{black},{ass_color('#000000', 0.45)},0,0,0,0,100,100,1.5,0,"
        f"{cap_border},{cap_outline},0,5,40,40,0,1",
        # title card
        # white with a heavy black stroke and no background box
        f"Style: Title,{cfg.title_font},{cfg.title_size},{white},{white},"
        f"{black},{black},{1 if cfg.title_bold else 0},0,0,0,100,100,0,0,"
        f"1,{cfg.title_outline},0,5,50,50,0,1",
        # follow banner
        f"Style: Follow,{cfg.font},{cfg.follow_size},{white},{white},"
        f"{follow_bg},{follow_bg},0,0,0,0,100,100,2,0,3,14,0,5,40,40,0,1",
    ]
    return rows


def _caption_events(words: list[dict], cfg: RenderConfig, geo: Geometry,
                    duration: float) -> list[str]:
    if not words:
        return []
    accent = ass_tag_color(cfg.caption_accent)
    white = ass_tag_color("#FFFFFF")
    black = ass_tag_color("#000000")
    dark = ass_tag_color("#101010")
    y = geo.clip_top + int(round(geo.clip_height * cfg.caption_y_frac))
    lo = geo.clip_top + cfg.caption_size // 2
    hi = geo.clip_bottom - cfg.caption_size // 2
    if cfg.follow_position == "seam":
        # the badge straddles the seam; keep captions clear of it
        badge = int(cfg.follow_size * 0.9) + cfg.caption_size // 2
        if geo.game_height == 0:
            lo = max(lo, int(round(geo.height * 0.72)) + badge)
        elif cfg.layout == "gameplay-bottom":
            hi = min(hi, geo.clip_bottom - badge)
        else:
            lo = max(lo, geo.clip_top + badge)
    y = int(max(lo, min(hi, y))) if hi > lo else int((lo + hi) // 2)
    pos = f"\\an5\\pos({geo.width // 2},{y})"

    lines = [ln for ln in group_lines(words, per_line=cfg.words_per_line) if ln]
    events: list[str] = []
    for li, line in enumerate(lines):
        # a line must be gone before the next one appears, or libass draws
        # both at the same position
        next_start = lines[li + 1][0]["start"] if li + 1 < len(lines) else duration
        for k, active in enumerate(line):
            start = active["start"]
            if k + 1 < len(line):
                end = max(line[k + 1]["start"], start + 0.08)
            else:
                end = max(active["end"] + 0.32, start + 0.2)
            end = min(end, duration, next_start)
            if end <= start:
                continue
            parts = []
            for j, w in enumerate(line):
                token = esc(w["word"])
                if j != k:
                    parts.append(token)
                elif cfg.caption_style == "highlight":
                    parts.append(f"{{\\c{accent}}}{token}{{\\c{white}}}")
                elif cfg.caption_style == "boxed":
                    parts.append(f"{{\\3c{accent}\\c{dark}}}"
                                 f"{token}{{\\3c{black}\\c{white}}}")
                else:  # pop
                    parts.append(
                        f"{{\\c{accent}\\fscx116\\fscy116"
                        f"\\t(0,110,\\fscx100\\fscy100)}}{token}"
                        f"{{\\c{white}\\fscx100\\fscy100}}")
            fade = "\\fad(70,0)" if k == 0 else ""
            events.append(
                f"Dialogue: 0,{ts(start)},{ts(end)},Cap,,0,0,0,,"
                f"{{{pos}{fade}}}" + " ".join(parts))
    return events


def _title_events(title: str, cfg: RenderConfig, geo: Geometry,
                  duration: float) -> list[str]:
    if not title.strip():
        return []
    lines = textwrap.wrap(title.strip(), width=cfg.title_wrap) or [title.strip()]
    wrapped = "\\N".join(esc(ln) for ln in lines)
    y = int(round(geo.height * cfg.title_y_frac))
    # keep it clear of the top edge whatever the fraction says
    y = max(y, int(cfg.title_size * 0.9) + 24)
    hold = duration if cfg.title_hold else min(duration, max(3.0, duration * 0.45))
    # no fade out: the headline stays up for the whole clip
    return [
        f"Dialogue: 1,{ts(0.0)},{ts(hold)},Title,,0,0,0,,"
        f"{{\\an5\\pos({geo.width // 2},{y})}}{wrapped}"
    ]


def _follow_events(cfg: RenderConfig, geo: Geometry, duration: float) -> list[str]:
    """The follow call-out. Placed on the seam by default: that is the optical
    centre of the frame and the one place a viewer's eye always passes."""
    text = cfg.follow_text.strip()
    if not text:
        return []

    if cfg.follow_position == "bottom":
        y = geo.height - int(cfg.follow_size * 0.9)
    elif cfg.follow_position == "top":
        y = int(cfg.follow_size * 1.0) + 24
    elif cfg.follow_position == "seam":
        if geo.game_height == 0:
            # full-frame: no seam exists, so sit below the footage and above
            # the captions, still well clear of the bottom edge
            y = int(round(geo.height * 0.72))
        else:
            y = geo.clip_bottom if cfg.layout == "gameplay-bottom" else geo.clip_top
        y = max(cfg.follow_size, min(geo.height - cfg.follow_size, y))
    else:
        raise ValueError(f"unknown follow_position {cfg.follow_position!r} "
                         "(expected seam, top or bottom)")

    tags = [f"\\an5\\pos({geo.width // 2},{y})"]
    if abs(cfg.follow_rotation) > 0.01:
        tags.append(f"\\frz{cfg.follow_rotation:g}")
    if cfg.follow_pulse:
        # breathe between 100% and 106% so it reads as alive, not flashing
        half = max(0.6, min(1.4, duration / 6.0))
        a, b = int(half * 1000), int(half * 2000)
        tags.append(f"\\t(0,{a},\\fscx106\\fscy106)"
                    f"\\t({a},{b},\\fscx100\\fscy100)")
    return [f"Dialogue: 2,{ts(0.0)},{ts(duration)},Follow,,0,0,0,,"
            f"{{{''.join(tags)}}}{esc(text)}"]


def build_ass(dest: Path, *, words: list[dict], title: str, duration: float,
              cfg: RenderConfig, with_gameplay: bool = True) -> Path:
    geo = geometry(cfg, with_gameplay)
    if cfg.censor:
        words = censor_words(words, include_mild=cfg.censor_mild)
        title = censor(title, include_mild=cfg.censor_mild)
    head = [
        "[Script Info]",
        "; generated by twitch-clipper",
        "ScriptType: v4.00+",
        f"PlayResX: {geo.width}",
        f"PlayResY: {geo.height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCrMatrix: None",
        "",
        "[V4+ Styles]",
        *_styles(cfg),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    body = (_caption_events(words, cfg, geo, duration)
            + _title_events(title, cfg, geo, duration)
            + _follow_events(cfg, geo, duration))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    return dest
