# 10: Step 2 rollout (streamfree)

**What to build:** Step 2 is verified on real traffic and ready to go live. RedZone is added to the lineup allowlist after backing up the lineup file. MLB is verified on a live game, a game listed by both Providers is verified to merge, and Provider fallback is verified. The maintainer doc gains gotchas for each streamfree quirk found. A rollback tag is made, and the deploy waits for the user's go-ahead.

**Blocked by:** 07, 09

**Status:** ready-for-agent (agent does everything up to the deploy; the deploy needs the user's approval)

- [ ] Before any change, a backup of the lineup file and a rollback tag of the live image are made.
- [ ] RedZone is added to the allowlist; no other lineup change is made.
- [ ] A live MLB game plays through to Jellyfin for several minutes behind the VPN with no Holes or Freezes (or any Holes reported with their cause).
- [ ] A game listed by both Providers shows as one game with Sources from each, ranked as agreed.
- [ ] Blocking one Provider mid-test leaves Channels intact, and a playing Stream falls back to the other Provider's Sources.
- [ ] The README settings table is regenerated, and the maintainer doc has gotchas for each streamfree quirk found (two domains, Referer rule, token lifetime, empty Source lists).
- [ ] The results are shown to the user, and the live image is retagged only after the user approves.
