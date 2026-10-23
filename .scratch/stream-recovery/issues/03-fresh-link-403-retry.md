# Retry a 403'd chunk on a fresh link

Status: built, not deployed (live question still open, see Answer)
Blocked by: none (01 and 02 deployed and watched 2026-10-22)

Spec: ../spec.md.

Before declaring a hole for a 403, re-read the playlist for a re-signed URL and retry once on a new connection. Settle the unverified assumption first: log whether the fresh URL differs and succeeds.

## Acceptance

- Off switch; off = today's 2-attempt retry.
- Bounded: one extra try, within the segment timeout budget.
- Tests for success, still-403, and playlist-also-failing.

## Answer

Question: does a re-read playlist ever list a different, working URL for the same chunk? **The live behaviour is not settled; offline evidence only (container logs of 2026-10-22 22:10 to 2026-10-23 04:09 UTC, 14 stream history lines, the code). It is built to be safe under either answer and to settle the question itself: the first real game fills `fresh_link` in stream health.**

What the 6h of logs say:

- 1,060 `Segment HTTP 403` warnings = **530 distinct chunks x 2 attempts**, nothing but 403 (no 404/500/timeouts/short reads). Attempt 2 is the same URL as attempt 1 (as the code retries the URL it has).
- All on `p19-common-sign.tiktokcdn.com`, three buckets in equal shares (`tos-alisg-v-4896` 191, `tos-alisg-v-af8c41-sg` 172, `tos-alisg-i-50187-sg` 167), `idc=my` 330 / `my2` 200. Each URL has its own `refresh_token`; `dr`, `ps`, `shp`, `shcp` are constant. `x-expires` is on the hour, 1 to 6h after the fetch (never expired). Per hour: 56, 81, 116, 108, 91, 78.
- **No object path, hence no URL, was refused more than once** (530 of 530 distinct). Today a re-signed link is a *different* dedup key (`_segment_key` strips `x-amz-*` and `token`/`signature`/`sig`/`expires`/`hmac`/`md5`/`hash`, **not** `x-signature` or `x-expires`), so a dropped chunk whose playlist entry was re-signed within the window would have been fetched again as "new": it would have shown up as a second 403 on the same path, or as a late delivery. No second 403 ever did. So refused chunks are overwhelmingly listed with the same link on every later poll (the chunk stays 4-5 polls in the window).
- But the late-delivery half did happen: 5 `Sent an older chunk` events in the 4.3h Devils stream (445790 after 445790, 446064 after 446064, 446387 after 446387, 446130 after 446131, 447850 after 447853), each 9 to 59s after a drop. The playlist did list a second link, under a new key, for a sequence the stream had already handled, and fetching it worked. The log line carries no URL, so whether any of the five was a chunk that had been refused is not provable. At most 5 of 439 refused chunks (1.1%), 0.13% of all chunks.
- Refusals are new with 2026-10-22: the 10-14 to 10-20 history lines have 0 to 1 `downloads_failed`; the five 10-22 lines have 48/444, 63/543, 36/380, 91/995, 439/3962 (9.1 to 11.6%).

So: expect `same_url` to dominate and `url_differed` to be a small minority at best. An immediate re-read cannot see a link re-signed 20 or 30s later (the late deliveries above came that late); if `url_differed` is near 0 and `Sent an older chunk` goes on, try a delayed re-read next. Why the CDN refuses 11% of links that look valid is unknown (not an expiry).

## Built, not deployed (2026-10-23)

`FRESH_LINK_RETRY` (live, on by default; gotcha #36). After a chunk's last attempt is a 403: re-read the playlist on a new connection (bounded), find the chunk by media sequence, one more attempt only if the listed URL differs; then the hole as before. Extra work capped at one `SEGMENT_TIMEOUT` per stream per minute, padded by the existing keepalive wrapper, new URL marked seen so the dedup does not fetch it again. Counters in stream health (`attempted`, `url_differed`, `succeeded`, `same_url`, `not_listed`, `reread_failed`, `retry_failed`, `skipped`) and per-attempt list in the history line; stats drop-down row "Fresh links (403)". Tests: `tools/check_freshlink.py`.

Verified in `streamed-m3u:verify-403` (scratch tag, nothing live touched): `check_freshlink.py` 85 checks (403 then fresh URL works; identical URL, no retry; re-read 500, hung, slow; three in a row with and without restamp; first chunk of a start; fresh URL 404 or empty; switch off; hooks and helpers rigged to raise; no padding; idle limit after a recovery); switch off byte-identical to the running image with matching timing (median 0.03s) and `clean` with the switch on likewise; `check_health.py` 192 and `--compare` identical; `check_console.py`, `check_multiview.py` (MULTIVIEW_ENABLE=1), `check_restamp.py` (53), `check_cushion.py` (68), `check_health_render.py` pass.

Deploy checklist (user's call): rollback tag first; after the first game read `fresh_link` in the stats drop-down / `stream_history.jsonl` (`url_differed`, `succeeded`, `fresh_link_list`) and record the live answer here.
