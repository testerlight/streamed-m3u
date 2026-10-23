# Retry a 403'd chunk on a fresh link

Status: ready-for-agent
Blocked by: none (01 and 02 deployed and watched 2026-10-22)

Spec: ../spec.md.

Before declaring a hole for a 403, re-read the playlist for a re-signed URL and retry once on a new connection. Settle the unverified assumption first: log whether the fresh URL differs and succeeds.

## Acceptance

- Off switch; off = today's 2-attempt retry.
- Bounded: one extra try, within the segment timeout budget.
- Tests for success, still-403, and playlist-also-failing.
