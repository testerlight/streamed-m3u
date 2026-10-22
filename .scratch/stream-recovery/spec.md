# Stream recovery (Phase 3 of stream health)

Status: agreed 2026-10-22 (grilling). Nothing deployed. Builds on `.scratch/stream-health/spec.md`; read its "Lessons from the first attempt" first. Terms: `GLOSSARY.md`. Decision: `docs/adr/0001-automatic-source-switching.md`.

## Why

NFL Network on `admin1-HD`, 2026-10-22: 48 freezes / 224s in 41 min (playlist 78s, source silent 74s, slow downloads 55s, empty chunks 15s), 48 chunks dropped after HTTP 403, with the link at 62 Mbit/s for 8-10 Mbit/s of video. The viewer reports freezes, then audio out of step until re-tuning. Jellyfin transcodes the stream, so a timestamp jump at a hole reaches its ffmpeg.

## Goal

Smooth playback and audio always in step, as long as the provider delivers the bytes. Success, per hour of viewing: at most 1 freeze over 3s and no audio-out-of-step episode the user notices, read from `stream_history.jsonl`. Holes are the provider's fault and are counted separately.

## Agreed decisions

- Play about 30s behind live (setting). Start from the oldest chunk in the playlist window, stay sequential, never jump forward after a stall. The window may cap the cushion; log the real window per source.
- Applies to all channels. Every piece has its own live off switch (setting, no rebuild).
- Ship order, each piece deployed alone and watched on real games before the next: 01 -> 02 -> 03 -> 04. Deploy whether or not anyone is watching, with a rollback tag first.
- Hole: keep byte passthrough but flag a timestamp discontinuity so the player resyncs (not ffmpeg in the path).
- Pad with keepalive null packets while a playlist request hangs (previously declined, now agreed).
- 403: re-read the playlist and retry on the fresh link before declaring a hole.
- Automatic source switch, trigger: source silent 20s+, or 3+ freezes in 2 min, or 3+ holes in the last 20 chunks. Candidate pre-checked with the existing `_check_source` while the current plays; max one switch per 2 min; never straight back; stay put if nothing passes; logged; manual picker kept (ADR 0001).

## Safety (from the first attempt, still binding)

- Every hook is exception-guarded; a bug here must not itself cause a freeze.
- With a switch off, output is byte-identical to the current image, with matching timing.
- Test with messy failures (slow, partial, then fine), not clean ones. Verify inside the image on a scratch tag; never retag the live image.
- New settings go through `settings.py`; README table regenerated with `python settings.py --markdown`; gotchas appended to `instructions.md`.

## Unverified assumptions (ticket 01 must settle the first)

- A TS discontinuity flag at a hole keeps Jellyfin's A/V in step. The audio offset was never measured (the 2026-10-22 capture failed: no cached link, extraction timed out).
- The 403s are dead signed links (inferred from the retry failing 0.1s later).
- NFL Network has only 2 candidate sources; auto-switch may rarely help there.
