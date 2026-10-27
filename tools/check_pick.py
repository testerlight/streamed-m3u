"""Verification harness for picking and pinning a Source across Providers (provider-swap ticket 06). Not shipped.

One seam: the Flask test client, with the network replaced by two scripted
Providers (ppv.st serving the REAL recorded list response, streamed.pk serving
games built from its start times, as in check_merge.py) and extraction stubbed:
_validate_candidate records the order the /stream cascade tries Sources in and
fails them all, _check_source passes. Run inside a container built from this
directory:

    docker run --rm -e STARTUP_DELAY=0 \
        -v $PWD/tools/check_pick.py:/tmp/check.py:ro \
        -v $PWD/tools/fixtures:/tmp/fixtures:ro \
        <image> python /tmp/check.py /tmp/fixtures/ppv_streams.json
"""

import copy
import json
import os
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

DATA = tempfile.mkdtemp(prefix="check-pick-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["PROVIDERS"] = "ppv,streamed"
os.environ["PPV_BASE_URL"] = "https://ppv.example"
os.environ["STREAMED_BASE_URL"] = "https://streamed.example"
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ.pop("CONSOLE_PASSWORD", None)
os.environ.pop("PPV_MIRRORS", None)
os.environ.pop("STREAMED_MIRRORS", None)

sys.path.insert(0, "/app")
import app  # noqa: E402

FIXTURE = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "tools/fixtures/ppv_streams.json"))

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  [%s]" % (detail,)) if detail != "" else ""))
    if not cond:
        fails.append(name)


HOUR = 3600
PACK = "Carolina Panthers at Green Bay Packers"
SLUG = "green-bay-packers"
HOLD = "team:" + SLUG
ST_URL = "https://embed.example/admin/sf-pack/1"
T_PACK = next(g["starts_at"] for c in FIXTURE["streams"] for g in c["streams"] if g["name"] == PACK)


# ── The scripted network ────────────────────────────────────────────────────

class Resp:
    def __init__(self, payload=None, status=200, content=b"", ctype="application/json"):
        self._payload, self.status_code, self.content = payload, status, content
        self.headers = {"Content-Type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise app.requests.HTTPError("%d" % self.status_code, response=self)

    def json(self):
        return self._payload


def pack_game():
    start = T_PACK + HOUR
    return {"id": "sf-pack", "title": "Green Bay Packers vs Carolina Panthers",
            "category": "american-football", "poster": "", "date": start * 1000,
            "teams": {"home": {"name": "Green Bay Packers", "badge": ""},
                      "away": {"name": "Carolina Panthers", "badge": ""}},
            "sources": [{"source": "admin", "id": "sf-pack"}]}


class Network:
    def __init__(self, ppv_payload, streamed):
        self.ppv, self.streamed = ppv_payload, streamed

    def get(self, url, timeout=None, headers=None, **kw):
        u = urlparse(url)
        if u.netloc == "ppv.example" and u.path == "/api/streams":
            return Resp(self.ppv)
        if u.netloc == "streamed.example":
            if u.path == "/api/sports":
                return Resp([{"id": s, "name": s.title()} for s in sorted({g["category"] for g in self.streamed})])
            if u.path == "/api/matches/live":
                return Resp([])
            if u.path.startswith("/api/matches/"):
                return Resp([g for g in self.streamed if g["category"] == u.path.rsplit("/", 1)[1]])
            if u.path.startswith("/api/stream/admin/"):
                gid = u.path.rsplit("/", 1)[1]
                return Resp([{"id": "s-" + gid, "streamNo": 1, "language": "English", "hd": True,
                              "embedUrl": "https://embed.example/admin/%s/1" % gid, "source": "admin"}])
        if u.netloc == "static.ppvservices.st":
            return Resp(content=b"\xff\xd8poster", ctype="image/jpeg")
        raise OSError("scripted: %s not reachable" % u.netloc)


def serve(streamed, ppv=None):
    app.requests.get = Network(ppv or FIXTURE, streamed).get
    app._install_seed_roster()
    app._load_team_roster()
    app.refresh_once()


def ppv_without_pack():
    p = copy.deepcopy(FIXTURE)
    for cat in p["streams"]:
        cat["streams"] = [g for g in cat["streams"] if g["name"] != PACK]
    return p


client = app.app.test_client()


def sources():
    r = client.get("/teams", query_string={"team": "Green Bay Packers"}).get_json()
    return ((r.get("playing") or {}).get("streams")) or []


# ── Stubs: only extraction is replaced ──────────────────────────────────────

attempts = []   # embed urls the /stream cascade validated, in order


def fake_validate(embed_url, label=""):
    attempts.append(embed_url)
    return False, None, "%s:fail" % (label or "x")


app._validate_candidate = fake_validate
app._check_source = lambda embed_url, label: (({"url": embed_url}, {}, {}, [], {}),
                                              "3 test chunks were real video")
app.REQUIRE_AUDIO = False


def start_stream(playing_url):
    """A player already watching the channel (what the console's picker acts on)."""
    sid = "%08x" % (len(app._active_streams) + 1)
    with app._active_streams_lock:
        app._active_streams[sid] = {"stream_id": sid, "embed_url": playing_url, "hold_key": HOLD,
                                    "stop": threading.Event(), "multiview": None}
    return sid


def pick(stream_id, embed_url):
    app._source_jobs.pop(SLUG, None)
    r = client.post("/api/streams/%s/source" % stream_id, json={"embed_url": embed_url})
    for _ in range(100):
        job = app._source_jobs.get(SLUG)
        if job and job["state"] != "checking":
            break
        time.sleep(0.05)
    return r


def reconnect():
    """A player (re)connecting to the channel; the order the cascade tried Sources in."""
    del attempts[:]
    client.get("/stream", query_string={"team": "Green Bay Packers"})
    return list(attempts)


def pinned():
    pin = app._source_pin(SLUG, app._team_match_id(SLUG))
    return pin["embed_url"] if pin else None


# ── 1. The merged game offers both Providers' Sources ──────────────────────

serve([pack_game()])
srcs = sources()
check("the merged game lists a Source from each Provider",
      sorted(s["code"] for s in srcs) == ["PPV", "ST"], [s["code"] for s in srcs])
ppv_url = next((s["embed_url"] for s in srcs if s["code"] == "PPV"), None)
check("...the PPV one ranked first (no quality label: just below 1080p, above 720p)",
      bool(srcs) and srcs[0]["code"] == "PPV", [s["code"] for s in srcs])
order = reconnect()
check("with no pick the cascade starts on the best-ranked Source, whatever its Provider",
      order[:1] == [ppv_url], order)

# ── 2. A manual pick of another Provider's Source is honoured ───────────────

sid = start_stream(ppv_url)
r = pick(sid, ST_URL)
check("the console accepts a pick of a Source from another Provider",
      r.status_code == 202, (r.status_code, r.get_data(as_text=True)))
check("...and ends the current session so the player reconnects onto it",
      app._active_streams[sid]["stop"].is_set())
app.SOURCE_PREPARED_TTL = 0          # the checked payload is stale: make the cascade validate
order = reconnect()
check("the reconnect tries the picked Source first, ahead of the better-ranked one",
      order[:1] == [ST_URL], order)
order = reconnect()
check("...and so does every later reconnect", order[:1] == [ST_URL], order)
check("...falling back to the other Provider's Source when the picked one fails",
      order[:2] == [ST_URL, ppv_url], order)

# ── 3. The pin survives the game's Providers changing ──────────────────────

serve([pack_game()], ppv_without_pack())
check("(setup) the preferred Provider stopped listing the game",
      [s["code"] for s in sources()] == ["ST"], [s["code"] for s in sources()])
check("the pick is kept while its Source is still listed", pinned() == ST_URL, pinned())
serve([pack_game()])
check("...and when the preferred Provider lists the game again (its listing becomes the primary)",
      pinned() == ST_URL, pinned())
order = reconnect()
check("...so the reconnect still tries the picked Source first", order[:1] == [ST_URL], order)

sid = start_stream(ST_URL)
pick(sid, ppv_url)
serve([pack_game()], ppv_without_pack())
serve([], FIXTURE)
check("a pick of the preferred Provider's Source is kept when the other Provider drops the game",
      pinned() == ppv_url, pinned())

# ── 4. A pick is forgotten once its Source is gone ─────────────────────────

serve([pack_game()], ppv_without_pack())
sid = start_stream(ST_URL)
pick(sid, ST_URL)
serve([], ppv_without_pack())
check("a pick whose Source is no longer listed is dropped", pinned() is None, pinned())

# ── 5. Clearing the pick returns to the ranked order ───────────────────────

serve([pack_game()])
sid = start_stream(ppv_url)
pick(sid, ST_URL)
r = client.post("/api/streams/%s/source/auto" % sid)
order = reconnect()
check("clearing a cross-Provider pick returns to the ranked order",
      r.status_code == 200 and order[:1] == [ppv_url], order)

# ── 6. A Provider whose Sources are all dead hands over to the other ───────

ppv_many = copy.deepcopy(FIXTURE)
for cat in ppv_many["streams"]:
    for g in cat["streams"]:
        if g["name"] == PACK:
            g["substreams"] = [{"id": 880000 + i, "name": "Alt %d" % i, "tag": "x", "poster": "",
                                "iframe": "https://embed.example/ppv/alt%d" % i} for i in range(1, 6)]
serve([pack_game()], ppv_many)
codes = [s["code"] for s in sources()]
check("(setup) six Sources from one Provider outrank the other Provider's one",
      codes.count("PPV") >= 5 and codes[-1] == "ST", codes)
order = reconnect()
check("the cascade reaches the other Provider even when the first has more Sources than attempts",
      ST_URL in order, order)

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
