# Provider swap, Step 1: Provider layer + ppv.st

Status: plan for review. Nothing built or deployed. Decisions come from the 2026-10-27 grilling; see `docs/adr/0002-multiple-providers.md` and the **Provider** entry in `GLOSSARY.md`.

## Goal

Get the app working again (streamed.pk is closed) by introducing a Provider layer and enabling ppv.st through it. streamed.pk stays in the code, switched off. streamfree is Step 2.

## Settled decisions

- Providers: streamfree primary, ppv.st secondary, streamed.pk off. A `PROVIDERS` setting lists the enabled ones in order.
- Base URLs are settings with an ordered mirror list; unknown domains are never adopted. Embed hosts come from each API response.
- Same game on two Providers is merged by team slug plus start within ~3 h; unsure means separate.
- Ranking: quality, then verified audio, then Provider order. ppv.st has no quality label: unknown, ranked just below a labelled 1080p. HD means 720p or above, and for ppv.st only when the probe sees it.
- Labels: `[SF1-1080p]`, `[PPV1]`.
- Games appear in the guide as soon as any Provider lists them.
- A failed list call keeps that Provider's last good list for a few hours and never removes or rebuilds Channels.
- Automatic switching (ADR 0001) may cross Providers under the same safeguards.
- Prewarming stays, serial and light.
- Dispatcharr names stay "streamed.pk teams".
- Allowlist: add NFL Network in Step 1; RedZone in Step 2 (streamfree only). Back up `lineup.json` first.

## Code touch points (current `app.py`)

| Area | Where | Change |
|---|---|---|
| Fetching | `_get` (1201), `fetch_sports` (1214), `fetch_all_matches` (1220), `fetch_streams_for_match` (1246) | Move behind a Provider interface; streamed.pk code becomes one implementation; add a ppv.st one (single `/api/streams` call, `iframe` field) |
| Source model | `ALL_SOURCES` (485), `SOURCE_ABBREV`, `format_channel_name` | Replace letter scheme with Provider + number + quality labels |
| Ranking | `_rank_streams` (1520), merge/sort (1480-1495) | Quality, audio, Provider order |
| Embed URL field | `embedUrl` reads at ~1886, 2002, 2202 | Normalise to one `embed_url` field in the Provider adapter |
| Source labels / picker | `_source_label` (2107), `_stream_source_view` (4000) | Show Provider in labels |
| Logos | `BASE_URL` poster join (2224), image proxy (~4090), VPN note (513) | Allow ppv.st poster host; keep proxying |
| Settings | `settings.py`, README table | Add `PROVIDERS`, per-Provider base URLs; regenerate the README table with `python settings.py --markdown` |
| Playlist header | `# Source: {BASE_URL}` (2180) | List enabled Providers |

## Work items

1. Add `PROVIDERS` and base URL settings; default order `streamfree,ppv`, with streamfree inert until Step 2.
2. Define the Provider interface (list games, list a game's Sources) and wrap the existing streamed.pk code as one implementation, behaviour unchanged.
3. Write the ppv.st Provider: parse `/api/streams`, map `substreams` to extra Sources, convert `starts_at`/`ends_at`, tag 24/7 entries as feeds.
4. Team matching for ppv.st titles ("Away at Home"): confirm they resolve through the existing team index; add NFL Network as a feed.
5. Last-good-list cache per Provider with a time limit; guard the sync so a failed fetch changes nothing.
6. Cross-Provider merge (slug + start window) — in place for Step 1 even though only one Provider is live, so Step 2 only adds the adapter.
7. Update ranking, labels, picker view and logo proxy as above.
8. Add NFL Network to the allowlist after backing up `lineup.json`.

## Verification (scratch tag only)

- `tools/check_console.py` inside a scratch-tagged image; never retag `ix-streamed-m3u-streamed-m3u:latest`.
- A live ppv.st game and NFL Network played through to Jellyfin for several minutes, behind the VPN, with no Holes or Freezes.
- Provider outage test: block ppv.st, confirm Channels and the guide stay intact and the last good list is served.
- Rollback tag before deploy: `pre-providers-2026-10-27` (adjust to the deploy date).

## Risks

- ppv.st chunks are WebP-wrapped and come from signed TikTok CDN links; gotcha #17 and the fresh-link retry should cover them but need re-testing.
- Only one Source per game on ppv.st: cross-Provider switching has nothing to switch to until Step 2.
- No MLB until Step 2.
- Current branch is `stream-recovery`; create a fresh branch for this work.

## Open before building

- Confirm the exact allowlist slug for NFL Network.
- Confirm ppv.st team names resolve through the existing alias list.
