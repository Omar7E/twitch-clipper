"""karak command line."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .pipeline import REPO, analyse, render_moments, work_dir_for
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
            raise SystemExit(f"no analysis yet for that VOD — run `karak find` first "
                             f"(looked in {p})")
        return p
    found = sorted(base.glob("*/moments.json"), key=lambda p: p.stat().st_mtime)
    if not found:
        raise SystemExit("nothing analysed yet — run `karak find <twitch-vod-url>` first")
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
    print(f"set one with:  karak title <#> \"YOUR TEXT\"")


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
        prog="karak",
        description="Find the best moments in a Twitch VOD and cut them into "
                    "vertical clips with captions and gameplay filler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  karak find https://www.twitch.tv/videos/123456789
  karak show
  karak render --gameplay gameplay/ --only 1,2,3
  karak auto https://www.twitch.tv/videos/123456789 --gameplay gameplay/
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
    t.add_argument("rank", type=int, help="moment number, as shown by `karak show`")
    t.add_argument("text", help="the caption to burn on screen")
    t.add_argument("--vod", help="VOD url or id (default: most recent analysis)")
    t.add_argument("--moments", help="path to a moments.json")
    t.add_argument("--work", type=Path, default=None)

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
                print("\nnext: karak render --gameplay gameplay/")
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
