# 01: Provider layer, no behaviour change

**What to build:** Games and Sources come from a Provider, and streamed.pk becomes the first one, behaving exactly as today. A setting lists the enabled Providers in preference order, and each Provider has a base URL and an ordered mirror list as settings. Only configured domains are ever contacted. This is the prefactor the rest depends on: the rest of the app sees one common game and Source shape and never knows which Provider it came from.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] With streamed.pk the only enabled Provider, the served playlist and `/stream` output are identical to before, checked against a recorded streamed.pk fixture through the existing test seam.
- [ ] The enabled-Providers setting controls which Providers are used and their order; streamed.pk can be turned off without a rebuild.
- [ ] Each Provider's base URL and mirror list are settings; mirrors are tried in order, and a domain that is not configured is never contacted.
- [ ] Embed hosts come from each Provider's own response, not from a hard-coded value.
- [ ] The README settings table is regenerated from the settings code.
- [ ] Existing checks still pass on a scratch-tagged image; the live image tag is not retagged.
