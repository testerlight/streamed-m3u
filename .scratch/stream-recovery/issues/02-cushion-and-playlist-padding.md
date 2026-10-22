# 30s cushion and padding during playlist waits

Status: ready-for-agent
Blocked by: 01

Spec: ../spec.md. Gotcha #20 (keepalive), #30 (IPv4).

Start at the oldest chunk in the window (up to a behind-live setting, default 30s), stay sequential after stalls, and send null-packet keepalives while a playlist request is running.

## Acceptance

- Settings: behind-live seconds (0 = today's behaviour), padding switch.
- Log and record the achieved cushion and the source's playlist window.
- Scripted messy-failure scenarios (playlist 500 for 10s, source silent 12s) show fewer or shorter freezes than the old image.
- Off = byte-identical to today.
