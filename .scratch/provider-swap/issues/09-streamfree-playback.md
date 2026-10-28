# 09: streamfree playback

**What to build:** A streamfree Channel plays through the proxy without a browser. The embed page is fetched with no Referer. It carries a signed playlist token valid for about 8 hours. The playlist is then requested with the embed page as the referrer, and the result takes the same shape the browser extractor returns, so the rest of the proxy is unchanged. The browser extractor stays as a fallback if the page layout changes, and it must load the page with no Referer, because a streamfree Referer is answered with a 403 "Violation".

**Blocked by:** 08

**Status:** implemented on provider-swap (awaiting review); Jellyfin leg not done

- [x] A recorded streamfree embed page is kept as a fixture; the token is read from it and the playlist is requested with the embed page as referrer, checked through the test seam with stubbed HTTP.
- [x] The extraction result has the same shape as the browser extractor's, so existing startup probing, audio check, pacing and Recovery apply unchanged.
- [x] If the page layout no longer yields a token, the browser fallback is used and loads the page with no Referer.
- [x] A 403 or Violation answer is treated as a failed Source, logged, and counted so the Provider's health shows in the dashboard.
- [x] Token reuse respects its lifetime, and expired tokens are re-fetched.
- [~] A live streamfree Channel plays through to Jellyfin for several minutes behind the VPN on a scratch-tagged image.
- [x] Embed pages are fetched only for prewarming and tune-in, and prewarming stays serial and light.
- [x] Existing checks still pass on a scratch-tagged image.

## Comments

2026-10-28 (agent). `providers.StreamfreeExtractor`, gotcha #44. The embed page (no Referer) carries `_0x`, a signed token per quality valid about 8 hours; the playlist `/live/<variant>/index.m3u8?_t&_e&_n` is requested with the page as Referer and returned in the browser extractor's shape. Tokens are held until 10 minutes before expiry; a refused one is re-fetched once. No token on a 200 page runs the browser with `EXTRACT_NO_REFERER=1`. A 403/Violation is a failed Source: logged, counted, shown in the dashboard's Provider banner (`playback_*` fields), no browser retry.
- Recorded embed page: `tools/fixtures/streamfree_embed.html`. Checked by `tools/check_streamfree_playback.py` (28 checks, including real Chromium proving the fallback sends no Referer) on scratch image `streamed-m3u:playback-09`.
- Existing checks on that image: streamfree, ppv, providers, ranking, merge, resilience, pick, console, reorder, cushion, freshlink, health, render all pass (render failed once, passed on two re-runs and on `streamfree-08`: a flake, not this change).
- Live, behind the VPN (scratch container in the live network namespace, port 8799, copy of the data dir): a live NBA game streamed for 6 minutes, 5.9 Mbps average, first byte 3.5 s, no gap, no browser started; a second variant (NHL) for 40 s. Live image not touched.
- **Not done: playing into Jellyfin.** Pulled `/stream` directly, as in ticket 07; a human or a deployed channel is needed for that, hence `[~]`.
- Not deployed. Needs the user's go-ahead (ticket 10).
