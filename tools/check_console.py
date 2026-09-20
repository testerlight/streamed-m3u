"""Verification harness for the streamed-m3u image. Not shipped.

Run inside a container built from this directory, with the script bind
mounted, so the real app.py and its providers are exercised end to end:

    docker run --rm -e STARTUP_DELAY=0 [-e CONSOLE_PASSWORD=pw] \
        -v $PWD/tools/check_console.py:/tmp/check.py:ro <image> python /tmp/check.py

Uses Flask's test client, so nothing binds a port and no background threads
start. Each phase's checks are gated on the features that phase adds, so the
script runs against any build from Phase A onward.
"""

import json
import os
import shutil
import sys
import tempfile

DATA = tempfile.mkdtemp(prefix="check-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ.pop("PUBLIC_BASE_URL", None)
PASSWORD = os.environ.get("CONSOLE_PASSWORD", "")

sys.path.insert(0, "/app")
import app  # noqa: E402

c = app.app.test_client()
fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  [%s]" % detail) if detail != "" else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n=== %s ===" % title)


# ─── Phase A ──────────────────────────────────────────────────────────────────
section("seed roster")
app._install_seed_roster()
app._load_team_roster()
check("seed installed on empty /data", len(app._team_roster) > 1300, len(app._team_roster))
check("no favourites in seed", not any(v.get("favourite") for v in app._team_roster.values()))
check("only team entries in seed", all(v.get("kind") == "team" for v in app._team_roster.values()))

section("playlist address derivation")
r = c.get("/playlist-teams.m3u", base_url="http://10.0.0.5:8787")
body = r.get_data(as_text=True)
check("team playlist 200", r.status_code == 200, r.status_code)
check("team playlist uses request host", "http://10.0.0.5:8787/stream?team=" in body)
check("no hardcoded LAN address", "192.168" not in body)
app.PUBLIC_BASE_URL = "http://proxy.example:9000"
body = c.get("/playlist-teams.m3u", base_url="http://10.0.0.5:8787").get_data(as_text=True)
check("PUBLIC_BASE_URL wins over request host", "http://proxy.example:9000/stream?team=" in body)
app.PUBLIC_BASE_URL = ""
with app._cache_lock:
    app._cached_m3u = "#EXTM3U\n#EXTINF:-1,x\n" + app._BASE_PLACEHOLDER + "/stream?url=abc\n"
body = c.get("/playlist.m3u", base_url="http://10.0.0.5:8787").get_data(as_text=True)
check("legacy playlist placeholder substituted", "http://10.0.0.5:8787/stream?url=abc" in body and "{{" not in body)
ov = c.get("/api/overview").get_json() if not PASSWORD else {}
if not PASSWORD:
    check("overview reports public_base", ov.get("public_base", {}).get("configured") is False)

section("routes")
for path in ("/health", "/teams", "/prewarm", "/epg.xml", "/playlist-teams.m3u"):
    r = c.get(path)
    check("open route %s" % path, r.status_code == 200, r.status_code)

# ─── Phase B: settings layer ──────────────────────────────────────────────────
if hasattr(app, "_settings"):
    S = app._settings
    section("settings layer")
    check("BASELINE snapshot taken", "REFRESH_SECONDS" in S.BASELINE)
    check("baseline equals running value", S.BASELINE["REFRESH_SECONDS"] == app.REFRESH_SECONDS)
    ok_specs = [s for s in S.SCHEMA if s.const not in app.__dict__]
    check("every schema constant exists in app.py", not ok_specs, [s.const for s in ok_specs])

    clean, ov_clean, errs = S.validate({"CASCADE_BUDGET": 60}, {}, S.effective_values(app.__dict__))
    check("validate rejects CASCADE_BUDGET=60", "CASCADE_BUDGET" in errs, errs)
    _, _, errs = S.validate({"SEGMENT_PROBE_TIMEOUT": 400}, {}, S.effective_values(app.__dict__))
    check("validate rejects probe timeout above max", "SEGMENT_PROBE_TIMEOUT" in errs)
    eff = S.effective_values(app.__dict__)
    eff["SEGMENT_TIMEOUT"] = 30
    _, _, errs = S.validate({"SEGMENT_PROBE_TIMEOUT": 40}, {}, eff)
    check("cross rule probe <= timeout", "_cross" in errs, errs)
    _, _, errs = S.validate({"PORT": 1}, {}, eff)
    check("PORT is not editable", "PORT" in errs)
    clean, _, errs = S.validate({"LOG_LEVEL": "debug", "REQUIRE_AUDIO": "off", "PREWARM_TEAMS": "A, B"}, {}, eff)
    check("coercion cleans values", clean == {"LOG_LEVEL": "DEBUG", "REQUIRE_AUDIO": False, "PREWARM_TEAMS": ["A", "B"]}, clean)
    _, ovc, errs = S.validate({}, {"feed_slug_overrides": {"Bad Key": "x"}}, eff)
    check("slug map rejects bad slug", "overrides.feed_slug_overrides" in errs)

    section("atomic writes")
    app._save_team_roster()
    app._save_team_roster()
    tf = os.environ["TEAMS_FILE"]
    check("roster saved", os.path.exists(tf))
    check("roster .bak kept", os.path.exists(tf + ".bak"))
    check("no .tmp left", not os.path.exists(tf + ".tmp"))
    with open(tf, "w") as f:
        f.write("{not json")
    app._team_roster.clear()
    app._load_team_roster()
    check("corrupt roster recovered from .bak", len(app._team_roster) > 1300, len(app._team_roster))
    check("corrupt copy preserved", any(n.startswith("teams.json.corrupt-") for n in os.listdir(DATA)))

    import threading
    def hammer():
        for _ in range(30):
            app._save_extract_cache()
    ts = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with open(os.environ["EXTRACT_CACHE_FILE"]) as f:
        json.load(f)
    check("concurrent cache saves leave valid JSON", True)

# ─── Phase D: auth, CSRF, write endpoint ──────────────────────────────────────
if hasattr(app, "auth"):
    A = app.auth
    section("auth (%s)" % ("password set" if PASSWORD else "no password"))
    if not PASSWORD:
        for path in ("/", "/api/overview", "/api/config", "/stream/status"):
            check("no password: %s open" % path, c.get(path).status_code == 200)
        r = c.put("/api/settings", json={"settings": {}})
        check("no password: write is 403 read-only", r.status_code == 403 and r.get_json().get("error") == "console_readonly", r.status_code)
    else:
        r = c.get("/")
        check("gated: / redirects to login", r.status_code == 302 and "/login" in r.headers.get("Location", ""), r.status_code)
        check("gated: /api/overview 401", c.get("/api/overview").status_code == 401)
        check("gated: /stream/status denied", c.get("/stream/status").status_code in (302, 401))
        for path in ("/health", "/teams", "/prewarm", "/epg.xml", "/playlist-teams.m3u", "/login", "/api/session"):
            check("still open: %s" % path, c.get(path).status_code == 200)
        import time
        t0 = time.time()
        r = c.post("/login", data={"password": "wrong"})
        check("wrong password is slow", time.time() - t0 >= 1.0 and r.status_code == 200)
        r = c.post("/login", data={"password": PASSWORD, "next": "/"})
        check("right password redirects", r.status_code == 302, r.status_code)
        cookie = r.headers.get("Set-Cookie", "")
        check("cookie HttpOnly + SameSite=Lax", "HttpOnly" in cookie and "SameSite=Lax" in cookie, cookie[:80])
        check("logged in: / 200", c.get("/").status_code == 200)
        sess = c.get("/api/session").get_json()
        check("session reports editing enabled", sess.get("editing_enabled") is True)
        token = sess.get("csrf_token")
        key = os.path.join(DATA, ".secret_key")
        check(".secret_key exists 0600", os.path.exists(key) and oct(os.stat(key).st_mode & 0o777) == "0o600")

        section("write endpoint")
        r = c.put("/api/settings", json={"settings": {"REFRESH_SECONDS": 601}})
        check("PUT without CSRF token is 403", r.status_code == 403 and r.get_json().get("error") == "csrf", r.status_code)
        r = c.put("/api/settings", json={"settings": {"REFRESH_SECONDS": 601}}, headers={"X-CSRF-Token": "nope"})
        check("PUT with wrong token is 403", r.status_code == 403)
        H = {"X-CSRF-Token": token}
        r = c.put("/api/settings", json={"settings": {"CASCADE_BUDGET": 60}}, headers=H)
        check("PUT invalid value is 400 with field error", r.status_code == 400 and "CASCADE_BUDGET" in r.get_json().get("errors", {}), r.status_code)
        r = c.put("/api/settings", json={"settings": {"REFRESH_SECONDS": 601, "LOG_LEVEL": "DEBUG"}}, headers=H)
        d = r.get_json() or {}
        check("PUT live keys 200 + applied", r.status_code == 200 and set(d.get("applied", [])) == {"REFRESH_SECONDS", "LOG_LEVEL"}, d.get("applied"))
        check("live rebind took effect", app.REFRESH_SECONDS == 601)
        import logging
        check("LOG_LEVEL applied to root logger", logging.getLogger().level == logging.DEBUG)
        check("health reports new interval", c.get("/health").get_json().get("refresh_every") == "601s")
        r = c.put("/api/settings", json={"settings": {"PREWARM_TEAMS": ["Boston Bruins"]}}, headers=H)
        d = r.get_json() or {}
        check("restart key reported", "PREWARM_TEAMS" in d.get("restart_required", []), d)
        check("restart key not rebound live", app.PREWARM_TEAMS != ["Boston Bruins"])
        check("restart_pending exposed", "PREWARM_TEAMS" in c.get("/api/overview").get_json().get("restart_pending", []))
        with open(os.environ["SETTINGS_FILE"]) as f:
            saved = json.load(f)
        check("settings file written", saved["settings"].get("REFRESH_SECONDS") == 601)
        check("settings .bak exists", os.path.exists(os.environ["SETTINGS_FILE"] + ".bak"))
        r = c.put("/api/settings", json={"settings": {"REFRESH_SECONDS": None}}, headers=H)
        check("null reverts to baseline", app.REFRESH_SECONDS == app._settings.BASELINE["REFRESH_SECONDS"])
        with open(os.environ["SETTINGS_FILE"]) as f:
            check("null removes key from file", "REFRESH_SECONDS" not in json.load(f)["settings"])
        r = c.put("/api/settings", json={"overrides": {"feed_slug_overrides": {"nfl-redzone": "nfl-vs-redzone", "abc": "def"}}}, headers=H)
        d = r.get_json() or {}
        check("override saved as restart", r.status_code == 200 and "overrides.feed_slug_overrides" in d.get("restart_required", []), d)
        cfg = c.get("/api/config").get_json()
        check("config editable + overrides present", cfg.get("editable") is True and "feed_slug_overrides" in cfg.get("overrides", {}))
        r = c.post("/logout", headers=H, json={})
        check("logout ok", r.status_code == 200)
        check("after logout / redirects", c.get("/").status_code == 302)

shutil.rmtree(DATA, ignore_errors=True)
print("\n%s" % ("ALL CHECKS PASSED" if not fails else "FAILURES: " + "; ".join(fails)))
sys.exit(1 if fails else 0)
