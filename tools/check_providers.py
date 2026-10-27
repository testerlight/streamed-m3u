"""Verification harness for the Provider layer (provider-swap ticket 01). Not shipped.

One seam: the served playlists, driven through Flask's test client, with the
Provider's HTTP replaced by a scripted streamed.pk (requests.get is the only
thing faked). Run inside a container built from this directory:

    # 1. record what the OLD image serves for the fixed games
    docker run --rm -e STARTUP_DELAY=0 -v $PWD/tools/check_providers.py:/tmp/check.py:ro \
        -v /tmp/prov:/tmp/prov <old-image> python /tmp/check.py --baseline /tmp/prov/baseline.json

    # 2. check the NEW image against it
    docker run --rm -e STARTUP_DELAY=0 -v $PWD/tools/check_providers.py:/tmp/check.py:ro \
        -v /tmp/prov:/tmp/prov:ro <new-image> python /tmp/check.py --compare /tmp/prov/baseline.json

The scripted catalogue follows the shapes the app reads from streamed.pk
(sports, matches with teams/sources/poster, per-source stream lists).
"""

import json
import os
import sys
import tempfile
import threading
from urllib.parse import urlparse

DATA = tempfile.mkdtemp(prefix="check-prov-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ["STREAMED_BASE_URL"] = "https://streamed.example"
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ.pop("PROVIDERS", None)

sys.path.insert(0, "/app")
import app  # noqa: E402

PRIMARY = "streamed.example"
MIRROR = "mirror.example"
STRANGER = "stranger.example"   # serves valid data, but nobody configured it

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  [%s]" % detail) if detail != "" else ""))
    if not cond:
        fails.append(name)


# ── The scripted streamed.pk ────────────────────────────────────────────────

SPORTS = [{"id": "baseball", "name": "Baseball"}, {"id": "football", "name": "Football"}]

MATCHES = {
    "baseball": [{
        "id": "bb-1", "title": "Milwaukee Brewers vs Los Angeles Dodgers",
        "category": "baseball", "date": 1793200000000, "poster": "/api/images/proxy/bb1.webp",
        "teams": {"home": {"name": "Milwaukee Brewers", "badge": "mil"},
                  "away": {"name": "Los Angeles Dodgers", "badge": "lad"}},
        "sources": [{"source": "admin", "id": "bb1a"}, {"source": "delta", "id": "bb1d"}],
    }],
    "football": [{
        "id": "fb-1", "title": "Green Bay Packers vs Chicago Bears",
        "category": "football", "date": 1793210000000, "poster": "https://img.example/fb1.webp",
        "teams": {"home": {"name": "Green Bay Packers", "badge": "gb"},
                  "away": {"name": "Chicago Bears", "badge": "chi"}},
        "sources": [{"source": "alpha", "id": "fb1a"}, {"source": "echo", "id": "fb1e"}],
    }, {
        "id": "fb-2", "title": "NFL vs RedZone", "category": "football", "date": 1793210000000,
        "poster": "", "teams": {}, "sources": [{"source": "delta", "id": "rz1d"}],
    }],
}
LIVE = [MATCHES["baseball"][0]]

STREAMS = {
    ("admin", "bb1a"): [{"id": "s1", "streamNo": 1, "language": "English", "hd": True,
                         "embedUrl": "https://embed.example/admin/bb1a/1", "source": "admin"}],
    ("delta", "bb1d"): [{"id": "s2", "streamNo": 1, "language": "English", "hd": True,
                         "embedUrl": "https://embed.example/delta/bb1d/1", "source": "delta"},
                        {"id": "s3", "streamNo": 2, "language": "English", "hd": False,
                         "embedUrl": "https://embed.example/delta/bb1d/2", "source": "delta"}],
    ("alpha", "fb1a"): [{"id": "s4", "streamNo": 1, "language": "English", "hd": False,
                         "embedUrl": "https://embed.example/alpha/fb1a/1", "source": "alpha"}],
    ("echo", "fb1e"): [{"id": "s5", "streamNo": 1, "language": "English", "hd": True,
                        "embedUrl": "https://embed.example/echo/fb1e/1", "source": "echo"}],
    ("delta", "rz1d"): [{"id": "s6", "streamNo": 1, "language": "English", "hd": True,
                         "embedUrl": "https://embed.example/delta/rz1d/1", "source": "delta"}],
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise app.requests.HTTPError("%d" % self.status_code, response=self)

    def json(self):
        return self._payload


class DeadHost(Exception):
    pass


class Network:
    """Stands in for requests.get. Records every host it is asked for."""

    def __init__(self, serves=(PRIMARY,), dead=(), missing=()):
        self.serves, self.dead, self.missing = set(serves), set(dead), set(missing)
        self.hosts, self.lock = [], threading.Lock()

    def get(self, url, timeout=None, headers=None, **kw):
        host, path = urlparse(url).netloc, urlparse(url).path
        with self.lock:
            self.hosts.append(host)
        if host in self.dead or host not in self.serves:
            raise DeadHost("scripted: %s unreachable" % host)
        if path in self.missing:
            return FakeResponse(None, status=404)
        if path == "/api/sports":
            return FakeResponse(SPORTS)
        if path == "/api/matches/live":
            return FakeResponse(LIVE)
        if path.startswith("/api/matches/"):
            return FakeResponse(MATCHES.get(path.rsplit("/", 1)[1], []))
        if path.startswith("/api/stream/"):
            _, _, _, src, sid = path.split("/")
            return FakeResponse(STREAMS.get((src, sid), []))
        raise DeadHost("scripted: no route for %s" % path)


def run_cycle(net):
    """One refresh, served by `net`; returns the two playlists with the
    timestamp line removed."""
    real = app.requests.get
    app.requests.get = net.get
    try:
        if hasattr(app, "refresh_once"):
            app.refresh_once()
        else:   # image from before the Provider layer
            matches = app.fetch_all_matches(app.fetch_sports())
            m3u, cnt = app.build_m3u(matches)
            app._cached_m3u = m3u
    finally:
        app.requests.get = real
    c = app.app.test_client()
    out = {}
    for name, path in (("playlist", "/playlist.m3u"), ("teams", "/playlist-teams.m3u")):
        body = c.get(path).get_data(as_text=True)
        out[name] = [ln for ln in body.splitlines() if not ln.startswith(("# Generated by", "# streamed-m3u team channels"))]
    return out


def entries(out):
    return sum(1 for ln in out["playlist"] if ln.startswith("#EXTINF"))


# ── Modes ───────────────────────────────────────────────────────────────────

if len(sys.argv) == 3 and sys.argv[1] == "--baseline":
    out = run_cycle(Network())
    json.dump(out, open(sys.argv[2], "w"), indent=1)
    print("baseline recorded: %d playlist entries, %d team lines -> %s"
          % (entries(out), len(out["teams"]), sys.argv[2]))
    sys.exit(0)

baseline = None
if len(sys.argv) == 3 and sys.argv[1] == "--compare":
    baseline = json.load(open(sys.argv[2]))

# 1. With streamed.pk the only Provider, nothing the viewer sees has changed.
net = Network()
got = run_cycle(net)
check("fixture serves games", entries(got) == 5, entries(got))
if baseline is not None:
    check("playlist identical to the pre-Provider image", got["playlist"] == baseline["playlist"])
    check("team playlist identical to the pre-Provider image", got["teams"] == baseline["teams"])
check("only the configured host was contacted", set(net.hosts) == {PRIMARY}, sorted(set(net.hosts)))
check("streamed is enabled by default", list(getattr(app, "PROVIDERS", [])) == ["streamed"],
      getattr(app, "PROVIDERS", None))

# 2. The setting decides which Providers are used.
app.PROVIDERS = []
net = Network()
off = run_cycle(net)
check("no Providers enabled: no games", entries(off) == 0, entries(off))
check("no Providers enabled: nothing contacted", net.hosts == [], net.hosts[:3])
app.PROVIDERS = ["streamed"]

app.PROVIDERS = ["nosuchprovider", "streamed"]
net = Network()
mixed = run_cycle(net)
check("unknown Provider name is ignored, the rest still work", entries(mixed) == entries(got), entries(mixed))
app.PROVIDERS = ["streamed"]

# 3. Mirrors are tried in order, and the app stops hammering a dead primary.
app.STREAMED_MIRRORS = ["https://" + MIRROR]
net = Network(serves=(MIRROR,), dead=(PRIMARY,))
via = run_cycle(net)
check("primary down: the mirror serves the same games", via["playlist"] == got["playlist"])
dead_tries = net.hosts.count(PRIMARY)
check("a dead primary is not retried on every request",
      dead_tries < len(net.hosts) / 2, "%d of %d" % (dead_tries, len(net.hosts)))

net = Network(serves=(PRIMARY, MIRROR))
both = run_cycle(net)
check("primary healthy: mirror is never used", MIRROR not in net.hosts, sorted(set(net.hosts)))
app.STREAMED_MIRRORS = []

# A 404 for one path is an answer, not an outage: the mirror is not asked and
# the app does not move over to it.
app.STREAMED_MIRRORS = ["https://" + MIRROR]
net = Network(serves=(PRIMARY, MIRROR), missing=("/api/stream/delta/bb1d",))
gap = run_cycle(net)
check("a 404 on one path does not send requests to the mirror", MIRROR not in net.hosts,
      sorted(set(net.hosts)))
check("a 404 on one path loses only that source", entries(gap) == entries(got) - 2, entries(gap))
app.STREAMED_MIRRORS = []

# Names typed into the console are tidied, not rejected.
app.PROVIDERS = ["Streamed", " STREAMED "]
net = Network()
tidy = run_cycle(net)
check("provider names are case and space tolerant, and not doubled", entries(tidy) == entries(got), entries(tidy))
app.PROVIDERS = ["streamed"]

# One odd item in an upstream list does not cost the whole refresh.
real_streams = dict(STREAMS)
STREAMS[("delta", "bb1d")] = [None, "oops"] + STREAMS[("delta", "bb1d")]
try:
    net = Network()
    odd = run_cycle(net)
finally:
    STREAMS.update(real_streams)
check("a malformed item is skipped, the rest of the refresh survives", entries(odd) == entries(got), entries(odd))

# 4. A domain nobody configured is never contacted, even if it would answer.
net = Network(serves=(STRANGER,), dead=(PRIMARY,))
none = run_cycle(net)
check("unconfigured host is never contacted", STRANGER not in net.hosts, sorted(set(net.hosts)))
check("all configured hosts down: no games, no crash", entries(none) == 0, entries(none))

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
