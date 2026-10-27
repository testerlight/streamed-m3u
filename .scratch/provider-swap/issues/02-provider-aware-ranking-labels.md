# 02: Provider-aware Source ranking and labels

**What to build:** A Channel's Sources are ranked by quality (1080p, then 720p, then 540p), then verified audio, then Provider preference as the tiebreak, replacing the old per-source order. A Source with no quality label ranks as unknown, just below a labelled 1080p. Labels read like `[SF1-1080p]` and `[PPV1]`, wherever a label shows (Dispatcharr, the console, the stats drop-down and picker). HD means 720p or above; for an unlabelled Source it counts as HD only when the startup probe sees 720p or higher.

**Blocked by:** 01

**Status:** implemented on provider-swap (awaiting review)

- [x] Given Sources from different Providers and qualities, the default order is quality, then audio, then Provider order, checked through the test seam.
- [x] An unlabelled Source ranks just below a labelled 1080p and above lower labelled qualities.
- [x] A Source known to have no audio ranks behind one that has audio, as today.
- [x] Labels show Provider, number and quality; the old letter labels are no longer produced.
- [x] The HD/SD badge follows the 720p-or-above rule, and for unlabelled Sources follows the probe.
- [x] The stats drop-down and picker show the Provider for each Source.
- [x] Existing checks still pass on a scratch-tagged image.
