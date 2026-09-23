"""Browser render check for the console's editing UI. Not shipped.

Run inside the image with the script and an output directory bind mounted:

    mkdir -p _shots && chmod 777 _shots
    docker run --rm -e STARTUP_DELAY=0 -e CONSOLE_PASSWORD=render-pw \
        -v $PWD/tools/check_render.py:/tmp/render.py:ro -v $PWD/_shots:/out \
        <image> python /tmp/render.py

Starts the real app on a loopback port, signs in with Chromium, edits two
settings (one live, one restart-only), saves, and asserts the badges and the
restart banner appear and survive a reload. Screenshots land in /out.

Run it a second time with **no** CONSOLE_PASSWORD and it takes the read-only
path instead: the console opens without a login and every control that writes
is inert. That is a browser question, not an API one — the API's refusal is
already covered by check_console.py; what is checked here is that the page
never offers what it would then have to refuse.

Multi-view is turned on unless the caller says otherwise, because the
multi-player section is the half of the console Python cannot check at all: a
corner that does not move, a slider that does not travel and a rule enforced
only in the renderer all look identical to a passing API. The section's
absence with the feature off is check_console.py's job, and every multi-view
step here is skipped when the section is not in the page, so a run with
MULTIVIEW_ENABLE=0 still passes.
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
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["PORT"] = "8899"
os.environ.setdefault("MULTIVIEW_ENABLE", "1")
PASSWORD = os.environ.get("CONSOLE_PASSWORD", "")
READONLY = not PASSWORD

sys.path.insert(0, "/app")
import app  # noqa: E402

app._install_seed_roster()
app._load_team_roster()

# The read-only pass needs something to be read-only about: an empty slot
# disables its mixer because there is no channel to set a level for, which
# would make "the mixer is inert" true for the wrong reason.
if READONLY and app.MULTIVIEW_ENABLE:
    _known = set(app._team_roster)
    _teams = sorted(s for s, v in app._team_roster.items()
                    if (v or {}).get("kind", "team") == "team")
    app.multiview.set_slot("1", {"primary": _teams[0], "secondary": _teams[1],
                                 "corner": "br", "size": "medium"},
                           path=app.multiview.MULTIVIEW_FILE,
                           slots=app.MULTIVIEW_SLOTS, known=_known)
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

    if READONLY:
        # No password: open, and offering nothing it would have to refuse.
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_timeout(2000)
        check("no password: the console opens without a login", "/login" not in page.url)
        if page.locator("#multiview").count():
            page.evaluate("document.getElementById('multiview').scrollIntoView({behavior:'instant',block:'start'})")
            page.wait_for_selector("#mv-panel .mv-stage", timeout=10000)
            check("no password: the section still renders",
                  page.locator(".mv-stage").count() == 1)
            check("no password: the stored slot is still shown",
                  page.locator(".mv-mini").count() == 1)
            check("no password: the channel pickers are inert",
                  page.locator('[data-mv-open="primary"]').is_disabled())
            check("no password: the corner picker is inert",
                  page.locator('[data-mv-corner="tl"]').is_disabled())
            check("no password: the size picker is inert",
                  page.locator('[data-mv-size="large"]').is_disabled())
            check("no password: the mixer is inert",
                  page.locator('[data-mv-gain="primary"]').is_disabled())
            check("no password: and the page says why",
                  "CONSOLE_PASSWORD" in page.locator("#multiview").inner_text())
            page.screenshot(path=OUT + "/multiview-readonly.png")
            page.click("#theme-toggle")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT + "/multiview-readonly-light.png")
        browser.close()
        print("\nbrowser problems:", problems if problems else "none")
        print("RENDER " + ("OK" if not fails and not problems
                           else "FAILED: " + "; ".join(fails + problems)))
        sys.exit(1 if fails or problems else 0)

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
    check("power button present", page.locator("#power-btn").count() == 1)
    page.click("#power-btn")
    page.wait_for_timeout(200)
    check("power menu opens", page.locator("#power-menu").is_visible())
    check("Restart label is typed out", page.locator("#restart-services").inner_text().strip() == "Restart")
    page.screenshot(path=OUT + "/console-signed-in.png")
    page.click("#power-btn")
    page.wait_for_timeout(150)
    check("power menu closes on toggle", page.locator("#power-menu").is_hidden())

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

    # ── Multi-player ─────────────────────────────────────────────────────
    # Every control is driven the way a person drives it, and each one is
    # judged by what moved on the page rather than by what the API answered.
    if page.locator("#multiview").count():
        page.evaluate("document.getElementById('multiview').scrollIntoView({behavior:'instant',block:'start'})")
        page.wait_for_selector("#mv-panel .mv-stage", timeout=10000)
        check("the section renders a stage", page.locator(".mv-stage").count() == 1)
        check("both slots get a tab", page.locator("[data-mv-tab]").count() == 2)

        # The rule the feature exists to obey, checked where a person meets it.
        if page.locator('[data-mv-clear="primary"]').count():
            page.click('[data-mv-clear="primary"]')
            page.wait_for_timeout(900)
        check("no miniplayer is drawn without a main picture",
              page.locator(".mv-mini").count() == 0)
        check("and the miniplayer cannot be chosen first",
              page.locator('[data-mv-open="secondary"]').is_disabled())
        # The panel shrinks when the slot empties, which can carry the section
        # off screen; a screenshot of the configuration table proves nothing.
        page.evaluate("document.getElementById('multiview').scrollIntoView({behavior:'instant',block:'start'})")
        page.wait_for_timeout(600)
        page.screenshot(path=OUT + "/multiview-empty.png")

        def pick(role, query):
            page.click('[data-mv-open="%s"]' % role)
            page.wait_for_selector("#mv-search", timeout=5000)
            page.click('[data-mv-scope="all"]')
            page.fill("#mv-search", query)
            page.wait_for_timeout(500)
            page.click(".mv-option")
            page.wait_for_timeout(1500)

        pick("primary", "Kansas City Chiefs")
        check("choosing a main picture fills the stage",
              "Kansas City Chiefs" in page.locator(".mv-stage").inner_text())
        check("and unlocks the miniplayer",
              not page.locator('[data-mv-open="secondary"]').is_disabled())
        pick("secondary", "Baltimore Orioles")
        check("choosing a miniplayer draws one", page.locator(".mv-mini").count() == 1)
        check("labelled with its own channel",
              "Baltimore Orioles" in page.locator(".mv-mini").inner_text())

        before = page.locator(".mv-mini").bounding_box()
        page.click('[data-mv-corner="tl"]')
        page.wait_for_timeout(1000)
        after = page.locator(".mv-mini").bounding_box()
        check("the corner picker moves the miniplayer",
              after["x"] < before["x"] and after["y"] < before["y"])
        page.click('[data-mv-size="large"]')
        page.wait_for_timeout(1000)
        bigger = page.locator(".mv-mini").bounding_box()
        check("the size picker resizes it", bigger["width"] > after["width"])

        # The slider is driven from the keyboard, so the events are the ones a
        # real drag sends: input while it travels, change when it is let go.
        sec = page.locator('[data-mv-gain="secondary"]')
        check("the miniplayer starts silent, as the default says",
              sec.input_value() == "0")
        sec.focus()
        for _ in range(8):
            page.keyboard.press("ArrowRight")
        page.wait_for_timeout(1500)
        check("the mixer travels", sec.input_value() == "40")
        check("and says so in words", "40%" in page.locator("#multiview").inner_text())
        page.evaluate("document.getElementById('multiview').scrollIntoView({behavior:'instant',block:'start'})")
        page.wait_for_timeout(300)
        page.screenshot(path=OUT + "/multiview-configured.png")
        page.click("#theme-toggle")
        page.wait_for_timeout(500)
        page.screenshot(path=OUT + "/multiview-light.png")
        page.click("#theme-toggle")
        page.wait_for_timeout(400)

        # All of it went through the API, so all of it survives a reload.
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.evaluate("document.getElementById('multiview').scrollIntoView({behavior:'instant',block:'start'})")
        page.wait_for_selector(".mv-mini", timeout=10000)
        text = page.locator("#multiview").inner_text()
        check("the selection survives a reload",
              "Kansas City Chiefs" in text and "Baltimore Orioles" in text)
        check("so does the size", page.locator(
            '[data-mv-size="large"][aria-pressed="true"]').count() == 1)
        check("so does the corner", page.locator(
            '[data-mv-corner="tl"][aria-pressed="true"]').count() == 1)
        check("so does the mix",
              page.locator('[data-mv-gain="secondary"]').input_value() == "40")

    # Reload: banner persists from the server-side pending set.
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    check("banner persists after reload", page.locator("#restart-banner").is_visible())
    page.click("#theme-toggle")
    page.wait_for_timeout(400)
    page.screenshot(path=OUT + "/light-signed-in.png")

    # Mobile viewport.
    # The same page, resized, rather than a second one: a fresh page means a
    # fresh context with no session, and the phone-sized shot would be of the
    # login form. Playwright refuses new_page() on this context anyway.
    page.set_viewport_size({"width": 414, "height": 900})
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.screenshot(path=OUT + "/mobile.png")
    if page.locator("#multiview").count():
        # The section is the console's only two-column block, so its collapse
        # is worth a picture rather than a media query read back to itself.
        page.evaluate("document.getElementById('multiview')"
                      ".scrollIntoView({behavior:'instant',block:'start'})")
        page.wait_for_timeout(1500)
        check("the section fits a phone",
              page.locator(".mv-stage").bounding_box()["width"] < 414)
        page.screenshot(path=OUT + "/mobile-multiview.png")
    page.set_viewport_size({"width": 1440, "height": 1000})

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
