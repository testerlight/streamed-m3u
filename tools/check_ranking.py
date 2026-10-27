"""Verification harness for Source ranking and labels (provider-swap ticket 02). Not shipped.

One seam: the served playlist, /teams and the console's stream status, driven
through Flask's test client, with the Providers replaced by scripted ones that
follow the common game/Source shape (providers.py). Run inside a container built
from this directory:

    docker run --rm -e STARTUP_DELAY=0 -v $PWD/tools/check_ranking.py:/tmp/check.py:ro \
        <image> python /tmp/check.py

A Source may carry `quality` (pixels high, when its Provider labels it) and is
stamped `_provider`; a Provider has a `name` and a short `code`.
"""

import os
import re
import struct
import sys
import tempfile
import threading

DATA = tempfile.mkdtemp(prefix="check-rank-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ.pop("PROVIDERS", None)

sys.path.insert(0, "/app")
import app  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  [%s]" % detail) if detail != "" else ""))
    if not cond:
        fails.append(name)


# ── Scripted Providers ──────────────────────────────────────────────────────

def src(provider, no, embed, quality=None, hd=False, source="main"):
    out = {"id": "x", "streamNo": no, "language": "English", "hd": hd,
           "embedUrl": "https://embed.example/" + embed, "source": source,
           "_provider": provider}
    if quality is not None:
        out["quality"] = quality
    return out


class Fake:
    def __init__(self, name, code, games, sources):
        self.name, self.code = name, code
        self._games, self._sources = games, sources

    def list_games(self):
        return [dict(g, _provider=self.name, _category="Football") for g in self._games]

    def list_sources(self, game):
        return [dict(s, _match_title=game["title"], _match_id=game["id"],
                     _match_category="Football", _match_poster="") for s in self._sources[game["id"]]]


GAME = {"id": "g1", "title": "Green Bay Packers vs Chicago Bears", "category": "football",
        "date": 1793210000000, "poster": "",
        "teams": {"home": {"name": "Green Bay Packers"}, "away": {"name": "Chicago Bears"}}}
SLUG = "green-bay-packers"

# One game whose Sources came from two Providers, listed in this order.
MIXED = [
    src("fa", 1, "fa-720", quality=720),
    src("fa", 2, "fa-unknown"),
    src("fb", 1, "fb-1080", quality=1080),
    src("fa", 3, "fa-540", quality=540),
    src("fb", 2, "fb-unknown"),
]

PROV = {"p": []}
app._enabled_providers = lambda: PROV["p"]


def serve(*provs):
    PROV["p"] = list(provs)
    app.PROVIDERS = [p.name for p in provs]
    app._install_seed_roster()
    app._load_team_roster()
    app.refresh_once()


def names():
    body = app.app.test_client().get("/playlist.m3u").get_data(as_text=True)
    return [ln.split(",", 1)[1] for ln in body.splitlines() if ln.startswith("#EXTINF")]


def options(slug=SLUG):
    """The console's picker options for a stream playing this channel."""
    with app._active_streams_lock:
        app._active_streams["abcd1234"] = {
            "stream_id": "abcd1234", "stop": threading.Event(), "hold_key": "team:" + slug,
            "started_at": 1.0, "client_ip": "x", "bytes_sent": 0, "segments_sent": 0,
            "last_segment_at": 1.0, "m3u8_url": "x",
            "embed_url": "https://embed.example/fa-720", "source_label": "x"}
    try:
        r = app.app.test_client().get("/stream/status")
        data = r.get_json() or {}
    finally:
        with app._active_streams_lock:
            app._active_streams.pop("abcd1234", None)
    streams = data.get("streams") or []
    return ((streams[0].get("source") or {}).get("options") or []) if streams else []


def order(opts):
    return [o["embed_url"].rsplit("/", 1)[1] for o in opts]


def base_game_sources(lst):
    return {"g1": lst}


# 1. Quality first, then Provider order; unlabelled just below a labelled 1080p.
serve(Fake("fa", "AA", [GAME], base_game_sources(MIXED)), Fake("fb", "BB", [], {}))
opts = options()
check("console lists every Source", len(opts) == 5, len(opts))
check("order is quality, then unlabelled, then lower qualities, Provider order breaking ties",
      order(opts) == ["fb-1080", "fa-unknown", "fb-unknown", "fa-720", "fa-540"], order(opts))

# 2. Provider preference is the tiebreak, so reversing the setting flips it.
serve(Fake("fb", "BB", [GAME], base_game_sources(MIXED)), Fake("fa", "AA", [], {}))
check("Provider order is the tiebreak at equal quality",
      order(options())[1:3] == ["fb-unknown", "fa-unknown"], order(options()))

# 3. A Source known to have no audio ranks behind one that has it, at equal quality.
serve(Fake("fa", "AA", [GAME], base_game_sources(MIXED)), Fake("fb", "BB", [], {}))
app._mark_silent("https://embed.example/fa-unknown")
app.refresh_once()
check("no-audio Source ranks behind an audio one at the same quality",
      order(options())[1:3] == ["fb-unknown", "fa-unknown"], order(options()))
with app._no_audio_lock:
    app._no_audio_cache.clear()
app.refresh_once()

# 4. Labels show Provider, number and quality; the old letters are gone.
got = names()
check("labels are Provider code, number, quality",
      any(n.startswith("[BB1-1080p] ") for n in got) and any(n.startswith("[AA3-540p] ") for n in got), got)
check("an unlabelled Source shows no quality", any(n.startswith("[AA2] ") for n in got), got)
check("no old letter labels or HD/SD suffixes",
      not any(re.match(r"\[[A-Z]+\d+-(HD|SD)\]", n) for n in got), got)
labels = [o["label"] for o in options()]
check("console labels read the same way", "BB1-1080p" in labels and "AA2" in labels, labels)
check("console options name the Provider", {o.get("provider") for o in options()} == {"fa", "fb"},
      [o.get("provider") for o in options()])

# 5. HD means 720p or above; an unlabelled Source is HD only if the probe saw it.
hd = {o["embed_url"].rsplit("/", 1)[1]: o["hd"] for o in options()}
check("HD badge: 1080p and 720p yes, 540p no", hd["fb-1080"] and hd["fa-720"] and not hd["fa-540"], hd)
check("HD badge: an unlabelled Source is SD until probed", not hd["fa-unknown"], hd)


def ts_packet(pid, payload, pusi=False):
    head = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, 0x10])
    return head + payload + b"\xff" * (184 - len(payload))


def psi(table_id, rest):
    body = struct.pack(">H", 0xB000 | (len(rest) + 4)) + rest + b"\0\0\0\0"
    return b"\x00" + bytes([table_id]) + body


def sps(width_mbs, height_mbs, crop_bottom=0):
    bits = []

    def u(n, v):
        bits.extend(int(b) for b in format(v, "0%db" % n))

    def ue(v):
        s = format(v + 1, "b")
        bits.extend([0] * (len(s) - 1))
        bits.extend(int(b) for b in s)
    u(8, 66); u(8, 0); u(8, 30)           # baseline, level 3.0
    ue(0); ue(0); ue(0); ue(0); ue(1); u(1, 0)
    ue(width_mbs - 1); ue(height_mbs - 1)
    u(1, 1); u(1, 1)
    if crop_bottom:
        u(1, 1); ue(0); ue(0); ue(0); ue(crop_bottom)
    else:
        u(1, 0)
    u(1, 0); u(1, 1)
    while len(bits) % 8:
        bits.append(0)
    raw = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
    return b"\x00\x00\x00\x01\x67" + raw


def chunk(height_mbs, crop_bottom=0):
    """A few TS packets: PAT, a PMT naming H.264 video and AAC audio, and a
    video PES carrying an SPS for a picture 1280 wide."""
    pat = psi(0, struct.pack(">HBBB", 1, 0xC1, 0, 0) + struct.pack(">HH", 1, 0xE000 | 0x100))
    pmt = psi(2, struct.pack(">HBBBHH", 1, 0xC1, 0, 0, 0xE000 | 0x101, 0xF000)
              + bytes([0x1B]) + struct.pack(">HH", 0xE000 | 0x101, 0xF000)
              + bytes([0x0F]) + struct.pack(">HH", 0xE000 | 0x102, 0xF000))
    es = sps(80, height_mbs, crop_bottom) + b"\x00\x00\x00\x01\x68\xce\x38\x80"
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x00\x00" + es
    return (ts_packet(0, pat, True) + ts_packet(0x100, pmt, True)
            + ts_packet(0x101, pes, True)) * 12


def probe(url, body):
    """Run the startup probe on `url`, whose first chunk is `body`."""
    app.extract_m3u8_via_browser = lambda u: {"url": "https://cdn.example/x.m3u8", "headers": {}, "cookies": {}}
    app.get_segments = lambda *a, **k: (["https://cdn.example/seg1.ts"], 200)

    class R:
        status_code, content = 200, body
    app.cf_requests.get = lambda *a, **k: R()
    return app._validate_candidate(url, "probe")[0]


def hd_map():
    return {o["embed_url"].rsplit("/", 1)[1]: o["hd"] for o in options()}


PLAIN = [src("fa", 1, "fa-720", quality=720), src("fa", 2, "fa-unknown"), src("fa", 3, "fa-other")]
serve(Fake("fa", "AA", [GAME], base_game_sources(PLAIN)))
probe("https://embed.example/fa-unknown", chunk(45))             # 1280x720
probe("https://embed.example/fa-other", chunk(30))               # 1280x480
app.refresh_once()
hd = hd_map()
check("unlabelled Source the probe saw at 720p counts as HD", hd["fa-unknown"] is True, hd)
check("unlabelled Source the probe saw below 720p stays SD", hd["fa-other"] is False, hd)
check("a probe does not change where an unlabelled Source ranks",
      order(options()) == ["fa-unknown", "fa-other", "fa-720"], order(options()))

# 1080p is 68 macroblock rows cropped by 8 lines.
serve(Fake("fa", "AA", [GAME], base_game_sources([src("fa", 1, "fa-1080")])))
probe("https://embed.example/fa-1080", chunk(68, crop_bottom=4))
app.refresh_once()
check("a cropped 1080p picture is read as 1080, not 1088", app._probed_height.get("https://embed.example/fa-1080") == 1080,
      app._probed_height.get("https://embed.example/fa-1080"))

# A chunk that stops inside its tables, or has no picture header, is not an error.
for name, body in (("only the tables", chunk(45)[:376]), ("cut mid-packet", chunk(45)[:250]),
                   ("no video at all", chunk(45)[:188])):
    try:
        ok = probe("https://embed.example/odd-" + name, body)
    except Exception as exc:
        ok = "raised %r" % exc
    check("probe copes with a chunk with %s" % name if "cut" not in name else "probe copes with a chunk %s" % name,
          ok is True, ok)

print()
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
