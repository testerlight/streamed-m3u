"""Verification harness for streamfree playback (provider-swap ticket 09). Not shipped.

One seam: `app.extract_m3u8_via_browser(embed_url)`, the call the click path
and the pre-warm loop both make, with the network replaced by a scripted
streamfree whose embed page is a REAL recorded one
(tools/fixtures/streamfree_embed.html, recorded 2026-10-27 from
https://strmfree.st/embed/basketball/portland-trail-blazers-vs-los-angeles-lakers1080p).
Only the extractor's HTTP function and the browser subprocess are faked.
The last section runs the real extract_stream.py in Chromium against a local
page to prove the fallback loads it with no Referer (skipped when Playwright is
not installed). Run inside a container built from this directory:

    docker run --rm -e STARTUP_DELAY=0 \
        -v $PWD/tools/check_streamfree_playback.py:/tmp/check.py:ro \
        -v $PWD/tools/fixtures:/tmp/fixtures:ro \
        <image> python /tmp/check.py /tmp/fixtures
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

DATA = tempfile.mkdtemp(prefix="check-sfplay-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["PROVIDERS"] = "streamfree,ppv"
os.environ["STREAMFREE_BASE_URL"] = "https://sf-api.example"
os.environ["STREAMFREE_EMBED_BASE_URL"] = "https://sf-embed.example"
os.environ["PPV_BASE_URL"] = "https://ppv.example"
for k in ("PUBLIC_BASE_URL", "STREAMFREE_MIRRORS", "STREAMFREE_EMBED_MIRRORS", "PPV_MIRRORS"):
    os.environ.pop(k, None)

sys.path.insert(0, "/app")
import app  # noqa: E402
import providers  # noqa: E402

FIXDIR = sys.argv[1] if len(sys.argv) > 1 else "tools/fixtures"
PAGE = open(os.path.join(FIXDIR, "streamfree_embed.html"), encoding="utf-8").read()

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  [%s]" % (detail,)) if detail != "" else ""))
    if not cond:
        fails.append(name)


KEY = "portland-trail-blazers-vs-los-angeles-lakers"
EMBED = "https://sf-embed.example/embed/basketball/%s1080p" % KEY
EMBED2 = "https://sf-embed.example/embed/basketball/%s1080p2" % KEY
EXP = int(re.search(r'"_e": (\d+)', PAGE).group(1))      # the recorded token's expiry
TOKEN = json.loads(re.search(r"const _0x = (\{.*?\});", PAGE).group(1))["1080p"]
PLAYLIST = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:1349\n#EXT-X-TARGETDURATION:4\n" \
           "#EXTINF:4.004,\n1349.js\n#EXTINF:4.004,\n1350.js\n"


class Streamfree:
    """A scripted streamfree: embed pages (no Referer allowed), playlists (Referer required)."""

    def __init__(self):
        self.calls = []                  # (kind, url, headers)
        self.page = PAGE
        self.page_status = 200
        self.live_status = 200           # the /live/ route
        self.cdn_status = 200            # the /live-cdn/ route
        self.server_name = "cdn"
        self.stale_once = False          # refuse the next playlist request, as a rotated token would

    def fetch(self, url, headers):
        u = urlparse(url)
        headers = dict(headers)
        if u.path.startswith("/embed/"):
            self.calls.append(("page", url, headers))
            if "Referer" in headers or "referer" in headers:
                return 403, "Violation"
            return self.page_status, ("Violation" if self.page_status == 403 else self.page)
        if u.path.startswith("/get-stream-key/"):
            self.calls.append(("key", url, headers))
            return 200, json.dumps({"stream_key": KEY, "server_name": self.server_name})
        if u.path.endswith("/index.m3u8"):
            self.calls.append(("playlist", url, headers))
            q = parse_qs(u.query)
            if headers.get("Referer", "").split("?")[0] not in (EMBED, EMBED2):
                return 403, "<html>403 Forbidden</html>"
            if self.stale_once:
                self.stale_once = False
                return 403, "Violation"
            route = u.path.split("/")[1]
            status = self.live_status if route == "live" else self.cdn_status
            if status != 200:
                return status, "not found"
            if q.get("_t") != [TOKEN["_t"]]:
                return 403, "bad token"
            return 200, PLAYLIST
        raise OSError("scripted: %s not reachable" % url)

    def n(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


sf = Streamfree()
clock = [EXP - 8 * 3600]                                  # the token has all its 8 hours left
app._STREAMFREE_EXTRACTOR.fetch = sf.fetch
app._STREAMFREE_EXTRACTOR.clock = lambda: clock[0]

browser_runs = []


class FakeProc:
    returncode = 0

    def __init__(self, args, **kw):
        browser_runs.append((args, kw))
        self.pid = 0

    def communicate(self, timeout=None):
        return json.dumps({"url": "https://sf-embed.example/live/x/index.m3u8?_t=b",
                           "cookies": {}, "headers": {"Referer": "https://sf-embed.example/"},
                           "browser_m3u8_status": 200}).encode(), b""

    def wait(self, timeout=None):
        return 0


REAL_POPEN = app.subprocess.Popen
app.subprocess.Popen = FakeProc


def reset():
    sf.calls[:] = []
    browser_runs[:] = []
    app._extract_cache.clear()
    app._STREAMFREE_EXTRACTOR._tokens.clear()
    app._provider_playback.clear()
    sf.page, sf.page_status, sf.live_status, sf.cdn_status = PAGE, 200, 200, 200
    sf.stale_once = False
    clock[0] = EXP - 8 * 3600


def status():
    return {p["name"]: p for p in app._provider_status()}["streamfree"]


# ── 1. Token from the recorded page; playlist asked with the page as Referer ─

reset()
d = app.extract_m3u8_via_browser(EMBED)
page_calls = [c for c in sf.calls if c[0] == "page"]
play_calls = [c for c in sf.calls if c[0] == "playlist"]
check("the embed page is fetched once, with no Referer",
      len(page_calls) == 1 and not any(k.lower() == "referer" for k in page_calls[0][2]), page_calls[:1])
check("the playlist is requested with the embed page as Referer",
      len(play_calls) == 1 and play_calls[0][2].get("Referer") == EMBED, play_calls[:1])
q = parse_qs(urlparse(d["url"]).query) if d else {}
check("the playlist URL carries the recorded token and variant",
      d and urlparse(d["url"]).path == "/live/%s1080p/index.m3u8" % KEY
      and q == {"_t": [TOKEN["_t"]], "_e": [str(TOKEN["_e"])], "_n": [TOKEN["_n"]]}, d and d["url"])
check("no browser was started", not browser_runs)

# ── 2. Same shape as the browser extractor's ────────────────────────────────

src = open("/app/extract_stream.py").read()
browser_keys = set(re.findall(r'^\s+"(\w+)":', src[src.index("output = {"):], re.M)[:4])
check("same top-level keys as the browser extractor's result",
      d and set(d) == browser_keys == {"url", "cookies", "headers", "browser_m3u8_status"},
      (sorted(d or {}), sorted(browser_keys)))
check("headers carry the Referer the proxy replays for the playlist",
      d and d["headers"].get("Referer") == EMBED and d["cookies"] == {})

# The existing probe, unchanged, accepts it: _validate_candidate uses get_segments
# and one segment request; both are replayed against a scripted CDN here.
segs_seen = []


class Seg:
    def __init__(self, status, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content
        self.headers = {"Content-Type": "video/mp2t"}


def cf_get(url, headers=None, **kw):
    segs_seen.append((url, dict(headers or {})))
    if url.endswith("index.m3u8") or "index.m3u8?" in url:
        return Seg(200, PLAYLIST)
    return Seg(200, "", b"\x47" + b"\x00" * 187)


app.cf_requests.get = cf_get
h = dict(d["headers"])
segments, st = app.get_segments(d["url"], h, d["cookies"])
check("get_segments reads the playlist and resolves its segments",
      st == 200 and segments and segments[0].endswith("/live/%s1080p/1349.js" % KEY), (st, segments))
check("the proxy's playlist request replays the Referer",
      segs_seen and segs_seen[0][1].get("Referer") == EMBED, segs_seen[:1])

# ── 3. Cache and token lifetime ─────────────────────────────────────────────

n_pages = sf.n("page")
again = app.extract_m3u8_via_browser(EMBED)
check("a second tune-in inside the extract cache makes no request at all",
      again == d and sf.n("page") == n_pages and sf.n("playlist") == 1)

app._extract_cache.clear()
clock[0] += 3600
app.extract_m3u8_via_browser(EMBED)
check("with the extract cache gone, the held token is reused: playlist asked, page not",
      sf.n("page") == 1 and sf.n("playlist") == 2, (sf.n("page"), sf.n("playlist")))

app._extract_cache.clear()
clock[0] = EXP - 300                                    # inside the margin before expiry
app.extract_m3u8_via_browser(EMBED)
check("a token about to expire is fetched again", sf.n("page") == 2, sf.n("page"))

# Another variant of the same game is its own link with its own token.
app.extract_m3u8_via_browser(EMBED2)
check("each embed link keeps its own token", sf.n("page") == 3)

# A held token the server now refuses (rotated early) is re-fetched once.
reset()
app.extract_m3u8_via_browser(EMBED)
app._extract_cache.clear()
sf.stale_once = True
d2 = app.extract_m3u8_via_browser(EMBED)
check("a refused held token is re-fetched once and then works",
      d2 and sf.n("page") == 2 and sf.n("playlist") == 3, (sf.n("page"), sf.n("playlist")))

# ── 4. The other playlist route, as the page's own script picks it ─────────

reset()
sf.live_status = 404
d3 = app.extract_m3u8_via_browser(EMBED)
check("if /live/ is not served, the route named by get-stream-key is used",
      d3 and urlparse(d3["url"]).path.startswith("/live-cdn/") and sf.n("key") == 1,
      d3 and d3["url"])

# ── 5. Layout change: the browser fallback, with no Referer ─────────────────

reset()
sf.page = PAGE.replace("const _0x = ", "const _tokens = ")
d4 = app.extract_m3u8_via_browser(EMBED)
args, kw = browser_runs[0] if browser_runs else (None, {})
check("a page with no token falls back to the browser", len(browser_runs) == 1 and d4 and "/live/x/" in d4["url"])
check("the browser is told to load the page with no Referer",
      kw.get("env") and kw["env"].get("EXTRACT_NO_REFERER") == "1", kw.get("env") and kw["env"].get("EXTRACT_NO_REFERER"))
check("a fallback that worked counts as good playback",
      status()["playback_ok"] == 1 and status()["playback_failing"] is False, status())

# ppv.st and every other host keep the Referer their extractor has always sent.
reset()
app.extract_m3u8_via_browser("https://ppv-embed.example/e/abc")
kw = browser_runs[0][1] if browser_runs else {}
check("other Providers' links are untouched: browser, Referer as before",
      len(browser_runs) == 1 and not kw.get("env") and sf.n("page") == 0, kw)

# ── 6. A 403 / Violation is a failed Source, logged and counted ─────────────

reset()
sf.page_status = 403
log_lines = []
handler = type("H", (app.logging.Handler,), {"emit": lambda self, r: log_lines.append(r.getMessage())})()
app.log.addHandler(handler)
d5 = app.extract_m3u8_via_browser(EMBED)
check("a Violation page gives no extraction and no browser retry", d5 is None and not browser_runs)
check("it is logged", any("streamfree" in l and "Violation" in l for l in log_lines), log_lines[-2:])
s = status()
check("the Provider's health counts it and shows playback failing",
      s["playback_failed"] == 1 and s["playback_failing"] is True and "Violation" in s["playback_error"], s)
overview = app.app.test_client().get("/api/overview").get_json()
sfo = {p["name"]: p for p in overview["providers"]}["streamfree"]
check("the dashboard overview carries it", sfo["playback_failing"] is True and sfo["playback_failed"] == 1)

# The playlist refused with a fresh token is also a failed Source.
reset()
sf.live_status = 403
sf.cdn_status = 403
app.extract_m3u8_via_browser(EMBED)
check("a refused playlist is a failed Source too", status()["playback_failed"] == 1 and not browser_runs)

# A later good tune-in clears the warning but keeps the count.
reset()
sf.page_status = 403
app.extract_m3u8_via_browser(EMBED)
sf.page_status = 200
app.extract_m3u8_via_browser(EMBED)
s = status()
check("a later good extraction clears 'failing' and keeps the counts",
      s["playback_failing"] is False and s["playback_failed"] == 1 and s["playback_ok"] == 1, s)
app.log.removeHandler(handler)

# ── 7. Pages are fetched only on tune-in and pre-warm ───────────────────────

reset()
before = len(sf.calls)
app.app.test_client().get("/api/overview")
app.app.test_client().get("/playlist.m3u")
check("listing and the dashboard fetch no embed page", len(sf.calls) == before == 0)

# ── 8. The browser fallback really loads the page with no Referer ──────────

try:
    import playwright  # noqa: F401
    have_browser = True
except ImportError:
    have_browser = False

if not have_browser:
    print("SKIP browser Referer check (Playwright not installed)")
else:
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Referer")))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>no player here</body></html>")

        def log_message(self, *a):
            pass

    app.subprocess.Popen = REAL_POPEN
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/embed/x1080p" % srv.server_port
    for label, extra in (("with EXTRACT_NO_REFERER", {"EXTRACT_NO_REFERER": "1"}), ("without it", {})):
        seen.clear()
        env = {k: v for k, v in os.environ.items() if k != "EXTRACT_NO_REFERER"}
        env.update(extra)
        subprocess.run(["python", "/app/extract_stream.py", url, "3"], env=env,
                       capture_output=True, timeout=90)
        page = [r for p, r in seen if p.startswith("/embed/")]
        if extra:
            check("browser %s: the page is requested with no Referer" % label,
                  page and all(r is None for r in page), page)
        else:
            check("browser %s: the Referer is sent as it always was" % label,
                  page and all(r for r in page), page)
    srv.shutdown()

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
