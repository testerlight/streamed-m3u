# 10: Step 2 rollout (streamfree)

**What to build:** Step 2 is verified on real traffic and ready to go live. RedZone is added to the lineup allowlist after backing up the lineup file. MLB is verified on a live game, a game listed by both Providers is verified to merge, and Provider fallback is verified. The maintainer doc gains gotchas for each streamfree quirk found. A rollback tag is made, and the deploy waits for the user's go-ahead.

**Blocked by:** 07, 09

**Status:** deployed 2026-10-28

- [x] Before any change, a backup of the lineup file and a rollback tag of the live image are made.
- [x] RedZone is added to the allowlist; no other lineup change is made.
- [~] A live MLB game plays through to Jellyfin for several minutes behind the VPN with no Holes or Freezes (or any Holes reported with their cause).
- [x] A game listed by both Providers shows as one game with Sources from each, ranked as agreed.
- [x] Blocking one Provider mid-test leaves Channels intact, and a playing Stream falls back to the other Provider's Sources.
- [x] The README settings table is regenerated, and the maintainer doc has gotchas for each streamfree quirk found (two domains, Referer rule, token lifetime, empty Source lists).
- [x] The results are shown to the user, and the live image is retagged only after the user approves.

## Log

2026-10-28 (agent). Verified on scratch image `streamed-m3u:playback-09` (code of f1be99b) in the live network namespace on port 8799, with a copy of the data dir. The live image and the running stream were not touched. Details in gotcha #45.

- Backup `lineup.json.pre-streamfree-2026-10-28`; rollback tags `pre-streamfree-2026-10-28` on both the app and sync images (the Step 1 build, 61479a03590c / f9b8e6d237a4).
- RedZone: `nfl-vs-redzone` was already in the allowlist, so no lineup change was made. streamfree's `redzone` listing resolves to it, with an empty Source list today (no game). Not seen playing.
- MLB: Brewers vs Rays `[SF1-1080p]` through `/stream` for 292 s at 7.4 Mbit/s, first byte 3.8 s, 0 freezes, 0 holes, 0 failed downloads, no browser, until the game's playlist ended. **Not played into Jellyfin by the agent**: the game ended, and no other MLB game was live. The same game and code were watched in Jellyfin by the user under ticket 09. Marked `[~]`.
- Merge: 4 games listed by both Providers show once, SF Sources then PPV.
- Fallback: with streamfree refused mid-stream the Stream ended after 6 playlist failures, the reconnect ran the cascade and played from PPV1 for 37 s with no freezes. The list refresh served the last good list and the playlist kept all 225 entries.
- README settings table regenerated in the image: no difference from the committed table.
- Offline checks pass on the scratch image: streamfree, streamfree_playback, providers, merge, pick, resilience, ranking, console.
- Deploy: not done. Needs the user's approval. `PROVIDERS` already defaults to `streamfree,ppv`, so the new image turns streamfree on with no config change; `PROVIDERS=ppv` turns it off again without a redeploy.

2026-10-28 (agent). Deployed on the user's go-ahead (commit fa00992): compose build + `up -d --no-deps` for streamed-m3u and sync, no active streams at the time, gluetun untouched. Live app serves `# Providers: streamfree, ppv`, 232 entries, 2 merged games; sync cycle completed. Still to confirm by eye: a streamfree game in Jellyfin; RedZone on an NFL Sunday.
