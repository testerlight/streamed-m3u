# 08: streamfree Provider (list and schedule)

**What to build:** streamfree games appear in the app with their quality-labelled Sources. Its list returns live and upcoming games with a Source list per game, where quality is part of each Source's link, and the Source list is empty until shortly before the game. Games with no Sources stay in the schedule and gain Sources when they go live. The API and embed hosts differ, and both are handled through the domain settings. The list is requested once per refresh cycle, since the operator asks for caching and light polling.

**Blocked by:** 05

**Status:** ready-for-agent

- [ ] Recorded real streamfree list responses are kept as fixtures and drive all checks offline.
- [ ] Each Source's quality is read from its link and shows in the label (`[SF1-1080p]`).
- [ ] A game with an empty Source list still appears in the guide, and gains Sources on a later refresh.
- [ ] Baseball, football, hockey, soccer, basketball, racing, tennis and cricket categories all map to Channels where the lineup includes them.
- [ ] A game also listed by ppv.st merges into one game (ticket 05) with streamfree ranked first on ties.
- [ ] API and embed hosts are configurable with mirrors; unconfigured domains are never contacted.
- [ ] One list request per refresh cycle.
- [ ] A failed or Violation response uses the last good list (ticket 04).
- [ ] Existing checks still pass on a scratch-tagged image.
