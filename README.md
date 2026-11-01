# twitch-clipper

Find the best moments in a Twitch VOD and cut them into vertical clips —
word-by-word captions, a title card saying what happened, a follow banner, and
Subway Surfers / GTA gameplay filler stacked above the clip.

Give it a VOD link, get back ranked, ready-to-post 1080×1920 MP4s.

```bash
clipper find https://www.twitch.tv/videos/123456789
clipper show                                    # review + edit the picks
clipper render --gameplay gameplay/             # cut and render them
```

## How it finds the moments

Most clipping tools read the transcript and ask an LLM what sounds interesting.
That misses the clutch 1v3 where nobody says a word. clipper blends two signals
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
chatters) clipper says so and falls back to audio alone.

## It only downloads what it needs

A 63-minute VOD is ~2 GB at 1080p60. clipper pulls the **audio-only** rendition
first (~76 MB), analyses the whole stream from that, and then downloads video
for just the windows that won. Analysing an hour-long VOD costs about a minute.

## Deciding how many

Picking a fixed "top 8" means the 8th clip on a quiet stream is noise. clipper
instead measures **prominence** — how far a moment towers over the minutes
around it — and cuts the list where prominence falls off a cliff.

The score curve is percentile-normalised, so every VOD spans 0..1 and an
absolute threshold cannot tell a dull stream from a great one. Prominence can.
On a real 63-minute VOD the top 7 moments scored 0.74–0.99 prominence and the
8th dropped to 0.39 — that cliff is the cut. On synthetic streams with 2, 3 and
5 planted moments it returns exactly 2, 3 and 5, and on pure noise it returns 0.

```bash
clipper find <url> -n 4                      # cap it lower
clipper find <url> --set score.adaptive=false   # force exactly -n clips
clipper find <url> --set score.select_k=3.5     # stricter bar
```

## Gameplay filler

```bash
clipper gameplay path/to/footage.mp4     # -> gameplay/footage_vertical.mp4
```

Converts any footage to a vertical 1080×1920 master by centre-cropping, so the
same file works in either band at any `clip_frac`. Drop the results in
`gameplay/`; clipper picks a random start point per clip and rotates through the
files round-robin.

## Install

```bash
pip install -e .
```

Needs `ffmpeg` and `ffprobe` on PATH. `yt-dlp`, `numpy` and `faster-whisper`
come in as dependencies.

## Usage

### 1. Analyse

```bash
clipper find https://www.twitch.tv/videos/123456789
```

**clipper decides how many clips to make.** `-n` is a cap, not a quota. A stream
with three standout moments gives three clips; a flat one can give none at all.
See [Deciding how many](#deciding-how-many).

Downloads audio + chat replay, scores the VOD, picks the moments, transcribes
just those windows, and writes `work/<vod-id>/moments.json`.

```
 #      start    len  score   chat  title
------------------------------------------------------------------------------
 1      55:02    18s   0.89     58  55 HP CLUTCH
 2      47:32    22s   0.77     24  SHE DON'T SIT DOWN
```

### 2. Write the titles

The title is the line that tells a scroller what they're about to watch —
*"TPCFP ACCIDENTALLY KILLS SOMEONE'S CRY EMOJI"*. It describes the **event**,
in the third person. It is not a quote of what the streamer said.

```bash
clipper show --full          # transcript + loudest chat for every moment
clipper title 1 "TPCFP ACCIDENTALLY KILLS SOMEONE'S CRY EMOJI"
```

`show --full` prints what was said and what chat said, which is usually enough
to work out what happened — chat is often the better clue. Then set the line
with `clipper title`.

Three ways to get titles, best first:

1. **Write them yourself** from `clipper show --full`. Free, and always the best.
2. **Set `ANTHROPIC_API_KEY`** and Claude writes them from the same evidence,
   briefed to describe the event rather than quote it.
3. **The built-in heuristic** runs when neither applies. It picks the punchiest
   line of speech, which is serviceable but *will* quote rather than describe —
   treat it as a placeholder.

### 3. Render

```bash
clipper render --gameplay gameplay/ --only 1,2,3
```

Drop your Subway Surfers / GTA footage in `gameplay/`. clipper picks a **random
start point** in each file so clips don't reuse the same footage, and assigns
files round-robin. See `gameplay/README.md`.

Or do both at once:

```bash
clipper auto https://www.twitch.tv/videos/123456789 --gameplay gameplay/
```

## The layout

```
┌──────────────┐
│ ┌──────────┐ │  ← title card: what happens in this clip
│ │  stream  │ │
│ │   clip   │ │  52% of the frame
│ │          │ │  ← word-by-word captions, active word in accent colour
│ └──────────┘ │
│══ FOLLOW ═══ │  ← follow badge on the seam: optical centre, tilted, pulsing
│  gameplay    │
│              │  Subway Surfers / GTA
└──────────────┘
```

Flip the panels with `--set render.layout=gameplay-top`. Move the badge with
`--set render.follow_position=top` (or `bottom`). Captions automatically keep
clear of the badge wherever it sits.

The clip is letterboxed into its band over a blurred, darkened copy of itself
(`contain-blur`) so nothing is cropped away. Use `--set render.clip_fit=cover`
to fill the band by cropping instead.

## Tuning

Any field can be overridden with `--set section.key=value`, or collected in a
JSON file passed with `--config`.

```bash
clipper render --gameplay gameplay/ \
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
| `render.layout` | `gameplay-bottom` | clip on top; `gameplay-top` flips it |
| `render.follow_position` | `seam` | `seam`, `top` or `bottom` |
| `render.follow_text` | `FOLLOW TPCFP ON TWITCH` | e.g. `SUB TO TPCFP` |
| `render.words_per_line` | 3 | caption words on screen at once |
| `render.gameplay_volume` | 0.0 | mix gameplay audio in (0 = muted) |
| `whisper_model` | `small` | `tiny`/`base`/`small`/`medium` |

## Notes

- **Twitch deletes VODs.** Past broadcasts are kept 7 days (60 for Partners),
  and not at all if "Store past broadcasts" is off. Clip them while they exist.
- Captions need `faster-whisper`; `--no-captions` skips transcription entirely.
- Everything is cached under `work/<vod-id>/`, so re-rendering after a title
  edit doesn't re-download anything.
