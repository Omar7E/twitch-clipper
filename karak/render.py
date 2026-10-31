"""Compose the vertical clip: stream footage + gameplay filler + burnt-in text."""
from __future__ import annotations

import json
import random
import shlex
import subprocess
from pathlib import Path

from .config import FONT_DIR, RenderConfig
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
                      with_gameplay: bool) -> str:
    geo = geometry(cfg)
    W = geo.width
    clip_h = geo.clip_height if with_gameplay else geo.height
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
    chain.append(f"{src}ass=filename={ass_file.as_posix()}:fontsdir={fonts}[vout]")
    return ";".join(chain)


def render_clip(clip: Path, gameplay: Path | None, ass_file: Path, dest: Path,
                duration: float, cfg: RenderConfig, *,
                seed: int | None = None, quiet: bool = True) -> Path:
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

    graph = build_filtergraph(cfg, ass_file, with_gameplay=with_gameplay)

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
