"""Verification harness for cross-Provider game merging (provider-swap ticket 05). Not shipped.

One seam: the served playlist, /teams and /epg.xml, driven through Flask's test
client, with the network replaced by two scripted Providers: ppv.st serving a
REAL recorded list response (tools/fixtures/ppv_streams.json, recorded
2026-10-27) and streamed.pk serving games built here from that recording's
start times (streamed.pk has closed, so there is nothing left to record; its
shapes are the ones the Provider has always had). requests.get is the only
thing faked. Run inside a container built from this directory:

    docker run --rm -e STARTUP_DELAY=0 \
        -v $PWD/tools/check_merge.py:/tmp/check.py:ro \
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
from urllib.parse import urlparse

DATA = tempfile.mkdtemp(prefix="check-merge-")
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
GAME_PPV = {c["category"]: {g["name"]: g for g in c["streams"]} for c in FIXTURE["streams"]}


def ppv_start(category, name):
    return GAME_PPV[category][name]["starts_at"]


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


def streamed_game(gid, title, sport, start, home=None, away=None, hd=True):
    teams = {}
    if home:
        teams["home"] = {"name": home, "badge": ""}
    if away:
        teams["away"] = {"name": away, "badge": ""}
    return {"id": gid, "title": title, "category": sport, "poster": "",
            "date": int(start) * 1000, "teams": teams,
            "sources": [{"source": "admin", "id": gid}], "_hd": hd}


class Network:
    """ppv.st serves the recording (plus any extra games); streamed.pk serves `streamed`."""

    def __init__(self, ppv_payload, streamed):
        self.ppv, self.streamed, self.urls = ppv_payload, streamed, []

    def get(self, url, timeout=None, headers=None, **kw):
        self.urls.append(url)
        u = urlparse(url)
        if u.netloc == "ppv.example" and u.path == "/api/streams":
            return Resp(self.ppv)
        if u.netloc == "streamed.example":
            games = [{k: v for k, v in g.items() if k != "_hd"} for g in self.streamed]
            if u.path == "/api/sports":
                return Resp([{"id": s, "name": s.title()} for s in sorted({g["category"] for g in games})])
            if u.path == "/api/matches/live":
                return Resp([])
            if u.path.startswith("/api/matches/"):
                return Resp([g for g in games if g["category"] == u.path.rsplit("/", 1)[1]])
            if u.path.startswith("/api/stream/admin/"):
                gid = u.path.rsplit("/", 1)[1]
                g = next(g for g in self.streamed if g["id"] == gid)
                return Resp([{"id": "s-" + gid, "streamNo": 1, "language": "English", "hd": g["_hd"],
                              "embedUrl": "https://embed.example/admin/%s/1" % gid, "source": "admin"}])
        if u.netloc == "static.ppvservices.st":
            return Resp(content=b"\xff\xd8poster", ctype="image/jpeg")
        raise OSError("scripted: %s not reachable" % u.netloc)


def serve(streamed, ppv=None):
    app.requests.get = Network(ppv or FIXTURE, streamed).get
    app._install_seed_roster()
    app._load_team_roster()
    app.refresh_once()


client = app.app.test_client()


def playlist_rows():
    """(title, label, url) for every playlist entry."""
    lines = client.get("/playlist.m3u").get_data(as_text=True).splitlines()
    rows = []
    for i, ln in enumerate(lines):
        if ln.startswith("#EXTINF"):
            name = ln.split(",", 1)[1]
            label, title = re.match(r"\[([^\]]+)\] (.*)", name).groups()
            rows.append((title, label, lines[i + 1]))
    return rows


def team(name):
    return client.get("/teams", query_string={"team": name}).get_json()


def codes(name):
    streams = ((team(name).get("playing") or {}).get("streams")) or []
    return [s["code"] for s in streams]


def programmes(channel_slug, needle):
    epg = client.get("/epg.xml").get_data(as_text=True)
    return [p for p in re.findall(r"<programme .*?</programme>", epg, re.S)
            if channel_slug in p.split(">", 1)[0] and needle in p]


# Games both Providers will list, anchored on the recorded start times.
PACK = "Carolina Panthers at Green Bay Packers"
T_PACK = ppv_start("American Football", PACK)
NETS = "Brooklyn Nets vs. Boston Celtics"
T_NETS = ppv_start("Basketball", NETS)
PIST = "Detroit Pistons vs. New York Knicks"
T_PIST = ppv_start("Basketball", PIST)
CBJ = "Columbus Blue Jackets vs. Philadelphia Flyers"
T_CBJ = ppv_start("Ice Hockey", CBJ)
BRUINS = "Boston Bruins vs. Carolina Hurricanes"
T_BRUINS = ppv_start("Ice Hockey", BRUINS)

streamed = [
    # Same teams, start 1h later, sides listed the other way round: one game.
    streamed_game("sf-pack", "Green Bay Packers vs Carolina Panthers", "american-football",
                  T_PACK + HOUR, home="Green Bay Packers", away="Carolina Panthers"),
    # Same teams, 5 hours apart: two games.
    streamed_game("sf-nets", "Brooklyn Nets vs Boston Celtics", "basketball",
                  T_NETS + 5 * HOUR, home="Brooklyn Nets", away="Boston Celtics"),
    # One side cannot be resolved to a team: stays separate rather than guessing.
    streamed_game("sf-pist", "Detroit Pistons vs New York Knicks", "basketball",
                  T_PIST, home="Detroit Pistons"),
    # Another Provider's doubleheader: the one ppv.st game fits both listings,
    # so it is merged with neither.
    streamed_game("sf-cbj1", "Columbus Blue Jackets vs Philadelphia Flyers", "hockey",
                  T_CBJ, home="Columbus Blue Jackets", away="Philadelphia Flyers"),
    streamed_game("sf-cbj2", "Columbus Blue Jackets vs Philadelphia Flyers G2", "hockey",
                  T_CBJ + 2 * HOUR, home="Columbus Blue Jackets", away="Philadelphia Flyers"),
    # A title with no teams at all.
    streamed_game("sf-bruins", "Bruins v Hurricanes", "hockey", T_BRUINS),
]

# ── 1. The same game from two Providers is one game with both Providers' Sources

serve(streamed)
rows = playlist_rows()
pack_rows = [r for r in rows if "Green Bay Packers" in r[0] and "Carolina Panthers" in r[0]]
check("a game both Providers list is one game in the playlist (one title)",
      len({r[0] for r in pack_rows}) == 1, sorted({r[0] for r in pack_rows}))
check("...carrying a Source from each Provider, labelled with its own Provider",
      sorted(r[1] for r in pack_rows) == ["PPV1", "ST1-720p"], [r[1] for r in pack_rows])
check("the home Channel plays it with both Sources, ranked (unlabelled just below 1080p, above 720p)",
      codes("Green Bay Packers") == ["PPV", "ST"], codes("Green Bay Packers"))
check("the away side's Channel plays the same merged game",
      codes("Carolina Panthers") == ["PPV", "ST"], codes("Carolina Panthers"))
p = (team("Green Bay Packers").get("playing") or {})
check("the merged game keeps the preferred Provider's title", p.get("match_title") == PACK, p.get("match_title"))

# The guide only shows a window around now, and the recording ages out of it:
# put the game a few hours ahead on both Providers.
soon = int(time.time()) + 3 * HOUR
ppv_soon = copy.deepcopy(FIXTURE)
for cat in ppv_soon["streams"]:
    for g in cat["streams"]:
        if g["name"] == PACK:
            g["starts_at"], g["ends_at"] = soon, soon + 3 * HOUR
serve([streamed_game("sf-pack", "Green Bay Packers vs Carolina Panthers", "american-football",
                     soon + HOUR, home="Green Bay Packers", away="Carolina Panthers")], ppv_soon)
shown = programmes("green-bay-packers", "Packers")
check("the guide lists the merged game once on its Channel", len(shown) == 1, len(shown))
check("...and the game still has both Providers' Sources", codes("Green Bay Packers") == ["PPV", "ST"],
      codes("Green Bay Packers"))
serve(streamed)

# ── 2. Same teams, starts too far apart: separate games ────────────────────

def rows_of(*titles):
    return [r for r in playlist_rows() if r[0] in titles]


nets_rows = rows_of(NETS, "Brooklyn Nets vs Boston Celtics")
check("same teams 5 hours apart stay two games", len({r[0] for r in nets_rows}) == 2,
      sorted({r[0] for r in nets_rows}))
check("...each with only its own Provider's Source",
      {r[0]: {re.sub(r"[0-9].*", "", r[1])} for r in nets_rows} ==
      {NETS: {"PPV"}, "Brooklyn Nets vs Boston Celtics": {"ST"}}, [(r[0], r[1]) for r in nets_rows])
check("...and the Channel plays one of them, not a mix", len(set(codes("Brooklyn Nets"))) == 1, codes("Brooklyn Nets"))

# ── 3. A title that cannot be resolved to teams stays separate ─────────────

pist_rows = rows_of(PIST, "Detroit Pistons vs New York Knicks")
check("a game naming only one team on one side is not merged",
      len({r[0] for r in pist_rows}) == 2 and sorted(r[1] for r in pist_rows) == ["PPV1", "ST1-720p"],
      [(r[0], r[1]) for r in pist_rows])
bruins_rows = rows_of(BRUINS, "Bruins v Hurricanes")
check("a title with no teams is not merged with the same game from the other Provider",
      len({r[0] for r in bruins_rows}) == 2 and sorted(r[1] for r in bruins_rows) == ["PPV1", "ST1-720p"],
      [(r[0], r[1]) for r in bruins_rows])
check("the unresolvable listing never put its Source on the other game's Channel",
      "ST" not in codes("New York Knicks") and "ST" not in codes("Boston Bruins"),
      (codes("New York Knicks"), codes("Boston Bruins")))

cbj_rows = rows_of(CBJ, "Columbus Blue Jackets vs Philadelphia Flyers", "Columbus Blue Jackets vs Philadelphia Flyers G2")
check("a listing that fits two games of the other Provider is merged with neither",
      len({r[0] for r in cbj_rows}) == 3 and sorted(r[1] for r in cbj_rows) == ["PPV1", "ST1-720p", "ST1-720p"],
      [(r[0], r[1]) for r in cbj_rows])
guide_ids = re.findall(r'tvg-id="([^"]*)"', client.get("/playlist.m3u").get_data(as_text=True))
check("every playlist entry keeps a distinct guide id after merging", len(guide_ids) == len(set(guide_ids)),
      len(guide_ids) - len(set(guide_ids)))

# ── 4. Always-live feeds merge by name or alias ────────────────────────────

ppv = copy.deepcopy(FIXTURE)
ppv["streams"][0]["streams"].append({
    "id": 990001, "name": "NFL RedZone", "tag": "NFL", "poster": "", "uri": "nfl-redzone",
    "starts_at": 0, "ends_at": 0, "always_live": 1, "category_name": "American Football",
    "iframe": "https://embed.example/ppv/redzone", "allowpaststreams": 0, "substreams": []})
feeds = [
    streamed_game("sf-nflnet", "NFL Network", "american-football", 0),
    streamed_game("sf-rz", "NFL vs RedZone", "american-football", 0),
]
for f in feeds:
    f["date"], f["teams"] = None, {}
    f["always_live"] = True
serve(feeds, ppv)
check("an always-live feed from two Providers merges by name (NFL Network)",
      codes("NFL Network") == ["PPV", "ST"], codes("NFL Network"))
check("...and by alias (NFL RedZone / 'NFL vs RedZone')",
      codes("NFL RedZone") == ["PPV", "ST"], codes("NFL RedZone"))
nfl_rows = [r for r in playlist_rows() if r[0] == "NFL Network"]
check("the merged feed is one entry title in the playlist", len({r[0] for r in nfl_rows}) == 1 and len(nfl_rows) == 2,
      [(r[0], r[1]) for r in nfl_rows])

# ── 5. Existing single-Provider behaviour is untouched ─────────────────────

serve([])
check("with one Provider answering, Sources are unchanged",
      codes("Oklahoma City Thunder") == ["PPV", "PPV", "PPV"], codes("Oklahoma City Thunder"))

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
