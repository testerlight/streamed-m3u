# 09: streamfree playback

**What to build:** A streamfree Channel plays through the proxy without a browser. The embed page is fetched with no Referer. It carries a signed playlist token valid for about 8 hours. The playlist is then requested with the embed page as the referrer, and the result takes the same shape the browser extractor returns, so the rest of the proxy is unchanged. The browser extractor stays as a fallback if the page layout changes, and it must load the page with no Referer, because a streamfree Referer is answered with a 403 "Violation".

**Blocked by:** 08

**Status:** ready-for-agent

- [ ] A recorded streamfree embed page is kept as a fixture; the token is read from it and the playlist is requested with the embed page as referrer, checked through the test seam with stubbed HTTP.
- [ ] The extraction result has the same shape as the browser extractor's, so existing startup probing, audio check, pacing and Recovery apply unchanged.
- [ ] If the page layout no longer yields a token, the browser fallback is used and loads the page with no Referer.
- [ ] A 403 or Violation answer is treated as a failed Source, logged, and counted so the Provider's health shows in the dashboard.
- [ ] Token reuse respects its lifetime, and expired tokens are re-fetched.
- [ ] A live streamfree Channel plays through to Jellyfin for several minutes behind the VPN on a scratch-tagged image.
- [ ] Embed pages are fetched only for prewarming and tune-in, and prewarming stays serial and light.
- [ ] Existing checks still pass on a scratch-tagged image.
