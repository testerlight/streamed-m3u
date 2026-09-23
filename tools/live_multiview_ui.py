"""Phase 9 live check: the console section driving a real encoder. Not shipped.

`check_render.py` proves every control moves what it claims to move, but it
runs offline, so the one thing it can never show is the section with a
composite actually running behind it - the status strip, "On air", a bitrate,
a viewer count, and a layout change landing on a picture someone is watching.

    docker run --rm --device /dev/dri -e STARTUP_DELAY=0 \\
        -e CONSOLE_PASSWORD=live-pw -e MV_CANDIDATES=nfl-network,tennis-channel \\
        -v $PWD/tools/live_multiview_ui.py:/tmp/live.py:ro -v $PWD/_shots:/out \\
        <image> python /tmp/live.py

Needs the network and the render node. Costs two real extractions and about a
minute of encoding, so it is a gate check, not something to run in a loop.
"""

import os
import sys
import tempfile
import threading
import time

DATA = tempfile.mkdtemp(prefix="live9-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["MULTIVIEW_ENABLE"] = "1"
os.environ["PORT"] = "8899"
PASSWORD = os.environ.get("CONSOLE_PASSWORD", "live-pw")
os.environ["CONSOLE_PASSWORD"] = PASSWORD
CANDIDATES = [s.strip() for s in
              os.environ.get("MV_CANDIDATES", "nfl-network,tennis-channel").split(",")
              if s.strip()]

sys.path.insert(0, "/app")
import app  # noqa: E402
import requests  # noqa: E402

OUT = "/out"
fails = []
problems = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  [%s]" % detail) if detail != "" else ""), flush=True)
    if not cond:
        fails.append(name)


print("fetching the fixture list …", flush=True)
app._install_seed_roster()
app._load_team_roster()
sports = app.fetch_sports()
matches = app.fetch_all_matches(sports)
app.build_m3u(matches)
print("roster %d, resolvable now %d"
      % (len(app._team_roster), sum(1 for v in app._team_map.values() if v["streams"])),
      flush=True)

# Which two actually resolve right now decides the test; a fixture listed is
# not a fixture on. The first two that warm are the ones the console gets.
picked = []
for slug in CANDIDATES:
    if len(picked) == 2:
        break
    entry = app._team_roster.get(slug)
    if not entry:
        print("  %-24s not on the roster" % slug, flush=True)
        continue
    t0 = time.time()
    app._prewarm_one(slug, entry.get("name") or slug, force=True)
    st = (app._prewarm_state.get(slug) or {}).get("status", "")
    print("  %-24s %-28s %4.1fs" % (slug, st, time.time() - t0), flush=True)
    if st.startswith("warm"):
        picked.append(slug)

check("two channels resolve right now", len(picked) == 2, picked)
if len(picked) < 2:
    sys.exit(1)
PRIMARY, SECONDARY = picked
PRIMARY_NAME = app._team_roster[PRIMARY].get("name") or PRIMARY
SECONDARY_NAME = app._team_roster[SECONDARY].get("name") or SECONDARY

threading.Thread(target=lambda: app.app.run(host="127.0.0.1", port=8899,
                                            threaded=True, use_reloader=False),
                 daemon=True).start()
time.sleep(2)

BASE = "http://127.0.0.1:8899"
pulled = {"bytes": 0, "stop": False, "first": None}


def pull():
    """An ordinary viewer, so the encoder has an audience and a reason to run."""
    try:
        r = requests.get(BASE + "/stream?multi=1", stream=True, timeout=120)
        for chunk in r.iter_content(65536):
            if pulled["stop"]:
                break
            if chunk:
                pulled["bytes"] += len(chunk)
                if pulled["first"] is None:
                    pulled["first"] = time.time()
        r.close()
    except Exception as exc:                      # noqa: BLE001
        problems.append("viewer: %s" % exc)


from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.on("console", lambda m: problems.append("console %s: %s" % (m.type, m.text))
            if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append("pageerror: %s" % e))

    page.goto(BASE + "/", wait_until="networkidle")
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    page.evaluate("document.getElementById('multiview')"
                  ".scrollIntoView({behavior:'instant',block:'start'})")
    page.wait_for_selector("#mv-panel .mv-stage", timeout=15000)

    def pick(role, name):
        page.click('[data-mv-open="%s"]' % role)
        page.wait_for_selector("#mv-search", timeout=5000)
        page.fill("#mv-search", name)
        page.wait_for_timeout(500)
        page.click(".mv-option")
        page.wait_for_timeout(2000)

    # Chosen from "Resolvable now", the default scope, which is where a live
    # fixture is found.
    pick("primary", PRIMARY_NAME)
    pick("secondary", SECONDARY_NAME)
    text = page.locator("#multiview").inner_text()
    check("both fixtures are on the slot",
          PRIMARY_NAME in text and SECONDARY_NAME in text, text[:200])
    check("and the console says they are warm",
          page.locator("#multiview").inner_text().count("Warm") == 2,
          page.locator("#multiview").inner_text().count("Warm"))
    page.screenshot(path=OUT + "/multiview-live-ready.png")

    threading.Thread(target=pull, daemon=True).start()

    # The encoder starts with the viewer, not with the selection.
    began = time.time()
    while time.time() - began < 150:
        page.wait_for_timeout(3000)
        if "On air" in page.locator("#multiview").inner_text():
            break
    live = page.locator("#multiview").inner_text()
    check("the status strip reports the encoder", "On air" in live,
          live[:160])
    check("with a bitrate", "Mbit/s" in live, live[:160])
    check("and a viewer", "1 viewer" in live, live[:160])
    # "On air" appears the moment the encoder reports alive, which can be
    # ahead of the first picture bytes reaching this process - so how much has
    # arrived *at this instant* is not a fact worth asserting. What the viewer
    # actually received is checked across the layout changes and again at the
    # end, where it means something.
    print("    (viewer had %d bytes when the strip flipped)" % pulled["bytes"],
          flush=True)
    page.evaluate("document.getElementById('multiview')"
                  ".scrollIntoView({behavior:'instant',block:'start'})")
    page.wait_for_timeout(600)
    page.screenshot(path=OUT + "/multiview-live-onair.png")

    # A layout change on a picture someone is actually watching.
    before = page.locator(".mv-mini").bounding_box()
    page.click('[data-mv-corner="tl"]')
    page.wait_for_timeout(2500)
    after = page.locator(".mv-mini").bounding_box()
    check("the corner moves while it is playing", after["x"] < before["x"])
    note = page.locator("#multiview").inner_text()
    check("and the console says it landed on the live picture",
          "already playing" in note, note[:200])
    page.click('[data-mv-size="large"]')
    page.wait_for_timeout(2500)
    bigger = page.locator(".mv-mini").bounding_box()
    check("so does the size", bigger["width"] > after["width"])
    sec = page.locator('[data-mv-gain="secondary"]')
    sec.focus()
    for _ in range(4):
        page.keyboard.press("ArrowRight")
    page.wait_for_timeout(2500)
    check("and the mixer", sec.input_value() == "20", sec.input_value())
    # The failure this is looking for is a composite that froze, which gives
    # about 1.5 KB of null-packet padding over this window. The floor is set
    # against that, not against a bitrate: CQP on low-motion content (darts,
    # a static scoreboard) legitimately encodes at a few hundred kbit/s, and
    # a threshold tuned to a busy baseball feed fails on a quiet one.
    bytes_at_change = pulled["bytes"]
    page.wait_for_timeout(8000)
    moved = pulled["bytes"] - bytes_at_change
    check("the stream never stopped for any of it", moved > 100_000,
          "%d bytes in 8s (~%.2f Mbit/s)" % (moved, moved * 8 / 8e6))
    page.screenshot(path=OUT + "/multiview-live-changed.png")

    # And the operator can stop it from the same strip.
    # Stopping joins the encoder's teardown, so the request can outlast any
    # fixed wait; ask the page until it says so rather than guessing.
    page.click("[data-mv-stop]")
    waited = time.time()
    while time.time() - waited < 30:
        page.wait_for_timeout(1000)
        if "Encoder stopped" in page.locator("#multiview").inner_text():
            break
    stopped = page.locator("#multiview").inner_text()
    check("Stop reports the encoder gone", "Encoder stopped" in stopped,
          stopped[:200])
    pulled["stop"] = True
    browser.close()

total = pulled["bytes"] / 1e6
check("the viewer received a real stream, not just padding",
      pulled["bytes"] > 1_000_000, pulled["bytes"])
print("\nviewer received %.1f MB" % total)
print("browser problems:", problems if problems else "none")
print("LIVE " + ("OK" if not fails and not problems
                 else "FAILED: " + "; ".join(fails + problems)))
sys.exit(1 if fails or problems else 0)
