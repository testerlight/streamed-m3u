"""Verification harness for the ppv.st Provider (provider-swap ticket 03). Not shipped.

One seam: the served playlist, /teams, /epg.xml and /logo, driven through
Flask's test client, with the network replaced by a scripted ppv.st that serves
a REAL recorded list response (tools/fixtures/ppv_streams.json, recorded
2026-10-27 from https://api.ppv.st/api/streams). requests.get is the only thing
faked. Run inside a container built from this directory:

    docker run --rm -e STARTUP_DELAY=0 \
        -v $PWD/tools/check_ppv.py:/tmp/check.py:ro \
        -v $PWD/tools/fixtures:/tmp/fixtures:ro \
        <image> python /tmp/check.py /tmp/fixtures/ppv_streams.json
"""

import copy
import json
import os
import re
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlparse

DATA = tempfile.mkdtemp(prefix="check-ppv-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["PROVIDERS"] = "ppv"
os.environ["PPV_BASE_URL"] = "https://ppv.example"
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ.pop("PPV_MIRRORS", None)

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
    def __init__(self, payload=None, status=200, content=b"", ctype="application/json"):
        self._payload, self.status_code, self.content = payload, status, content
        self.headers = {"Content-Type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise app.requests.HTTPError("%d" % self.status_code, response=self)

    def json(self):
        return self._payload


class Network:
    """Stands in for requests.get; records every URL asked for."""

    def __init__(self, payload):
        self.payload, self.urls = payload, []

    def get(self, url, timeout=None, headers=None, **kw):
        self.urls.append(url)
        u = urlparse(url)
        if u.netloc == "ppv.example" and u.path == "/api/streams":
            return Resp(self.payload)
        if u.netloc == "static.ppvservices.st":
            return Resp(content=b"\xff\xd8poster", ctype="image/jpeg")
        raise OSError("scripted: %s not reachable" % u.netloc)

    def hosts(self):
        return sorted({urlparse(u).netloc for u in self.urls})


def serve(payload):
    net = Network(payload)
    app.requests.get = net.get
    app._install_seed_roster()
    app._load_team_roster()
    app.refresh_once()
    return net


client = app.app.test_client()


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


def games():
    return [(c["category"], g) for c in FIXTURE["streams"] for g in c["streams"]]


# ── 1. One list call per refresh, only to the configured address ───────────

net = serve(FIXTURE)
rows = playlist_rows()
check("one request to ppv.st per refresh", net.urls.count("https://ppv.example/api/streams") == 1, net.urls)
check("nothing else is contacted during a refresh", net.hosts() == ["ppv.example"], net.hosts())
header = client.get("/playlist.m3u").get_data(as_text=True).splitlines()[2]
check("playlist header lists the enabled Providers", header == "# Providers: ppv", header)

# ── 2. A game with substreams is one game with several ranked Sources ──────

OKC = "Oklahoma City Thunder vs. Dallas Mavericks"
okc = [r for r in rows if r[0].endswith(OKC)]
check("a game with two substreams offers three Sources",
      [r[0].split("]")[0] for r in okc] == ["[PPV1", "[PPV2", "[PPV3"], [r[0] for r in okc])
check("Sources of one game have distinct embed links", len({r[2] for r in okc}) == 3, [r[2] for r in okc])
check("embed hosts come from the response", all("taifood-blog.asia" in unquote(r[2]) for r in okc),
      [unquote(r[2]) for r in okc])
t = team("Oklahoma City Thunder")
streams = ((t.get("playing") or {}).get("streams")) or []
check("the team's Channel carries all three Sources, labelled PPV",
      [s["code"] for s in streams] == ["PPV"] * 3 and [s["n"] for s in streams] == [1, 2, 3], streams)
check("ppv.st Sources have no quality label", all(s.get("quality") is None for s in streams), streams)
check("the single-Source case is one entry", len([r for r in rows if r[0].endswith("Brooklyn Nets vs. Boston Celtics")]) == 2)

# ── 3. "Away at Home" and "X vs. Y" titles land on the right Channels ───────

p = (team("Green Bay Packers").get("playing") or {})
check("'Away at Home': the home side's Channel plays the game",
      p.get("match_title") == "Carolina Panthers at Green Bay Packers" and p.get("side") == "home", p.get("match_title"))
p = (team("Carolina Panthers").get("playing") or {})
check("'Away at Home': the away side resolves too (NFL alias)",
      p.get("match_title") == "Carolina Panthers at Green Bay Packers" and p.get("side") == "away", p.get("match_title"))
p = (team("Boston Celtics").get("playing") or {})
check("'X vs. Y': the second-named side resolves as away", p.get("side") == "away", p)
check("ppv.st sport names map to the app's sport ids",
      (team("Green Bay Packers").get("playing") or {}).get("sport") == "american-football"
      and (team("Boston Celtics").get("playing") or {}).get("sport") == "basketball"
      and (team("Detroit Red Wings").get("playing") or {}).get("sport") in ("hockey", None))

MAJOR_TAGS = {"NFL", "NBA", "NHL", "MLB"}
unresolved = []
for cat, g in games():
    if g.get("tag") not in MAJOR_TAGS or g.get("always_live"):
        continue
    parts = re.split(r"\s+at\s+|\s+vs\.?\s+", g["name"], maxsplit=1)
    for nm in parts:
        # A team with two games listed plays the nearer one; it resolves if
        # it has a game that names it.
        if nm not in ((team(nm).get("playing") or {}).get("match_title") or ""):
            unresolved.append("%s: %s" % (g["tag"], nm))
if unresolved:
    print("INFO teams that did not resolve to their game (extend EXTRA_ALIAS_TEAMS / MAJOR_LEAGUE_TEAMS):")
    for u in sorted(set(unresolved)):
        print("INFO   ", u)
check("every NFL/NBA/NHL/MLB team in the list has a Channel playing one of its games", not unresolved, sorted(set(unresolved)))

# ── 4. Feeds use the Provider's own flag; NFL Network's slug ────────────────

nfl = team("NFL Network")
check("NFL Network resolves as a feed on slug nfl-network",
      nfl.get("slug") == "nfl-network" and (nfl.get("roster") or {}).get("kind") == "feed", nfl.get("slug"))
check("NFL Network plays its ppv.st Source",
      [s["code"] for s in ((nfl.get("playing") or {}).get("streams") or [])] == ["PPV"], nfl.get("playing"))
app_lineup = app.lineup.current()
check("nfl-network is on the bundled allowlist", app.lineup.in_lineup(app_lineup, "nfl-network"))
flags = {g["name"]: g["always_live"] for _, g in games() if g.get("always_live")}
check("ppv.st always-live entries exist in the fixture", "NFL Network" in flags and "24/7 South Park" in flags)
pool = [(k, v) for k, v in app._team_map.items() if (v.get("match_title") or "").startswith("24/7")]
check("an always-live entry with no feed Channel does not take a shared slot", not pool, pool)

# ── 5. A game with no Source yet is still in the guide ─────────────────────

bare = copy.deepcopy(FIXTURE)
for cat in bare["streams"]:
    for g in cat["streams"]:
        if g["name"] == "Washington Wizards vs. Cleveland Cavaliers":
            g["iframe"], g["substreams"] = "", []
            # The guide only shows a window around now, and the recording
            # ages out of it; put this one game a few hours ahead.
            g["starts_at"] = int(time.time()) + 3 * 3600
            g["ends_at"] = g["starts_at"] + 3 * 3600
serve(bare)
check("a game with no Source offers no playlist entry",
      not [r for r in playlist_rows() if r[0].endswith("Washington Wizards vs. Cleveland Cavaliers")])
p = (team("Washington Wizards").get("playing") or {})
check("...but the team's Channel still knows the game", p.get("match_title") == "Washington Wizards vs. Cleveland Cavaliers"
      and p.get("streams") == [], p)
guide = client.get("/epg.xml").get_data(as_text=True)
check("...and the guide lists it", "Washington Wizards vs. Cleveland Cavaliers" in guide)

# ── 6. Posters go through the image proxy, and are cached ──────────────────

net = serve(FIXTURE)
poster = next(r[1] for r in playlist_rows() if r[0].endswith(OKC))
check("poster links point at the app's own proxy", "/logo?url=" in poster and "ppvservices" in unquote(poster), poster)
target = parse_qs(urlparse(poster.replace("{{PUBLIC_BASE_URL}}", "http://x")).query)["url"][0]
r1, r2 = client.get("/logo", query_string={"url": target}), client.get("/logo", query_string={"url": target})
check("the proxy serves the poster", r1.status_code == 200 and r1.data == b"\xff\xd8poster", r1.status_code)
check("the second request is served from cache", sum("ppvservices" in u for u in net.urls) == 1, net.urls[-3:])
check("only the configured API and the poster host are ever contacted",
      set(net.hosts()) == {"ppv.example", "static.ppvservices.st"}, net.hosts())

# ── 7. Embed hosts follow the response, with no code change ────────────────

moved = json.loads(json.dumps(FIXTURE).replace("taifood-blog.asia", "moved.example"))
serve(moved)
check("a changed embed host in the response is followed",
      all("moved.example" in unquote(r[2]) for r in playlist_rows()) and playlist_rows(), playlist_rows()[:1])

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
