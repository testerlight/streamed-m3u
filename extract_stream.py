"""
Extract m3u8 URL from a sports embed page (embedsporty.top, embedsports.top, etc.)
Outputs JSON: {"url": "...", "cookies": {...}, "headers": {...}}
Usage: python extract_stream.py <embed_url> [timeout_seconds]
"""
import sys, json, time
from urllib.parse import urlparse

def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    embed_url = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    parsed = urlparse(embed_url)
    embed_origin = f"{parsed.scheme}://{parsed.netloc}"

    from playwright.sync_api import sync_playwright

    result = None
    cookies = {}
    referer = f"{embed_origin}/"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
                "--single-process", "--disable-blink-features=AutomationControlled",
                "--disable-extensions", "--disable-plugins", "--no-first-run",
                "--disable-background-networking", "--disable-sync",
                "--disable-features=TranslateUI",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36"
                ),
                extra_http_headers={"Referer": f"{embed_origin}/"},
                viewport={"width": 1280, "height": 720},
                service_workers="block",
            )
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
            """)

            # Block heavy, irrelevant resources to speed player init and cut VPN
            # bandwidth. We only need the player's scripts + the m3u8 (xhr/fetch),
            # never images/fonts/CSS, so abort those and let everything else
            # (document/script/xhr/fetch/websocket/media) through untouched.
            _BLOCKED_TYPES = {"image", "font", "stylesheet"}

            def _route(route):
                try:
                    if route.request.resource_type in _BLOCKED_TYPES:
                        route.abort()
                    else:
                        route.continue_()
                except Exception:
                    try:
                        route.continue_()
                    except Exception:
                        pass

            context.route("**/*", _route)

            page = context.new_page()

            # Intercept network requests for m3u8 — fastest possible detection,
            # fires the moment the browser makes the request rather than waiting
            # for JS polling or fixed sleeps. We also capture the *real* request
            # headers (Referer/Origin/sec-fetch/UA) the browser used, so app.py
            # can replay them exactly instead of guessing — the CDN's /secure/
            # endpoint validates these and rejects mismatches with 403.
            # The player probes several load-balancer tokens; the *first* m3u8 it
            # requests is often a dead one (404). We therefore prefer the manifest
            # whose RESPONSE was 200, falling back to the first request only if no
            # 200 is observed. We also capture the real request headers so app.py
            # can replay Referer/Origin/sec-fetch exactly.
            network_m3u8 = {"url": None, "headers": {}, "status": None, "confirmed": False}
            m3u8_req_headers = {}

            def _is_target_m3u8(url):
                return ".m3u8" in url and "streamed" not in url

            def on_request(req):
                url = req.url
                if not _is_target_m3u8(url):
                    return
                try:
                    m3u8_req_headers[url] = dict(req.all_headers())
                except Exception:
                    try:
                        m3u8_req_headers[url] = dict(req.headers)
                    except Exception:
                        m3u8_req_headers[url] = {}
                # Best-effort fallback until a 200 response confirms a live one.
                if network_m3u8["url"] is None:
                    network_m3u8["url"] = url
                    network_m3u8["headers"] = m3u8_req_headers[url]

            def on_response(resp):
                url = resp.url
                if not _is_target_m3u8(url):
                    return
                if network_m3u8["status"] is None:
                    network_m3u8["status"] = resp.status
                # Lock onto the first manifest that actually returned 200.
                if resp.status == 200 and not network_m3u8["confirmed"]:
                    network_m3u8["url"] = url
                    network_m3u8["headers"] = m3u8_req_headers.get(
                        url, network_m3u8["headers"])
                    network_m3u8["status"] = 200
                    network_m3u8["confirmed"] = True

            page.on("request", on_request)
            page.on("response", on_response)

            # Also inject JW Player proxy as a fallback for players that load
            # the m3u8 via JS config rather than a direct network fetch.
            page.add_init_script("""
                window.__intercepted_url = null;
                Object.defineProperty(window, 'jwplayer', {
                    get: function() { return window.__jw_proxy; },
                    set: function(val) {
                        window.__jw_proxy = new Proxy(val, {
                            apply: function(target, thisArg, args) {
                                var inst = target.apply(thisArg, args);
                                return new Proxy(inst, {
                                    get: function(obj, prop) {
                                        if (prop === 'setup') {
                                            return function(cfg) {
                                                try {
                                                    if (cfg && cfg.file) window.__intercepted_url = cfg.file;
                                                    if (cfg && cfg.playlist && cfg.playlist[0]) {
                                                        var p = cfg.playlist[0];
                                                        if (p.file) window.__intercepted_url = p.file;
                                                        if (p.sources) p.sources.forEach(function(s) {
                                                            if (s.file) window.__intercepted_url = s.file;
                                                        });
                                                    }
                                                } catch(e) {}
                                                return obj.setup ? obj.setup(cfg) : null;
                                            };
                                        }
                                        return obj[prop];
                                    }
                                });
                            }
                        });
                    },
                    configurable: true
                });
            """)

            # Step 1: load outer page to find iframe and collect cookies.
            # Use "commit" so we don't wait for all resources — we only need
            # the DOM to find the iframe src.
            try:
                page.goto(embed_url, timeout=8000, wait_until="commit")
            except Exception:
                pass

            # Wait for iframe to appear in DOM rather than sleeping a fixed amount.
            inner_url = None
            try:
                page.wait_for_selector("iframe[src]", timeout=4000)
                inner_url = page.evaluate("""() => {
                    const iframe = document.querySelector('iframe[src]');
                    return iframe ? iframe.src : null;
                }""")
            except Exception:
                pass

            # Step 2: navigate to player page (iframe URL or embed URL directly)
            target_url = inner_url if inner_url else embed_url
            try:
                page.goto(target_url, timeout=12000, wait_until="commit")
            except Exception:
                pass

            # Click to dismiss any play overlay / consent dialogs
            try:
                page.mouse.click(960, 540)
            except Exception:
                pass

            # Wait for m3u8 — prefer a manifest the browser got a 200 on (the
            # player probes dead 404 tokens first), then a JW Player proxy hook,
            # then any intercepted request as a last resort. Poll tightly (100ms).
            deadline = time.time() + timeout
            from_network = False
            while time.time() < deadline:
                if network_m3u8["confirmed"]:
                    result = network_m3u8["url"]
                    from_network = True
                    break
                jw_result = page.evaluate("() => window.__intercepted_url")
                if jw_result:
                    result = jw_result
                    break
                time.sleep(0.1)

            # No confirmed-200 manifest within the window: fall back to the first
            # intercepted m3u8 request (best effort) if we saw one.
            if not result and network_m3u8["url"]:
                result = network_m3u8["url"]
                from_network = True

            # Give the browser a brief moment to finish the m3u8 round-trip so
            # any DDoS-Guard cookies it sets are present before we snapshot them.
            settle_deadline = time.time() + 0.8
            while time.time() < settle_deadline and network_m3u8["status"] is None:
                time.sleep(0.1)

            cookies = {c["name"]: c["value"] for c in context.cookies()}

            # Prefer the browser's REAL request headers for the m3u8 (captured at
            # intercept time). Fall back to a sane Referer/Origin only when we
            # couldn't observe the network request (e.g. JW-proxy-only path).
            captured = network_m3u8["headers"] if from_network else {}
            # Header keys from all_headers() are lowercase.
            req_referer = captured.get("referer")
            req_origin = captured.get("origin")
            if not req_referer:
                req_referer = f"{embed_origin}/"
            if not req_origin:
                req_origin = req_referer.rstrip("/")

        finally:
            try:
                browser.close()
            except Exception:
                pass

    if result:
        # Pass through the browser's real request context. Include the key
        # headers the CDN validates; app.py overrides User-Agent with a stable
        # desktop UA but keeps Referer/Origin/sec-fetch as observed.
        out_headers = {
            "Referer": req_referer,
            "Origin": req_origin,
        }
        for h in ("user-agent", "accept", "accept-language",
                  "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"):
            if captured.get(h):
                # Normalize to canonical header casing for requests.
                canonical = "-".join(p.capitalize() for p in h.split("-"))
                out_headers[canonical] = captured[h]

        output = {
            "url": result,
            "cookies": cookies,
            "headers": out_headers,
            "browser_m3u8_status": network_m3u8["status"],
        }
        # Diagnostic on stderr so it shows even if stdout is consumed as JSON.
        print(f"M3U8_STATUS={network_m3u8['status']}", file=sys.stderr)
        print(json.dumps(output), end="")
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
