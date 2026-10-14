# Stream health

Status: ready-for-agent (Phase 1 only)

Second attempt. The first (2026-10-10) shipped detection and automatic
recovery together with the lineup allowlist, made buffering worse, and was
fully reverted on 2026-10-11. This time it ships in three phases, one at a
time, each running on real games before the next starts.

1. **Show what's happening** (this spec): watch only.
2. **Notice freezes faster**: not designed yet. Its thresholds come from
   Phase 1's data.
3. **Fix it without dropping the viewer**: not designed yet. Also depends on
   Phase 1's data; if freezes turn out to be mostly VPN slowness, the right fix
   may not be in this code at all.

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
