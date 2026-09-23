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
import threading
import time

DATA = tempfile.mkdtemp(prefix="check-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
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

section("seed lineup")
app.lineup.reload(os.environ["LINEUP_FILE"])
lf = os.environ["LINEUP_FILE"]
check("seed lineup installed on empty /data", os.path.exists(lf))
seed_lu = app.lineup.current()[0]
check("seed lineup policy is allowlist", seed_lu.get("policy") == "allowlist", seed_lu.get("policy"))
check("seed MLB slug in lineup", app.lineup.in_lineup(seed_lu, "philadelphia-phillies"))
check("seed soccer slug out of lineup", not app.lineup.in_lineup(seed_lu, "1-fc-heidenheim-1846"))
all_teams = c.get("/teams?all=1").get_json() or {}
philly = (all_teams.get("roster") or {}).get("philadelphia-phillies") or {}
soccer = (all_teams.get("roster") or {}).get("1-fc-heidenheim-1846") or {}
check("/teams?all=1 carries in_lineup", "in_lineup" in philly and "league" in philly)
check("seed MLB in_lineup true", philly.get("in_lineup") is True, philly.get("in_lineup"))
check("seed soccer in_lineup false", soccer.get("in_lineup") is False, soccer.get("in_lineup"))
check("seed MLB league", philly.get("league") == "mlb", philly.get("league"))
one = c.get("/teams?team=Philadelphia%20Phillies").get_json() or {}
check("/teams?team= carries in_lineup", one.get("in_lineup") is True and one.get("league") == "mlb")

section("implicit-all lineup (existing /data)")
os.remove(lf)
app.lineup.reload(lf)
impl, implicit = app.lineup.current()
check("missing file is implicit all", implicit is True and impl.get("policy") == "all")
all_again = c.get("/teams?all=1").get_json() or {}
check("existing roster no lineup.json => all in_lineup",
      all((e or {}).get("in_lineup") for e in (all_again.get("roster") or {}).values()),
      "some out")
# Restore the seed allowlist so later checks see a file again.
app.lineup.install_seed(lf, app.SEED_LINEUP_FILE, teams_existed=False)
app.lineup.reload(lf)

section("lineup membership unit")
L = app.lineup
all_doc = {"version": 1, "policy": "all", "include": [], "exclude": ["hide-me"]}
check("all: in unless excluded", L.in_lineup(all_doc, "keep-me") and not L.in_lineup(all_doc, "hide-me"))
allow = {"version": 1, "policy": "allowlist", "include": ["keep-me"], "exclude": []}
check("allowlist: only include", L.in_lineup(allow, "keep-me") and not L.in_lineup(allow, "hide-me"))
removed = L.apply_op(all_doc, "remove", ["keep-me"])
check("all − adds exclude", "keep-me" in removed["exclude"] and removed["policy"] == "all")
added = L.apply_op(removed, "add", ["keep-me"])
check("all + clears exclude", "keep-me" not in added["exclude"])
allow2 = L.apply_op(allow, "add", ["new-one"])
check("allowlist + adds include", "new-one" in allow2["include"])
allow3 = L.apply_op(allow2, "remove", ["keep-me"])
check("allowlist − drops include", "keep-me" not in allow3["include"] and "new-one" in allow3["include"])

section("sync hide helper")
import dispatcharr_sync as sync
check("stream slug from URL",
      sync.stream_team_slug({"url": "http://x/stream?team=boston-red-sox"}) == "boston-red-sox")
check("stream slug ignores name",
      sync.stream_team_slug({"name": "Boston Red Sox", "url": "http://x/stream?team=other-slug"}) == "other-slug")
# A composite is addressed by slot, so its slug comes from the tvg-id. Found
# in Phase 12: without this the Multi-Player channels were never created.
check("composite stream slug from tvg-id",
      sync.stream_team_slug({"name": "Multi-Player 1", "tvg_id": "streamed.feed.multi-player-1",
                             "url": "http://x/stream?multi=1"}) == "multi-player-1")
check("team= still wins over the tvg-id",
      sync.stream_team_slug({"tvg_id": "streamed.team.wrong",
                             "url": "http://x/stream?team=right"}) == "right")
check("a foreign tvg-id gives no slug",
      sync.stream_team_slug({"tvg_id": "atwill-1.slot", "url": "http://y/play/1"}) is None)
check("nothing to go on gives no slug", sync.stream_team_slug({}) is None)
fake_channels = [
    {"id": 1, "tvg_id": "streamed.team.a", "hidden_from_output": False},
    {"id": 2, "tvg_id": "streamed.team.b", "hidden_from_output": False},
    {"id": 3, "tvg_id": "streamed.team.c", "hidden_from_output": True},
    {"id": 4, "tvg_id": "other.x", "hidden_from_output": False},
]
slug_of = lambda ch: (ch["tvg_id"].split(".", 2)[2]
                      if str(ch.get("tvg_id") or "").startswith("streamed.") else None)
diffs = sync.visibility_diffs(fake_channels, {"a": True, "b": False, "c": False}, slug_of)
check("hide diffs only changes",
      diffs == [{"id": 2, "hidden_from_output": True}], diffs)
dups = [
    {"id": 10, "tvg_id": "streamed.team.a", "hidden_from_output": True, "channel_number": 5},
    {"id": 11, "tvg_id": "streamed.team.a", "hidden_from_output": False, "channel_number": 99},
]
dup_diffs = sync.visibility_diffs(dups, {"a": True}, slug_of)
check("duplicate slug keeps lowest number",
      dup_diffs == [
          {"id": 10, "hidden_from_output": False},
          {"id": 11, "hidden_from_output": True},
      ], dup_diffs)

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

section("dockerctl")
D = app.dockerctl
check("socket unavailable in harness", D.available() is False)
check("allowlist default includes both",
      D.CONTAINERS == ["streamed-m3u", "streamed-m3u-sync"], D.CONTAINERS)
check("unknown name rejected", D.restart_one("not-a-container").get("error") == "not_allowlisted")
check("self without socket is unavailable",
      D.restart_one("streamed-m3u").get("error") == "docker_unavailable")
check("sync without socket is unavailable",
      D.restart_one("streamed-m3u-sync").get("error") == "docker_unavailable")
_called = []
_result = D.restart_services(self_exit=lambda: _called.append(1), delay=0)
check("restart_services refuses without socket", _result.get("ok") is False, _result)
check("self_exit not called when unavailable", _called == [])

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
        r = c.put("/api/lineup", json={"op": "remove", "slugs": ["philadelphia-phillies"]})
        check("no password: lineup write is 403", r.status_code == 403 and r.get_json().get("error") == "console_readonly", r.status_code)
        lu_r = c.get("/api/lineup")
        lu = lu_r.get_json() or {}
        check("no password: GET /api/lineup open", lu_r.status_code == 200 and "policy" in lu, lu_r.status_code)
        check("no password: lineup groups present", any(g.get("id") == "mlb" for g in lu.get("groups") or []))
        js = c.get("/static/dashboard.js").get_data(as_text=True)
        html = c.get("/").get_data(as_text=True)
        check("console html has lineup scope chips", 'id="roster-scope-lineup"' in html and 'id="roster-scope-hidden"' in html)
        check("console js has lineup actions", "data-lineup-add" in js and "/api/lineup" in js)
        css = c.get("/static/dashboard.css").get_data(as_text=True)
        check("console css has circle button", ".btn-circle" in css)
        before = c.get("/playlist-teams.m3u").get_data(as_text=True).count("#EXTINF")
        check("playlist still has every roster row", before == len(app._team_roster), "%s vs %s" % (before, len(app._team_roster)))
        ov = c.get("/api/overview").get_json() or {}
        check("no password: overview reports restart unavailable",
              ov.get("restart", {}).get("available") is False, ov.get("restart"))
        html = c.get("/").get_data(as_text=True)
        check("console html has power button", 'id="power-btn"' in html)
        check("console html has Restart label", 'id="restart-services"' in html and ">Restart<" in html)
        css = c.get("/static/dashboard.css").get_data(as_text=True)
        check("console css has power menu", ".power-menu" in css and ".power-menu-item" in css)
        js = c.get("/static/dashboard.js").get_data(as_text=True)
        check("console js wires restart", 'id="restart-services"' in html and "/api/restart" in js)
        r = c.post("/api/restart", json={})
        check("no password: restart without socket is 503",
              r.status_code == 503 and r.get_json().get("error") == "docker_unavailable",
              r.status_code)
        calls = []
        def fake_restart():
            calls.append(True)
            return {"ok": True, "restarting": ["streamed-m3u", "streamed-m3u-sync"],
                    "results": []}
        app.dashboard._restart_services = fake_restart
        r = c.post("/api/restart", json={})
        check("no password: mocked restart is 202",
              r.status_code == 202 and r.get_json().get("ok") is True, r.status_code)
        check("no password: mocked restart invoked", calls == [True], calls)
        app.dashboard._restart_services = app.dockerctl.restart_services
        r = c.post("/api/streams/not-hex/disconnect", json={})
        check("no password: disconnect invalid id is 400", r.status_code == 400 and r.get_json().get("error") == "invalid_id", r.status_code)
        r = c.post("/api/streams/deadbeef/disconnect", json={})
        check("no password: disconnect missing id is 404", r.status_code == 404 and r.get_json().get("error") == "not_found", r.status_code)
        ev = threading.Event()
        with app._active_streams_lock:
            app._active_streams["deadbeef"] = {
                "stream_id": "deadbeef", "stop": ev,
                "hold_key": "team:philadelphia-eagles",
            }
        r = c.post("/api/streams/deadbeef/disconnect", json={})
        check("no password: disconnect sets stop", r.status_code == 200 and ev.is_set(), r.status_code)
        check("no password: hold refuses team reconnect",
              c.get("/stream?team=philadelphia-eagles").status_code == 410)
        check("no password: hold does not affect HEAD",
              c.head("/stream?team=philadelphia-eagles").status_code == 200)
        check("no password: other team is not held",
              c.get("/stream?team=boston-red-sox").status_code != 410)
        with app._active_streams_lock:
            app._active_streams.pop("deadbeef", None)
        with app._disconnect_holds_lock:
            app._disconnect_holds.clear()

        section("extract cache console actions")
        js = c.get("/static/dashboard.js").get_data(as_text=True)
        check("console js has extract refresh button", "data-cache-refresh" in js)
        check("console js has extract clear button", "data-cache-clear" in js)
        css = c.get("/static/dashboard.css").get_data(as_text=True)
        check("console css has warn button", ".btn-warn" in css)
        EMBED = "https://embed.example/ppv-philadelphia-eagles"
        with app._extract_cache_lock:
            app._extract_cache[EMBED] = {
                "data": {"url": "https://cdn.example/old.m3u8"},
                "ts": time.time(),
            }
        r = c.post("/api/cache/extract/clear", json={})
        check("no password: clear missing url is 400",
              r.status_code == 400 and r.get_json().get("error") == "missing_url", r.status_code)
        r = c.post("/api/cache/extract/refresh", json={"embed_url": "https://embed.example/nope"})
        check("no password: refresh unknown is 404", r.status_code == 404, r.status_code)
        r = c.post("/api/cache/extract/clear", json={"embed_url": "https://embed.example/nope"})
        check("no password: clear unknown is 404", r.status_code == 404, r.status_code)

        original = app.extract_m3u8_via_browser
        calls = []
        def fake_ok(embed_url, force=False):
            calls.append((embed_url, force))
            data = {"url": "https://cdn.example/fresh.m3u8", "headers": {}, "cookies": {}}
            with app._extract_cache_lock:
                app._extract_cache[embed_url] = {"data": data, "ts": time.time()}
            return data
        app.extract_m3u8_via_browser = fake_ok
        r = c.post("/api/cache/extract/refresh", json={"embed_url": EMBED})
        check("no password: refresh 200", r.status_code == 200, r.status_code)
        check("no password: refresh forced extract", calls == [(EMBED, True)], calls)
        cached = (c.get("/api/cache").get_json() or {}).get("extract", {}).get("entries", [])
        check("no password: refresh replaced m3u8",
              any(e.get("embed_url") == EMBED and e.get("m3u8_url") == "https://cdn.example/fresh.m3u8"
                  for e in cached))

        def fake_fail(embed_url, force=False):
            return None
        app.extract_m3u8_via_browser = fake_fail
        r = c.post("/api/cache/extract/refresh", json={"embed_url": EMBED})
        check("no password: failed refresh is 502", r.status_code == 502, r.status_code)
        cached = (c.get("/api/cache").get_json() or {}).get("extract", {}).get("entries", [])
        check("no password: failed refresh keeps entry",
              any(e.get("embed_url") == EMBED and e.get("m3u8_url") == "https://cdn.example/fresh.m3u8"
                  for e in cached))
        app.extract_m3u8_via_browser = original

        r = c.post("/api/cache/extract/clear", json={"embed_url": EMBED})
        check("no password: clear 200", r.status_code == 200, r.status_code)
        cached = (c.get("/api/cache").get_json() or {}).get("extract", {}).get("entries", [])
        check("no password: clear removed entry",
              not any(e.get("embed_url") == EMBED for e in cached))
        r = c.post("/api/cache/extract/refresh", json={"embed_url": EMBED})
        check("no password: refresh of cleared entry is 404", r.status_code == 404, r.status_code)
    else:
        r = c.get("/")
        check("gated: / redirects to login", r.status_code == 302 and "/login" in r.headers.get("Location", ""), r.status_code)
        check("gated: /api/overview 401", c.get("/api/overview").status_code == 401)
        check("gated: /stream/status denied", c.get("/stream/status").status_code in (302, 401))
        check("gated: disconnect 401", c.post("/api/streams/deadbeef/disconnect", json={}).status_code == 401)
        check("gated: restart 401", c.post("/api/restart", json={}).status_code == 401)
        check("gated: extract refresh 401",
              c.post("/api/cache/extract/refresh", json={"embed_url": "x"}).status_code == 401)
        check("gated: extract clear 401",
              c.post("/api/cache/extract/clear", json={"embed_url": "x"}).status_code == 401)
        # Multi-view is off in this harness, so these prove the gate rather
        # than the feature: the guard is the blueprint's, not the feature's.
        check("gated: multiview read 401", c.get("/api/multiview").status_code == 401)
        check("gated: multiview write 401",
              c.put("/api/multiview/1", json={"corner": "tl"}).status_code == 401)
        check("gated: multiview stop 401",
              c.post("/api/multiview/1/stop", json={}).status_code == 401)
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
        section("lineup write endpoint")
        before = c.get("/playlist-teams.m3u").get_data(as_text=True).count("#EXTINF")
        r = c.put("/api/lineup", json={"op": "remove", "slugs": ["philadelphia-phillies"]})
        check("PUT lineup without CSRF is 403", r.status_code == 403 and r.get_json().get("error") == "csrf", r.status_code)
        r = c.put("/api/lineup", json={"op": "remove", "slugs": ["philadelphia-phillies"]}, headers=H)
        d = r.get_json() or {}
        check("PUT lineup remove slug 200", r.status_code == 200 and d.get("ok") is True, r.status_code)
        check("removed slug now out",
              (c.get("/teams?all=1").get_json() or {}).get("roster", {}).get("philadelphia-phillies", {}).get("in_lineup") is False)
        r = c.put("/api/lineup", json={"op": "add", "slugs": ["philadelphia-phillies"]}, headers=H)
        check("PUT lineup add slug 200", r.status_code == 200)
        check("added slug back in",
              (c.get("/teams?all=1").get_json() or {}).get("roster", {}).get("philadelphia-phillies", {}).get("in_lineup") is True)
        r = c.put("/api/lineup", json={"op": "remove", "group": "mlb"}, headers=H)
        check("PUT lineup remove group 200", r.status_code == 200, r.status_code)
        mlb_out = (c.get("/teams?all=1").get_json() or {}).get("roster", {}).get("boston-red-sox", {})
        check("group remove hid an MLB slug", mlb_out.get("in_lineup") is False, mlb_out.get("in_lineup"))
        r = c.put("/api/lineup", json={"op": "add", "group": "mlb"}, headers=H)
        check("PUT lineup add group 200", r.status_code == 200)
        check("group add restored MLB slug",
              (c.get("/teams?all=1").get_json() or {}).get("roster", {}).get("boston-red-sox", {}).get("in_lineup") is True)
        after = c.get("/playlist-teams.m3u").get_data(as_text=True).count("#EXTINF")
        check("playlist #EXTINF unchanged after lineup edits", after == before, "%s -> %s" % (before, after))
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
        r = c.post("/api/restart", json={})
        check("restart without CSRF is 403", r.status_code == 403 and r.get_json().get("error") == "csrf", r.status_code)
        calls = []
        def fake_restart():
            calls.append(True)
            return {"ok": True, "restarting": ["streamed-m3u", "streamed-m3u-sync"],
                    "results": []}
        app.dashboard._restart_services = fake_restart
        r = c.post("/api/restart", json={}, headers=H)
        check("restart with CSRF is 202",
              r.status_code == 202 and r.get_json().get("ok") is True, r.status_code)
        check("restart with CSRF invoked", calls == [True], calls)
        app.dashboard._restart_services = app.dockerctl.restart_services
        r = c.post("/api/streams/deadbeef/disconnect", json={})
        check("disconnect without CSRF is 403", r.status_code == 403 and r.get_json().get("error") == "csrf", r.status_code)
        r = c.post("/api/cache/extract/clear", json={"embed_url": "https://embed.example/x"})
        check("extract clear without CSRF is 403", r.status_code == 403 and r.get_json().get("error") == "csrf", r.status_code)
        ev = threading.Event()
        with app._active_streams_lock:
            app._active_streams["deadbeef"] = {
                "stream_id": "deadbeef", "stop": ev,
                "hold_key": "team:philadelphia-eagles",
            }
        r = c.post("/api/streams/deadbeef/disconnect", json={}, headers=H)
        check("disconnect with CSRF sets stop", r.status_code == 200 and ev.is_set(), r.status_code)
        check("hold refuses team reconnect",
              c.get("/stream?team=philadelphia-eagles").status_code == 410)
        check("hold does not affect HEAD",
              c.head("/stream?team=philadelphia-eagles").status_code == 200)
        with app._active_streams_lock:
            app._active_streams.pop("deadbeef", None)
        with app._disconnect_holds_lock:
            app._disconnect_holds.clear()
        EMBED = "https://embed.example/ppv-philadelphia-eagles"
        with app._extract_cache_lock:
            app._extract_cache[EMBED] = {
                "data": {"url": "https://cdn.example/old.m3u8"},
                "ts": time.time(),
            }
        original = app.extract_m3u8_via_browser
        app.extract_m3u8_via_browser = (
            lambda embed_url, force=False: {"url": "https://cdn.example/fresh.m3u8"}
        )
        r = c.post("/api/cache/extract/refresh", json={"embed_url": EMBED}, headers=H)
        check("refresh with CSRF 200", r.status_code == 200, r.status_code)
        r = c.post("/api/cache/extract/clear", json={"embed_url": EMBED}, headers=H)
        check("clear with CSRF 200", r.status_code == 200, r.status_code)
        cached = (c.get("/api/cache").get_json() or {}).get("extract", {}).get("entries", [])
        check("clear with CSRF removed entry",
              not any(e.get("embed_url") == EMBED for e in cached))
        app.extract_m3u8_via_browser = original
        cfg = c.get("/api/config").get_json()
        check("config editable + overrides present", cfg.get("editable") is True and "feed_slug_overrides" in cfg.get("overrides", {}))
        r = c.post("/logout", headers=H, json={})
        check("logout ok", r.status_code == 200)
        check("after logout / redirects", c.get("/").status_code == 302)

# ─── Multi-view, Phase 2 ──────────────────────────────────────────────────────
# Settings schema and the /data/multiview.json document. No channels and no
# encoder yet, so what is checked here is that the configuration is exposed,
# and that a hand-edited file cannot produce a slot the later phases would
# then have to defend against.
section("multi-view settings")
import multiview  # noqa: E402
import settings as _st  # noqa: E402

# With a password set the section above ends logged out, and /api/config is
# gated — so sign back in rather than reading a redirect as an empty config.
if PASSWORD:
    c.post("/login", data={"password": PASSWORD, "next": "/"})
cfg = c.get("/api/config").get_json() or {}
by_env = {}
for grp in (cfg.get("groups") or []):
    for s in (grp.get("settings") or []):
        by_env[s.get("env")] = s

for env in ("MULTIVIEW_ENABLE", "MULTIVIEW_SLOTS", "MULTIVIEW_NAME",
            "MULTIVIEW_MAX_ACTIVE", "MULTIVIEW_ENCODER", "MULTIVIEW_QP",
            "MULTIVIEW_IDLE_TIMEOUT", "MULTIVIEW_FILE", "MULTIVIEW_RENDER_NODE"):
    check("/api/config exposes %s" % env, env in by_env)

check("MULTIVIEW_ENABLE defaults off", app.MULTIVIEW_ENABLE is False, app.MULTIVIEW_ENABLE)
check("MULTIVIEW_QP carries bounds",
      by_env.get("MULTIVIEW_QP", {}).get("min") == 1
      and by_env.get("MULTIVIEW_QP", {}).get("max") == 51)
check("MULTIVIEW_ENCODER offers vaapi and cpu",
      tuple(by_env.get("MULTIVIEW_ENCODER", {}).get("choices") or ()) == ("vaapi", "cpu"),
      by_env.get("MULTIVIEW_ENCODER", {}).get("choices"))
check("MULTIVIEW_FILE is environment only",
      by_env.get("MULTIVIEW_FILE", {}).get("editable") is False)

# The cap means nothing above the slot count, so the schema refuses it.
_, _, errs = _st.validate({"MULTIVIEW_MAX_ACTIVE": 4}, {},
                          {"MULTIVIEW_SLOTS": 2, "MULTIVIEW_MAX_ACTIVE": 1})
check("MAX_ACTIVE above SLOTS is rejected", "_cross" in errs, errs.get("_cross"))
_, _, errs = _st.validate({"MULTIVIEW_MAX_ACTIVE": 2}, {},
                          {"MULTIVIEW_SLOTS": 2, "MULTIVIEW_MAX_ACTIVE": 1})
check("MAX_ACTIVE equal to SLOTS is allowed", "_cross" not in errs, errs)

section("multi-view document")
MV = os.path.join(DATA, "multiview.json")
mv_empty = multiview.normalize({}, slots=2)
check("empty doc squares up to the slot count", sorted(mv_empty["slots"]) == ["1", "2"])
check("empty slot is unconfigured", not multiview.configured(mv_empty["slots"]["1"]))

known = {"kansas-city-chiefs", "baltimore-orioles"}
slot = multiview.normalize_slot(
    {"primary": "Kansas-City-Chiefs", "secondary": "baltimore-orioles",
     "corner": "tl", "size": "large", "audio": {"primary": 80, "secondary": 40}}, known)
check("primary is lowercased", slot["primary"] == "kansas-city-chiefs", slot["primary"])
check("secondary kept alongside a primary", slot["secondary"] == "baltimore-orioles")
check("corner and size kept", slot["corner"] == "tl" and slot["size"] == "large")
check("gains kept", slot["audio"] == {"primary": 80, "secondary": 40}, slot["audio"])

# The rule the console enforces in the UI, enforced again for hand edits.
orphan = multiview.normalize_slot({"secondary": "baltimore-orioles"}, known)
check("secondary without a primary is dropped", orphan["secondary"] == "")
check("orphaned secondary is muted", orphan["audio"]["secondary"] == 0)

gone = multiview.normalize_slot(
    {"primary": "kansas-city-chiefs", "secondary": "team-that-left"}, known)
check("unknown secondary is dropped", gone["secondary"] == "")
gone = multiview.normalize_slot({"primary": "team-that-left"}, known)
check("unknown primary leaves the slot unconfigured", not multiview.configured(gone))

same = multiview.normalize_slot(
    {"primary": "kansas-city-chiefs", "secondary": "kansas-city-chiefs"}, known)
check("a slug cannot be shown against itself", same["secondary"] == "")

bad = multiview.normalize_slot(
    {"primary": "kansas-city-chiefs", "corner": "middle", "size": "enormous",
     "audio": {"primary": 900, "secondary": -5}}, known)
check("bad corner falls back", bad["corner"] == multiview.DEFAULT_CORNER, bad["corner"])
check("bad size falls back", bad["size"] == multiview.DEFAULT_SIZE, bad["size"])
check("gains are clamped", bad["audio"]["primary"] == 100, bad["audio"])
check("slot survives every field being wrong", multiview.configured(bad))

section("multi-view persistence")
stored = multiview.set_slot("1", {"primary": "kansas-city-chiefs",
                                  "secondary": "baltimore-orioles",
                                  "corner": "tr", "size": "small"},
                            path=MV, slots=2, known=known)
check("set_slot returns what was stored", stored["corner"] == "tr", stored["corner"])
check("set_slot stamps updated", bool(stored.get("updated")))
check("file written", os.path.exists(MV))
again = multiview.load(MV, slots=2)
check("round-trips from disk",
      again["slots"]["1"]["primary"] == "kansas-city-chiefs"
      and again["slots"]["1"]["corner"] == "tr")
check("untouched slot stays empty", not multiview.configured(again["slots"]["2"]))
check("sources lists primary first",
      multiview.sources(again["slots"]["1"]) == ["kansas-city-chiefs", "baltimore-orioles"])

multiview.clear_slot("1", path=MV, slots=2)
check("clear_slot empties the slot",
      not multiview.configured(multiview.load(MV, slots=2)["slots"]["1"]))

# Same discipline as the roster: the corrupt file is moved aside, the .bak is
# tried, and only with nothing to recover does the slot go empty. Never fatal
# on either path. A .bak exists here because the slot was written twice above.
with open(MV, "w") as fh:
    fh.write("{not json at all")
salvaged = multiview.load(MV, slots=2)
check("corrupt file recovers from .bak rather than raising",
      sorted(salvaged["slots"]) == ["1", "2"], sorted(salvaged["slots"]))
check("corrupt original is moved aside",
      any(f.startswith("multiview.json.corrupt-") for f in os.listdir(DATA)))

# Nothing to recover from: degrade to empty rather than carrying a half-read
# document forward.
MV2 = os.path.join(DATA, "multiview-nobak.json")
with open(MV2, "w") as fh:
    fh.write("not json either")
bare = multiview.load(MV2, slots=2)
check("corrupt file with no backup degrades to empty",
      sorted(bare["slots"]) == ["1", "2"]
      and not multiview.configured(bare["slots"]["1"]))

# Slot count changes survive in both directions.
grown = multiview.normalize({"slots": {"1": {"primary": "kansas-city-chiefs"}}}, slots=3)
check("raising the slot count adds empty slots", sorted(grown["slots"]) == ["1", "2", "3"])
shrunk = multiview.normalize(
    {"slots": {"1": {"primary": "kansas-city-chiefs"},
               "2": {"primary": "baltimore-orioles"}}}, slots=1)
check("lowering the slot count drops the extras", sorted(shrunk["slots"]) == ["1"])

# ─── Multi-view, Phase 3 ──────────────────────────────────────────────────────
# Channels: seeding, the playlist entry, the guide row and the stream route.
# The feature is off in this process, so the disabled half is checked here and
# the enabled half runs in a second process below (the slot count and the
# derived slug map are read once at import).
section("multi-view channels (disabled)")
check("no slugs derived when off", app._MULTIVIEW_BY_SLUG == {}, app._MULTIVIEW_BY_SLUG)
check("no multi entries seeded when off",
      not any(v.get("kind") == "multi" for v in app._team_roster.values()))
body, _n = app.build_team_m3u()
check("playlist carries no multi channels when off", "multi=" not in body)
r = c.get("/stream?multi=1")
check("multi tune 404s when off", r.status_code == 404, r.status_code)
r = c.get("/stream")
check("no parameter is still a 400", r.status_code == 400, r.status_code)

# The console's half of the same promise. The section is omitted from the
# markup rather than hidden by script, so a build with the feature off renders
# what it rendered before the feature existed. (Both modes reach this point
# with a session: the gated branch above signs in.)
r = c.get("/")
check("console renders", r.status_code == 200, r.status_code)
page = r.get_data(as_text=True)
check("no multi-player section when off", 'id="multiview"' not in page)
check("no multi-player anchor when off", 'href="#multiview"' not in page)
check("no multi-player copy when off", "Multi-player" not in page)

shutil.rmtree(DATA, ignore_errors=True)
print("\n%s" % ("ALL CHECKS PASSED" if not fails else "FAILURES: " + "; ".join(fails)))
sys.exit(1 if fails else 0)
