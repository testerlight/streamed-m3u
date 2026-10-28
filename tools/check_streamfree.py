"""Verification harness for the streamfree Provider (provider-swap ticket 08). Not shipped.

One seam: the served playlist, /teams, /epg.xml and /api/overview, driven
through Flask's test client, with the network replaced by two scripted
services: streamfree serving a REAL recorded list response
(tools/fixtures/streamfree_streams.json, recorded 2026-10-27 from
https://streamfree.top/api/v1/streams) and ppv.st serving its recorded one
(tools/fixtures/ppv_streams.json). requests.get is the only thing faked. The
recordings are moved in time to sit around "now", so the guide still shows
them whenever this runs. Run inside a container built from this directory:

    docker run --rm -e STARTUP_DELAY=0 \
        -v $PWD/tools/check_streamfree.py:/tmp/check.py:ro \
        -v $PWD/tools/fixtures:/tmp/fixtures:ro \
        <image> python /tmp/check.py /tmp/fixtures
"""

import copy
import json
import os
import re
import sys
import tempfile
import time
from urllib.parse import unquote, urlparse

DATA = tempfile.mkdtemp(prefix="check-streamfree-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["PROVIDERS"] = "streamfree,ppv"
os.environ["STREAMFREE_BASE_URL"] = "https://sf-api.example"
os.environ["PPV_BASE_URL"] = "https://ppv.example"
for k in ("PUBLIC_BASE_URL", "STREAMFREE_MIRRORS", "STREAMFREE_EMBED_BASE_URL",
          "STREAMFREE_EMBED_MIRRORS", "PPV_MIRRORS"):
    os.environ.pop(k, None)

sys.path.insert(0, "/app")
import app  # noqa: E402

FIXDIR = sys.argv[1] if len(sys.argv) > 1 else "tools/fixtures"
SF_REC = json.load(open(os.path.join(FIXDIR, "streamfree_streams.json")))
PPV_REC = json.load(open(os.path.join(FIXDIR, "ppv_streams.json")))

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  [%s]" % (detail,)) if detail != "" else ""))
    if not cond:
        fails.append(name)


# ── The recordings, moved in time ──────────────────────────────────────────
# Both were recorded at the same moment, so one shift keeps every game that
# both list at the same start in each. The earliest MLB game starts an hour ago.

REC_BREWERS = next(g["match_timestamp"] for g in SF_REC["streams"]
                   if g["stream_key"] == "milwaukee-brewers-vs-tampa-bay-rays")
SHIFT = int(time.time()) - 3600 - REC_BREWERS


def moved(sf_rec, ppv_rec):
    sf, ppv = copy.deepcopy(sf_rec), copy.deepcopy(ppv_rec)
    for g in sf["streams"]:
        if g.get("team1"):
            g["match_timestamp"] += SHIFT
    for cat in ppv["streams"]:
        for g in cat["streams"]:
            if g.get("starts_at") and not g.get("always_live"):
                g["starts_at"] += SHIFT
                g["ends_at"] = (g.get("ends_at") or g["starts_at"]) + SHIFT
    return sf, ppv


SF, PPV = moved(SF_REC, PPV_REC)


def sf_game(sf, key):
    return next(g for g in sf["streams"] if g["stream_key"] == key)


# ── The scripted network ───────────────────────────────────────────────────

class Resp:
    def __init__(self, payload=None, status=200, text="", ctype="application/json", content=b""):
        self._payload, self.status_code, self.text, self.content = payload, status, text, content
        self.headers = {"Content-Type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise app.requests.HTTPError("%d" % self.status_code, response=self)

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class Network:
    """Stands in for requests.get; records every URL asked for."""

    def __init__(self, sf, ppv):
        self.sf, self.ppv, self.urls = sf, ppv, []
        self.sf_mode = "ok"           # ok | violation | down | forbidden

    def get(self, url, timeout=None, headers=None, **kw):
        self.urls.append(url)
        u = urlparse(url)
        if u.netloc == "sf-api.example" and u.path == "/api/v1/streams":
            if self.sf_mode == "violation":
                return Resp(None, 200, text="Violation", ctype="text/plain")
            if self.sf_mode == "forbidden":
                return Resp(None, 403, text="Violation", ctype="text/plain")
            if self.sf_mode == "down":
                raise OSError("scripted: streamfree is down")
            return Resp(self.sf)
        if u.netloc == "ppv.example" and u.path == "/api/streams":
            return Resp(self.ppv)
        raise OSError("scripted: %s not reachable" % u.netloc)

    def hosts(self):
        return sorted({urlparse(u).netloc for u in self.urls})


net = Network(SF, PPV)
app.requests.get = net.get
client = app.app.test_client()


def refresh():
    app._install_seed_roster()
    app._load_team_roster()
    app.refresh_once()


def playlist_rows():
    """(name, tvg-logo or "", url) for every playlist entry."""
    lines = client.get("/playlist.m3u").get_data(as_text=True).splitlines()
    rows = []
    for i, ln in enumerate(lines):
        if ln.startswith("#EXTINF"):
            logo = re.search(r'tvg-logo="([^"]*)"', ln)
            rows.append((ln.split(",", 1)[1], logo.group(1) if logo else "", lines[i + 1]))
    return rows


def team(name):
    return client.get("/teams", query_string={"team": name}).get_json()


def labels(name, code="SF"):
    """Labels of a game's playlist entries from one Provider, in playlist order."""
    return [r[0].split("]")[0] + "]" for r in playlist_rows()
            if r[0].endswith(name) and r[0].startswith("[" + code)]


def ranked(name):
    """(code, quality) of the Sources on a team's Channel, best first."""
    return [(s["code"], s["quality"]) for s in playing(name).get("streams", [])]


def playing(name):
    return team(name).get("playing") or {}


# ── 1. One list request per refresh, only to configured addresses ──────────

net.sf, net.ppv, net.urls = SF, PPV, []
refresh()
check("one request to streamfree per refresh",
      net.urls.count("https://sf-api.example/api/v1/streams") == 1, net.urls)
check("only the two configured API hosts are contacted", net.hosts() == ["ppv.example", "sf-api.example"], net.hosts())
header = client.get("/playlist.m3u").get_data(as_text=True).splitlines()[2]
check("playlist header lists the enabled Providers", header == "# Providers: streamfree, ppv", header)

# ── 2. Quality comes from the link and shows in the label ──────────────────

LAKERS = "Portland Trail Blazers vs Los Angeles Lakers"
check("both live variants are Sources, labelled by Provider, number and quality",
      labels(LAKERS) == ["[SF1-1080p]", "[SF2-1080p]"], labels(LAKERS))
check("a variant that is not live is not offered; the live one keeps its place in the list",
      labels("Minnesota Wild vs Utah Mammoth") == ["[SF1-1080p]"]
      and any(u.endswith("1080p2") for u in [unquote(r[2])
                                             for r in playlist_rows() if r[0].endswith("Minnesota Wild vs Utah Mammoth")]),
      labels("Minnesota Wild vs Utah Mammoth"))
t = playing("Utah Mammoth")
check("quality reaches the team's Sources as a number",
      [s["quality"] for s in (t.get("streams") or []) if s["code"] == "SF"] == [1080], t.get("streams"))

# quality is read from the link itself, not assumed
odd = copy.deepcopy(SF)
g = sf_game(odd, "portland-trail-blazers-vs-los-angeles-lakers")
g["sources"] = [u.replace("1080p", "720p") if u.endswith("1080p") else u for u in g["sources"]]
g["live_sources"] = list(g["sources"])
net.sf = odd
refresh()
check("a 720p link is labelled 720p",
      labels(LAKERS) == ["[SF1-720p]", "[SF2-1080p]"], labels(LAKERS))
check("...and ranks below the 1080p and below ppv.st's unlabelled Sources (unknown sits just under 1080p)",
      ranked("Los Angeles Lakers") == [("SF", 1080), ("PPV", None), ("PPV", None), ("SF", 720)],
      ranked("Los Angeles Lakers"))
net.sf = SF
refresh()

# ── 3. A game also listed by ppv.st merges; streamfree ranks first ─────────

check("a game both Providers list is one game: streamfree's and ppv.st's Sources pooled",
      labels(LAKERS) == ["[SF1-1080p]", "[SF2-1080p]"] and labels(LAKERS, "PPV") == ["[PPV1]", "[PPV2]"],
      (labels(LAKERS), labels(LAKERS, "PPV")))
check("...and ranked: streamfree's 1080p first, ppv.st's unlabelled Sources after",
      ranked("Los Angeles Lakers") == [("SF", 1080), ("SF", 1080), ("PPV", None), ("PPV", None)],
      ranked("Los Angeles Lakers"))
title = playing("Los Angeles Lakers").get("match_title")
check("the merged game keeps the first Provider's title", title == LAKERS, title)
rows = [r for r in playlist_rows() if "Brewers" in r[0] and "Rays" in r[0]]
check("a merged baseball game has streamfree and ppv.st Sources together",
      sorted(r[0].split("]")[0] for r in rows) == ["[PPV1", "[SF1-1080p"], [r[0] for r in rows])
check("only one game on each side's Channel", playing("Tampa Bay Rays").get("match_title") == playing("Milwaukee Brewers").get("match_title"))
sf_only = sum(1 for r in playlist_rows() if r[0].startswith("[SF"))
check("streamfree Sources reach the playlist", sf_only >= 7, sf_only)

# ── 4. Sports of every category land on Channels ───────────────────────────

sports = {
    "Milwaukee Brewers": "baseball",
    "Minnesota Wild": "hockey",
    "Los Angeles Lakers": "basketball",
}
for nm, sport in sports.items():
    check("%s: the %s game lands on its team Channel" % (nm, sport),
          playing(nm).get("sport") == sport, playing(nm).get("sport"))

# Categories the recording has no team game for: one made here per category,
# on the same shape the recording has.
synth = copy.deepcopy(SF)
start = int(time.time()) + 3600
extra = {
    "soccer": ("Arsenal", "Chelsea", "football"),
    "football": ("Green Bay Packers", "Chicago Bears", "american-football"),
    "tennis": ("Alcaraz", "Sinner", "tennis"),
    "cricket": ("India", "Australia", "cricket"),
    "racing": ("Team Penske", "Team Joseph Gibbs", "motor-sports"),
}
for cat, (a, b, _) in extra.items():
    key = "%s-vs-%s" % (a.lower().replace(" ", "-"), b.lower().replace(" ", "-"))
    base = "https://strmfree.st/embed/%s/%s" % (cat, key)
    synth["streams"].append({
        "id": key, "name": "%s vs %s" % (a, b), "category": cat, "league": cat.upper(),
        "stream_key": key, "match_timestamp": start, "is_external": False,
        "team1": {"name": a, "logo": ""}, "team2": {"name": b, "logo": ""},
        "sources": [base + "1080p", base + "1080p2"], "live_sources": [base + "1080p"],
        "thumbnail_url": "https://sf.example/t/%s" % key,
    })
net.sf = synth
refresh()
for cat, (a, b, sport) in extra.items():
    if cat in ("racing",):
        continue            # series/feeds follow their own rules, below
    got = playing(a) or playing(b)
    check("%s games map to the sport %s" % (cat, sport), got.get("sport") == sport, got.get("sport") or got)
net.sf = SF
refresh()

# ── 5. Feeds: no teams, no start ───────────────────────────────────────────

rz = team("NFL RedZone")
check("RedZone is recognised as the NFL RedZone feed Channel",
      (rz.get("roster") or {}).get("kind") == "feed" and rz.get("slug") == "nfl-vs-redzone", rz.get("slug"))
pool = [(k, v.get("match_title")) for k, v in app._team_map.items()
        if (v.get("match_title") or "") in ("Sky Sports Tennis", "Sky Sports Cricket", "Willow")]
check("an always-live entry with no feed Channel takes no shared slot", not pool, pool)
f1 = team("Formula 1").get("playing") or {}
check("Sky Sports F1 plays on the Formula 1 feed Channel (existing alias)",
      f1.get("match_title") == "Sky Sports F1" and [s["code"] for s in f1.get("streams", [])] == ["SF"], f1)
listed = {g["id"]: g for g in app._enabled_providers()[0].list_games()}
check("streamfree's lack of teams is what marks a feed",
      all(listed["sf-" + g["stream_key"]]["always_live"] == (not g.get("team1")) for g in SF["streams"]))

# ── 6. A game with no Source yet is in the schedule, and gains them ────────

now = int(time.time())
later = copy.deepcopy(SF)
g = sf_game(later, "buffalo-sabres-vs-san-jose-sharks")
g["match_timestamp"] = now + 2 * 3600
g["sources"], g["live_sources"] = [], []
net.sf = later
refresh()
BUF = "Buffalo Sabres vs San Jose Sharks"
check("a streamfree game with an empty Source list offers no playlist entry from streamfree",
      not [r for r in playlist_rows() if r[0].startswith("[SF") and r[0].endswith(BUF)])
p = playing("Buffalo Sabres")
check("...but the team's Channel still knows the game",
      p.get("match_title") == BUF and "SF" not in [s["code"] for s in p.get("streams", [])], p)
check("...and the guide lists it", BUF in client.get("/epg.xml").get_data(as_text=True))

# The Provider's list now carries the Sources, for the same game.
g["sources"] = ["https://strmfree.st/embed/hockey/buffalo-sabres-vs-san-jose-sharks1080p",
                "https://strmfree.st/embed/hockey/buffalo-sabres-vs-san-jose-sharks1080p2"]
g["live_sources"] = list(g["sources"])
refresh()
check("...and it gains Sources on a later refresh",
      [s["code"] for s in playing("Buffalo Sabres").get("streams", [])][:2] == ["SF", "SF"],
      playing("Buffalo Sabres").get("streams"))

# RedZone in the recording has Sources listed but none live: nothing to offer yet.
check("a feed whose variants are listed but not live has no Sources yet",
      (team("NFL RedZone").get("playing") or {}).get("streams", []) == [], team("NFL RedZone").get("playing"))
live = copy.deepcopy(SF)
rzg = sf_game(live, "redzone")
rzg["live_sources"] = [rzg["sources"][0]]
net.sf = live
refresh()
check("...and gains one when it goes live",
      [s["code"] for s in (team("NFL RedZone").get("playing") or {}).get("streams", [])] == ["SF"],
      team("NFL RedZone").get("playing"))
net.sf = SF
refresh()

# ── 7. Hosts: API and embed are configured; others are never used ──────────

check("the embed link stays on the configured embed address",
      all(urlparse(unquote(r[2].split("url=", 1)[1])).netloc == "strmfree.st"
          for r in playlist_rows() if r[0].startswith("[SF")))
app.STREAMFREE_EMBED_BASE_URL = "https://embed2.example"
refresh()
hosts = {urlparse(unquote(r[2].split("url=", 1)[1])).netloc
         for r in playlist_rows() if r[0].startswith("[SF")}
check("a link on an address nobody configured is moved onto the configured embed address",
      hosts == {"embed2.example"}, hosts)
app.STREAMFREE_EMBED_MIRRORS = ["https://strmfree.st"]
refresh()
hosts = {urlparse(unquote(r[2].split("url=", 1)[1])).netloc
         for r in playlist_rows() if r[0].startswith("[SF")}
check("a configured embed mirror is used as the response lists it", hosts == {"strmfree.st"}, hosts)
app.STREAMFREE_EMBED_BASE_URL, app.STREAMFREE_EMBED_MIRRORS = "https://strmfree.st", []

app.STREAMFREE_BASE_URL = "https://sf-api2.example"
app.STREAMFREE_MIRRORS = ["https://sf-api.example"]
net.urls = []
refresh()
check("API mirrors are tried in order; the one that answers keeps the Sources",
      net.urls[0].startswith("https://sf-api2.example") and any(u.startswith("https://sf-api.example") for u in net.urls)
      and labels(LAKERS) == ["[SF1-1080p]", "[SF2-1080p]"], net.urls[:3])
check("no address outside the settings was contacted", net.hosts() == ["ppv.example", "sf-api.example", "sf-api2.example"], net.hosts())
app.STREAMFREE_BASE_URL, app.STREAMFREE_MIRRORS = "https://sf-api.example", []
refresh()

# ── 8. A failed or Violation answer uses the last good list ────────────────

net.sf = SF
refresh()
before = labels(LAKERS)
channels_before = sorted(app._team_map)
for mode in ("violation", "forbidden", "down"):
    net.sf_mode = mode
    refresh()
    check("streamfree %s: the last good list is served" % mode, labels(LAKERS) == before and before, labels(LAKERS))
    check("streamfree %s: Channels are intact" % mode, sorted(app._team_map) == channels_before)
    st = {p["name"]: p for p in client.get("/api/overview").get_json()["providers"]}
    check("streamfree %s: the dashboard shows it failing and serving stale" % mode,
          st["streamfree"]["ok"] is False and st["streamfree"]["serving_stale"] is True and st["ppv"]["ok"] is True,
          st["streamfree"])
net.sf_mode = "ok"
refresh()
st = {p["name"]: p for p in client.get("/api/overview").get_json()["providers"]}
check("once it answers again it is healthy", st["streamfree"]["ok"] is True and not st["streamfree"]["error"], st["streamfree"])

# ── 9. Posters go through the image proxy ──────────────────────────────────

logos = [r[1] for r in playlist_rows() if r[0].startswith("[SF")]
check("poster links point at the app's own proxy", logos and all("/logo?url=" in l for l in logos), logos[:1])

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
