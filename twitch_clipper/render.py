"""Compose the vertical clip: stream footage + gameplay filler + burnt-in text."""
from __future__ import annotations

import json
import random
import shlex
import subprocess
from pathlib import Path

from .config import FONT_DIR, RenderConfig
from .textfit import measure, wrap_to_width
from .subtitles import geometry


class RenderError(RuntimeError):
    pass


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RenderError(f"ffprobe failed on {path}: {out.stderr[-300:]}")
    return json.loads(out.stdout or "{}")


def media_info(path: Path) -> dict:
    data = probe(path)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        dur = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    return {
        "duration": dur,
        "has_video": v is not None,
        "has_audio": a is not None,
        "width": int(v.get("width") or 0) if v else 0,
        "height": int(v.get("height") or 0) if v else 0,
    }


def _title_font_file(cfg: RenderConfig) -> Path | None:
    """Locate the headline font on disk so its text can be measured."""
    if cfg.title_font_file:
        cand = FONT_DIR / cfg.title_font_file
        if cand.exists():
            return cand
    query = f"{cfg.title_font}:bold" if cfg.title_bold else cfg.title_font
    out = subprocess.run(["fc-match", "-f", "%{file}", query],
                         capture_output=True, text=True)
    path = Path(out.stdout.strip()) if out.returncode == 0 else None
    return path if path and path.exists() else None


def measure_line(text: str, cfg: RenderConfig) -> tuple[float, float]:
    """(width, line height) of one rendered headline line, in pixels.

    Used to drop emoji artwork in line with the end of the text rather than
    centred underneath it. Falls back to a rough estimate if the font cannot
    be measured, which only shifts the emoji slightly.
    """
    fallback = (len(text) * cfg.title_size * 0.55, cfg.title_size * 1.2)
    path = _title_font_file(cfg)
    if path is None:
        return fallback
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(str(path), cfg.title_size)
        width = float(font.getlength(text))
        asc, desc = font.getmetrics()
        return width, float(asc + desc)
    except Exception:
        return fallback


def is_playable(path: Path) -> bool:
    """True if the file is complete enough to decode.

    A process killed mid-write (ffmpeg under the OOM killer, an interrupted
    download) leaves a file that exists and has size but no moov atom. Any
    cache check based on existence alone will happily reuse it.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    try:
        return out.returncode == 0 and float(out.stdout.strip()) > 0
    except ValueError:
        return False


# libass renders COLR/CBDT emoji fonts as flat monochrome glyphs, and the
# opaque-box border goes ragged around them, so emoji are composited as
# images instead. Noto Emoji artwork (Apache 2.0) is the closest openly
# licensed match to the set these clips are styled after; Apple's own set
# is proprietary and cannot be redistributed.
EMOJI_CDN = ("https://cdn.jsdelivr.net/gh/googlefonts/noto-emoji@v2.047"
             "/png/128")


def emoji_codepoint(ch: str) -> str:
    """Noto filename stem for one emoji character, e.g. emoji_u1f62d."""
    pts = "_".join(f"{ord(c):x}" for c in ch if ord(c) != 0xFE0F)
    return f"emoji_u{pts}" if pts else ""


def fetch_emoji(chars: str, cache: Path) -> list[Path]:
    """Artwork for each emoji in `chars`, downloaded once and cached."""
    import urllib.error
    import urllib.request
    cache.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for ch in chars:
        if ch.isspace() or ord(ch) == 0xFE0F:
            continue
        stem = emoji_codepoint(ch)
        if not stem:
            continue
        dest = cache / f"{stem}.png"
        if not dest.exists() or dest.stat().st_size == 0:
            try:
                with urllib.request.urlopen(f"{EMOJI_CDN}/{stem}.png",
                                            timeout=30) as r:
                    data = r.read()
            except (urllib.error.URLError, TimeoutError, OSError):
                continue
            if not data.startswith(b"\x89PNG"):
                continue
            dest.write_bytes(data)
        out.append(dest)
    return out


def find_gameplay(paths: list[Path]) -> list[Path]:
    """Expand files and directories into a list of usable gameplay clips."""
    exts = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
    found: list[Path] = []
    for p in paths:
        if p.is_dir():
            found.extend(sorted(c for c in p.rglob("*")
                                if c.suffix.lower() in exts and c.is_file()))
        elif p.is_file() and p.suffix.lower() in exts:
            found.append(p)
    return found


def build_filtergraph(cfg: RenderConfig, ass_file: Path, *,
                      with_gameplay: bool, out_label: str = "vout") -> str:
    geo = geometry(cfg, with_gameplay)
    W = geo.width
    clip_h = geo.clip_height
    chain: list[str] = []

    pre = "[0:v]"
    if cfg.clip_crop:
        pre += f"crop={cfg.clip_crop},"
    else:
        pre += ""
    pre += f"fps={cfg.fps},setsar=1,format=yuv420p"

    if cfg.clip_fit == "cover":
        chain.append(f"{pre},scale={W}:{clip_h}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{clip_h}[clipreg]")
    elif cfg.clip_fit == "contain-blur":
        chain.append(f"{pre},split=2[cmain][cbg]")
        chain.append(f"[cbg]scale={W}:{clip_h}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{clip_h},gblur=sigma=30,eq=brightness=-0.14[bg]")
        chain.append(f"[cmain]scale={W}:{clip_h}:force_original_aspect_ratio=decrease[fg]")
        chain.append("[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[clipreg]")
    else:
        raise RenderError(f"unknown clip_fit {cfg.clip_fit!r} "
                          "(expected contain-blur or cover)")

    if with_gameplay:
        chain.append(f"[1:v]fps={cfg.fps},setsar=1,format=yuv420p,"
                     f"scale={W}:{geo.game_height}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{geo.game_height}[gamereg]")
        order = ("[gamereg][clipreg]" if cfg.layout == "gameplay-top"
                 else "[clipreg][gamereg]")
        chain.append(f"{order}vstack=inputs=2[stack]")
        src = "[stack]"
    else:
        src = "[clipreg]"

    fonts = shlex.quote(str(FONT_DIR)).strip("'")
    chain.append(f"{src}ass=filename={ass_file.as_posix()}:fontsdir={fonts}"
                 f"[{out_label}]")
    return ";".join(chain)


def render_clip(clip: Path, gameplay: Path | None, ass_file: Path, dest: Path,
                duration: float, cfg: RenderConfig, *,
                seed: int | None = None, quiet: bool = True,
                title: str = "", emoji: str = "",
                emoji_cache: Path | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    clip_info = media_info(clip)
    if not clip_info["has_video"]:
        raise RenderError(f"{clip} has no video stream")

    cmd: list[str] = ["ffmpeg", "-y", "-v", "error", "-stats"]
    cmd += ["-i", str(clip)]

    with_gameplay = gameplay is not None
    game_has_audio = False
    if with_gameplay:
        g = media_info(gameplay)
        if not g["has_video"]:
            raise RenderError(f"gameplay file {gameplay} has no video stream")
        game_has_audio = g["has_audio"]
        gdur = g["duration"]
        # start somewhere random so every clip doesn't reuse the same footage
        if gdur > duration + 2.0:
            rng = random.Random(seed)
            start = rng.uniform(0.0, max(0.0, gdur - duration - 1.0))
            cmd += ["-ss", f"{start:.2f}"]
        cmd += ["-stream_loop", "-1", "-i", str(gameplay)]

    art = fetch_emoji(emoji, emoji_cache) if (emoji and emoji_cache) else []
    for png in art:
        cmd += ["-i", str(png)]

    base = "vbase" if art else "vout"
    graph = build_filtergraph(cfg, ass_file, with_gameplay=with_gameplay,
                              out_label=base)

    if art:
        # sit the artwork in line with the end of the last headline line,
        # the way short-form captions do, rather than centred underneath
        import textwrap as _tw
        geo = geometry(cfg, with_gameplay)
        lines = wrap_to_width(
            title.strip(), cfg.title_font, cfg.title_size,
            geo.width - 2 * cfg.safe_x - 2 * cfg.title_outline,
            bold=cfg.title_bold, file_name=cfg.title_font_file) or [""]
        title_y = max(int(round(geo.height * cfg.title_y_frac)),
                      int(cfg.title_size * 0.9) + 24)
        last_w, line_h = measure(lines[-1], cfg.title_font, cfg.title_size,
                                 bold=cfg.title_bold,
                                 file_name=cfg.title_font_file)
        block_h = line_h * len(lines)
        # centre of the last line within the \an5-anchored block
        last_cy = title_y - block_h / 2 + line_h * (len(lines) - 0.5)
        size = int(cfg.title_size * 0.92)
        gap = int(size * 0.16)
        x = geo.width / 2 + last_w / 2 + cfg.title_outline + gap
        total = len(art) * size + (len(art) - 1) * gap
        # keep the run of emoji on screen
        x = min(x, geo.width - cfg.safe_x - total)
        y = int(last_cy - size / 2)
        label = base
        for i, _png in enumerate(art):
            graph += f";[{(2 if with_gameplay else 1) + i}:v]scale={size}:{size}[e{i}]"
            nxt = "vout" if i == len(art) - 1 else f"ov{i}"
            graph += (f";[{label}][e{i}]overlay="
                      f"{int(x) + i * (size + gap)}:{y}[{nxt}]")
            label = nxt

    # audio: stream audio always, gameplay mixed in only if asked for
    gv = max(0.0, cfg.gameplay_volume)
    norm = ",loudnorm=I=-14:TP=-1.5:LRA=11" if cfg.loudnorm else ""
    if clip_info["has_audio"] and with_gameplay and game_has_audio and gv > 0:
        graph += (f";[0:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                  f"channel_layouts=stereo[a0]"
                  f";[1:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                  f"channel_layouts=stereo,volume={gv:.3f}[a1]"
                  f";[a0][a1]amix=inputs=2:duration=first:dropout_transition=0"
                  f"{norm}[aout]")
        amap = ["-map", "[aout]"]
    elif clip_info["has_audio"]:
        graph += (f";[0:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                  f"channel_layouts=stereo{norm}[aout]")
        amap = ["-map", "[aout]"]
    else:
        amap = []

    cmd += ["-filter_complex", graph, "-map", "[vout]", *amap,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
            "-movflags", "+faststart", "-r", str(cfg.fps)]
    if amap:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    cmd += [str(dest)]

    proc = subprocess.run(cmd, capture_output=quiet, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-2500:] if quiet else ""
        raise RenderError(f"ffmpeg render failed:\n{err}\n\ncmd: {' '.join(cmd)}")
    if not dest.exists() or dest.stat().st_size == 0:
        raise RenderError(f"render produced no output at {dest}")
    return dest


def normalise_gameplay(src: Path, dest: Path, *, width: int = 1080,
                       height: int = 1920, fps: int = 30, crf: int = 23,
                       quiet: bool = True) -> Path:
    """Centre-crop and scale any footage to a vertical 1080x1920 master.

    Stored portrait so the same file works whichever band it lands in and
    whatever `clip_frac` is set to — the renderer then crops its band out of
    this with almost no further loss.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = media_info(src)
    if not info["has_video"]:
        raise RenderError(f"{src} has no video stream")
    vf = (f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},setsar=1,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-an", "-movflags", "+faststart", str(dest)]
    proc = subprocess.run(cmd, capture_output=quiet, text=True)
    if proc.returncode != 0:
        raise RenderError(f"gameplay normalise failed:\n"
                          f"{(proc.stderr or '')[-800:]}")
    return dest
