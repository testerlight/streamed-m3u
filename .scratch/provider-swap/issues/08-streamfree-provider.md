# 08: streamfree Provider (list and schedule)

**What to build:** streamfree games appear in the app with their quality-labelled Sources. Its list returns live and upcoming games with a Source list per game, where quality is part of each Source's link, and the Source list is empty until shortly before the game. Games with no Sources stay in the schedule and gain Sources when they go live. The API and embed hosts differ, and both are handled through the domain settings. The list is requested once per refresh cycle, since the operator asks for caching and light polling.

**Blocked by:** 05

**Status:** implemented on provider-swap (awaiting review)

- [x] Recorded real streamfree list responses are kept as fixtures and drive all checks offline.
- [x] Each Source's quality is read from its link and shows in the label (`[SF1-1080p]`).
- [x] A game with an empty Source list still appears in the guide, and gains Sources on a later refresh.
- [x] Baseball, football, hockey, soccer, basketball, racing, tennis and cricket categories all map to Channels where the lineup includes them.
- [x] A game also listed by ppv.st merges into one game (ticket 05) with streamfree ranked first on ties.
- [x] API and embed hosts are configurable with mirrors; unconfigured domains are never contacted.
- [x] One list request per refresh cycle.
- [x] A failed or Violation response uses the last good list (ticket 04).
- [x] Existing checks still pass on a scratch-tagged image.

## Comments

2026-10-28 (agent). `providers.Streamfree`, gotcha #43. Uses `GET /api/v1/streams` (one call per refresh); only `live_sources` become Sources; quality is read from the link suffix. API and embed hosts are separate settings (`STREAMFREE_BASE_URL`/`_MIRRORS`, `STREAMFREE_EMBED_BASE_URL`/`_MIRRORS`); an embed link on an unconfigured host is moved onto the configured one. Checked offline by `tools/check_streamfree.py` (recorded list, merged with the recorded ppv.st list) and on scratch image `streamed-m3u:streamfree-08`: check_streamfree, ppv, providers, ranking, merge, resilience, pick, console, reorder, cushion, freshlink, health, render all pass. Live image not touched. Playback is ticket 09.
