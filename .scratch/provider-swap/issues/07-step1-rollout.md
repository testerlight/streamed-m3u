# 07: Step 1 rollout (ppv.st)

**What to build:** Step 1 is verified on real traffic and ready to go live. NFL Network is added to the lineup allowlist after backing up the lineup file, and the Provider layer with ppv.st is checked end to end behind the VPN on a scratch-tagged image. The maintainer doc gains gotchas for each ppv.st quirk found. A rollback tag is made. The deploy itself waits for the user's go-ahead.

**Blocked by:** 04, 05, 06

**Status:** ready-for-agent (agent does everything up to the deploy; the deploy needs the user's approval)

- [ ] Before any change, a backup of the lineup file and a rollback tag of the live image are made.
- [ ] NFL Network is added to the allowlist; no other lineup change is made.
- [ ] A live ppv.st game and NFL Network each play through to Jellyfin for several minutes, behind the VPN, with no Holes or Freezes (or any Holes reported with their cause).
- [ ] A Provider outage test (ppv.st blocked) leaves Channels and the guide intact and serves the last good list.
- [ ] The README settings table is regenerated, and the maintainer doc has gotchas for each ppv.st quirk found.
- [ ] The results are shown to the user, and the live image is retagged only after the user approves.
