# Stream health

Status: Phases 1 and 2 deployed 2026-10-13/14; Phase 3 built 2026-10-14.

Second attempt. The first (2026-10-10) shipped detection and automatic
recovery together with the lineup allowlist, made buffering worse, and was
fully reverted on 2026-10-11. This time it ships in three phases, one at a
time, each running on real games before the next starts.

1. **Show what's happening** (this spec): watch only.
2. **Notice freezes faster**: early warnings, scored, never acted on. See
   "Phase 2" below.
3. **Fix it without dropping the viewer**: backup requests and a quick first
   retry. See "Phase 3" below.

## Lessons from the first attempt

- **Two changes shipped together** (allowlist + feature), so nobody could tell
  which one caused what. Phase 1 ships alone, on top of the code already
  running (master f8ad75d, verified byte-identical to the live image).
- **All three steps shipped at once**, with recovery live from day one and no
  real freeze ever observed first.
- **The fake failures were too clean.** A dead link stayed dead, a new one
  worked instantly. Real failures are slow, partial, then fine again. The
  tests here script messy failures.
- **No baseline.** Nobody measured how often freezes happened before the
  change, so "worse" was a judgement. Phase 1 *is* the baseline.
- **Recovery made hiccups worse**: it threw away downloads that were slow but
  working, then spent 3-20s re-extracting and skipped ~10s.
- **The rollback was messy.** This time the pre-deploy image is tagged first.

## Decisions (grilling, 2026-10-13)

- The allowlist stays out. Its uncommitted changes stay in the main checkout,
  untouched; Phase 1 is built from a separate worktree on branch
  `stream-health-p1`. Known risk the user accepted: a rebuild from the main
  folder drops Phase 1 and ships the allowlist.
- Evidence lives both in the console and in a saved history that survives
  restarts and deploys.
- The user sees every symptom: pauses that resume by themselves, freezes
  that last until the channel is changed, Jellyfin errors / kick-outs, and
  jumps back / replays. Phase 1 must tell these apart.
- Phase 2 starts only after the history has caught at least five real
  freezes and the two of us have read it together.
- Stop and ask before deploying; deploy only when nothing is playing.

## Phase 1 scope

Watch only. **Nothing the proxy sends, and nothing about when it sends it,
may change.** Timeouts, retries, keepalives and the 45s idle limit stay as
they are.

Per stream:

1. **Speed**: video seconds per second of download, per chunk and rolling.
   Below 1.0x the link cannot keep up. Plus link Mbit/s, and the quality
   variant the source offered vs the one taken (the code always takes the
   first: seen 2026-10-13 as 1080p @ 8 Mbit/s with 540p @ 0.7 Mbit/s also on
   offer).
2. **Cushion**: seconds of video sent ahead of a player that started at the
   first byte and plays in real time. Empty for at least 1s counts as a
   freeze, timed until video arrives again. An estimate: Dispatcharr and
   Jellyfin buffer a little, so it errs towards reporting freezes.
3. **Cause of each freeze**: what the loop was doing while the cushion was
   empty: downloading too slowly, a download stalled or failing, the source
   not publishing, playlist errors, or an internal error. Time spent blocked
   sending to a player that is not reading is *not* a freeze.
4. **Jumps**: chunks skipped (sequence gaps not explained by failed
   downloads), the playlist going backwards, discontinuities, and how far
   behind the live edge delivery is.
5. **Why it ended**: client left, console stop, idle timeout, playlist
   failures, fatal HTTP status. Plus whether the same channel reconnected
   within 60s, and whether it replayed chunks already sent.

Surfaces:

- `/stream/status`: each stream gains a `health` object (additive only).
- Console, Active streams: a Health column (Healthy / Struggling / Frozen +
  reason); clicking it shows the stream's timeline.
- Console, new Recent streams section: one row per finished stream, with
  totals by cause.
- `/data/stream_history.jsonl`: one line per finished stream, last 500.
- `GET /api/streams/history`: the same, newest first.
- Log / Events panel: each freeze, swallowed errors (previously silent),
  reconnects, and a summary line when a stream ends.

## Safety requirements

- Every hook is exception-guarded. The stream loop answers any exception
  with `idle(2)`, so an unguarded bug here would itself cause freezes.
- Hooks do no I/O on the stream path; the history file is written from a
  background thread after the stream has ended.
- Proof before deploy: the same scripted messy-failure scenarios run through
  the old image and the new one (and the new one with every hook rigged to
  raise) must produce byte-identical video, with matching delivery timing.

## Out of scope for Phase 1

Any change to detection thresholds, timeouts, retries, cache eviction,
variant choice or reconnect behaviour. Those are Phase 2/3 material and need
Phase 1's data first.

## Phase 2: notice freezes faster (2026-10-14)

### What Phase 1 caught first

Three streams in the history when Phase 2 started (2026-10-13 evening). Two
Rays streams (10 and 20 min) had no freezes. The Warriors stream (67 min,
admin1-HD, 5s chunks, link 275 Mbit/s for 8.9 Mbit/s of video) had four
freezes totalling 54s:

| At | Length | Cause |
|----|--------|-------|
| 3:28 | 18.8s | playlist |
| 31:01 | 14.8s | playlist |
| 42:48 | 1.6s (2 stalls over 6s) | download stalled |
| 52:34 | 18.3s | playlist |

Each playlist freeze was a playlist request hanging for most of the 15s
timeout in `get_segments`, preceded in the log by a chunk skipped because it
left the window while the request hung. During that request `_generate`
sends nothing at all: keepalives only pad chunk downloads.

### Decisions (2026-10-14)

- The gate was five freezes; there were four, three sharing one clear cause.
  The user chose to start Phase 2 now, because it only watches. In exchange,
  Phase 3 waits until Phase 2 has run on real games and its scores have been
  read together.
- Flag only. Keepalives during the playlist request were offered and declined
  for now: that changes what reaches the player, which is Phase 3 material.
- Alerts in the console and the log only; no push notifications.
- Deploy when idle, with a rollback tag, then report.
- The allowlist shipped separately, earlier the same day, with its own
  rollback tags. It does not touch the playback path.

### What it does

A watcher thread ticks every live tracker twice a second and raises:

| Warning | When | Would (Phase 3) |
|---------|------|-----------------|
| `playlist_slow` | a playlist request still running after 4s | ask again on a fresh connection |
| `playlist_failing` | 2 playlist failures in a row | fetch a fresh stream link |
| `download_quiet` | a chunk download silent for 4s | fetch the chunk again on a fresh connection |
| `too_slow` | rolling speed below 1.0x over 3+ chunks | switch to the lower variant, if offered |
| `source_quiet` | polls finding nothing new for max(6s, 2 chunks) | check another source |

plus a freeze alarm once the cushion has been empty for 1s. Every warning is
scored when freezes close: a hit if it was up (or cleared under 20s before)
during a freeze, else a false alarm. Every freeze records `noticed_s`
(negative = warned ahead; null = over before any tick saw it, a miss).

### Phase 3 gate

Phase 2 on real games, then read together: per warning kind, hits against
false alarms and lead time, and how many freezes were missed. A kind with
too many false alarms gets no automatic action. Phase 3 also starts with the
one thing the data already points at: the playlist request that hangs.

### First real game under Phase 2 (2026-10-14, NFL Network, 3h18m)

7 freezes, 34s, all in the last 80 minutes; 6 of them after the playlist
server answered HTTP 500 (13 times). The detector was miscalibrated: every one
of 1,907 playlist requests took ~4.4s, so the fixed 4s `playlist_slow` fired on
each, 196 of the 200 scored warnings were false, and "7 of 7 warned ahead" was
meaningless. Scoring was also capped at 200 warnings. Had Phase 3 been acting
on it, it would have re-requested the playlist ~1,900 times.

Cause of the 4.4s: gluetun's DNS takes ~2s to say the playlist host has no
IPv6 address, paid on each of the two fresh requests per poll (instructions
gotcha #30).

Decisions (user, 2026-10-14):

- Fix Phase 2: `playlist_slow` relative to the stream's typical request
  (+3s, fixed 4s until 10 seen), whole-stream counts, once-a-minute logging,
  the alarm's remedy follows what is stuck.
- Force IPv4 on stream requests (playlist, probe chunk, chunk downloads) now,
  despite being Phase 3 material: it changes no bytes, only how long a DNS
  lookup takes, and has a live off switch (`UPSTREAM_IPV4`). Catalog and logo
  requests left alone. Deployed together with the Phase 2 fix, when idle.
- Live A/B before deploy, same stream, 90s each: playlist polls 0.54s (IPv4)
  vs 4.37s; cushion ~22s vs ~14.5s; no freezes either way, no TS breaks.

The Phase 3 gate is unchanged. The next games will show what freezes remain
once polls are fast; the playlist 500s are the candidate after that.

## Phase 3: fix it without dropping the viewer (2026-10-14)

### Evidence it was built on

After the IPv4 change: four streams, ~75 minutes, playlist polls 0.53s.

- lask, 14:33: one chunk download went silent until its 30s timeout. 18.6s
  freeze, 5 chunks (16s) skipped. `download_quiet` warned 6s ahead; the
  automatic retry then fetched the same chunk in 0.17s.
- lask, 13:34: playlist request hung, then HTTP 500 twice; 2s waits between
  retries. 5.5s freeze. Same pattern as 6 of NFL Network's 7 freezes.
- Scores: `playlist_slow` 2/2 hits, `playlist_failing` 2/2, `download_quiet`
  1/5, `source_quiet` 1/33, `too_slow` 0/1.

### Decisions (user, 2026-10-14)

- Fixes: backup chunk request, backup playlist request, quick first retry.
  Not: keepalives during playlist polls (no video, low value at 0.5s), lower
  quality or another source (their warnings scored 0/1 and 1/33, and switching
  mid-stream is what made the first attempt worse).
- Shipped together, each with its own live off switch and its outcomes
  recorded per stream, so each one's effect is measured directly.
- Gate accepted as met on the evidence above. Deploy when idle.

### What it does

| Fix | When | Setting |
|-----|------|---------|
| Backup chunk request | download received nothing for 4s | `CHUNK_BACKUP_AFTER` (0 = off) |
| Backup playlist request | 3s past the stream's median request (4s until ten seen) | `PLAYLIST_BACKUP_AFTER` (0 = off) |
| Quick first retry | first playlist error in a row: 0.5s, not 2s | `PLAYLIST_RETRY_DELAY` (2 = off) |

Rules: a request is added, never replaced; the first complete answer wins; if
both fail the original's failure is what counts; a failed quick retry does not
count towards giving up, and puts the schedule back on the old 2s rhythm.

### Proof before deploy

- Every scenario: chunks intact and in order. Where no fix fires, byte-identical
  and timing-identical to the live image. Where one fires: never a chunk
  missing that the old code delivered, never later by more than one poll, up
  to 21s earlier (chunkhang: 25s stall froze 2.1s instead, and the 5 chunks the
  old code skipped were delivered). Every fix off: identical to the live image.
- Slow-but-flowing downloads, a quiet source and a steady stream fire nothing.
- Real stream (NFL Network, 3 min): no freezes, no fix needed, playlist polls
  0.54s.

### Found on the way, not fixed here

Some chunks carry a 72-byte `RIFF....WEBPVP8X` wrapper that reaches the player
unstripped (2-6 per 150s on NFL Network). The live image does the same, so it
predates Phase 3. Worth its own look: `_strip_ts_prefix` should catch it.
