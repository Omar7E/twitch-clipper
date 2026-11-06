"""Configuration: every tunable in one place, overridable from JSON or CLI."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FONT_DIR = REPO / "assets" / "fonts"


@dataclass
class ScoreConfig:
    """How we decide what a 'best moment' is."""
    # sliding analysis
    hop: float = 0.25                 # seconds per analysis frame
    smooth_seconds: float = 3.0       # smoothing of the final score curve

    # chat signal
    chat_window: float = 8.0          # seconds of chat pooled per point
    chat_weight: float = 0.6          # blend weight when chat is usable
    chat_min_rate: float = 2.0        # msgs/min below this -> chat ignored as noise
    unique_damp: float = 0.5          # 0=ignore uniqueness, 1=fully damp single spammers
    hype_weight: float = 1.4          # extra pull from hype words/emotes

    # audio signal
    audio_weight: float = 0.4
    # Impulsive-transient signal: gunfire, explosions, crashes. Off by
    # default because on a talking or casual stream it finds nothing;
    # raise it (0.3-0.5) for shooters, where the action is the gunfight
    # and the streamer is often silent through the best of it.
    combat_weight: float = 0.0
    baseline_seconds: float = 90.0    # rolling baseline for "louder than usual"
    loud_weight: float = 0.60
    hf_weight: float = 0.25           # shouting/laughing sits higher in the spectrum
    dyn_weight: float = 0.15          # dynamic/peaky audio beats steady talking

    # window selection
    count: int = 8                    # upper bound on clips, not a quota
    adaptive: bool = True             # only keep moments that clear a quality bar
    min_clips: int = 0                # 0 = allowed to say "nothing worth clipping"
    select_k: float = 2.2             # how many MADs above the VOD's own median
    select_floor: float = 0.45        # and at least this fraction of the VOD's peak
    min_len: float = 18.0
    max_len: float = 50.0
    pre_roll: float = 9.0             # seconds of build-up kept before the peak
    post_roll: float = 11.0           # seconds of reaction kept after it
    min_gap: float = 25.0             # keep picks from clustering on one event
    snap_window: float = 3.5          # hunt for a quiet cut point within this range
    ignore_head: float = 30.0         # skip stream-start dead air
    ignore_tail: float = 20.0


@dataclass
class RenderConfig:
    """How the vertical video is composed."""
    width: int = 1080
    height: int = 1920
    fps: int = 30

    layout: str = "gameplay-bottom"   # gameplay-bottom = stream clip on top
    clip_frac: float = 0.52           # share of the canvas given to the stream clip
    clip_fit: str = "contain-blur"    # contain-blur | cover
    clip_crop: str = ""               # optional "w:h:x:y" crop of the source first

    # text
    font: str = "Bebas Neue"
    font_file: str = "BebasNeue-Regular.ttf"
    caption_size: int = 118
    caption_style: str = "pop"        # pop | highlight | boxed
    caption_accent: str = "#FFD93D"
    words_per_line: int = 3
    caption_y_frac: float = 0.84      # vertical position *within the clip band*

    title_size: int = 80
    title_wrap: int = 24
    title_bg: str = "#000000"
    title_opacity: float = 0.55

    follow_text: str = "FOLLOW TPCFP ON TWITCH"
    follow_size: int = 62
    follow_bg: str = "#9146FF"
    follow_position: str = "seam"     # seam | top | bottom
    follow_rotation: float = -3.0     # slight tilt reads as a sticker
    follow_pulse: bool = True         # gentle scale pulse to catch the eye

    # audio
    gameplay_volume: float = 0.0      # 0 = muted gameplay
    loudnorm: bool = True

    crf: int = 19
    preset: str = "veryfast"


@dataclass
class VisualConfig:
    """Verify candidates on screen before clipping them."""
    enabled: bool = True
    probe_seconds: float = 6.0     # how much of each candidate to decode
    candidate_factor: float = 2.5  # shortlist this many times the final count
    max_still_frac: float = 0.40   # above this the screen is frozen: reject
    min_motion: float = 1.5        # below this nothing is really happening
    motion_weight: float = 0.35    # how much motion counts in the final rank


@dataclass
class Config:
    score: ScoreConfig = field(default_factory=ScoreConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    visual: VisualConfig = field(default_factory=VisualConfig)
    whisper_model: str = "small"
    whisper_lang: str | None = None

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        cfg = cls()
        if not path:
            return cfg
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config not found: {p}")
        raw = json.loads(p.read_text())
        for section, obj in (("score", cfg.score), ("render", cfg.render),
                             ("visual", cfg.visual)):
            for k, v in (raw.get(section) or {}).items():
                if not hasattr(obj, k):
                    raise ValueError(f"unknown {section} option: {k}")
                setattr(obj, k, v)
        for k in ("whisper_model", "whisper_lang"):
            if k in raw:
                setattr(cfg, k, raw[k])
        return cfg

    def apply_overrides(self, pairs: list[str]) -> None:
        """Apply `--set score.count=12` style overrides."""
        known = {"score": self.score, "render": self.render,
                 "visual": self.visual}
        for pair in pairs:
            if "=" not in pair:
                raise ValueError(f"--set expects section.key=value, got {pair!r}")
            key, _, val = pair.partition("=")
            key = key.strip()
            if "." in key:
                section, _, name = key.partition(".")
            else:
                section, name = None, key
            if section in known:
                obj = known[section]
            elif section is None and hasattr(self, name):
                obj = self
            else:
                raise ValueError(f"unknown option {key!r}")
            if not hasattr(obj, name):
                raise ValueError(f"unknown option {key!r}")
            cur = getattr(obj, name)
            typed: object = val
            if isinstance(cur, bool):
                typed = val.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int) and not isinstance(cur, bool):
                typed = int(float(val))
            elif isinstance(cur, float):
                typed = float(val)
            setattr(obj, name, typed)

    def to_dict(self) -> dict:
        return {"score": asdict(self.score), "render": asdict(self.render),
                "visual": asdict(self.visual),
                "whisper_model": self.whisper_model,
                "whisper_lang": self.whisper_lang}


def font_path(cfg: RenderConfig) -> Path:
    return FONT_DIR / cfg.font_file
