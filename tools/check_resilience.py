"""Verification harness for Provider failure resilience (provider-swap ticket 04). Not shipped.

One seam: the served playlists, /teams, /api/overview and /api/config, driven
through Flask's test client, with the network replaced by a scripted ppv.st (a
REAL recorded list response, tools/fixtures/ppv_streams.json) and a scripted
streamed.pk. requests.get is the only thing faked, plus the wall clock, which
is moved forward to pass the retention limit. Run inside a container built
from this directory:

    docker run --rm -e STARTUP_DELAY=0 \
        -v $PWD/tools/check_resilience.py:/tmp/check.py:ro \
        -v $PWD/tools/fixtures:/tmp/fixtures:ro \
        <image> python /tmp/check.py /tmp/fixtures/ppv_streams.json
"""

import json
import logging
import os
import re
import sys
import tempfile
import time
from urllib.parse import urlparse

DATA = tempfile.mkdtemp(prefix="check-resilience-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["PROVIDERS"] = "ppv,streamed"
os.environ["PPV_BASE_URL"] = "https://ppv.example"
os.environ["STREAMED_BASE_URL"] = "https://streamed.example"
os.environ["PROVIDER_STALE_HOURS"] = "6"
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ.pop("PPV_MIRRORS", None)
os.environ.pop("STREAMED_MIRRORS", None)

sys.path.insert(0, "/app")
import app  # noqa: E402

FIXTURE = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "tools/fixtures/ppv_streams.json"))

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  [%s]" % detail) if detail != "" else ""))
    if not cond:
        fails.append(name)


# ── The scripted network ────────────────────────────────────────────────────

class Resp:
    def __init__(self, payload=None, status=200, text=None):
        self._payload, self.status_code, self.text = payload, status, text
        self.headers = {"Content-Type": "application/json"}
        self.content = b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise app.requests.HTTPError("%d" % self.status_code, response=self)

    def json(self):
        if self._payload is _NOT_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


_NOT_JSON = object()

CUBS = {
    "id": "bb-1", "title": "Chicago Cubs vs San Diego Padres",
    "category": "baseball", "date": int(time.time() + 3600) * 1000, "poster": "",
    "teams": {"home": {"name": "Chicago Cubs", "badge": "chc"},
              "away": {"name": "San Diego Padres", "badge": "sd"}},
    "sources": [{"source": "admin", "id": "bb1a"}],
}
CUBS_STREAM = [{"id": "s1", "streamNo": 1, "language": "English", "hd": True,
                   "embedUrl": "https://embed.example/admin/bb1a/1", "source": "admin"}]


class World:
    """What each scripted Provider answers right now."""

    def __init__(self):
        self.ppv = "ok"          # ok | down | 403 | violation-text | violation-json | empty
        self.streamed = "ok"     # ok | down
        self.urls = []

    def get(self, url, timeout=None, headers=None, **kw):
        self.urls.append(url)
        u = urlparse(url)
        if u.netloc == "ppv.example" and u.path == "/api/streams":
            if self.ppv == "ok":
                return Resp(FIXTURE)
            if self.ppv == "empty":
                return Resp({"success": True, "streams": []})
            if self.ppv == "403":
                return Resp(None, status=403)
            if self.ppv == "violation-text":
                return Resp(_NOT_JSON, text="Violation")
            if self.ppv == "violation-json":
                return Resp({"success": False, "error": "Violation"})
            raise OSError("scripted: ppv unreachable")
        if u.netloc == "streamed.example":
            if self.streamed != "ok":
                raise OSError("scripted: streamed unreachable")
            if u.path == "/api/sports":
                return Resp([{"id": "baseball", "name": "Baseball"}])
            if u.path == "/api/matches/live":
                return Resp([CUBS])
            if u.path == "/api/matches/baseball":
                return Resp([CUBS])
            if u.path.startswith("/api/stream/"):
                return Resp(CUBS_STREAM)
        if u.netloc == "static.ppvservices.st":
            return Resp(None)
        raise OSError("scripted: %s not reachable" % u.netloc)


world = World()
app.requests.get = world.get
client = app.app.test_client()

# Provider failures are logged; capture them to prove it.
class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append("%s %s" % (record.levelname, record.getMessage()))


cap = Capture()
logging.getLogger().addHandler(cap)

# The wall clock, movable. Only time.time() moves; sleeps and monotonic do not.
_real_time = time.time
_offset = [0.0]
time.time = lambda: _real_time() + _offset[0]


def refresh():
    app.refresh_once()


def rows():
    lines = client.get("/playlist.m3u").get_data(as_text=True).splitlines()
    return [ln.split(",", 1)[1] for ln in lines if ln.startswith("#EXTINF")]


def channels():
    """The Channels Dispatcharr is told to have: one entry per team/feed/series."""
    lines = client.get("/playlist-teams.m3u").get_data(as_text=True).splitlines()
    return sorted(ln.split(",", 1)[1] for ln in lines if ln.startswith("#EXTINF"))


def providers_view():
    ov = client.get("/api/overview").get_json() or {}
    return {p["name"]: p for p in ov.get("providers") or []}


OKC = "Oklahoma City Thunder vs. Dallas Mavericks"
CUBS_GAME = "Chicago Cubs vs San Diego Padres"

# ── 0. Everything up: both Providers' games are served ─────────────────────

world.ppv = world.streamed = "ok"
refresh()
good_rows, good_channels = rows(), channels()
check("baseline: ppv.st games are served", any(r.endswith(OKC) for r in good_rows))
check("baseline: streamed.pk games are served", any(r.endswith(CUBS_GAME) for r in good_rows))
check("baseline: Channels exist", len(good_channels) > 0, len(good_channels))
v = providers_view()
check("dashboard: both Providers show as healthy with a last success",
      all(v.get(n, {}).get("ok") is True and v[n].get("last_success") for n in ("ppv", "streamed")), v)

# ── 1. A failed list call keeps the last good games, Channels untouched ────

for mode in ("down", "403", "violation-text", "violation-json"):
    world.ppv = mode
    log_before = len(cap.lines)
    try:
        refresh()
        crashed = None
    except Exception as exc:                                    # noqa: BLE001
        crashed = repr(exc)
    check("ppv.st %s: the refresh does not crash" % mode, crashed is None, crashed)
    check("ppv.st %s: its last good games are still served" % mode,
          any(r.endswith(OKC) for r in rows()) and len(rows()) == len(good_rows),
          "%d rows, was %d" % (len(rows()), len(good_rows)))
    check("ppv.st %s: Channels are exactly as before" % mode, channels() == good_channels)
    check("ppv.st %s: the failure is logged" % mode,
          any("ppv" in ln.lower() for ln in cap.lines[log_before:]), cap.lines[log_before:][-2:])
    v = providers_view()
    check("ppv.st %s: dashboard shows ppv failing, with when it last succeeded and why" % mode,
          v.get("ppv", {}).get("ok") is False and v["ppv"].get("last_success")
          and v["ppv"].get("error") and v["ppv"].get("serving_stale") is True, v.get("ppv"))
    check("ppv.st %s: dashboard does not blame streamed.pk" % mode, v.get("streamed", {}).get("ok") is True, v.get("streamed"))

# ── 2. One Provider failing does not touch the other's games ───────────────

world.ppv = "down"
refresh()
check("streamed.pk games are served while ppv.st is down", any(r.endswith(CUBS_GAME) for r in rows()))
world.ppv, world.streamed = "ok", "down"
refresh()
check("ppv.st games are served while streamed.pk is down", any(r.endswith(OKC) for r in rows()))
# streamed.pk fetches each game's Sources with calls of their own, which are
# down too: the kept game has no playlist row but the Channel still knows it.
kept = (client.get("/teams", query_string={"team": "Chicago Cubs"}).get_json() or {}).get("playing") or {}
check("streamed.pk's last good game is kept (known to its Channel, no Source while down)",
      kept.get("match_title") == CUBS_GAME and kept.get("streams") == [], kept)
check("Channels are exactly as before with streamed.pk down", channels() == good_channels)

# ── 3. A legitimately empty list is an answer, not a failure ───────────────

world.streamed = "ok"
world.ppv = "empty"
refresh()
check("an empty ppv.st list replaces the old one", not any(r.endswith(OKC) for r in rows()))
check("an empty list is not reported as a failure", providers_view().get("ppv", {}).get("ok") is True,
      providers_view().get("ppv"))
check("Channels survive an empty list", channels() == good_channels)

# ── 4. After the retention limit the stale list stops being served ─────────

world.ppv = "ok"
refresh()
check("ppv.st recovers on the next good call", any(r.endswith(OKC) for r in rows())
      and providers_view()["ppv"]["ok"] is True)
world.ppv = "down"
_offset[0] = 5 * 3600
refresh()
check("inside the limit (5h of 6h): still served", any(r.endswith(OKC) for r in rows()))
_offset[0] = 7 * 3600
refresh()
check("past the limit (7h of 6h): the stale ppv.st games stop being served",
      not any(r.endswith(OKC) for r in rows()), len(rows()))
check("...streamed.pk's games are unaffected", any(r.endswith(CUBS_GAME) for r in rows()))
check("...Channels are still not removed or rebuilt", channels() == good_channels)
v = providers_view()
check("dashboard: ppv shows failing, no longer serving stale, with its last success kept",
      v["ppv"]["ok"] is False and v["ppv"]["serving_stale"] is False and v["ppv"]["last_success"], v["ppv"])

# Everything failing past the limit must still leave Channels alone.
world.streamed = "down"
_offset[0] = 20 * 3600
refresh()
check("every Provider failed past the limit: playlist empty, Channels intact",
      not rows() and channels() == good_channels, "%d rows" % len(rows()))

# ── 5. The retention limit is a setting ────────────────────────────────────

_offset[0] = 0.0
world.ppv = world.streamed = "ok"
refresh()
cfg = client.get("/api/config").get_json() or {}
names = {s.get("env") for grp in (cfg.get("groups") or []) for s in (grp.get("settings") or [])} \
    | {s.get("env") for s in (cfg.get("settings") or [])}
check("PROVIDER_STALE_HOURS is a setting", "PROVIDER_STALE_HOURS" in names, sorted(n for n in names if n)[:5])

app.PROVIDER_STALE_HOURS = 1
world.ppv = "down"
_offset[0] = 2 * 3600
refresh()
check("a smaller limit expires the stale list sooner (1h limit, 2h old)",
      not any(r.endswith(OKC) for r in rows()))
app.PROVIDER_STALE_HOURS = 6
_offset[0] = 0.0
world.ppv = "ok"
refresh()

# ── 6. A Provider that has never answered contributes nothing, quietly ─────

app._provider_state.clear() if hasattr(app, "_provider_state") else None
world.ppv, world.streamed = "down", "ok"
refresh()
check("a Provider with no good list yet serves no games and nothing breaks",
      not any(r.endswith(OKC) for r in rows()) and any(r.endswith(CUBS_GAME) for r in rows()))
v = providers_view()
check("...and the dashboard says it has never succeeded",
      v.get("ppv", {}).get("ok") is False and not v["ppv"].get("last_success"), v.get("ppv"))

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
