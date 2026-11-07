---
name: "stream-clipper"
description: Expert judgment for turning Twitch VODs into viral short-form clips (TikTok, YouTube Shorts, Instagram Reels) with the user's twitch-clipper tool (the `clipper` CLI). Use this skill whenever the user sends a Twitch VOD link (twitch.tv/videos/...), asks to clip, cut, find highlights or best moments in a stream, or mentions tpcfp clips, even if they don't say "skill". It decides which moments are worth clipping, where each clip should start and end, how long it should be, what dead air to cut out, and how to title it.
---

# Stream Clipper

You are acting as a top-tier stream clipper. The `clipper` CLI (Omar7E/twitch-clipper) does the mechanical work: it scores a VOD from chat reactions and audio energy, picks peaks, transcribes them, and renders 1080x1920 clips with word-by-word captions, a title card and a "FOLLOW TPCFP ON TWITCH" banner over gameplay filler. What it cannot do is understand what actually happened. Peaks in a signal are only candidates. Your job is the judgment layer on top: confirm which candidates are genuinely clip-worthy, find what the signals missed, tighten the edges, cut the filler, and write titles that make people stop scrolling.

The only thing that matters is whether a stranger who has never heard of tpcfp would watch the clip to the end and want to follow. Keep that test in mind for every decision below.

## Workflow

1. **Analyse.** Run `clipper find <vod-url>` (add `-n` only to cap the count). Let the tool decide how many clips; `-n` is a ceiling, not a quota. If chat was too sparse it falls back to audio only, so be more sceptical of the picks in that case, because loud is not the same as good.
2. **Read the evidence.** Run `clipper show --full` to see the transcript and the loudest chat lines for each candidate. Chat is often the best clue to what really happened, especially when the streamer said nothing.
3. **Judge each candidate** using the sections below. Keep, trim, extend, or reject. Say why in one line each.
4. **Hunt for misses.** Scan the transcript between candidates for moments the signals underweight (see "What the signals miss"). Add any you find by adjusting the start and end times in `work/<vod-id>/moments.json`, or by re-running with different `--set score.*` options. Check the file and the CLI source first to see exactly how moments are stored; do not guess the schema.
5. **Set edges and cuts** (see "Where a clip starts and ends" and "Cutting dead air").
6. **Write titles** with `clipper title <n> "<title>"`.
7. **Render** with `clipper render --gameplay gameplay/ --only <ids>`, or `clipper auto` when the user wants it hands-off. Everything is cached under `work/<vod-id>/`, so re-rendering after tweaks is cheap.
8. **Report** a short ranked list (see "What to tell the user").

Twitch deletes VODs after 7 days (60 for Partners), so if the user mentions the stream is old, warn them early rather than after a failed download.

## What makes a moment worth clipping

A clip is worth making when it has a clear **setup, payoff and reaction**, and a viewer with zero context can feel it within about two seconds. Judge on these, roughly in this order:

- **Payoff is visible or audible.** Something definitely happens: a kill, a fail, a reveal, a punchline, a rage, a win. A streamer talking about something interesting is rarely enough on its own.
- **Self-contained.** It makes sense without knowing the game, the streamer, or the last ten minutes. If it needs context, either the first seconds must supply it or skip it.
- **Emotion.** Genuine surprise, laughter, panic, disbelief, anger. Real reactions beat performed ones, and clips where the streamer is audibly caught off guard tend to travel furthest.
- **Stakes and contrast.** Low HP clutch, last second, tiny chance, huge fail after confidence, calm to chaos. Contrast is what makes a payoff land.
- **Chat agrees.** A burst of clip it, W, KEKW, OMEGALUL or no way from many different people is strong confirmation. One spammer is not.
- **Rewatchable or shareable.** Would someone send this to a friend? Would they watch it twice?

### By stream type

The user streams a mix of content, so first work out which kind of stream each candidate comes from (the game and the transcript make it obvious) and use the matching lens.

- **Competitive or action games (FPS, battle royale, etc.):** clutches, aces, low-HP wins, impossible shots, funny deaths, absurd luck, teammate betrayals, a loss snatched from a win. The silent clutch matters here: the streamer may go quiet while concentrating, so chat and the audio spike carry the signal.
- **Variety, co-op and story games:** rage and fail moments, discovering something unexpected, a boss finally dying after many attempts, glitches, unscripted chaos with friends, jump scares, a joke that builds across several lines.
- **Just chatting and IRL:** a complete short story with a twist, a spicy or funny hot take, a strong reaction to something on screen, a roast or banter exchange, a surprising admission. Here the words are the content, so the transcript matters more than audio loudness.

## What the signals miss, and what they falsely love

The CLI blends chat rate and audio excitement. Both have predictable blind spots; compensate for them.

**False positives to reject or demote:**
- Chat spikes caused by a raid, follow or sub alert, giveaway, "type 1 in chat" prompt, emote-only spam, or a copy-pasted message. The burst is about the alert, not the gameplay.
- Loud but empty audio: intro or outro music, game explosions or menus, donation text-to-speech, a streamer reading chat aloud, laughing at an off-screen thing the viewer can never see.
- Sustained shouting with no payoff, and arguments about settings, mic, lag or bans.
- Moments that only work if you were watching live, such as an inside joke with chat that has no visible setup.
- Anything dominated by a copyrighted song or someone else's voice, which risks muting or removal on the platforms.

**Real moments the signals often underweight:**
- The quiet clutch where the streamer is silent and focused, with the payoff in the replay.
- Deadpan comedy: a calm one-liner that chat reacts to but the audio doesn't spike on.
- The delayed reaction: a pause, then the realisation. The pause is the joke, so keep it.
- A funny fail that leads to a laugh a few seconds after the audio peak.

If you add a moment the tool missed, say so explicitly and explain what you saw in the transcript or chat that justified it.

## Where a clip starts and ends

The tool defaults to roughly 9 seconds before the peak and 11 after, snapped to the quietest nearby frame. Treat that as a starting point, not an answer, because the peak is usually the reaction, and the real story begins earlier.

**Start:**
- Begin as late as possible while still showing what is about to happen. Aim for the hook within the first 1 to 2 seconds. A slow lead-in loses viewers before the payoff.
- Start on the action or a line that creates a question ("Okay, one more try, I'm not leaving this room"), not on greetings, "uh", or the streamer settling in.
- Never start mid-word or mid-sentence. If the setup sentence is essential, include all of it.

**End:**
- End just after the reaction resolves: the laugh finishing, the "no way" landing, the celebration beat. Cut before the energy fades and before the streamer starts explaining or moving on.
- Do not end on dead air, and do not end mid-word. A last line that works as a button (a punchline, a "what the hell") makes the clip feel finished and encourages replays.
- If a clip can end on the same visual or sentiment it started on, it loops better, which helps on all three platforms.

## How long each clip should be

Length should follow the moment, not a fixed number. Shorter is better only until the payoff stops making sense. These are working ranges; hold the shorter end when unsure.

| Moment type | Typical length | Why |
|---|---|---|
| Instant payoff (a kill, a jump scare, a one-liner, a quick fail) | 8 to 20 s | Nothing to build, so any extra length dilutes it |
| Clutch or build-up with a tense lead-in | 15 to 35 s | The tension is what makes the payoff land, so keep the build |
| Rage, meltdown or escalating chaos | 20 to 40 s | The escalation is the content |
| Story, hot take or banter exchange | 30 to 60 s | Needs the setup and twist; cut hard inside it instead of cutting the ending |
| Boss kill or long-awaited win | 20 to 45 s | Include enough struggle to make the win feel earned |

General rules:
- The clip works on TikTok, Reels and Shorts at once, so stay at or under 60 seconds. Most clips should land between 15 and 40 seconds.
- If a moment needs more than about 60 seconds to make sense, it is probably two moments or not a clip. Pick the strongest part.
- Retention beats length. A tight 18-second clip with a strong ending outperforms a 45-second one with a lull in the middle.

## Cutting dead air

Inside a moment there is often a gap between the setup and the payoff: walking across the map, a loading screen, looting, a long silence, menu navigation, the streamer repeating themselves. Remove it so the clip keeps moving, but protect the beats that make it work.

**Cut:**
- Silence or filler longer than about 0.7 seconds, unless it is a dramatic pause that is itself the joke.
- Travel, loading, menus, inventory, respawn timers, spectating and repeated narration of the same point.
- "Um", false starts, and stretches where nothing on screen changes.

**Keep:**
- The payoff and the full reaction after it. Never trim into the laugh or the "no way".
- A deliberate pause right before a punchline or just after a shocking event.
- Anything the viewer needs to understand what is going on or why it is funny.
- The first 2 seconds intact so the hook is clean and the opening is never a jump cut.

**How to cut cleanly:**
- Cut at word boundaries and in quiet frames, so there is no clipped syllable or audio pop. A short audio crossfade of about 30 to 80 ms hides the join.
- Keep the video consistent across a cut. If the visual jumps (for example the character teleports), that is fine on gameplay, but avoid cutting mid-motion on the streamer's face.
- Use few cuts. One or two well-placed jump cuts feel like editing; six feel like a mess.
- Check how the CLI represents a clip (a single start and end, or several segments) by reading `work/<vod-id>/moments.json` and the `twitch_clipper` source. If it only supports one window, either narrow the window or build the multi-segment cut with ffmpeg (trim each keeper segment, concatenate, then re-run captions on the stitched file) so the captions stay in sync with the new timing. Say which approach you used.

## Titles

The title card is a headline, and its job is to stop a scroller who has never heard of tpcfp. Nobody knows the streamer yet, so a name carries zero pull and wastes the most valuable words on screen. Write it the way a good news or YouTube headline is written: a hook that gives just enough context to create a question the viewer can only answer by watching.

**Principles**
- **Lead with the situation, stakes or twist, not the streamer.** Leave "TPCFP" out of the title by default. The follow banner and captions already carry the brand. Use a name only if it genuinely adds something, such as a recognisable guest or collab partner.
- **Give context in a few words.** The viewer should instantly know what is happening and why it matters: the odds, the cost, the absurdity, the contrast.
- **Open a curiosity gap, then let the clip close it.** Hint at the outcome without stating the payoff, so the clip delivers the reveal. Never give away the ending in the title.
- **Be specific.** Concrete details beat vague hype. "1 HP" beats "insane", and "after 40 attempts" beats "finally".
- **Use second person or a generic subject when it helps:** "this guy", "he", "my teammate", "you", or a role like "a boss". Talk to the viewer rather than about an unknown person.
- **Keep it short and readable in about 2 seconds on a phone,** ideally 4 to 9 words, in caps.
- **Be honest.** The clip must deliver what the title promises. Clickbait that disappoints kills retention and gets the clip buried.
- **Describe the event, do not quote the streamer.** Pull the facts from `clipper show --full`, and trust chat's reading of the event when the streamer's words are vague.

**Headline patterns that work**
- Stakes plus twist: "1 HP AGAINST 4 PLAYERS AND HE STILL WON"
- Unexpected cause: "THIS ONE MISTAKE COST HIM THE ENTIRE GAME"
- Escalation: "IT WENT FROM FUNNY TO TERRIFYING IN 5 SECONDS"
- Effort versus payoff: "40 ATTEMPTS LATER, THIS HAPPENED"
- Disbelief: "NOBODY EXPECTED THE ENDING TO GO LIKE THIS"
- Relatable situation: "WHEN YOUR TEAMMATE DOES THE ONE THING YOU TOLD HIM NOT TO"
- Question hook: "WHY DID HE JUMP OFF THE BRIDGE?"

**Before and after**
- Weak: "TPCFP ACCIDENTALLY KILLS SOMEONE'S CRY EMOJI". It only works if you already know the streamer.
- Better: "HE ACCIDENTALLY KILLED THE ONE PERSON WHO WAS CRYING FOR HELP", or "THIS WAS THE WORST POSSIBLE TIME TO MISS A SHOT". Both give context and a hook with no name needed.

Write each title yourself with `clipper title`, and do not rely on the built-in heuristic, which quotes lines instead of describing events. For each clip, draft two or three options, pick the one with the strongest hook that the clip fully backs up, and say which you chose if the user wants to swap it.

## Branding and layout

Keep the channel branding the tool already applies: the follow banner reads "FOLLOW TPCFP ON TWITCH" and the clip sits over Subway Surfers or GTA footage in the default `gameplay-bottom` layout with the word-by-word `pop` captions. Only change layout, caption style, accent colour or banner text when the user asks, using the `--set render.*` options. Make sure captions stay readable and clear of the banner.

## Quality gates before you render

Run through this list for each clip and fix or drop anything that fails:

1. Would a stranger understand and enjoy it with no context?
2. Is the hook in the first 1 to 2 seconds?
3. Does it end on the reaction or a natural button, with no trailing dead air?
4. Is the length the shortest that still lands the payoff?
5. Are there any silences, loading, or filler left inside that can go?
6. Does chat or the transcript genuinely support picking it, rather than just a loud spike?
7. Does the title work for someone who has never heard of tpcfp, hook them, and stay accurate?
8. Does it avoid copyrighted music, private information, and anything that would get it removed?

It is fine, and better, to deliver fewer clips than the tool suggested. A flat stream can honestly yield one or two clips, or none. Say so rather than padding the list with weak ones.

## What to tell the user

Finish with a short ranked list, best first. For each clip give the VOD timestamp, the final length, the title, and one line on why it made the cut. Flag any clip you are unsure about, any moment you added that the tool missed, and any you rejected and why (briefly). Mention where the rendered files are. Keep it brief; the user wants clips they can post, not a report.

## Title from the finished clip, never from a probe

A probe is a few seconds sampled from the longest planned segment. The
finished clip is several segments cut together, and the moment visible in
the probe may not survive the cut at all.

A clip was once titled "THE COUNTDOWN ENDS IN A SPINNER" from a collision
seen in its probe frame. The rendered clip contained no collision: the five
chosen segments skipped it entirely, leaving the raft drifting through open
water with no payoff. The title described something the viewer never sees,
which is worse than a dull title.

So: plan and cut first, then build a contact sheet **from the rendered
clip**, and write the title from that. If the finished clip turns out to
have no payoff, drop it rather than re-titling it. Not every candidate that
survives the visual check is a clip.

## Transcription errors that change the meaning

Whisper mishears domain words, and the result can invert a joke. "Mod" (a
Twitch moderator) came back as "mud" throughout one clip, turning "I'm going
to take away his mod" into something about dirt. The title and every caption
were wrong together, because both come from the same transcript.

Before titling, scan the transcript for words that make no sense in context,
especially streaming vocabulary: mod, sub, raid, host, ban, clip, VOD, POG,
and player or channel names. Fix them in the moment's `words` as well as in
the title, or the captions will contradict the headline.

## Headline style

The headline is the one element every viewer reads, so keep it legible and
constant:

- Its own font, a geometric sans rather than the condensed caption face.
- Around 20% of frame height. Hard against the top edge reads as an
  afterthought and can collide with platform UI.
- Up for the whole clip with no fade out, so late arrivals still get it.
- An emoji where it genuinely adds something, not on every clip.

Emoji must be composited as images. libass renders colour emoji fonts as
flat monochrome glyphs and the opaque-box border goes ragged around them;
the renderer fetches PNG artwork and overlays it under the headline. Set a
moment's `emoji` field in moments.json.

## Check whether the signal is telling you anything

Before trusting any ranking, profile the score across the whole VOD: load
`work/<vod-id>/signals.npz` and print mean and max per ten-minute block.

On a stream where the streamer talks steadily throughout, the curve comes
out **flat**. On one real two-hour VOD every block averaged 0.28 to 0.36
with peaks of 0.67 to 0.78, and the only outliers were two pause menus. A
flat curve means audio excitement cannot separate the good minutes from the
ordinary ones and the top picks are close to arbitrary.

When it is flat: say so rather than presenting the ranking as discovery,
raise `--set visual.motion_weight=0.55`, widen the shortlist with
`--set visual.candidate_factor=2.0`, and expect to pin several moments by
hand. That is the normal outcome, not a failure.

## See the whole stream cheaply, before clipping any of it

`yt-dlp -f sb0 -o board.mhtml <url>` pulls Twitch's scrubbing thumbnails for
an entire VOD in well under a megabyte; extract the JPEGs from the mhtml
with Python's `email` module. One frame per ~36 seconds is too coarse to
judge a single moment, but it maps the whole session at a glance: where the
menus are, where a level changes, where the session ends. Read it first on
any long VOD.

Then, in increasing cost: the probe files under `work/<vod-id>/probes/`,
a contact sheet per candidate, or one montage of every candidate at once
(extract a frame from each probe into a numbered directory and tile them) —
judging twelve candidates from one image beats twelve separate looks.

Pass `-nostdin` to every ffmpeg call inside a shell loop, or it consumes
the loop's stdin and silently processes every other item.

**The Twitch category is often wrong.** One VOD was tagged VALORANT and was
a co-op raft game. Confirm the game from frames, and tell the user.

## Moments that score badly and are always worth checking

- **The end of the session.** Beating the game, the final run, the
  completion screen. Often the most shareable moment of the stream, and it
  reliably scores no higher than average because everyone is calm or quietly
  concentrating. On one VOD the run that finished the game ranked nowhere.
  Always look at the last ten minutes before deciding you are done.
- **Milestone and stat screens.** Death counters, timers, completion
  summaries. Static, so the visual check rejects them, but the numbers make
  excellent titles — "THEY BEAT THE GAME AFTER 407 DEATHS" came straight off
  one. Pin them and keep the window contiguous so the reveal survives.
- **The first success after many failures.** The relief is the payoff and it
  is quieter than the failures were.

## Co-op and party games specifically

The comedy is social, not mechanical: blame ("that was you" / "that's not on
me") after a group wipe; everyone dying at once; coordination collapse, four
people shouting different directions; self-deprecation, noticing the death
count or that nobody is watching.

Reject in this genre: route planning, menu and settings talk, debating who
steers, long stretches of "go left, no right". Animated, scores well on
audio, nothing happens.

## Pinning moments by hand

`clipper add <timestamp> --title "..."` inserts a moment the signals missed;
accepts `1:56:20`, `56:20` or raw seconds, and plans its own cuts from the
saved curve. Pass `--set clip.multi_cut=false` when it must stay contiguous
— a reveal or results screen is ruined by jump cuts.

A pinned moment is not transcribed automatically: slice its window from
`work/<vod-id>/audio.m4a`, run `transcribe_words`, remap with `remap_words`,
and write the result back into `moments.json`.

Expect to pin moments on most VODs. The signals find reactions; you find
meaning.

## Verify the render, not the log

The pipeline reports success while producing the wrong thing. Every serious
fault found so far was silent: a clip cache keyed on rank served the
previous run's footage under a new title; a threshold picked by quantile
matched every frame so "multi-cut" returned whole windows untouched; an
ffmpeg killed by the OOM killer left a file that existed, had size, and was
accepted as a finished cache entry; a single-segment plan skipped trimming
and shipped an 85-second clip planned as 27. None of these printed an
error. Watching the log tells you nothing.

After rendering, check each output against what it claimed to be:

- **Duration** against the planned `duration` in moments.json. A large
  overshoot means trimming did not happen.
- **Motion and still fraction** (decode small and greyscale, take the mean
  absolute frame difference). Real gameplay runs well above a static
  screen; a high still fraction on a clip that is not a deliberate results
  screen means the wrong footage.
- **Caption end time** against clip duration. Captions running past the end
  mean the remap onto the cut timeline is wrong.
- **One frame, looked at**, for at least the headline clip.

When an ffmpeg wrapper reports a failure with empty stderr, suspect a
signal rather than a filter error, and re-run the command outside the
output capture to see the real exit code. Exit 137 is the OOM killer.

## Two Twitch facts that shape what is possible

**Highlight VODs carry almost no chat replay.** Chat is attached to the
original broadcast, so a VOD titled "Highlight: ..." will come back with a
fraction of the messages and the chat signal will be useless. Check whether
the VOD is a highlight before concluding the stream was quiet.

**Many channels only publish a source rendition.** `yt-dlp -F` on one VOD
offered just audio and a 5.66 GB 1080p60 source — no 480p, no 360p. Whole-
video analysis is then impossible on a slow connection, which is why
candidates are probed individually and the storyboard is the only cheap
whole-stream view. Check the format list before planning any approach that
needs the video end to end.
