# Mark a timestamp break at each hole

Status: ready-for-agent
Blocked by: none

Spec: ../spec.md. Gotcha #31 (empty chunks dropped), #29 (health hooks), #16/#20.

When a chunk is dropped as a hole (403, empty, left the window), set the TS discontinuity indicator on the first packets of the next delivered chunk so the player resyncs audio and video. Passthrough otherwise untouched.

## Acceptance

- First reproduce: build a TS stream with a missing chunk, run it through ffmpeg as Jellyfin does, and measure the A/V offset after the hole. Record the result in this ticket. If the flag does not fix it, stop and report; do not widen scope.
- Off switch setting; off = byte-identical output.
- Hook exception-guarded; unit test with a rigged raise.
- Holes still counted by the tracker as today.
- instructions.md gotcha added, settings table regenerated.

## Answer

Measured 2026-10-22 with `tools/check_hole_av.py` (inside a scratch image's ffmpeg 7.1.5, synthetic source; nothing live). Source: picture flashes and audio beeps together every 2s, H.264 + AAC, cut into 6s chunks at keyframes. Chunks are dropped, the rest concatenated, and the result transcoded six ways the way Jellyfin might (plain mpegts, `-copyts`, hls with `-fps_mode cfr`, hls + `-copyts`, hls + `igndts`, hls + `aresample=async`). The table is the beep-minus-flash offset after the hole; a control with no hole reads 0.02-0.04s in every mode (hls + `-copyts` starts at -1.44s even with no hole, so subtract that).

| Hole | Plain, `-copyts`, hls cfr, hls async | hls + `-copyts` | hls + `igndts` |
|------|----------------------------------|-----------------|----------------|
| 1 chunk (6s) | in step | in step | in step |
| 2 chunks (12s) | in step | **-13.4s** (audio 12s ahead of picture) | **-12.0s** |
| 3 chunks (18s) | in step | **-19.4s** | **-18.0s** |

- A single dropped chunk does not put audio out of step in any mode. A run of holes longer than ffmpeg's 10s timestamp-jump threshold does, in the modes that keep input timestamps (`-copyts`, `igndts`): the offset equals the length of the hole and stays until the input is reopened, which is "out of step until re-tuning".
- **The discontinuity flag does not fix it.** Adding a `discontinuity_indicator` adaptation-only packet on each elementary stream before the first chunk after the hole leaves every number above unchanged (identical offsets, flag vs no flag, at 6, 12 and 18s). ffmpeg decides from the size of the timestamp jump and does not read the flag. The flag only moved the demuxer's "corrupt packet" warnings from 23 to 21 lines (these also occur with no hole: HLS chunk ends).
- Chunk continuity counters restart at every HLS chunk anyway (gotcha #36), so a hole shows ffmpeg nothing except the timestamp jump.
- **What does fix it**: closing the jump on our side. Shifting every PTS, DTS and PCR in the chunks after a hole back by the hole's length (`RESTAMP=1` in the tool) leaves a continuous timeline: after 18s of holes the offset is 0.03-0.04s in every mode (the -1.45 in hls + `-copyts` is the same start-up artifact as the control). That is a rewrite of the bytes at PES-start and PCR packets (a few percent of packets), not byte passthrough, so it departs from the "flag, keep passthrough" decision and needs the user's call.

Stopped here per the ticket: the flag was not implemented in `app.py`. Caveats: synthetic content and a guessed Jellyfin command line (real sources' chunk boundaries, B-frames and audio alignment may differ); the 2026-10-22 NFL Network holes were mostly single 403 chunks, which this says are harmless, so the real offset episodes need consecutive holes over 10s or a different trigger. Next step: a capture of the real stream (or Jellyfin's actual ffmpeg line from its transcode log) through the same tool.
