Status: ready-for-agent

# Provider swap: streamed.pk to ppv.st and streamfree

Agreed 2026-10-27 (grilling). Terms: `GLOSSARY.md` (Provider, Source, Channel, Stream, Hole, Freeze, Recovery, Pin). Decision: `docs/adr/0002-multiple-providers.md`. Related: `docs/adr/0001-automatic-source-switching.md`. Working notes: `.scratch/provider-swap/step1-plan.md`.

## Problem Statement

streamed.pk closed in October 2026. Every game and every Source came from it, so the app now finds nothing to play: Channels are dark, the guide is empty, and Jellyfin has nothing to tune to. The only fix that doesn't leave the same single point of failure is to take games and Sources from more than one Provider, and the replacement Providers differ from streamed.pk in how they list games, how many Sources they offer, whether they label quality, and how their video is delivered.

## Solution

The app takes games and Sources from several Providers at once. ppv.st and streamfree are the new ones; streamed.pk stays in the code, switched off. A game listed by more than one Provider becomes one game on one Channel, with every Provider's Sources in a single ranked list. Viewers keep the same stable Channels they have today. Playback, the source picker and automatic switching behave as before, and now can fall back to another Provider when one fails.

It ships in two steps, each with its own rollback tag: Step 1 is the Provider layer plus ppv.st, which gets the app working again fastest. Step 2 adds streamfree, MLB and RedZone.

## User Stories

1. As a viewer, I want my team's Channel to play again, so that the app is useful after streamed.pk closed.
2. As a viewer, I want a game to appear in the guide as soon as any Provider lists it, so that I can see what's coming even before a Source is live.
3. As a viewer, I want a Channel to start playing as soon as a Source appears for its game, so that I don't need to do anything when the game goes live.
4. As a viewer, I want one game to be one entry even when two Providers list it, so that the guide has no duplicates.
5. As a viewer, I want the best-quality working Source chosen by default, so that I get 1080p when it exists.
6. As a viewer, I want a Source with no audio to be skipped, so that I never land on a silent picture by default.
7. As a viewer, I want a stream to carry on when its Source fails, by switching to another Source, even from another Provider, so that a Provider outage doesn't end my viewing.
8. As a viewer, I want switching to follow the existing safeguards (sustained trouble, a pre-checked target, rate limited, never straight back), so that it doesn't make playback worse.
9. As a viewer, I want to see in the stats drop-down which Provider and quality each Source is, so that I can pick one deliberately.
10. As a viewer, I want to pin a Source from any Provider, so that I can stay on one I trust for the game.
11. As a viewer, I want NFL Network available as a Channel after Step 1, so that I have a 24/7 NFL feed.
12. As a viewer, I want MLB games and RedZone available after Step 2, so that baseball and the NFL red zone feed return.
13. As a viewer, I want Channel names and ids to stay the same as today, so that my Jellyfin library, favourites and history are not disturbed.
14. As the operator, I want to choose which Providers are enabled and in what order with a setting, so that I can turn one off without a rebuild.
15. As the operator, I want streamed.pk kept but off by default, so that old code can be removed later without losing rollback.
16. As the operator, I want each Provider's base URL and mirrors in settings, so that a moved domain is a config change.
17. As the operator, I want the app never to trust a domain I haven't configured, so that parked or hijacked lookalike domains can't feed it.
18. As the operator, I want embed hosts to come from each Provider's own response, so that an embed host change needs no code change.
19. As the operator, I want a failed list call to keep that Provider's last good list for a few hours, so that a blip doesn't empty the guide.
20. As the operator, I want a failed fetch never to remove or rebuild Channels, so that an outage degrades coverage only.
21. As the operator, I want Provider failures and 403/Violation answers logged and shown in the dashboard, so that I can tell which Provider is unhealthy.
22. As the operator, I want requests to streamfree kept minimal (one list call per refresh cycle, embed pages fetched for prewarm and tune-in only), so that I stay within the operator's request and avoid a block.
23. As the operator, I want prewarming to stay serial and light, so that it does not load the box or the Providers.
24. As the operator, I want the lineup allowlist unchanged apart from NFL Network (Step 1) and RedZone (Step 2), with a backup of the lineup file first, so that nothing is added automatically.
25. As the operator, I want the Dispatcharr account and EPG names left as they are, so that nothing is duplicated or remapped.
26. As the operator, I want each step to have a rollback tag and to be built and checked on a scratch tag first, so that the live image is never retagged during verification.
27. As the operator, I want the README settings table regenerated from the settings code, so that docs match the app.
28. As the operator, I want the maintainer doc to gain gotchas for each new Provider quirk, so that the next person doesn't redo the debugging.
29. As the maintainer, I want a game from two Providers merged by team slug plus a start time within about 3 hours, so that merging is predictable.
30. As the maintainer, I want an unsure match left separate rather than merged, so that a wrong merge never puts the wrong video on a Channel.
31. As the maintainer, I want ppv.st's "Away at Home" titles to resolve through the existing team index and aliases, so that their games land on the right Channels.
32. As the maintainer, I want ppv.st substreams treated as extra Sources for the same game, so that Recovery has something to switch to.
33. As the maintainer, I want ppv.st Sources with no quality label ranked as unknown quality just below a labelled 1080p, so that ranking stays sensible.
34. As the maintainer, I want HD to mean 720p or above, and for ppv.st only when the startup probe sees it, so that the HD badge is truthful.
35. As the maintainer, I want Source labels like `[SF1-1080p]` and `[PPV1]`, so that the Provider and quality are visible everywhere a label shows.
36. As the maintainer, I want always-live feeds recognised from each Provider's own flags, so that feeds like NFL Network need no title tricks.
37. As the maintainer, I want streamfree games listed with no Sources to appear in the schedule and gain Sources when they go live, so that the guide stays current.
38. As the maintainer, I want streamfree playback to use a lightweight HTTP fetch of the embed page and its signed playlist token, so that no browser is needed per tune-in.
39. As the maintainer, I want the browser extractor kept as a streamfree fallback that loads the page with no Referer, so that a page redesign doesn't end streamfree.
40. As the maintainer, I want streamfree playlists requested with the embed page as the referrer, so that the player-like request is accepted.
41. As the maintainer, I want ppv.st playback to keep using the existing extractor, so that Step 1 reuses what is already proven.
42. As the maintainer, I want ppv.st's WebP-wrapped chunks and TikTok-signed links handled by the existing strip and fresh-link logic, so that nothing new is needed there.
43. As the maintainer, I want poster images from the new Providers served through the existing image proxy, so that Dispatcharr never contacts them directly.
44. As the maintainer, I want the playlist header to list the enabled Providers, so that the output says where its games came from.
45. As the maintainer, I want all Provider fetches to leave through the VPN, so that behaviour matches how the app runs today.
46. As the maintainer, I want tests to assert only external behaviour through the existing test client, so that refactors don't break them.
47. As the maintainer, I want the real ppv.st and streamfree responses recorded as fixtures, so that tests run offline and repeatably.

## Implementation Decisions

- **Provider layer.** A Provider is a module that can list games and list a game's Sources, and returns both in one common shape. The rest of the app (matching, ranking, labels, picker, Recovery, sync) only sees that shape and does not know which Provider a game came from. streamed.pk becomes one Provider, behaviour unchanged.
- **Enablement.** A setting lists enabled Providers in preference order. Default: streamfree, ppv.st; streamed.pk off. In Step 1 streamfree is listed but inert.
- **Domains.** Each Provider has a base URL and an ordered mirror list as settings. Mirrors are tried in order. No domain that isn't configured is ever contacted. Embed hosts are taken from each Provider's response, never hard-coded.
- **ppv.st Provider.** One list call returns all games by sport, with an embed URL, start and end Unix times, an always-live flag, and optional substreams. Substreams become additional Sources for the same game. Always-live entries are feeds. No quality label is provided.
- **streamfree Provider (Step 2).** The list call returns live and upcoming games with a Source list per game (quality is part of each Source's link); the Source list is empty until shortly before the game. API and embed hosts differ. Games with no Sources stay in the schedule and gain Sources when they go live.
- **Extraction.** ppv.st uses the existing browser extractor. streamfree fetches the embed page (no Referer), reads the signed playlist token from it (valid about 8 hours), requests the playlist with the embed page as referrer, and returns the same result shape as the browser extractor. The browser extractor is the fallback and must load the page with no Referer, because a streamfree Referer returns a 403 Violation.
- **Matching across Providers.** Games merge by team slug from the existing team index plus a start time within about 3 hours. If unsure, keep separate. Title shapes differ ("Away at Home" versus "X vs Y"); always-live feeds match by name or alias.
- **Ranking.** Quality first (1080p, 720p, 540p), then verified audio, then Provider preference as the tiebreak. Sources with no quality label rank as unknown, just below a labelled 1080p. HD means 720p or above; for ppv.st only when the startup probe observes it. This replaces the old per-source order.
- **Labels.** Provider code, number and quality (`[SF1-1080p]`, `[PPV1]`), replacing the letter scheme. Labels appear in Dispatcharr, the console and the stats drop-down.
- **Guide.** A game appears as soon as any Provider lists it, with or without a Source.
- **Failure handling.** Each Provider keeps its last good game list for a limited time. A failed list call serves that list and is logged and surfaced. The sync never changes Channels when it cannot read its inputs, as with the lineup rule today.
- **Recovery.** Automatic switching under ADR 0001 may cross Providers with the same safeguards; the manual picker may pick any Provider's Source.
- **Politeness.** One list call per Provider per refresh cycle. Embed pages are fetched only for prewarming and tune-in; prewarming stays serial and light. Prewarm does not run against Sources that don't exist yet.
- **Images.** Posters from the new Providers go through the existing image proxy and cache.
- **Lineup.** NFL Network is added in Step 1 and RedZone in Step 2, each after backing up the lineup file. Nothing else is added automatically.
- **Naming.** The Dispatcharr account and EPG names are unchanged.
- **Rollout.** Two deploys, each with a rollback tag, built and checked on a scratch tag; the live image tag is only changed after results are reviewed with the user.
- **Docs.** README settings table regenerated; maintainer doc gains gotchas for each Provider quirk; GLOSSARY and ADR 0002 are already written.

## Testing Decisions

- A good test asserts only external behaviour: what the served playlist contains, what `/stream` returns, and what happens when a Provider fetch fails. It does not call internal functions or depend on how the Provider layer is organised.
- One seam: the Flask test client run inside a scratch-tagged image, in the same style as the existing console and health checks, with Provider HTTP replaced by recorded real responses and the browser extractor stubbed.
- Behaviours to cover through that seam: ppv.st games and substreams appear as one game with the right Sources; labels and quality ranking; unlabelled quality ordering; a game listed by two Providers merges, and an uncertain pair stays separate; a Provider outage serves the last good list and leaves Channels intact; the guide shows games with no Source; NFL Network resolves as a feed; cross-Provider switching target selection; streamfree token parsing and playlist request via a stubbed HTTP layer (Step 2).
- Real playback verification, behind the VPN on a scratch tag: a live game on each Provider plays through to Jellyfin for several minutes with no Holes or Freezes, a game on both Providers merges correctly, and a Provider outage test leaves Channels intact.
- Prior art: `tools/check_console.py`, `tools/check_health.py`, `tools/check_freshlink.py` (stubbing the extractor, test client, scratch image).
- Fixtures: record real ppv.st and streamfree list responses (and one streamfree embed page) and keep them with the checks.

## Out of Scope

- Deleting the streamed.pk code.
- Renaming the Dispatcharr account or EPG source.
- Adding any other 24/7 feeds, cartoons or racing channels to the lineup.
- Auto-discovering Provider mirrors.
- Any change to Phase 3 recovery logic beyond allowing Sources from other Providers.
- A soak test of a full game (can be added later).
- Any Provider other than streamed.pk, ppv.st and streamfree.

## Further Notes

- ppv.st (checked 2026-10-27): about 200 games; mostly soccer, NFL, college football, NBA, hockey; no MLB. Usually one Source per game. Chunks arrive WebP-wrapped from signed TikTok CDN links.
- streamfree (checked 2026-10-27): 37 games listed, 6 with Sources; 1080p H.264/AAC MPEG-TS chunks from one host; tokens last about 8 hours; its embed server answers a request with a streamfree Referer with a 403 "Violation" message. Operator asks for caching and light polling.
- Lookalike domains (streamfree.st, strmfree.top, streamfree.cc) are parked or unrelated; do not use them.
- The server's resolver needed streamfree.top and strmfree.st allowed in Pi-hole; the app container resolves them through the VPN.
- Open before Step 1: the exact allowlist slug for NFL Network; whether ppv.st team names resolve through the existing aliases.
- The work is on branch `provider-swap`, branched from `stream-recovery`.
