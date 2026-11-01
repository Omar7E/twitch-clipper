"""Wire the stages together: analyse a VOD, then render the picks."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from . import audio as audio_mod
from . import twitch
from .chat import build_signal
from .config import Config
from .render import find_gameplay, media_info, render_clip
from .score import blend, pick_moments
from .subtitles import build_ass
from .titles import make_titles
from .transcribe import TranscribeUnavailable, slice_wav, transcribe_words

REPO = Path(__file__).resolve().parent.parent


def _log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


def work_dir_for(vod_id: str, base: Path | None = None) -> Path:
    return (base or (REPO / "work")) / vod_id


def analyse(url_or_id: str, cfg: Config, *, work_base: Path | None = None,
            captions: bool = True, use_claude: bool = True,
            quiet: bool = False) -> Path:
    """Download audio + chat, score the VOD, pick moments, write moments.json."""
    vod_id = twitch.parse_vod_id(url_or_id)
    work = work_dir_for(vod_id, work_base)
    work.mkdir(parents=True, exist_ok=True)

    _log(f"→ VOD {vod_id}", quiet)
    meta = twitch.fetch_meta(vod_id)
    _log(f"  {meta.channel}: {meta.title!r} "
         f"[{meta.game or 'no game'}] {meta.duration / 60:.0f} min", quiet)

    # 1. audio (audio-only rendition: small and fast)
    audio_path = work / "audio.m4a"
    _log("→ downloading audio track…", quiet)
    t0 = time.time()
    twitch.download_audio(vod_id, audio_path)
    _log(f"  {audio_path.stat().st_size / 1e6:.0f} MB in {time.time() - t0:.0f}s", quiet)

    # 2. chat replay
    chat_path = work / "chat.json"
    if chat_path.exists():
        comments = json.loads(chat_path.read_text())
        _log(f"→ chat replay (cached): {len(comments)} messages", quiet)
    else:
        _log("→ fetching chat replay…", quiet)
        try:
            comments = twitch.fetch_chat(vod_id, meta.duration)
        except twitch.TwitchError as e:
            _log(f"  chat unavailable ({e}); continuing on audio alone", quiet)
            comments = []
        chat_path.write_text(json.dumps(comments))
        _log(f"  {len(comments)} messages", quiet)

    # 3. signals
    _log("→ analysing audio…", quiet)
    feat = audio_mod.extract(audio_path, hop=cfg.score.hop)
    duration = meta.duration or (len(feat) * feat.hop)
    chat_signal, chat_stats = (None, None)
    if comments:
        chat_signal, chat_stats = build_signal(
            comments, duration, cfg.score.hop,
            window=cfg.score.chat_window,
            unique_damp=cfg.score.unique_damp,
            hype_weight=cfg.score.hype_weight,
        )
    curve, report, info = blend(feat, chat_signal, chat_stats, cfg.score)
    if info.get("chat_used"):
        _log(f"  blending chat ({chat_stats['msgs_per_min']:.1f} msg/min) "
             f"with audio", quiet)
    else:
        _log(f"  {info.get('chat_skip_reason', 'audio only')}", quiet)

    quiet_prof = audio_mod.silence_profile(feat)
    moments = pick_moments(curve, feat, cfg.score, duration, quiet_prof,
                           report=report)
    if cfg.score.adaptive:
        bar = moments[0].get("gate", 0.0) if moments else 0.0
        _log(f"→ {len(moments)} moment(s) cleared the bar "
             f"(score ≥ {bar:.2f}, cap {cfg.score.count})", quiet)
        if not moments:
            _log("  nothing in this VOD stood out enough to clip — try "
                 "--set score.adaptive=false to force the top picks", quiet)
    else:
        _log(f"→ {len(moments)} moments selected", quiet)

    # 4. transcribe just the windows
    if captions and moments:
        _log(f"→ transcribing {len(moments)} windows (whisper "
             f"{cfg.whisper_model})…", quiet)
        for i, m in enumerate(moments, 1):
            wav = work / "slices" / f"{i:02d}.wav"
            try:
                slice_wav(audio_path, m["start"], m["end"], wav)
                m["words"] = transcribe_words(
                    wav, model_size=cfg.whisper_model, language=cfg.whisper_lang)
                _log(f"  [{i}/{len(moments)}] {len(m['words'])} words", quiet)
            except TranscribeUnavailable as e:
                _log(f"  captions off: {e}", quiet)
                for mm in moments:
                    mm.setdefault("words", [])
                break
            except Exception as e:  # a bad slice shouldn't kill the run
                _log(f"  [{i}] transcription failed: {e}", quiet)
                m["words"] = []
    else:
        for m in moments:
            m["words"] = []

    # 5. titles
    titles, source = make_titles(moments, comments, use_claude=use_claude,
                                 streamer=meta.channel)
    for m, t in zip(moments, titles):
        m["title"] = t
        m["title_source"] = source
    if moments:
        _log(f"→ titles written ({source})", quiet)

    for m in moments:
        near = [c for c in comments if m["start"] <= c.get("t", -1) <= m["end"]]
        m["chat_count"] = len(near)
        m["chat_sample"] = [
            {"t": round(c["t"] - m["start"], 1), "user": c["user"], "text": c["text"]}
            for c in near[:25]
        ]

    vod_dict = asdict(meta)
    vod_dict["url"] = meta.url  # a property, so asdict() does not include it
    out = {
        "vod": vod_dict,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": cfg.to_dict(),
        "chat_stats": chat_stats or {"messages": len(comments)},
        "signal": info,
        "moments": moments,
    }
    dest = work / "moments.json"
    dest.write_text(json.dumps(out, indent=2))
    _log(f"✓ {dest}", quiet)
    return dest


def render_moments(moments_file: Path, cfg: Config, *,
                   gameplay: list[Path] | None = None,
                   only: list[int] | None = None,
                   out_dir: Path | None = None,
                   quiet: bool = False) -> list[Path]:
    data = json.loads(moments_file.read_text())
    vod = data["vod"]
    vod_id = vod["id"]
    work = moments_file.parent
    out_dir = out_dir or (REPO / "out" / vod_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    clips = find_gameplay(gameplay or [])
    if clips:
        _log(f"→ {len(clips)} gameplay file(s) available", quiet)
    else:
        _log("→ no gameplay supplied — rendering the clip full-frame", quiet)

    fmt = twitch.pick_format(vod_id)
    selected = [m for m in data["moments"]
                if only is None or m["rank"] in only]
    results: list[Path] = []

    for m in selected:
        rank = m["rank"]
        _log(f"\n── clip {rank}/{len(data['moments'])} "
             f"@ {m['start']:.0f}s ({m['duration']:.0f}s) "
             f"score {m['score']:.2f}", quiet)
        raw = work / "clips" / f"{rank:02d}.mp4"
        if not raw.exists():
            _log("  downloading section…", quiet)
            twitch.download_section(vod_id, m["start"], m["end"], raw, fmt=fmt)
        info = media_info(raw)
        duration = min(info["duration"] or m["duration"], m["duration"])

        ass = work / "subs" / f"{rank:02d}.ass"
        build_ass(ass, words=m.get("words") or [], title=m.get("title", ""),
                  duration=duration, cfg=cfg.render)

        game = clips[(rank - 1) % len(clips)] if clips else None
        dest = out_dir / f"{rank:02d}_{_slug(m.get('title') or 'clip')}.mp4"
        _log(f"  rendering → {dest.name}", quiet)
        render_clip(raw, game, ass, dest, duration, cfg.render,
                    seed=rank, quiet=True)
        results.append(dest)
        _log(f"  ✓ {dest.stat().st_size / 1e6:.1f} MB", quiet)

    return results


def _slug(text: str, limit: int = 40) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    s = "".join(keep)
    while "--" in s:
        s = s.replace("--", "-")
    return (s.strip("-") or "clip")[:limit].strip("-")
