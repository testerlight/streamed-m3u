"""Browser check for the console's stream health UI. Not shipped.

    mkdir -p _shots && chmod 777 _shots
    docker run --rm -e STARTUP_DELAY=0 \
        -v $PWD/tools/check_health.py:/tmp/check_health.py:ro \
        -v $PWD/tools/check_health_render.py:/tmp/hr.py:ro \
        -v $PWD/_shots:/out <image> python /tmp/hr.py

Plays a few scripted streams from check_health.py's fake origin so Recent
streams has rows, keeps one deliberately slow stream running so Active
streams has a live Health badge, then drives the real page in Chromium:
opens the badge's timeline, checks both sections render without a script
error, and screenshots both themes into /out.
"""

import os
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import quote

DATA = tempfile.mkdtemp(prefix="health-ui-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["STREAM_IDLE_TIMEOUT"] = "12"
for name, leaf in (("TEAMS_FILE", "teams.json"), ("LINEUP_FILE", "lineup.json"),
                   ("EXTRACT_CACHE_FILE", "extract_cache.json"),
                   ("SETTINGS_FILE", "settings.json"),
                   ("STREAM_HISTORY_FILE", "stream_history.jsonl")):
    os.environ[name] = os.path.join(DATA, leaf)
os.environ["PORT"] = "8899"

sys.path.insert(0, "/app")
sys.path.insert(0, "/tmp")
import app  # noqa: E402
import check_health as ch  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  [%s]" % (detail,)) if detail != "" else ""), flush=True)
    if not cond:
        fails.append(name)


port = ch.start_origin()
client = app.app.test_client()
URLS = {}


def fake_extract(embed, *a, **k):
    return {"url": URLS[embed], "headers": {}, "cookies": {}, "browser_m3u8_status": 200}


app.extract_m3u8_via_browser = fake_extract


def scenario(sc, key):
    ch.SCENARIOS[key] = sc
    embed = "https://embedsports.top/embed/admin/%s/1" % key
    URLS[embed] = "http://127.0.0.1:%d/%s/master.m3u8" % (port, key)
    sc.reset()
    return embed


# Finished streams for Recent streams.
for sc, key in ((ch.Scenario("stall", take=13, stall=(5.0, 14.0, 8)), "phillies-stall"),
                (ch.Scenario("errors", take=16, pl_errors=(4.5, 7.5),
                             seg_fail={9: "503", 11: "midstall", 13: "short"}), "eagles-errors"),
                (ch.Scenario("steady", take=8), "flyers-steady")):
    embed = scenario(sc, key)
    res = ch.pull(app, client, embed, sc.take)
    print("history stream %s: %d chunks" % (key, len(res.get("segments", []))), flush=True)
app.streamhealth.HISTORY.flush()

threading.Thread(target=lambda: app.app.run(host="127.0.0.1", port=8899, threaded=True,
                                            use_reloader=False), daemon=True).start()
time.sleep(1.5)

# One live stream that cannot keep up: 1s chunks taking 1.6s each.
live = scenario(ch.Scenario("live", take=10 ** 6, slow=(4, 10 ** 6, 1.6)), "sixers-live")
stop_live = threading.Event()


def keep_reading():
    req = urllib.request.urlopen("http://127.0.0.1:8899/stream?url=" + quote(live, safe=""),
                                 timeout=60)
    while not stop_live.is_set():
        if not req.read(65536):
            break
    req.close()


threading.Thread(target=keep_reading, daemon=True).start()
time.sleep(14)

from playwright.sync_api import sync_playwright  # noqa: E402

problems = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.on("pageerror", lambda e: problems.append("pageerror: %s" % e))
    page.on("console", lambda m: problems.append("console %s: %s" % (m.type, m.text))
            if m.type == "error" else None)
    page.goto("http://127.0.0.1:8899/")
    page.wait_for_selector("[data-health]", timeout=15000)
    badge = page.inner_text("[data-health] .badge")
    check("live stream shows a health badge", badge in ("Healthy", "Struggling", "Frozen"), badge)
    sub = page.inner_text("#streams-panel .health-sub")
    check("badge carries speed and cushion", "×" in sub and "cushion" in sub, sub)
    page.click("[data-health]")
    page.wait_for_selector("tr.health-row", timeout=5000)
    facts = page.inner_text("tr.health-row .health-facts")
    check("timeline row shows the figures",
          "Speed" in facts and "Quality taken" in facts and "1920x1080" in facts, facts[:200])
    check("timeline lists events", page.locator("tr.health-row .health-timeline li").count() >= 1)
    time.sleep(5.5)   # one fast poll: the row must survive a re-render
    check("open timeline survives a poll", page.locator("tr.health-row").count() == 1)
    page.wait_for_selector("#history-panel table", timeout=25000)
    rows = page.locator("#history-panel tbody tr").count()
    check("recent streams lists the finished streams", rows >= 3, rows)
    hist = page.inner_text("#history-panel")
    check("recent streams names causes",
          "Source not publishing" in hist and "Download stalled or failing" in hist, hist[:300])
    check("recent streams shows the skipped chunks", "7 skipped" in hist)
    page.locator("#streams").screenshot(path="/out/health-streams-dark.png")
    page.locator("#history").screenshot(path="/out/health-history-dark.png")
    page.click("#theme-toggle")
    time.sleep(0.5)
    page.locator("#streams").screenshot(path="/out/health-streams-light.png")
    page.locator("#history").screenshot(path="/out/health-history-light.png")
    page.set_viewport_size({"width": 820, "height": 1000})
    time.sleep(0.5)
    page.locator("#streams").screenshot(path="/out/health-streams-narrow.png")
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
    check("narrow window: the page itself does not scroll sideways", not overflow)
    browser.close()

stop_live.set()
check("no script errors on the page", not problems, problems[:5])
print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
sys.exit(1 if fails else 0)
