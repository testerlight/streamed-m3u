# 06: Cross-Provider switching and picking

**What to build:** When a Stream hits sustained trouble, automatic switching (ADR 0001) may move it to a Source from another Provider, under the same safeguards: only on sustained trouble, only to a Source pre-checked in the background while the current one keeps playing, at most one switch per 2 minutes, never straight back, logged, with the live off switch. The manual picker can choose a Source from any Provider, and a pin works across Providers.

**Blocked by:** 02, 05

**Status:** manual pick/pin part implemented on provider-swap (awaiting review); automatic switching NOT done

- [ ] (deferred: needs the auto-switcher, stream-recovery 04, which does not exist) With a merged game that has Sources from two Providers, the best-ranked pre-checked Source is chosen as the switch target regardless of Provider.
- [ ] (deferred, same reason) The existing safeguards (sustained trouble, pre-check, rate limit, no immediate return, logging, off switch) still hold when switching crosses Providers.
- [x] A manual pick or pin of a Source from another Provider is honoured and survives a reconnect, falling back as today if it later fails.
- [x] (by the cascade) If one Provider's Sources are all dead, the Stream moves to the other Provider's Sources.
- [x] (pick/pin/cascade only) Checked through the test seam with stubbed extractor behaviour, in the style of the existing health checks.
- [x] Existing checks still pass on a scratch-tagged image.

Scoped 2026-10-27 with the user to the manual picker and pin; the auto-switch criteria move to stream-recovery 04. Found and fixed: a pick was dropped when a merged game's id changed; the 4-attempt cascade could hide the other Provider's Sources. Checked by `tools/check_pick.py` (#41).
