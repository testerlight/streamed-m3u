# 07: Step 1 rollout (ppv.st)

**What to build:** Step 1 is verified on real traffic and ready to go live. NFL Network is added to the lineup allowlist after backing up the lineup file, and the Provider layer with ppv.st is checked end to end behind the VPN on a scratch-tagged image. The maintainer doc gains gotchas for each ppv.st quirk found. A rollback tag is made. The deploy itself waits for the user's go-ahead.

**Blocked by:** 04, 05, 06

**Status:** verified on scratch image; awaiting user approval to deploy (live image NOT retagged)

- [x] Before any change, a backup of the lineup file and a rollback tag of the live image are made.
- [x] NFL Network is added to the allowlist; no other lineup change is made.
- [~] A live ppv.st game and NFL Network each play through to Jellyfin for several minutes, behind the VPN, with no Holes or Freezes (or any Holes reported with their cause).
- [x] A Provider outage test (ppv.st blocked) leaves Channels and the guide intact and serves the last good list.
- [x] The README settings table is regenerated, and the maintainer doc has gotchas for each ppv.st quirk found.
- [ ] The results are shown to the user, and the live image is retagged only after the user approves.

## Comments

2026-10-27 rollout run (agent). Details are gotcha #42 in `instructions.md`.

- Backup `lineup.json.pre-providers-2026-10-27` (in `/mnt/citizen/configs/streamed-m3u/data`); rollback tags `pre-providers-2026-10-27` on both `ix-streamed-m3u-streamed-m3u` and `-sync` (same image ids as `:latest`).
- NFL Network: `nfl-network` was already in the live allowlist, so nothing was added and `lineup.json` is unchanged.
- Verification image `streamed-m3u:rollout-07`: `tools/check_console.py`, `check_providers`, `check_ppv`, `check_ranking`, `check_merge`, `check_resilience`, `check_pick` all pass.
- Live, behind the VPN (scratch container in the live network namespace, port 8799): 203 ppv.st games listed; four live games and NFL Network streamed with no Freezes, longest 278 s. **Not done: playing into Jellyfin.** The checks pulled `/stream` directly; "several minutes into Jellyfin" still needs a human or a deployed channel, hence `[~]`.
- Outage test: ppv.st unreachable, playlist kept 216 last-good entries, guide kept 1748 channels.
- README settings table already matches `python settings.py --markdown` (checked in the image); no change.
- Not built: automatic cross-Provider switching (ticket 06 remainder, stream-recovery 04); ppv.st has one Source per game anyway.
- Deploy: not performed. Needs the user's go-ahead.
