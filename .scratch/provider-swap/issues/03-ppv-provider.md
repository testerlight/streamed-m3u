# 03: ppv.st Provider

**What to build:** ppv.st games and their alternates appear as games with Sources, and play through the existing browser extractor. ppv.st lists everything in one call, with an embed link, start and end times, an always-live flag and optional substreams. Substreams become extra Sources for the same game. Games appear in the guide as soon as ppv.st lists them, even with no live Source. Always-live entries such as NFL Network are recognised as feeds. "Away at Home" titles must resolve through the existing team index and aliases. Posters go through the existing image proxy, and the playlist header lists the enabled Providers.

**Blocked by:** 01, 02

**Status:** implemented on provider-swap (awaiting review)

- [x] Recorded real ppv.st list responses are kept as fixtures and drive all checks offline.
- [x] A game with substreams yields one game with several ranked Sources.
- [x] A game with no Source yet still appears in the guide.
- [x] Titles in the "Away at Home" shape land on the correct team Channels; any team that fails to resolve is reported so the alias list can be extended.
- [x] An always-live entry (NFL Network) is recognised as a feed using the Provider's own flag, and its exact lineup slug is confirmed.
- [x] Poster images are served through the image proxy and cache, and the new poster host is allowed.
- [x] The playlist header lists the enabled Providers.
- [x] One request to ppv.st per refresh cycle for the list.
- [x] Existing checks still pass on a scratch-tagged image.
