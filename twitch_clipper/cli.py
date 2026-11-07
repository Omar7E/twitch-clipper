"""twitch-clipper command line."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .pipeline import REPO, _slug, analyse, render_moments, work_dir_for
from .render import RenderError, find_gameplay, normalise_gameplay
from .transcribe import TranscribeUnavailable
from .twitch import TwitchError, parse_vod_id


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="JSON config file with score/render overrides")
    p.add_argument("--set", dest="overrides", action="append", default=[],
                   metavar="k=v",
                   help="override one option, e.g. --set score.count=12 "
                        "--set render.layout=gameplay-bottom (repeatable)")
    p.add_argument("--work", type=Path, default=None,
                   help="working directory root (default: ./work)")
    p.add_argument("-q", "--quiet", action="store_true")


def _load_cfg(args) -> Config:
    cfg = Config.load(args.config)
    cfg.apply_overrides(args.overrides or [])
    return cfg


def _resolve_moments(args) -> Path:
    if getattr(args, "moments", None):
        p = Path(args.moments)
        if not p.exists():
            raise SystemExit(f"no such moments file: {p}")
        return p
    base = args.work or (REPO / "work")
    if getattr(args, "vod", None):
        p = work_dir_for(parse_vod_id(args.vod), args.work) / "moments.json"
        if not p.exists():
            raise SystemExit(f"no analysis yet for that VOD — run `clipper find` first "
                             f"(looked in {p})")
        return p
    found = sorted(base.glob("*/moments.json"), key=lambda p: p.stat().st_mtime)
    if not found:
        raise SystemExit("nothing analysed yet — run `clipper find <twitch-vod-url>` first")
    return found[-1]


def _print_full(path: Path) -> None:
    """Everything needed to write a title: what was said, what chat said."""
    data = json.loads(path.read_text())
    print(f"\n{data['vod']['channel']} — {data['vod']['title']}\n")
    for m in data["moments"]:
        mm, ss = divmod(int(m["start"]), 60)
        words = " ".join(w["word"] for w in (m.get("words") or [])) or "(no speech)"
        print(f"── #{m['rank']}  {mm:02d}:{ss:02d}  {m['duration']:.0f}s  "
              f"score {m['score']:.2f}  chat {m.get('chat_count', 0)}")
        print(f"   title now : {m.get('title') or '(none)'}")
        print(f"   said      : {words[:400]}")
        sample = m.get("chat_sample") or []
        if sample:
            joined = " | ".join(c["text"][:34] for c in sample[:10])
            print(f"   chat      : {joined[:400]}")
        print()
    print(f"set one with:  clipper title <#> \"YOUR TEXT\"")


def _print_table(path: Path) -> None:
    data = json.loads(path.read_text())
    vod = data["vod"]
    print(f"\n{vod['channel']} — {vod['title']}")
    url = vod.get("url") or f"https://www.twitch.tv/videos/{vod.get('id', '')}"
    print(f"{url}  ({vod.get('duration', 0) / 60:.0f} min)")
    sig = data.get("signal", {})
    chat = data.get("chat_stats", {})
    basis = ("chat + audio" if sig.get("chat_used") else "audio only")
    print(f"signal: {basis}   chat: {chat.get('messages', 0)} msgs "
          f"from {chat.get('chatters', '?')} chatters")
    if sig.get("chat_skip_reason"):
        print(f"note:   {sig['chat_skip_reason']}")
    print()
    print(f"{'#':>2}  {'start':>9}  {'len':>5}  {'score':>5}  {'chat':>5}  title")
    print("-" * 78)
    for m in data["moments"]:
        mm, ss = divmod(int(m["start"]), 60)
        h, mm = divmod(mm, 60)
        stamp = f"{h}:{mm:02d}:{ss:02d}" if h else f"{mm:02d}:{ss:02d}"
        print(f"{m['rank']:>2}  {stamp:>9}  {m['duration']:>4.0f}s  "
              f"{m['score']:>5.2f}  {m.get('chat_count', 0):>5}  "
              f"{(m.get('title') or '—')[:40]}")
    print(f"\nedit titles in: {path}")


def parse_timestamp(text: str) -> float:
    """Accept 1:56:20, 56:20 or a bare number of seconds."""
    text = text.strip()
    if ":" not in text:
        return float(text)
    bits = [float(x) for x in text.split(":")]
    if len(bits) == 2:
        return bits[0] * 60 + bits[1]
    if len(bits) == 3:
        return bits[0] * 3600 + bits[1] * 60 + bits[2]
    raise ValueError(f"cannot read timestamp {text!r}")


def _parse_only(s: str | None) -> list[int] | None:
    if not s:
        return None
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clipper",
        description="Find the best moments in a Twitch VOD and cut them into "
                    "vertical clips with captions and gameplay filler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  clipper find https://www.twitch.tv/videos/123456789
  clipper show
  clipper render --gameplay gameplay/ --only 1,2,3
  clipper auto https://www.twitch.tv/videos/123456789 --gameplay gameplay/
""")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="analyse a VOD and pick the best moments")
    f.add_argument("url", help="Twitch VOD url or id")
    f.add_argument("-n", "--count", type=int, help="how many moments to find")
    f.add_argument("--no-captions", action="store_true",
                   help="skip transcription (no word-level captions)")
    f.add_argument("--no-claude", action="store_true",
                   help="never call the Claude API for titles")
    _common(f)

    s = sub.add_parser("show", help="print the moments found for a VOD")
    s.add_argument("--vod", help="VOD url or id (default: most recent analysis)")
    s.add_argument("--moments", help="path to a moments.json")
    s.add_argument("--work", type=Path, default=None)
    s.add_argument("--full", action="store_true",
                   help="also show transcript and chat for each moment")

    t = sub.add_parser("title", help="set the on-screen title for one moment")
    t.add_argument("rank", type=int, help="moment number, as shown by `clipper show`")
    t.add_argument("text", help="the caption to burn on screen")
    t.add_argument("--vod", help="VOD url or id (default: most recent analysis)")
    t.add_argument("--moments", help="path to a moments.json")
    t.add_argument("--work", type=Path, default=None)

    ad = sub.add_parser("add", help="add a moment by hand at a timestamp")
    ad.add_argument("at", help="where it happens: 1:56:20, 56:20 or 6980")
    ad.add_argument("--title", default="", help="on-screen caption")
    ad.add_argument("--window", type=float, default=None,
                    help="seconds of source to consider (default: clip.window)")
    ad.add_argument("--vod", help="VOD url or id (default: most recent analysis)")
    ad.add_argument("--moments", help="path to a moments.json")
    ad.add_argument("--work", type=Path, default=None)
    ad.add_argument("--set", dest="overrides", action="append", default=[],
                    metavar="k=v")
    ad.add_argument("--config", default=None)
    ad.add_argument("-q", "--quiet", action="store_true")

    ap = sub.add_parser("approve",
                        help="mark clips approved and collect them for posting")
    ap.add_argument("which", help="clip numbers (1,3 or 1-4) or 'all'")
    ap.add_argument("--to", type=Path, default=None,
                    help="where to put them (default: ./approved/<vod-id>)")
    ap.add_argument("--vod", help="VOD url or id (default: most recent analysis)")
    ap.add_argument("--moments", help="path to a moments.json")
    ap.add_argument("--work", type=Path, default=None)

    r = sub.add_parser("render", help="cut and render the selected moments")
    r.add_argument("--vod", help="VOD url or id (default: most recent analysis)")
    r.add_argument("--moments", help="path to a moments.json")
    r.add_argument("-g", "--gameplay", action="append", default=[], type=Path,
                   help="gameplay video file or directory (repeatable)")
    r.add_argument("--only", help="render a subset, e.g. 1,3 or 1-4")
    r.add_argument("-o", "--out", type=Path, default=None, help="output directory")
    _common(r)

    gp = sub.add_parser("gameplay",
                        help="prep filler footage into vertical 1080x1920")
    gp.add_argument("source", nargs="+",
                    help="local video file(s) or directory to convert")
    gp.add_argument("-o", "--out", type=Path, default=None,
                    help="where to write (default: ./gameplay)")

    a = sub.add_parser("auto", help="find then render in one go")
    a.add_argument("url", help="Twitch VOD url or id")
    a.add_argument("-n", "--count", type=int)
    a.add_argument("-g", "--gameplay", action="append", default=[], type=Path)
    a.add_argument("-o", "--out", type=Path, default=None)
    a.add_argument("--no-captions", action="store_true")
    a.add_argument("--no-claude", action="store_true")
    _common(a)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "show":
        path = _resolve_moments(args)
        if getattr(args, "full", False):
            _print_full(path)
        else:
            _print_table(path)
        return 0

    if args.cmd == "title":
        path = _resolve_moments(args)
        data = json.loads(path.read_text())
        hit = next((m for m in data["moments"] if m["rank"] == args.rank), None)
        if hit is None:
            raise SystemExit(f"no moment #{args.rank} "
                             f"(have 1..{len(data['moments'])})")
        hit["title"] = args.text.strip()
        hit["title_source"] = "manual"
        path.write_text(json.dumps(data, indent=2))
        print(f"#{args.rank} title set to: {hit['title']}")
        return 0

    if args.cmd == "gameplay":
        out = args.out or (REPO / "gameplay")
        srcs = find_gameplay([Path(x) for x in args.source])
        if not srcs:
            raise SystemExit("no video files found in: "
                             + ", ".join(str(x) for x in args.source))
        for src in srcs:
            dest = out / f"{src.stem}_vertical.mp4"
            if dest.resolve() == src.resolve():
                continue
            print(f"→ {src.name} → {dest.name}")
            normalise_gameplay(src, dest)
            print(f"  ✓ {dest.stat().st_size / 1e6:.1f} MB  1080x1920")
        print(f"\nready in {out}")
        return 0

    if args.cmd == "approve":
        import shutil
        path = _resolve_moments(args)
        data = json.loads(path.read_text())
        vod = data["vod"]
        vod_id = vod["id"]
        rendered = REPO / "out" / vod_id
        wanted = (None if args.which.strip().lower() == "all"
                  else set(_parse_only(args.which) or []))
        dest = args.to or (REPO / "approved" / vod_id)
        dest.mkdir(parents=True, exist_ok=True)

        picked, missing = [], []
        for m in data["moments"]:
            if wanted is not None and m["rank"] not in wanted:
                continue
            hits = sorted(rendered.glob(f"{m['rank']:02d}_*.mp4"))
            if not hits:
                if wanted is not None:
                    missing.append(m["rank"])
                continue
            m["approved"] = True
            picked.append((m, hits[0]))

        if missing:
            print(f"not rendered yet, skipped: {sorted(missing)}")
        if not picked:
            raise SystemExit("nothing to approve - render the clips first")

        lines = [f"{vod['channel']} - {vod['title']}", vod.get("url", ""), ""]
        for i, (m, src) in enumerate(picked, 1):
            mm, ss = divmod(int(m.get("peak", m["start"])), 60)
            hh, mm = divmod(mm, 60)
            stamp = f"{hh}-{mm:02d}-{ss:02d}" if hh else f"{mm:02d}-{ss:02d}"
            out = dest / f"{i:02d}_{stamp}_{_slug(m.get('title') or 'clip')}.mp4"
            shutil.copy2(src, out)
            lines.append(f"{i:>2}. [{stamp.replace('-', ':')}] "
                         f"{m.get('title', '')} {m.get('emoji', '')}".rstrip())
            lines.append(f"    {out.name}  ({m['duration']:.0f}s)")
            # the caption to paste when posting
            caption = m.get("caption") or ""
            if not caption:
                caption = f"{m.get('title', '').capitalize()}\n\nTwitch: Tpcfp"
            lines.append("    ---")
            for cl in caption.splitlines():
                lines.append(f"    {cl}" if cl else "")
            lines.append("    ---")
            lines.append("")
        (dest / "clips.txt").write_text("\n".join(lines) + "\n")
        path.write_text(json.dumps(data, indent=2))
        print(f"approved {len(picked)} clip(s) -> {dest}")
        for ln in lines[3:]:
            print("  " + ln)
        return 0

    if args.cmd == "add":
        import numpy as np
        from .segments import plan_segments
        cfg = _load_cfg(args)
        path = _resolve_moments(args)
        data = json.loads(path.read_text())
        at = parse_timestamp(args.at)
        vod_dur = float(data["vod"].get("duration") or 0) or (at + 120)
        width = args.window or cfg.clip.window
        ws = max(0.0, at - width * 0.45)
        we = min(vod_dur, ws + width)
        ws = max(0.0, we - width)

        sig = path.parent / "signals.npz"
        segs = [(ws, we)]
        if sig.exists() and cfg.clip.multi_cut:
            z = np.load(sig)
            segs = plan_segments(z["curve"], float(z["hop"]), ws, we,
                                 target=cfg.clip.target_duration,
                                 min_segment=cfg.clip.min_segment,
                                 merge_gap=cfg.clip.merge_gap,
                                 max_segments=cfg.clip.max_segments,
                                 quiet=z["quiet"])
        moment = {
            "rank": 0, "peak": round(at, 2),
            "start": round(ws, 2), "end": round(we, 2),
            "window_start": round(ws, 2), "window_end": round(we, 2),
            "segments": [[a, b] for a, b in segs],
            "duration": round(sum(b - a for a, b in segs), 2),
            "score": 1.0, "prominence": 1.0, "mean_score": 1.0,
            "title": args.title.strip(), "title_source": "manual",
            "words": [], "chat_count": 0, "chat_sample": [], "manual": True,
        }
        data["moments"].append(moment)
        data["moments"].sort(key=lambda m: -m.get("score", 0))
        for i, m in enumerate(data["moments"], 1):
            m["rank"] = i
        path.write_text(json.dumps(data, indent=2))
        mm, ss = divmod(int(at), 60)
        print(f"added moment at {mm}:{ss:02d} — {len(segs)} segment(s), "
              f"{moment['duration']:.0f}s from a {width:.0f}s window")
        print(f"transcribe + render it with: clipper render --only "
              f"{next(m['rank'] for m in data['moments'] if m.get('manual'))}")
        return 0

    cfg = _load_cfg(args)
    if getattr(args, "count", None):
        cfg.score.count = args.count

    try:
        if args.cmd in ("find", "auto"):
            moments_file = analyse(
                args.url, cfg, work_base=args.work,
                captions=not args.no_captions,
                use_claude=not args.no_claude,
                quiet=args.quiet,
            )
            if args.cmd == "find":
                _print_table(moments_file)
                print("\nnext: clipper render --gameplay gameplay/")
                return 0
        else:
            moments_file = _resolve_moments(args)

        outputs = render_moments(
            moments_file, cfg,
            gameplay=list(getattr(args, "gameplay", []) or []),
            only=_parse_only(getattr(args, "only", None)),
            out_dir=args.out,
            quiet=args.quiet,
        )
        if outputs:
            print(f"\n✓ {len(outputs)} clip(s) in {outputs[0].parent}")
            for o in outputs:
                print(f"  {o.name}")
        else:
            print("nothing rendered")
        return 0
    except (TwitchError, RenderError, TranscribeUnavailable, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
