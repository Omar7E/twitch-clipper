"""Twitch access: VOD metadata, chat replay, and (partial) media downloads.

Chat replay comes from Twitch's public web GraphQL endpoint, the same one the
VOD page itself uses. Nothing here needs an account or an API key.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

GQL = "https://gql.twitch.tv/gql"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's public web client id
COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

_VOD_PATTERNS = (
    re.compile(r"twitch\.tv/videos/(\d+)"),
    re.compile(r"twitch\.tv/\w+/v(?:ideo)?/(\d+)"),
    re.compile(r"^(\d{6,})$"),
)


class TwitchError(RuntimeError):
    pass


def parse_vod_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    for pat in _VOD_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    raise TwitchError(
        f"could not find a VOD id in {url_or_id!r} — "
        "expected something like https://www.twitch.tv/videos/123456789"
    )


def _gql(payload: object, timeout: float = 30.0, retries: int = 3) -> object:
    body = json.dumps(payload).encode()
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            GQL, data=body,
            headers={"Client-ID": WEB_CLIENT_ID, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
    raise TwitchError(f"Twitch GraphQL request failed after {retries} tries: {last}")


@dataclass
class VodMeta:
    id: str
    title: str
    duration: float
    channel: str
    channel_login: str
    game: str
    created_at: str

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/videos/{self.id}"


def fetch_meta(vod_id: str) -> VodMeta:
    q = """
    query($id: ID!) {
      video(id: $id) {
        id title lengthSeconds createdAt
        game { name }
        owner { login displayName }
      }
    }"""
    res = _gql({"query": q, "variables": {"id": vod_id}})
    v = (res or {}).get("data", {}).get("video")
    if not v:
        raise TwitchError(
            f"VOD {vod_id} not found. It may be deleted, subscriber-only, or private."
        )
    owner = v.get("owner") or {}
    return VodMeta(
        id=str(v["id"]),
        title=v.get("title") or "",
        duration=float(v.get("lengthSeconds") or 0),
        channel=owner.get("displayName") or owner.get("login") or "",
        channel_login=owner.get("login") or "",
        game=((v.get("game") or {}).get("name") or ""),
        created_at=v.get("createdAt") or "",
    )


def _comments_page(vod_id: str, *, offset: float | None = None,
                   cursor: str | None = None) -> dict:
    variables: dict = {"videoID": vod_id}
    if cursor:
        variables["cursor"] = cursor
    else:
        variables["contentOffsetSeconds"] = int(offset or 0)
    res = _gql([{
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": variables,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": COMMENTS_HASH}},
    }])
    if not isinstance(res, list) or not res:
        raise TwitchError("unexpected chat response shape")
    node = res[0].get("data", {}).get("video")
    if node is None:
        raise TwitchError(f"chat replay unavailable for VOD {vod_id}")
    return node.get("comments") or {"edges": [], "pageInfo": {}}


def _flatten(edge: dict) -> dict | None:
    n = edge.get("node") or {}
    msg = n.get("message") or {}
    frags = msg.get("fragments") or []
    text_parts, emotes = [], []
    for f in frags:
        t = f.get("text") or ""
        if t:
            text_parts.append(t)
        if f.get("emote"):
            emotes.append(t.strip() or "emote")
    off = n.get("contentOffsetSeconds")
    if off is None:
        return None
    commenter = n.get("commenter") or {}
    return {
        "id": n.get("id"),
        "t": float(off),
        "user": commenter.get("login") or "anon",
        "text": "".join(text_parts).strip(),
        "emotes": emotes,
    }


def fetch_chat(vod_id: str, duration: float, *, shards: int = 8,
               progress=None) -> list[dict]:
    """Fetch the whole chat replay.

    Cursor pagination is inherently serial, so we split the VOD into time
    shards, seek into each by offset, and paginate them in parallel. Shards
    overlap at the seams; comments are de-duplicated by id.
    """
    duration = max(duration, 1.0)
    shards = max(1, min(shards, 16))
    bounds = [(duration * i / shards, duration * (i + 1) / shards) for i in range(shards)]
    done = [0]

    def run_shard(bound: tuple[float, float]) -> list[dict]:
        start, end = bound
        out: list[dict] = []
        cursor: str | None = None
        offset: float | None = start
        for _ in range(4000):  # hard stop; ~400k comments per shard
            try:
                page = _comments_page(vod_id, offset=offset, cursor=cursor)
            except TwitchError:
                break
            edges = page.get("edges") or []
            if not edges:
                break
            stop = False
            for e in edges:
                flat = _flatten(e)
                if not flat:
                    continue
                # let the last shard run to the end of the VOD
                if flat["t"] > end and end < duration - 1e-6:
                    stop = True
                    break
                out.append(flat)
            if stop or not (page.get("pageInfo") or {}).get("hasNextPage"):
                break
            cursor = edges[-1].get("cursor")
            offset = None
            if not cursor:
                break
        done[0] += 1
        if progress:
            progress(done[0], shards)
        return out

    with ThreadPoolExecutor(max_workers=min(shards, 8)) as pool:
        chunks = list(pool.map(run_shard, bounds))

    seen: set[str] = set()
    merged: list[dict] = []
    for chunk in chunks:
        for c in chunk:
            key = c.get("id") or f"{c['t']}:{c['user']}:{c['text']}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
    merged.sort(key=lambda c: c["t"])
    return merged


# ---------------------------------------------------------------- downloads

def _run(cmd: list[str], *, quiet: bool = True) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.STDOUT if quiet else None,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-1500:]
        raise TwitchError(f"command failed ({' '.join(cmd[:3])}...):\n{tail}")


def download_audio(vod_id: str, dest: Path, *, quiet: bool = True) -> Path:
    """Grab audio-only for the whole VOD — small and fast, enough to analyse."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmpl = str(dest.with_suffix(".%(ext)s"))
    _run([
        "yt-dlp", "--no-warnings", "--no-playlist",
        "-f", "Audio_Only/bestaudio/best",
        "-o", tmpl, f"https://www.twitch.tv/videos/{vod_id}",
    ], quiet=quiet)
    for cand in sorted(dest.parent.glob(dest.stem + ".*")):
        if cand.suffix.lower() not in (".part", ".ytdl"):
            if cand != dest:
                cand.replace(dest)
            return dest
    raise TwitchError("audio download produced no file")


def download_section(vod_id: str, start: float, end: float, dest: Path,
                     *, fmt: str = "best", quiet: bool = True) -> Path:
    """Download just [start, end) of the VOD at full quality."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmpl = str(dest.with_suffix(".%(ext)s"))
    _run([
        "yt-dlp", "--no-warnings", "--no-playlist",
        "-f", fmt,
        "--download-sections", f"*{start:.2f}-{end:.2f}",
        "--force-keyframes-at-cuts",
        "-o", tmpl, f"https://www.twitch.tv/videos/{vod_id}",
    ], quiet=quiet)
    for cand in sorted(dest.parent.glob(dest.stem + ".*")):
        if cand.suffix.lower() not in (".part", ".ytdl"):
            if cand != dest:
                cand.replace(dest)
            return dest
    raise TwitchError(f"section {start:.1f}-{end:.1f} download produced no file")


def pick_format(vod_id: str, want_height: int = 1080) -> str:
    """Prefer a real 1080p/720p rendition, falling back to whatever exists."""
    try:
        out = subprocess.run(
            ["yt-dlp", "--no-warnings", "-F", f"https://www.twitch.tv/videos/{vod_id}"],
            capture_output=True, text=True, timeout=180,
        ).stdout
    except Exception:
        return "best"
    ids = re.findall(r"^(\S+)\s+mp4\s+(\d+)x(\d+)", out, re.M)
    if not ids:
        return "best"
    ranked = sorted(
        ((fid, int(h)) for fid, _w, h in ids),
        key=lambda x: (abs(x[1] - want_height), -x[1]),
    )
    return f"{ranked[0][0]}/best"
