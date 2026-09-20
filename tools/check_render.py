"""Browser render check for the console's editing UI. Not shipped.

Run inside the image with the script and an output directory bind mounted:

    mkdir -p _shots && chmod 777 _shots
    docker run --rm -e STARTUP_DELAY=0 -e CONSOLE_PASSWORD=render-pw \
        -v $PWD/tools/check_render.py:/tmp/render.py:ro -v $PWD/_shots:/out \
        <image> python /tmp/render.py

Starts the real app on a loopback port, signs in with Chromium, edits two
settings (one live, one restart-only), saves, and asserts the badges and the
restart banner appear and survive a reload. Screenshots land in /out.
"""

import os
import sys
import tempfile
import threading
import time

DATA = tempfile.mkdtemp(prefix="render-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["PORT"] = "8899"
PASSWORD = os.environ.get("CONSOLE_PASSWORD", "")

sys.path.insert(0, "/app")
import app  # noqa: E402

app._install_seed_roster()
app._load_team_roster()
threading.Thread(target=lambda: app.app.run(host="127.0.0.1", port=8899,
                                            threaded=True, use_reloader=False),
                 daemon=True).start()
time.sleep(2)

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8899"
OUT = "/out"
problems = []
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    # A 4xx fetch response is logged as a console error by Chromium; the
    # validation step below provokes a 400 on purpose, so that one is expected.
    page.on("console", lambda m: problems.append("console %s: %s" % (m.type, m.text))
            if m.type == "error" and "status of 400" not in m.text else None)
    page.on("pageerror", lambda e: problems.append("pageerror: %s" % e))

    # Logged out: the console redirects to the login page.
    page.goto(BASE + "/", wait_until="networkidle")
    check("redirected to login", "/login" in page.url)
    page.screenshot(path=OUT + "/login.png")

    page.fill("#password", "wrong")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    check("wrong password shows error", "not accepted" in page.content())

    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    check("signed in lands on console", page.url.rstrip("/") == BASE)
    check("sign out control present", page.locator("#sign-out").count() == 1)
    page.screenshot(path=OUT + "/console-signed-in.png")

    # Edit mode: inputs render for editable settings.
    page.evaluate("document.getElementById('config').scrollIntoView()")
    page.wait_for_timeout(500)
    check("number input for REFRESH_SECONDS", page.locator('input[data-env="REFRESH_SECONDS"]').count() == 1)
    check("select for LOG_LEVEL", page.locator('select[data-env="LOG_LEVEL"]').count() == 1)
    check("PORT stays read-only", page.locator('input[data-env="PORT"]').count() == 0)
    page.screenshot(path=OUT + "/config-edit-mode.png")

    # Change a live setting and a restart-only one, then save.
    page.fill('input[data-env="REFRESH_SECONDS"]', "601")
    page.fill('input[data-env="PREWARM_TEAMS"]', "Boston Bruins")
    page.wait_for_timeout(300)
    check("save bar visible with 2 changes", page.locator("#save-bar").is_visible() and
          "2 changes" in page.locator("#save-count").inner_text())
    page.screenshot(path=OUT + "/config-dirty.png")
    page.click("#save-settings")
    page.wait_for_timeout(1500)
    body = page.content()
    check("Applied badge shown", "Applied" in body)
    check("Restart required badge shown", "Restart required" in body)
    check("restart banner visible", page.locator("#restart-banner").is_visible())
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    page.screenshot(path=OUT + "/after-save-banner.png")

    # Validation error path.
    page.evaluate("document.getElementById('config').scrollIntoView()")
    page.fill('input[data-env="CASCADE_BUDGET"]', "60")
    page.wait_for_timeout(300)
    page.click("#save-settings")
    page.wait_for_timeout(1200)
    check("field error rendered", page.locator(".field-error").count() >= 1)
    page.screenshot(path=OUT + "/config-error.png")
    page.click("#discard-settings")

    # Overrides panel.
    page.evaluate("document.getElementById('overrides').scrollIntoView()")
    page.wait_for_timeout(500)
    check("override textareas render", page.locator("textarea[data-override]").count() == 4)
    page.fill('textarea[data-override="feed_slug_overrides"]', "nfl-redzone = nfl-vs-redzone")
    page.wait_for_timeout(300)
    page.click("#save-settings")
    page.wait_for_timeout(1200)
    page.evaluate("document.getElementById('overrides').scrollIntoView()")
    page.wait_for_timeout(400)
    page.screenshot(path=OUT + "/overrides.png")

    # Reload: banner persists from the server-side pending set.
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    check("banner persists after reload", page.locator("#restart-banner").is_visible())
    page.click("#theme-toggle")
    page.wait_for_timeout(400)
    page.screenshot(path=OUT + "/light-signed-in.png")

    # Mobile viewport.
    m = browser.new_page(viewport={"width": 414, "height": 900})
    m.goto(BASE + "/", wait_until="networkidle")
    m.wait_for_timeout(1200)
    m.screenshot(path=OUT + "/mobile.png")
    m.close()

    # Sign out.
    page.click("#sign-out")
    try:
        page.wait_for_url("**/login*", timeout=8000)
    except Exception:
        pass
    check("sign out returns to login", "/login" in page.url)
    browser.close()

print("\nbrowser problems:", problems if problems else "none")
print("RENDER " + ("OK" if not fails and not problems else "FAILED: " + "; ".join(fails + problems)))
sys.exit(1 if fails or problems else 0)
