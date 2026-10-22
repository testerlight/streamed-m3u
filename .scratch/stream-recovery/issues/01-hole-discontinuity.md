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
