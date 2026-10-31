# karak

Find the best moments in a Twitch VOD and cut them into vertical clips —
word-by-word captions, a title card saying what happened, a follow banner, and
Subway Surfers / GTA gameplay filler stacked above the clip.

Give it a VOD link, get back ranked, ready-to-post 1080×1920 MP4s.

```bash
karak find https://www.twitch.tv/videos/123456789
karak show                                    # review + edit the picks
karak render --gameplay gameplay/             # cut and render them
```

## How it finds the moments

Most clipping tools read the transcript and ask an LLM what sounds interesting.
That misses the clutch 1v3 where nobody says a word. karak blends two signals
that actually track hype on a gameplay stream:

**Chat reaction (weight 0.6)** — pulled from Twitch's chat replay, no account or
API key needed. Rate alone is noisy, so it also weighs:
- *who* reacted — a burst from 40 people beats 40 messages from one spammer
  (`unique_damp`)
- *what they said* — a tuned lexicon where `clip it` and `someone clip` score
  highest, through `OMEGALUL` / `KEKW` / `no way`, down to a bare `W`.
  Short tokens match whole-words only, so "how" and "wow" don't register as `w`.

**Audio excitement (weight 0.4)** — always available, and the only signal when
chat is dead. Measured against *that streamer's own* rolling baseline, so a
loud streamer isn't permanently "excited":
- loudness above baseline
- high-frequency energy (shouting and laughing sit higher in the spectrum)
- local dynamics (bursty audio beats steady narration)

The blended curve is smoothed, peaks are picked greedily with non-maximum
suppression so you never get two clips of the same event, and the cut points
snap to the quietest frame nearby so clips don't start mid-word.

If chat is too sparse to trust (`< chat_min_rate` msgs/min, or fewer than 2
chatters) karak says so and falls back to audio alone.

## It only downloads what it needs

A 63-minute VOD is ~2 GB at 1080p60. karak pulls the **audio-only** rendition
first (~76 MB), analyses the whole stream from that, and then downloads video
for just the windows that won. Analysing an hour-long VOD costs about a minute.

## Install

```bash
pip install -e .
```

Needs `ffmpeg` and `ffprobe` on PATH. `yt-dlp`, `numpy` and `faster-whisper`
come in as dependencies.

## Usage

### 1. Analyse

```bash
karak find https://www.twitch.tv/videos/123456789 -n 8
```

Downloads audio + chat replay, scores the VOD, picks the moments, transcribes
just those windows, and writes `work/<vod-id>/moments.json`.

```
 #      start    len  score   chat  title
------------------------------------------------------------------------------
 1      55:02    18s   0.89     58  55 HP CLUTCH
 2      47:32    22s   0.77     24  SHE DON'T SIT DOWN
```

### 2. Review and fix the titles

Titles are the one part a machine is worst at. Edit the `title` field of any
moment in `moments.json` before rendering — that text is what gets burned on
screen. If `ANTHROPIC_API_KEY` is set, Claude writes them instead of the
built-in heuristic (`--no-claude` to always skip it).

### 3. Render

```bash
karak render --gameplay gameplay/ --only 1,2,3
```

Drop your Subway Surfers / GTA footage in `gameplay/`. karak picks a **random
start point** in each file so clips don't reuse the same footage, and assigns
files round-robin. See `gameplay/README.md`.

Or do both at once:

```bash
karak auto https://www.twitch.tv/videos/123456789 --gameplay gameplay/
```

## The layout

```
┌──────────────┐
│  gameplay    │  gameplay-top (default), 48% of the frame
├──────────────┤
│ ┌──────────┐ │  ← title card: what happened
│ │  stream  │ │
│ │   clip   │ │  ← word-by-word captions, active word in accent colour
│ └──────────┘ │
│  FOLLOW ...  │  ← follow banner, pinned for the whole clip
└──────────────┘
```

The clip is letterboxed into its band over a blurred, darkened copy of itself
(`contain-blur`) so nothing is cropped away. Use `--set render.clip_fit=cover`
to fill the band by cropping instead.

## Tuning

Any field can be overridden with `--set section.key=value`, or collected in a
JSON file passed with `--config`.

```bash
karak render --gameplay gameplay/ \
  --set render.layout=gameplay-bottom \
  --set render.caption_style=boxed \
  --set render.caption_accent='#00E5FF' \
  --set render.follow_text='FOLLOW TPCFP ON TWITCH'
```

Worth knowing:

| option | default | what it does |
|---|---|---|
| `score.count` | 8 | how many moments to find |
| `score.chat_weight` / `score.audio_weight` | 0.6 / 0.4 | signal blend |
| `score.pre_roll` / `score.post_roll` | 9 / 11 | seconds kept around the peak |
| `score.min_gap` | 25 | stops two clips covering one event |
| `render.clip_frac` | 0.52 | share of the frame given to the stream |
| `render.caption_style` | `pop` | `pop`, `highlight` or `boxed` |
| `render.words_per_line` | 3 | caption words on screen at once |
| `render.gameplay_volume` | 0.0 | mix gameplay audio in (0 = muted) |
| `whisper_model` | `small` | `tiny`/`base`/`small`/`medium` |

## Notes

- **Twitch deletes VODs.** Past broadcasts are kept 7 days (60 for Partners),
  and not at all if "Store past broadcasts" is off. Clip them while they exist.
- Captions need `faster-whisper`; `--no-captions` skips transcription entirely.
- Everything is cached under `work/<vod-id>/`, so re-rendering after a title
  edit doesn't re-download anything.
