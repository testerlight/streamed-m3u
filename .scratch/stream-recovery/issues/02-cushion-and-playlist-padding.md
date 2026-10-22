# 30s cushion and padding during playlist waits

Status: ready-for-agent
Blocked by: none (01 resolved and watched on real games 2026-10-22)

Spec: ../spec.md. Gotcha #20 (keepalive), #30 (IPv4).

Start at the oldest chunk in the window (up to a behind-live setting, default 30s), stay sequential after stalls, and send null-packet keepalives while a playlist request is running.

## Acceptance

- Settings: behind-live seconds (0 = today's behaviour), padding switch.
- Log and record the achieved cushion and the source's playlist window.
- Scripted messy-failure scenarios (playlist 500 for 10s, source silent 12s) show fewer or shorter freezes than the old image.
- Off = byte-identical to today.

## Built, not deployed (2026-10-22)

Commit 918f5d6: BEHIND_LIVE_S (cap on the start, default 30) and PLAYLIST_KEEPALIVE (padding during playlist requests), gotcha #35, tools/check_cushion.py (68 checks) plus check_health 192, console, multi-view, restamp all passing in streamed-m3u:verify-cushion; output byte-identical to the live image with both switches off.

Finding: the real playlist window on current sources is only about 12-20s, so the cap changes nothing there (stream_ts already starts at the oldest chunk of the window). Padding stops Dispatcharr seeing silence but does not shorten freezes. A real 30s cushion needs the proxy to HOLD the start while chunks publish (about +13s to first picture, chunks held in memory) - not built, awaiting the user decision.
