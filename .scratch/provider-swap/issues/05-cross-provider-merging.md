# 05: Cross-Provider game merging

**What to build:** The same game listed by two Providers becomes one game on one Channel, with every Provider's Sources in a single ranked list. Games match by team slug from the existing team index plus a start time within about 3 hours. If a pair is not a confident match, keep them separate: a wrong merge puts the wrong video on a Channel. This must work even though only one real Provider is live in Step 1, so Step 2 only adds an adapter.

**Blocked by:** 03

**Status:** implemented on provider-swap (awaiting review)

- [x] Two fixture games with the same teams and starts within 3 hours merge into one game with Sources from both Providers.
- [x] Two games with the same teams but starts further apart stay separate.
- [x] A title that cannot be resolved to teams on one side stays separate rather than guessing.
- [x] Always-live feeds listed by two Providers merge by name or alias.
- [x] The merged Source list is ranked as in ticket 02, and each label shows its own Provider.
- [x] Checked through the test seam with recorded fixtures from two Providers.
- [x] Existing checks still pass on a scratch-tagged image.
