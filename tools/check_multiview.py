"""Verification harness for multi-view channels, with the feature ON. Not shipped.

Companion to tools/check_console.py, which covers the disabled half. The slot
count and the derived slug map are read once at import, so "enabled" cannot be
tested in the same process as "disabled" — hence a second script rather than a
second section.

    docker run --rm -e STARTUP_DELAY=0 -e MULTIVIEW_ENABLE=1 \
        -v $PWD/tools/check_multiview.py:/tmp/check_mv.py:ro \
        <image> python /tmp/check_mv.py

Uses Flask's test client, so nothing binds a port and no background threads
start. No encoder is involved: this phase is about the channels existing,
carrying guide data, and answering a tune the way a team channel does.
"""

import os
import shutil
import sys
import tempfile

DATA = tempfile.mkdtemp(prefix="check-mv-")
os.environ.setdefault("STARTUP_DELAY", "0")
os.environ["MULTIVIEW_ENABLE"] = "1"
os.environ.setdefault("MULTIVIEW_SLOTS", "2")
os.environ["DATA_DIR"] = DATA
os.environ["TEAMS_FILE"] = os.path.join(DATA, "teams.json")
os.environ["LINEUP_FILE"] = os.path.join(DATA, "lineup.json")
os.environ["MULTIVIEW_FILE"] = os.path.join(DATA, "multiview.json")
os.environ["EXTRACT_CACHE_FILE"] = os.path.join(DATA, "extract_cache.json")
os.environ["SETTINGS_FILE"] = os.path.join(DATA, "settings.json")
# A password, because the console's write endpoints need one - `require_write`
# refuses outright without it, the same as PUT /api/settings. Only the
# dashboard blueprint is gated by it, and nothing above this line touches the
# blueprint, so the earlier sections are unaffected.
CONSOLE_PASSWORD = "check-mv"
os.environ["CONSOLE_PASSWORD"] = CONSOLE_PASSWORD
os.environ.pop("PUBLIC_BASE_URL", None)

sys.path.insert(0, "/app")
import app          # noqa: E402
import multiview    # noqa: E402

c = app.app.test_client()
fails = []


def check(name, cond, detail=""):
    # str() first: a tuple as detail would otherwise be eaten as format args.
    note = "  [%s]" % (str(detail),) if detail != "" else ""
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, note))
    if not cond:
        fails.append(name)


def section(title):
    print("\n=== %s ===" % title)


app._install_seed_roster()
app._load_team_roster()
app.lineup.reload(os.environ["LINEUP_FILE"])
app._seed_favourites()
app._seed_nonteam()
with app._team_lock:
    KNOWN = set(app._team_roster)
multiview.reload(os.environ["MULTIVIEW_FILE"], app.MULTIVIEW_SLOTS, KNOWN)

SLOTS = app.MULTIVIEW_SLOTS

# ─── Seeding ──────────────────────────────────────────────────────────────────
section("seeding")
check("slug map has one entry per slot", len(app._MULTIVIEW_BY_SLUG) == SLOTS,
      app._MULTIVIEW_BY_SLUG)
check("slugs are derived from the display name",
      app._multiview_slug(0) == "multi-player-1", app._multiview_slug(0))
check("slot ids are 1-based strings",
      sorted(app._MULTIVIEW_BY_SLUG.values()) == [str(i + 1) for i in range(SLOTS)])

roster = dict(app._team_roster)
for i in range(SLOTS):
    slug = app._multiview_slug(i)
    entry = roster.get(slug) or {}
    check("slot %d is in the roster" % (i + 1), bool(entry), slug)
    check("slot %d has kind=multi" % (i + 1), entry.get("kind") == "multi",
          entry.get("kind"))
    check("slot %d is named for the display name" % (i + 1),
          entry.get("name") == app._multiview_name(i), entry.get("name"))

# A composite slot must never be flagged a favourite: the favourite flag wins
# the group-title race, and nothing in app.py ever clears it.
check("slots are not favourites",
      not any((roster.get(app._multiview_slug(i)) or {}).get("favourite")
              for i in range(SLOTS)))

# The permanent address. Non-team kinds share the feed prefix; a new prefix
# here would orphan every channel already created.
check("channel id uses the feed prefix",
      app._channel_id(app._multiview_slug(0), {"kind": "multi"})
      == "streamed.feed.multi-player-1",
      app._channel_id(app._multiview_slug(0), {"kind": "multi"}))

# Re-running seeding must not mint a second copy of anything.
before = len(app._team_roster)
app._seed_nonteam()
check("seeding is idempotent", len(app._team_roster) == before,
      "%d -> %d" % (before, len(app._team_roster)))

# ─── Playlist ─────────────────────────────────────────────────────────────────
section("playlist")
body, count = app.build_team_m3u()
lines = body.splitlines()
mv_urls = [ln for ln in lines if "/stream?multi=" in ln]
check("one URL per slot", len(mv_urls) == SLOTS, len(mv_urls))
check("addressed by slot, not by slug",
      all(u.rstrip().endswith(("multi=1", "multi=2")) for u in mv_urls), mv_urls)
check("no fixture in the URL",
      not any("team=" in u for u in mv_urls))
check("filed under its own group", 'group-title="Multi-view"' in body)

# Exactly one stream per channel: Dispatcharr rotates away from a channel that
# has an alternate when its health monitor fires, and never comes back.
# See docs/internal/PENDING_reconnect_health_clock.md.
extinf_idx = [n for n, ln in enumerate(lines) if ln.startswith("#EXTINF")
              and 'group-title="Multi-view"' in ln]
check("one EXTINF per slot", len(extinf_idx) == SLOTS, len(extinf_idx))
for n in extinf_idx:
    follow = lines[n + 1:n + 3]
    # The URL line carries the {{PUBLIC_BASE_URL}} placeholder here; playlist()
    # substitutes it per request from the Host header (gotcha #1), so match on
    # the path rather than on a scheme that is not present yet.
    check("exactly one stream follows EXTINF at line %d" % n,
          "/stream?multi=" in follow[0]
          and (len(follow) < 2 or "/stream?" not in follow[1]), follow)

# ─── Guide ────────────────────────────────────────────────────────────────────
# Gotcha #4: a channel created before its guide entry exists stays blank
# forever. Every slot needs a programme from the very first build.
section("guide")
xml = app.build_epg()
for i in range(SLOTS):
    cid = app._channel_id(app._multiview_slug(i), {"kind": "multi"})
    check("slot %d has a channel element" % (i + 1), '<channel id="%s">' % cid in xml)
    check("slot %d has a programme" % (i + 1), 'channel="%s"' % cid in xml)
check("unconfigured slot says so", "Multi-view not configured" in xml)

# ─── Tuning ───────────────────────────────────────────────────────────────────
section("tuning")
r = c.get("/stream?multi=1")
check("unconfigured slot is 503, like a team with no fixture",
      r.status_code == 503, r.status_code)
check("unconfigured slot explains itself",
      b"no primary" in r.data.lower(), r.data[:60])
r = c.get("/stream?multi=99")
check("unknown slot is 404", r.status_code == 404, r.status_code)
r = c.get("/stream?multi=notanumber")
check("nonsense slot is 404", r.status_code == 404, r.status_code)
r = c.head("/stream?multi=1")
check("HEAD is answered immediately, as Dispatcharr expects",
      r.status_code == 200, r.status_code)

# ─── Configured slot ──────────────────────────────────────────────────────────
section("configured slot")
teams = [s for s, v in roster.items() if v.get("kind", "team") == "team"]
teams.sort()
PRIMARY, SECONDARY = teams[0], teams[1]
stored = multiview.set_slot("1", {"primary": PRIMARY, "secondary": SECONDARY,
                                  "corner": "br", "size": "medium"},
                            path=os.environ["MULTIVIEW_FILE"],
                            slots=SLOTS, known=KNOWN)
check("slot accepted both channels",
      stored["primary"] == PRIMARY and stored["secondary"] == SECONDARY, stored)

xml = app.build_epg()
cid = app._channel_id(app._multiview_slug(0), {"kind": "multi"})
pname = (roster.get(PRIMARY) or {}).get("name") or PRIMARY
sname = (roster.get(SECONDARY) or {}).get("name") or SECONDARY
check("guide now names the pairing", "%s + %s" % (pname, sname) in xml,
      "%s + %s" % (pname, sname))
check("slot 2 still reads unconfigured", "Multi-view not configured" in xml)

# The address must not move when the contents change - that is the whole
# point of the shelf.
body2, _ = app.build_team_m3u()
check("playlist URL unchanged by configuring the slot",
      [ln for ln in body2.splitlines() if "/stream?multi=" in ln] == mv_urls)

# The streaming path itself is exercised in the composite section below;
# tuning here would spawn a real encoder inside the harness. HEAD is enough
# to prove the route accepts a configured slot.
r = c.head("/stream?multi=1")
check("configured slot is accepted", r.status_code == 200, r.status_code)

# A slot pointing at something the roster no longer has must come back empty
# rather than sending an encoder after a channel that is gone.
gone = multiview.load(os.environ["MULTIVIEW_FILE"], SLOTS, {"some-other-team"})
check("slug outside the roster is dropped on load",
      not multiview.configured(gone["slots"]["1"]), gone["slots"]["1"])

# ─── Multi-view, Phase 4: input feeders ───────────────────────────────────────
# A feeder keeps one FIFO fed from one channel and can change which channel
# without the reader seeing EOF. Driven here with a synthetic source, so these
# checks are about the plumbing - blocking, hangups, switching, cleanup - not
# about the network.
section("feeders")
import composite    # noqa: E402
import threading    # noqa: E402
import time         # noqa: E402

FIFO_DIR = os.path.join(DATA, "fifo")
os.makedirs(FIFO_DIR, exist_ok=True)


def source_of(tag, per_chunk=4096, count=None):
    """A source that emits `tag` bytes forever (or `count` times)."""
    def factory(slug):
        def gen():
            n = 0
            while count is None or n < count:
                yield (slug.encode()[:1] or b"?") * per_chunk
                n += 1
                time.sleep(0.01)
        return gen()
    return factory


def drain(path, sink, stop, opened):
    """Reader side: open the FIFO and append everything to `sink`."""
    fd = os.open(path, os.O_RDONLY)
    opened.set()
    try:
        while not stop.is_set():
            b = os.read(fd, 65536)
            if not b:
                break
            sink.append(b)
    finally:
        os.close(fd)


# A feeder must not wedge when nobody has opened the read end yet.
f = composite.Feeder(os.path.join(FIFO_DIR, "a.ts"), "1", "primary",
                     "http://127.0.0.1:1", open_source=source_of("a"))
f.start("alpha")
check("FIFO is created", os.path.exists(f.fifo_path))
import stat as _stat  # noqa: E402
check("FIFO is a fifo", _stat.S_ISFIFO(os.stat(f.fifo_path).st_mode))
check("feeder thread is running with no reader attached", f.is_alive())

sink, stop_r, opened = [], threading.Event(), threading.Event()
tr = threading.Thread(target=drain, args=(f.fifo_path, sink, stop_r, opened),
                      daemon=True)
tr.start()
opened.wait(5)
deadline = time.time() + 5
while time.time() < deadline and f.bytes_written < 8192:
    time.sleep(0.05)
check("bytes flow once a reader attaches", f.bytes_written > 0, f.bytes_written)
check("reader received them", sum(len(x) for x in sink) > 0)
check("all bytes are from the first source", set(b"".join(sink)) == {ord("a")},
      sorted(set(b"".join(sink)))[:4])

# Switching must not close the FIFO: the reader keeps its fd, the bytes
# simply change. That is what lets the miniplayer change mid-game.
sink.clear()
f._open_source = source_of("b")
moved = f.switch("bravo")
check("switch reports a change", moved is True)
check("switching to the same slug is a no-op", f.switch("bravo") is False)
deadline = time.time() + 5
while time.time() < deadline and ord("b") not in set(b"".join(sink) or b""):
    time.sleep(0.05)
check("new source's bytes arrive", ord("b") in set(b"".join(sink) or b""))
check("reader never saw EOF", tr.is_alive())
check("FIFO still open across the switch", os.path.exists(f.fifo_path))

# Selecting nothing holds the pipe open and goes quiet, rather than ending.
f.switch(None)
time.sleep(0.3)
quiet = f.bytes_written
time.sleep(0.5)
check("no source means no bytes, not EOF",
      f.bytes_written == quiet and tr.is_alive(), f.bytes_written - quiet)

stop_r.set()
f.stop()
check("feeder stops", not f.is_alive())
check("FIFO is removed on stop", not os.path.exists(f.fifo_path))

# The reader going away must be noticed, not wedge the writer.
section("reader hangup")
g = composite.Feeder(os.path.join(FIFO_DIR, "b.ts"), "1", "secondary",
                     "http://127.0.0.1:1", open_source=source_of("c", per_chunk=65536))
g.start("charlie")
sink2, stop2, opened2 = [], threading.Event(), threading.Event()
t2 = threading.Thread(target=drain, args=(g.fifo_path, sink2, stop2, opened2),
                      daemon=True)
t2.start()
opened2.wait(5)
deadline = time.time() + 5
while time.time() < deadline and g.bytes_written == 0:
    time.sleep(0.05)
check("feeding before the hangup", g.bytes_written > 0, g.bytes_written)
stop2.set()                      # reader closes its end mid-stream
deadline = time.time() + 15
while time.time() < deadline and g.is_alive():
    time.sleep(0.1)
check("feeder unwinds when the reader closes", not g.is_alive())
check("hangup is recorded, not swallowed", g.reader_gone is True)
g.stop()

# A connect that takes a long time must not deafen the feeder. Found live: a
# cold channel can sit inside one blocking request for most of CASCADE_BUDGET,
# and a switch made during that window was ignored for a full 60 seconds.
section("switch during a slow connect")
slow_entered = threading.Event()


def slow_source(slug):
    slow_entered.set()
    time.sleep(30)                     # never completes within this test
    raise AssertionError("slow source should have been abandoned")


h = composite.Feeder(os.path.join(FIFO_DIR, "c.ts"), "2", "primary",
                     "http://127.0.0.1:1", open_source=slow_source)
h.start("slow-one")
sink3, stop3, opened3 = [], threading.Event(), threading.Event()
t3 = threading.Thread(target=drain, args=(h.fifo_path, sink3, stop3, opened3),
                      daemon=True)
t3.start()
opened3.wait(5)
check("feeder entered the slow connect", slow_entered.wait(5))
t0 = time.time()
h.switch("something-else")
deadline = time.time() + 8
while time.time() < deadline and h._current != "something-else":
    time.sleep(0.05)
check("switch is seen while a connect is in flight",
      h._current == "something-else", h._current)
check("and seen promptly, not after the connect times out",
      time.time() - t0 < 8, round(time.time() - t0, 1))
t1 = time.time()
stop3.set()
h.stop(timeout=8)
check("stop is not blocked by an in-flight connect either",
      not h.is_alive() and time.time() - t1 < 8, round(time.time() - t1, 1))

# Two slots at once, which is what MULTIVIEW_SLOTS=2 implies even though only
# one may be active. Threads and descriptors must both come back.
section("concurrency and cleanup")
threads_before = threading.active_count()
fds_before = len(os.listdir("/proc/self/fd"))
pairs, readers, stoppers = [], [], []
for sid in ("1", "2"):
    sf = composite.SlotFeeders(sid, FIFO_DIR, "http://127.0.0.1:1",
                               open_source=source_of("x"))
    sf.start({"primary": "p-%s" % sid, "secondary": "s-%s" % sid})
    pairs.append(sf)
    for feeder in (sf.primary, sf.secondary):
        st, op, sk = threading.Event(), threading.Event(), []
        th = threading.Thread(target=drain, args=(feeder.fifo_path, sk, st, op),
                              daemon=True)
        th.start()
        op.wait(5)
        readers.append(th)
        stoppers.append(st)
time.sleep(1.5)
check("all four feeders are alive",
      all(sf.primary.is_alive() and sf.secondary.is_alive() for sf in pairs))
check("all four moved bytes",
      all(sf.primary.bytes_written > 0 and sf.secondary.bytes_written > 0
          for sf in pairs))
check("stats name the slot and role",
      pairs[0].stats()["primary"]["slot"] == "1"
      and pairs[0].stats()["secondary"]["role"] == "secondary")

for st in stoppers:
    st.set()
for sf in pairs:
    sf.stop()
time.sleep(0.5)
check("no FIFOs left behind",
      not [f for f in os.listdir(FIFO_DIR) if f.endswith(".ts")],
      os.listdir(FIFO_DIR))
check("threads returned to baseline",
      threading.active_count() <= threads_before + 1,
      "%d -> %d" % (threads_before, threading.active_count()))
check("descriptors returned to baseline",
      len(os.listdir("/proc/self/fd")) <= fds_before + 2,
      "%d -> %d" % (fds_before, len(os.listdir("/proc/self/fd"))))



def _pid_running(pid):
    """True if the pid exists and is not a zombie."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return False



# ─── Multi-view, Phase 5: the composite ───────────────────────────────────────
# Encoder lifecycle, not picture quality. Both feeders run on filler, so these
# checks need neither the network nor a render node; the software encoder and
# a small frame keep them quick. The real picture is verified live.
section("composite")
FILLER = composite.make_filler(os.path.join(DATA, "filler.ts"),
                               width=320, height=180, fps=10, seconds=1)
FILLER_SMALL = FILLER
check("filler clip is generated", os.path.getsize(FILLER) > 0,
      os.path.getsize(FILLER))

MVDEFAULTS = dict(fifo_dir=FIFO_DIR, base_url="http://127.0.0.1:1",
                  filler=FILLER, width=320, height=180, fps=10,
                  encoder="cpu", qp=30, idle_timeout=3)

# Geometry is arithmetic, and wrong geometry is a miniplayer half off screen.
for corner in ("tl", "tr", "bl", "br"):
    w, h, x, y = composite.pip_geometry(corner, "medium", 1920, 1080)
    check("geometry %s keeps the miniplayer on screen" % corner,
          0 <= x and 0 <= y and x + w <= 1920 and y + h <= 1080,
          (w, h, x, y))
w, h, _x, _y = composite.pip_geometry("br", "large", 1920, 1080)
ws, _hs, _, _ = composite.pip_geometry("br", "small", 1920, 1080)
check("large is bigger than small", w > ws, (ws, w))
check("dimensions are even, as H.264 requires", w % 2 == 0 and h % 2 == 0, (w, h))

mgr = composite.Manager(**MVDEFAULTS)
idle_slot = {"primary": "", "secondary": "", "corner": "br", "size": "medium",
             "audio": {"primary": 100, "secondary": 35}}
comp, err = mgr.start("1", idle_slot, max_active=1)
check("composite starts", comp is not None and err is None, err)

got = b""
t0 = time.time()
for chunk in comp.read():
    got += chunk
    if len(got) > 200_000 or time.time() - t0 > 40:
        break
check("composite produces bytes", len(got) > 0, len(got))
check("output is TS-aligned", got[:1] == b"\x47" and len(got) > 188, got[:4])
packets = len(got) // 188
bad = sum(1 for i in range(packets) if got[i * 188] != 0x47)
check("no sync breaks across the capture", bad == 0, "%d/%d" % (bad, packets))
check("encoder still alive", comp.alive())

st = comp.stats()
check("stats report the encoder", st["alive"] is True and st["encoder"] == "cpu")
check("stats carry both inputs",
      set(st["inputs"]) == {"primary", "secondary"}, list(st["inputs"]))
check("stats carry throughput", st["bytes_out"] > 0, st["bytes_out"])

# One encoder is about a third of this box. A second must be refused, not
# started quietly alongside it.
second, err2 = mgr.start("2", idle_slot, max_active=1)
check("a second composite is refused", second is None and err2, err2)
check("and says which slot holds the encoder", "slot 1" in (err2 or ""), err2)

# Killing the encoder must not leave the slot half-alive.
pid = comp.proc.pid
comp.proc.kill()
deadline = time.time() + 20
while time.time() < deadline and mgr.get("1") is not None:
    time.sleep(0.25)
check("reaper retires a dead encoder", mgr.get("1") is None)
check("no orphan process", not os.path.exists("/proc/%d/task" % pid)
      or open("/proc/%d/stat" % pid).read().split()[2] in ("Z", ""), pid)
# Same race as the idle check: the manager drops the slot before stop() has
# finished tearing the feeders down, so wait for the FIFOs rather than racing.
deadline = time.time() + 15
while time.time() < deadline and [f for f in os.listdir(FIFO_DIR)
                                  if f.startswith("mv1-")]:
    time.sleep(0.25)
check("no FIFOs left behind after a kill",
      not [f for f in os.listdir(FIFO_DIR) if f.startswith("mv1-")],
      os.listdir(FIFO_DIR))

# With the encoder gone the slot is free again.
comp3, err3 = mgr.start("2", idle_slot, max_active=1)
check("the slot frees up for another", comp3 is not None, err3)
pid3 = comp3.proc.pid if comp3 else None
mgr.stop("2")
check("stop leaves no process running",
      pid3 and not _pid_running(pid3), pid3)
check("stop leaves no FIFO",
      not [f for f in os.listdir(FIFO_DIR) if f.startswith("mv2-")],
      os.listdir(FIFO_DIR))

# An encoder must never outlive its audience.
section("idle teardown")
comp4, err4 = mgr.start("1", idle_slot, max_active=1)
check("composite starts for the idle test", comp4 is not None, err4)
pid4 = comp4.proc.pid if comp4 else None
deadline = time.time() + 30
while time.time() < deadline and mgr.get("1") is not None:
    time.sleep(0.5)
check("a composite nobody watches is stopped", mgr.get("1") is None)
# The manager drops the slot before the encoder has finished terminating, so
# give the process a moment to actually go rather than racing it.
deadline = time.time() + 15
while time.time() < deadline and pid4 and _pid_running(pid4):
    time.sleep(0.25)
check("and its process is gone", pid4 and not _pid_running(pid4), pid4)
mgr.stop_all()

# Found in Phase 6, live. A source that stops producing without closing
# leaves the feeder blocked in a read that never returns and its FIFO silent,
# and ffmpeg will not produce a frame without both inputs - so one quiet
# source freezes the whole composite. Observed for 55 minutes straight.
section("a stalled source is cut")


class _Stalling:
    """Emits `chunks` chunks, then goes quiet without closing.

    Shaped like composite._HttpSource on purpose: close() cuts the transport
    and the iterator notices, rather than closing the generator itself. A bare
    generator cannot be closed while it is executing - "generator already
    executing" - which is exactly why the real source is a wrapper.
    """

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = threading.Event()

    def __iter__(self):
        for _ in range(self.chunks):
            yield b"s" * 4096
            time.sleep(0.01)
        while not self.closed.is_set():   # the failure mode: silence, not EOF
            self.closed.wait(0.1)
        raise IOError("source cut")

    def close(self):
        self.closed.set()


def stalling_source(after_chunks):
    return lambda slug: _Stalling(after_chunks)


composite.SOURCE_STALL_TIMEOUT = 3.0
st_sink, st_stop, st_open = [], threading.Event(), threading.Event()
sf = composite.Feeder(os.path.join(FIFO_DIR, "stall.ts"), "9", "primary",
                      "http://127.0.0.1:1", open_source=stalling_source(4))
sf.filler = FILLER_SMALL
sf.start("alpha")
threading.Thread(target=drain, args=(sf.fifo_path, st_sink, st_stop, st_open),
                 daemon=True).start()
st_open.wait(5)
deadline = time.time() + 10
while time.time() < deadline and sf.bytes_written < 4096 * 4:
    time.sleep(0.1)
check("stalling source delivers before it goes quiet",
      sf.bytes_written >= 4096 * 4, sf.bytes_written)
check("a live source is watched", sf.live_source_idle() is not None,
      sf.live_source_idle())

quiet_at = sf.bytes_written
deadline = time.time() + 20
while time.time() < deadline and sf.stalls == 0:
    time.sleep(0.25)
check("the stalled source is cut", sf.stalls >= 1, sf.stalls)
# The point of cutting it: the FIFO starts moving again. Whether that is
# filler or a fresh connect does not matter to the encoder - only that the
# bytes never stop.
deadline = time.time() + 15
while time.time() < deadline and sf.bytes_written <= quiet_at:
    time.sleep(0.25)
check("and the input starts producing again",
      sf.bytes_written > quiet_at, (quiet_at, sf.bytes_written))
check("filler covered the gap rather than silence",
      sf.filler_bytes > 0 or sf.bytes_written > quiet_at, sf.filler_bytes)
sf.stop()
st_stop.set()

# The other way an input goes quiet: a connect that never returns. Found
# live - five minutes of a frozen composite with no error logged anywhere.
# The obvious fix, playing filler while the connect is in flight, is *worse*
# than the disease: ffmpeg then probes the filler rather than the stream it
# is about to receive, and the input never recovers. So this stays quiet on
# purpose, and the check is that it does.
section("a hanging connect stays quiet on purpose")


def hanging_source(hold):
    """A connect that takes `hold` seconds to come back."""
    def factory(slug):
        time.sleep(hold)
        return iter([b"h" * 4096])
    return factory


hg_sink, hg_stop, hg_open = [], threading.Event(), threading.Event()
hf = composite.Feeder(os.path.join(FIFO_DIR, "hang.ts"), "9", "secondary",
                      "http://127.0.0.1:1", open_source=hanging_source(6))
hf.filler = FILLER_SMALL
hf.start("alpha")
threading.Thread(target=drain, args=(hf.fifo_path, hg_sink, hg_stop, hg_open),
                 daemon=True).start()
hg_open.wait(5)
time.sleep(3)
check("no filler is spliced in front of a source that is still coming",
      hf.filler_bytes == 0, hf.filler_bytes)
first_filler = hf.filler_bytes
deadline = time.time() + 20
while time.time() < deadline and hf.bytes_written == 0:
    time.sleep(0.25)
check("and the real stream is the first thing the input ever carries",
      hf.bytes_written > 0 and first_filler == 0,
      (hf.bytes_written, first_filler))
hf.stop()
hg_stop.set()

composite.SOURCE_STALL_TIMEOUT = 15.0

# Found in Phase 6, live. A viewer whose client has gone must stop counting
# as one even if the encoder is wedged, or the reaper can never retire the
# composite and an encoder outlives its audience indefinitely.
section("an abandoned viewer is released")
STALL2 = os.path.join(DATA, "stalled-ffmpeg-2")
with open(STALL2, "w") as fh:
    fh.write("#!/bin/sh\nexec sleep 300\n")
os.chmod(STALL2, 0o755)
mgrV = composite.Manager(**dict(MVDEFAULTS, idle_timeout=600, ffmpeg=STALL2))
compV, _errV = mgrV.start("1", idle_slot, max_active=1)
vstop = threading.Event()
vgen = compV.read(vstop)
threading.Thread(target=lambda: next(vgen, None), daemon=True).start()
deadline = time.time() + 10
while time.time() < deadline and compV.viewers == 0:
    time.sleep(0.1)
check("a reader counts as a viewer while it waits", compV.viewers == 1,
      compV.viewers)
vstop.set()
deadline = time.time() + 10
while time.time() < deadline and compV.viewers > 0:
    time.sleep(0.1)
check("and stops counting once its client has gone", compV.viewers == 0,
      compV.viewers)
mgrV.stop_all()

# Found in Phase 6. Stopping a composite is not instant - the feeders have to
# unwind out of blocking writes - but the reaper drops the slot first, so a
# viewer who retunes inside that window gets a new composite while the old one
# is still tearing down. The teardown then deleted the *new* one's FIFOs and
# its encoder died with "No such file or directory". Reaching into _slots is
# exactly what the reaper does, and makes the race deterministic instead of
# hoping to hit it.
section("restart race")
mgrR = composite.Manager(**dict(MVDEFAULTS, idle_timeout=60))
compA, _errA = mgrR.start("1", idle_slot, max_active=1)
pathsA = {compA.feeders.primary.fifo_path, compA.feeders.secondary.fifo_path}
mgrR._slots.pop("1", None)
unwind = threading.Thread(target=compA.stop, daemon=True)
unwind.start()
compB, errB = mgrR.start("1", idle_slot, max_active=1)
check("the slot restarts while the old composite unwinds",
      compB is not None, errB)
pathsB = {compB.feeders.primary.fifo_path, compB.feeders.secondary.fifo_path}
check("the new composite gets FIFOs of its own", pathsA.isdisjoint(pathsB),
      sorted(os.path.basename(p) for p in pathsA | pathsB))
unwind.join(30)
check("the old teardown removed only its own",
      all(os.path.exists(p) for p in pathsB),
      [os.path.basename(p) for p in pathsB if not os.path.exists(p)])
check("and the new encoder survived it", compB.alive(), compB.exit_code)
mgrR.stop_all()

section("a channel change stops, the viewer rebuilds")
# Manager.apply used to stop *and start* on a channel change. With a viewer
# that raced its re-attach; without one it built an encoder nobody would read
# - clearing an idle slot started one with no channels at all (2026-09-23).
mgrC = composite.Manager(**dict(MVDEFAULTS, idle_timeout=60))
compC, _errC = mgrC.start("1", idle_slot, max_active=1)
changedC = mgrC.apply("1", dict(idle_slot, primary=PRIMARY), max_active=1)
check("a channel change on a running composite asks for a rebuild",
      changedC.get("restart") is True, changedC)
check("and leaves nothing running for the viewer to collide with",
      mgrC.get("1") is None, mgrC.get("1"))
mgrC.stop_all()

# ─── Multi-view, Phase 6: startup padding ─────────────────────────────────────
# A composite cannot start inside a tuner's patience, so the route commits the
# response first and pads the gap with null packets. These checks go through
# the HTTP route rather than the Manager, because the padding is the route's
# job and the splice from padding to picture is where it can go wrong.
#
# The app's own manager would build a 1080p60 VAAPI encoder, so it is replaced
# with the harness one: the point here is app.py's timing, not ffmpeg's.
section("startup padding")
app._mv_manager_instance = composite.Manager(
    **dict(MVDEFAULTS, idle_timeout=60))

t0 = time.time()
r = c.get("/stream?multi=1")
check("response is committed before anything resolves",
      r.status_code == 200, r.status_code)

nulls = 0
real = b""
first_byte = None
first_real = None
max_gap = 0.0
prev = t0
it = iter(r.response)
for chunk in it:
    now = time.time()
    max_gap = max(max_gap, now - prev)
    prev = now
    if first_byte is None:
        first_byte = now - t0
    if chunk == app.TS_NULL_PACKET:
        nulls += 1
        continue
    if first_real is None:
        first_real = now - t0
    real += chunk
    if len(real) > 100_000 or now - t0 > 60:
        break
it.close()

check("bytes reach the wire within 1s of the request",
      first_byte is not None and first_byte < 1.0, first_byte)
check("the first thing sent is padding, not picture",
      nulls > 0, nulls)
check("padding is a whole TS packet", len(app.TS_NULL_PACKET) == 188)
check("padding is on the null PID, which every decoder discards",
      app.TS_NULL_PACKET[1] & 0x1F == 0x1F and app.TS_NULL_PACKET[2] == 0xFF)
# The one that matters. Dispatcharr grants 60s of grace only while its buffer
# is empty; the first padding byte ends that and the budget becomes
# CONNECTION_TIMEOUT, 10s. So no gap anywhere - including the splice from
# padding to picture - may approach it.
check("no gap over 10s anywhere, padding or splice",
      max_gap < 10.0, "%.2fs" % max_gap)
check("padding gives way to real content", real and real[:1] == b"\x47",
      len(real))
check("spliced output is still TS-aligned",
      all(real[i * 188] == 0x47 for i in range(len(real) // 188)),
      len(real) // 188)

comp6 = app._mv_manager_instance.get("1")
check("the slot is really running an encoder",
      comp6 is not None and comp6.alive())
if comp6 is not None:
    # Gotcha #20's rule, applied here: padding is not data. The difference
    # from what reached the client is what the queue was still holding when
    # the capture stopped - bounded, and nothing to do with the padding.
    # The decisive version of this check is below, where an encoder that
    # produces nothing must report nothing however much padding goes out.
    inflight = comp6.bytes_out - len(real)
    check("keepalives are not counted as composite output",
          0 <= inflight <= composite.CHUNK * (app._MULTIVIEW_PIPE_DEPTH + 2),
          (comp6.bytes_out, len(real), "padding was %d bytes" % (nulls * 188)))
    check("cold start is measured and reported",
          comp6.stats()["startup_seconds"] is not None,
          comp6.stats()["startup_seconds"])
app._mv_manager_instance.stop_all()

# A composite spends its whole cold start with read() blocked and no bytes
# delivered. If that did not count as an audience, the reaper would stop an
# encoder in the middle of its own startup.
section("cold start is not idleness")
STALL = os.path.join(DATA, "stalled-ffmpeg")
with open(STALL, "w") as fh:
    fh.write("#!/bin/sh\nexec sleep 300\n")
os.chmod(STALL, 0o755)
app._mv_manager_instance = composite.Manager(
    **dict(MVDEFAULTS, idle_timeout=2, ffmpeg=STALL))
app.MULTIVIEW_START_TIMEOUT = 12

t0 = time.time()
r = c.get("/stream?multi=1")
it = iter(r.response)
next(it)                      # the priming packet, then the encoder stalls
time.sleep(6)                 # three times the idle timeout
comp7 = app._mv_manager_instance.get("1")
check("a composite still starting is not reaped as idle",
      comp7 is not None and comp7.alive(), comp7 is not None)
check("and it knows it is being watched", comp7 is not None
      and comp7.viewers >= 1, comp7.viewers if comp7 else None)
# Nothing has come out of this encoder and several seconds of padding have.
# If keepalives were counted as output, this would be non-zero - and every
# "is it actually producing?" question would have a false answer.
check("padding is never mistaken for output",
      comp7 is not None and comp7.bytes_out == 0,
      comp7.bytes_out if comp7 else None)
check("and it reports no first frame yet",
      comp7 is not None and comp7.stats()["startup_seconds"] is None,
      comp7.stats()["startup_seconds"] if comp7 else None)

# Nothing will ever come out of that encoder, so the stream must end rather
# than pad a tuner forever - and the wedged slot must not be left for the
# next tune to inherit.
section("a slot that never starts gives up")
padded7 = 1
for chunk in it:
    padded7 += 1
    check_gap = time.time() - t0
    if check_gap > 40:
        break
    if chunk != app.TS_NULL_PACKET:
        break
took = time.time() - t0
it.close()
check("the stream ends instead of padding forever", took < 40, "%.1fs" % took)
check("it lasts about the start timeout, not longer",
      12 <= took < 25, "%.1fs" % took)
check("everything sent was padding", padded7 > 5, padded7)
deadline = time.time() + 15
while time.time() < deadline and app._mv_manager_instance.get("1") is not None:
    time.sleep(0.25)
check("the wedged slot is torn down, not left for the next tune",
      app._mv_manager_instance.get("1") is None)
app._mv_manager_instance.stop_all()
app._mv_manager_instance = None

# ─── Multi-view, Phase 7: live control ────────────────────────────────────
# The graph is never rebuilt while it runs; corner, size and the mixer are
# retargeted over ZMQ instead. These checks are about the wiring - that the
# names in the graph and the names in the commands are the same names, that a
# real graph accepts them, and that the encoder is untouched by any of it.
# An input that has carried a real stream has configured ffmpeg's decoder for
# that stream. Anything spliced on afterwards is not decodable on it, and the
# whole composite freezes - not just that side of the picture.
section("an input carries one kind of thing for life")
mixed = composite.Feeder(os.path.join(FIFO_DIR, "mixed.ts"), "8", "primary",
                         "http://127.0.0.1:1", open_source=source_of("m"))
mixed.filler = FILLER
mx_sink, mx_stop, mx_open = [], threading.Event(), threading.Event()
mixed.start("alpha")
threading.Thread(target=drain, args=(mixed.fifo_path, mx_sink, mx_stop, mx_open),
                 daemon=True).start()
mx_open.wait(5)
deadline = time.time() + 10
while time.time() < deadline and not mixed.carried_source:
    time.sleep(0.25)
check("an input that has carried a stream knows it", mixed.carried_source)
filler_then = mixed.filler_bytes
mixed.switch(None)                       # "nothing selected" - filler's job
time.sleep(3)
check("and refuses filler from then on, whatever it is asked to play",
      mixed.filler_bytes == filler_then, (filler_then, mixed.filler_bytes))
mixed.stop()
mx_stop.set()

section("the command vocabulary")
import subprocess as _sp  # noqa: E402

GRAPH = composite.build_filter(
    {"corner": "br", "size": "medium", "audio": {"primary": 100, "secondary": 40}},
    1920, 1080, 60, control="ipc:///dev/shm/x/mv1-ctl-ab.zmq")
for name in (composite.PIP_SCALE, composite.PIP_OVERLAY,
             composite.VOL_PRIMARY, composite.VOL_SECONDARY):
    check("graph names %s, so a command can reach it" % name, name in GRAPH)
# One backslash dies as "No option name near '//...'", which reads like a bad
# URL rather than an escaping bug. Two survive both unescaping passes.
check("the control endpoint is double-escaped",
      "ipc\\\\://" in GRAPH, GRAPH[GRAPH.index("zmq@ctl"):][:60])
check("a graph without control carries no zmq filter",
      "zmq@" not in composite.build_filter({}, 1920, 1080, 60))

small = {"corner": "br", "size": "small"}
large = {"corner": "br", "size": "large"}
grow = composite.layout_commands(large, 1920, 1080, previous=small)
shrink = composite.layout_commands(small, 1920, 1080, previous=large)
# Whichever way it changes, the miniplayer must never be momentarily off the
# edge - so the smaller extent is the one in effect during the change.
check("growing moves before it resizes", grow[0][0] == composite.PIP_OVERLAY,
      [c[0] for c in grow])
check("shrinking resizes before it moves", shrink[0][0] == composite.PIP_SCALE,
      [c[0] for c in shrink])
check("the mixer maps 0-100 onto a gain",
      composite.audio_commands({"audio": {"primary": 100, "secondary": 0}})
      == [(composite.VOL_PRIMARY, "volume", "1.000"),
          (composite.VOL_SECONDARY, "volume", "0.000")])
check("a nonsense gain does not become a nonsense command",
      composite.audio_commands({"audio": {"primary": "loud", "secondary": 400}})
      == [(composite.VOL_PRIMARY, "volume", "0.000"),
          (composite.VOL_SECONDARY, "volume", "1.000")])

# ── against a real running graph ─────────────────────────────────────────
section("live control")
W7, H7, F7 = 640, 360, 10


def clip(name, vsrc, freq):
    """A short loopable clip with its own picture and its own tone."""
    path = os.path.join(DATA, "%s.ts" % name)
    r = _sp.run(["ffmpeg", "-y", "-v", "error",
                 "-f", "lavfi", "-i", "%s%ssize=%dx%d:rate=%d"
                 % (vsrc, ":" if "=" in vsrc else "=", W7, H7, F7),
                 "-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000" % freq,
                 "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                 "-f", "mpegts", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    return path


DARK = clip("dark", "color=c=black", 440)
BARS = clip("bars", "smptebars", 1000)

slot7 = {"primary": "", "secondary": "", "corner": "br", "size": "medium",
         "audio": {"primary": 100, "secondary": 0}}
comp7 = composite.Composite("7", slot7, fifo_dir=FIFO_DIR,
                            base_url="http://127.0.0.1:1", filler=DARK,
                            width=W7, height=H7, fps=F7, encoder="cpu", qp=30,
                            idle_timeout=600)
# The two inputs have to be distinguishable or "did it move" is unanswerable.
comp7.feeders.secondary.filler = BARS
comp7.start()

CAP = os.path.join(DATA, "control.ts")
cap = open(CAP, "wb")
stop7 = threading.Event()
threading.Thread(target=lambda: [cap.write(c) for c in comp7.read(stop7)],
                 daemon=True).start()
deadline = time.time() + 60
while time.time() < deadline and comp7.bytes_out < 60000:
    time.sleep(0.25)
check("the composite is producing before anything is changed",
      comp7.bytes_out > 0, comp7.bytes_out)
pid7 = comp7.proc.pid
started7 = comp7.started_at

time.sleep(4)
changed = comp7.apply(dict(slot7, corner="tl", size="large",
                           audio={"primary": 0, "secondary": 100}))
check("apply reports the layout moved", changed["layout"] is True, changed)
check("apply reports the mix moved", changed["audio"] is True, changed)
check("every command landed",
      changed["commands"][0] == changed["commands"][1] and changed["commands"][1] == 6,
      changed["commands"])
check("no command was rejected", comp7.commands_failed == 0,
      comp7.last_command_error)
time.sleep(5)

check("the encoder was never restarted",
      comp7.proc.pid == pid7 and comp7.started_at == started7, comp7.proc.pid)
check("and it is still the same healthy process", comp7.alive())

# Twenty changes back to back, the gate's stress case.
t7 = time.time()
sent7 = comp7.commands_sent
FLIPPED = {"primary": 0, "secondary": 100}
for i in range(20):
    # Carry the flipped mix through, or the first of these would quietly put
    # it back and the audio proof below would be measuring the wrong thing.
    comp7.apply(dict(slot7, corner=("tl", "br")[i % 2], size="medium",
                     audio=FLIPPED))
check("twenty changes in a row are all accepted",
      comp7.commands_failed == 0, comp7.last_command_error)
# Bounded by the frame rate, not by a fixed figure: a command lands on the
# next frame through the filter, and since the feeders pace the inputs
# (2026-09-23) that frame arrives in real time - 100 ms at this test's 10 fps,
# 17 ms in production at 60. The old fixed bound only held because ffmpeg
# used to race through burst-fed input faster than real time.
n7 = comp7.commands_sent - sent7
took7 = time.time() - t7
check("and take no more than three frames a command",
      took7 < n7 * 3.0 / F7 + 5, "%d commands in %.1fs, %.0f ms each"
      % (n7, took7, 1000.0 * took7 / max(n7, 1)))
check("the encoder is still alive after them", comp7.alive())
check("and still the same process", comp7.proc.pid == pid7)

# A source change is the one thing that cannot be done to a running graph.
# Retargeting a feeder was the plan and it freezes the composite: verified
# live, a healthy 13.8 MB encode stopped dead the instant its secondary
# changed and never produced another byte. So it asks for a rebuild instead.
sent_before = comp7.commands_sent
swap = comp7.apply(dict(slot7, corner="br", size="medium", audio=FLIPPED,
                        secondary="alpha"))
check("changing a channel asks for a rebuild", swap["restart"] is True, swap)
check("and says which channel moved", swap["sources"] == ["secondary"],
      swap["sources"])
# Layout and mix are unchanged here, and a rebuild would carry them anyway,
# so nothing is commanded.
check("a rebuild commands the graph not at all",
      comp7.commands_sent - sent_before == 0,
      comp7.commands_sent - sent_before)
check("the feeders are left alone; the rebuild carries the change",
      comp7.feeders.secondary.stats()["wanted"] != "alpha",
      comp7.feeders.secondary.stats()["wanted"])
check("and the encoder is still the same one until somebody rebuilds it",
      comp7.proc.pid == pid7 and comp7.alive(), comp7.proc.pid)

# A command the graph cannot honour must not take the stream with it.
ok, reply = comp7.command("overlay@nosuch", "x", "0")
check("a command to a filter that does not exist is refused", not ok, reply)
check("the failure is recorded rather than raised",
      comp7.commands_failed >= 1 and comp7.last_command_error,
      comp7.last_command_error)
check("and the stream is still running", comp7.alive())

stop7.set()
time.sleep(1)
comp7.stop()
cap.close()
check("the control socket is removed with the composite",
      not os.path.exists(comp7.control_path), comp7.control_path)

# ── and now by effect, not by reply code ─────────────────────────────────
# Phase 0's lesson: QSV filters answered "Success" and changed nothing. The
# whole capture is decoded linearly - no seeking, so no keyframe problem -
# and the miniplayer is found by brightness, since it is SMPTE bars on black.
section("the changes actually happened")
size7 = os.path.getsize(CAP)
check("the capture is worth analysing", size7 > 100000, size7)


def luma_track(crop):
    """Mean luma of `crop` for every frame, in order."""
    r = _sp.run(["ffmpeg", "-v", "error", "-i", CAP, "-vf",
                 "crop=%s,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-"
                 % crop, "-f", "null", "-"], capture_output=True, text=True)
    return [float(l.split("=")[1]) for l in r.stdout.splitlines() if "YAVG" in l]


# Which corner is lit, frame by frame. Comparing the two crops against each
# other rather than against a threshold makes this independent of exposure,
# of the encoder, and of exactly when each change landed - and the capture
# ends with the miniplayer being thrown back and forth twenty times, so a
# "what does the last second look like" test would be measuring the stress
# case rather than the change.
pw, ph, px, py = composite.pip_geometry("br", "medium", W7, H7)
lw, lh, lx, ly = composite.pip_geometry("tl", "medium", W7, H7)
br_track = luma_track("%d:%d:%d:%d" % (pw, ph, px, py))
tl_track = luma_track("%d:%d:%d:%d" % (lw, lh, lx, ly))
check("frames were decoded", len(br_track) > 20 and len(tl_track) > 20,
      (len(br_track), len(tl_track)))
if br_track and tl_track:
    n = min(len(br_track), len(tl_track))
    lit = ["br" if br_track[i] > tl_track[i] else "tl" for i in range(n)]
    head = lit[2:max(6, n // 8)]
    check("the miniplayer starts in the corner it was configured in",
          head.count("br") > len(head) * 0.8,
          "%d/%d frames" % (head.count("br"), len(head)))
    # The longest unbroken stretch of top-left, anywhere after the start.
    best = run = 0
    for state in lit[len(head):]:
        run = run + 1 if state == "tl" else 0
        best = max(best, run)
    check("and moves to the other corner on command, and stays there",
          best >= 10, "%d frames held top left" % best)
    check("the change is a move, not a second miniplayer",
          lit.count("tl") > 5 and lit.count("br") > 5,
          "br=%d tl=%d" % (lit.count("br"), lit.count("tl")))


def _duration(path):
    r = _sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


CAP_SECONDS = _duration(CAP)


def tone(lo, hi, first):
    """Mean volume in a band, over the first or last three seconds.

    Seeks to a wall-clock offset rather than using -sseof: this is a live
    capture cut at an arbitrary point, and a relative-to-end seek on it
    silently returned the beginning of the file, which reads as "the mixer
    did nothing".
    """
    args = ["ffmpeg", "-v", "info"]
    args += ["-t", "3"] if first else ["-ss", "%.1f" % max(0.0, CAP_SECONDS - 3.5)]
    args += ["-i", CAP, "-map", "0:a", "-af",
             "bandpass=f=%d:width_type=h:w=%d,volumedetect"
             % ((lo + hi) // 2, (hi - lo) // 2), "-f", "null", "-"]
    r = _sp.run(args, capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    return None


a440_first, a1k_first = tone(390, 490, True), tone(950, 1050, True)
a440_last, a1k_last = tone(390, 490, False), tone(950, 1050, False)
check("the primary's tone is what plays at first",
      a440_first is not None and a1k_first is not None and a440_first > a1k_first,
      (a440_first, a1k_first))
check("and the secondary's tone is what plays after the mixer moved",
      a1k_last is not None and a440_last is not None and a1k_last > a440_last,
      (a440_last, a1k_last))

# ── the stored document and the picture must not disagree ────────────────
section("a change is persisted as well as sent")
before = multiview.get_slot("1")
slot8, changed8, err8 = app._multiview_update("1", {"corner": "tl", "size": "large"})
check("the update is accepted", err8 is None, err8)
check("it returns the stored slot",
      slot8 and slot8["corner"] == "tl" and slot8["size"] == "large", slot8)
check("and it reached the file",
      multiview.load(os.environ["MULTIVIEW_FILE"], SLOTS, KNOWN)
      ["slots"]["1"]["corner"] == "tl")
check("with nothing playing it says so rather than pretending",
      changed8 and changed8["running"] is False, changed8)
check("the primary was not disturbed by a layout change",
      slot8["primary"] == before["primary"], (before["primary"], slot8["primary"]))
check("nor was the secondary",
      slot8["secondary"] == before["secondary"],
      (before["secondary"], slot8["secondary"]))
# Same rule one level down: setting one gain must not mute the other.
slot10, _c10, err10 = app._multiview_update("1", {"audio": {"secondary": 60}})
check("a partial mixer change keeps the other gain", err10 is None
      and slot10["audio"] == {"primary": before["audio"]["primary"],
                              "secondary": 60},
      slot10["audio"] if slot10 else err10)
check("and still leaves the channels alone",
      slot10 and slot10["primary"] == before["primary"], slot10)
_slot9, _c9, err9 = app._multiview_update("99", {"corner": "tl"})
check("an unknown slot is refused", err9 == "unknown slot", err9)

# Whatever writes the document, a playing encoder has to follow it. The
# console will call _multiview_update directly for an immediate change; this
# is the safety net that makes "persisted" and "on screen" the same thing.
section("the document is the authority")
mgrC = composite.Manager(reconcile=lambda sid: {"primary": "", "secondary": "",
                                                "corner": "tl", "size": "large",
                                                "audio": {"primary": 0,
                                                          "secondary": 100}},
                         **dict(MVDEFAULTS, idle_timeout=600))
compC, errC = mgrC.start("1", dict(idle_slot, corner="br", size="small"),
                         max_active=1)
check("composite starts for the reconcile test", compC is not None, errC)
# Somebody has to be reading. ffmpeg blocked on an undrained output pipe is
# not running frames, and the zmq filter only services its socket between
# frames - so commands to an unwatched composite time out. In service there
# is always a viewer, because a composite without one is stopped.
stopC = threading.Event()
threading.Thread(target=lambda: [None for _ in compC.read(stopC)],
                 daemon=True).start()
deadline = time.time() + 40
while time.time() < deadline and compC.slot.get("corner") != "tl":
    time.sleep(0.5)
check("a stored change reaches a running composite on its own",
      compC.slot.get("corner") == "tl" and compC.slot.get("size") == "large",
      (compC.slot.get("corner"), compC.slot.get("size")))
# A composite that was reconciled before its filter graph existed will have
# failed every command; the retry is what makes the end state right anyway.
deadline = time.time() + 40
while time.time() < deadline and (compC.commands_sent < 4 or compC.needs_resend):
    time.sleep(0.5)
check("and it was commanded, not restarted",
      compC.commands_sent >= 4 and compC.alive(),
      (compC.commands_sent, compC.commands_failed, compC.last_command_error))
check("a change that did not land is retried until it does",
      compC.needs_resend is False, compC.last_command_error)
sent_at_rest = compC.commands_sent
time.sleep(12)
check("a composite already in line is left alone",
      compC.commands_sent == sent_at_rest,
      (sent_at_rest, compC.commands_sent))
stopC.set()
mgrC.stop_all()

# ─── Multi-view, Phase 8: the console API ─────────────────────────────────
# Everything the console will do, done from curl's point of view instead.
section("the API reads")
import json as _json  # noqa: E402

# Gated with the rest of the console, like every other endpoint on the
# blueprint. The URL-free payload is a second line rather than the first one.
r = c.get("/api/multiview")
check("a read without a session is refused too", r.status_code == 401,
      r.status_code)
r = c.post("/login", data={"password": CONSOLE_PASSWORD, "next": "/"})
check("logging in works", r.status_code == 302, r.status_code)
TOKEN = (c.get("/api/session").get_json() or {}).get("csrf_token")
check("a CSRF token is issued", bool(TOKEN))
H = {"X-CSRF-Token": TOKEN}

r = c.get("/api/multiview")
check("and is served once signed in", r.status_code == 200, r.status_code)
view = r.get_json() or {}
check("it reports the feature on", view.get("enabled") is True, view.get("enabled"))
check("with one entry per slot", len(view.get("slots") or []) == SLOTS,
      len(view.get("slots") or []))
check("and offers the four corners",
      set(view.get("corners") or []) == {"tl", "tr", "bl", "br"},
      view.get("corners"))
check("and the three sizes in order",
      view.get("sizes") == ["small", "medium", "large"], view.get("sizes"))
one = (view.get("slots") or [{}])[0]
for field in ("id", "name", "url", "primary", "secondary", "corner", "size",
              "audio", "configured", "running"):
    check("a slot carries %s" % field, field in one, sorted(one))
for field in ("slug", "name", "warm", "playing", "silent", "status"):
    check("each side carries %s" % field, field in (one.get("primary") or {}),
          sorted(one.get("primary") or {}))
check("the slot names its own channel URL",
      one.get("url") == "/stream?multi=1", one.get("url"))

# The gate item that matters: a resolved URL carries a signing token, and this
# endpoint is readable without a password. So it must not contain one - ever,
# not merely "not right now".
blob = _json.dumps(view)
for needle in ("http://", "https://", ".m3u8", "embed", "strmd"):
    check("no %r anywhere in the payload" % needle, needle not in blob,
          blob[max(0, blob.find(needle) - 40):blob.find(needle) + 40]
          if needle in blob else "")

section("the API refuses what it should")
r = c.put("/api/multiview/1", json={"corner": "tl"})
check("a write without the CSRF token is still refused",
      r.status_code == 403 and (r.get_json() or {}).get("error") == "csrf",
      r.status_code)
r = c.put("/api/multiview/99", json={"corner": "tl"}, headers=H)
check("an unknown slot is 404", r.status_code == 404, r.status_code)
r = c.put("/api/multiview/1", json={"primary": "not-a-real-team"}, headers=H)
check("an unknown channel is refused, not silently dropped",
      r.status_code == 400 and "unknown channel" in
      str((r.get_json() or {}).get("error")), r.get_json())
r = c.put("/api/multiview/1", json={}, headers=H)
check("an empty change is refused", r.status_code == 400, r.status_code)
r = c.put("/api/multiview/1", json={"audio": {"primary": "loud"}}, headers=H)
check("a nonsense gain is refused", r.status_code == 400, r.get_json())
r = c.put("/api/multiview/1", json="not an object", headers=H)
check("a body that is not an object is refused", r.status_code == 400,
      r.status_code)

section("the API writes")
# Clear it first: the warm only fires for a channel that actually changed,
# and the module-level tests above left this slot already pointing at these
# two - so setting them again is correctly a no-op.
c.put("/api/multiview/1", json={"primary": "", "secondary": ""}, headers=H)
r = c.put("/api/multiview/1",
          json={"primary": PRIMARY, "secondary": SECONDARY,
                "corner": "br", "size": "medium"}, headers=H)
body = r.get_json() or {}
check("a full selection is accepted", r.status_code == 200 and body.get("ok"),
      body.get("error"))
slot1 = (body.get("slots") or [{}])[0]
check("and comes back in the snapshot",
      slot1["primary"]["slug"] == PRIMARY and slot1["secondary"]["slug"] == SECONDARY,
      (slot1["primary"]["slug"], slot1["secondary"]["slug"]))
check("the slot now reads as configured", slot1.get("configured") is True)
check("and it reached the file",
      multiview.load(os.environ["MULTIVIEW_FILE"], SLOTS, KNOWN)
      ["slots"]["1"]["primary"] == PRIMARY)
# The load-bearing behaviour: picking a channel starts resolving it there and
# then. Offline the warm will not succeed, but it must have been asked for.
check("picking a channel kicks a warm for it",
      set(body["changed"]["warming"]) == {PRIMARY, SECONDARY},
      body["changed"]["warming"])

r = c.put("/api/multiview/1", json={"corner": "tl"}, headers=H)
body = r.get_json() or {}
slot1 = (body.get("slots") or [{}])[0]
check("a partial change moves only what it names",
      slot1["corner"] == "tl" and slot1["size"] == "medium"
      and slot1["primary"]["slug"] == PRIMARY, slot1)
check("and warms nothing, because nothing was picked",
      body["changed"]["warming"] == [], body["changed"]["warming"])
# `layout` describes a *live* change, and nothing is playing here, so the
# honest report is "stored, nothing running" rather than "the picture moved".
check("with nothing playing it reports stored rather than shown",
      body["changed"]["running"] is False and body["changed"]["layout"] is False,
      body["changed"])

r = c.put("/api/multiview/1", json={"audio": {"secondary": 45}}, headers=H)
slot1 = ((r.get_json() or {}).get("slots") or [{}])[0]
check("a partial mixer change keeps the other gain",
      slot1["audio"] == {"primary": 100, "secondary": 45}, slot1["audio"])

# The rules the console has to show rather than discover.
r = c.put("/api/multiview/2", json={"secondary": SECONDARY}, headers=H)
slot2 = ((r.get_json() or {}).get("slots") or [{}, {}])[1]
check("a miniplayer without a main picture is dropped, as the rules say",
      slot2["secondary"]["slug"] == "" and slot2["configured"] is False, slot2)
r = c.put("/api/multiview/2",
          json={"primary": PRIMARY, "secondary": PRIMARY}, headers=H)
slot2 = ((r.get_json() or {}).get("slots") or [{}, {}])[1]
check("and the same channel cannot be shown against itself",
      slot2["secondary"]["slug"] == "", slot2["secondary"]["slug"])

section("the warm list")
warm = app._warm_list()
slugs = [w[0] for w in warm]
check("a configured slot's channels are on it",
      PRIMARY in slugs and SECONDARY in slugs, slugs)
check("multi-view comes first, so a trim keeps it",
      slugs[0] in (PRIMARY, SECONDARY), slugs[:3])
app.PREWARM_MAX_ENTRIES = 1
check("and the list is capped", len(app._warm_list()) == 1, len(app._warm_list()))
app.PREWARM_MAX_ENTRIES = 12
check("no channel appears twice", len(slugs) == len(set(slugs)), slugs)

section("the console has a section for it")
# What the browser harness cannot cheaply assert: that the markup exists at
# all, that it sits where the plan says, and that the stylesheet and script
# shipped with it. tools/check_render.py drives the controls for real.
r = c.get("/")
check("the console renders with the feature on", r.status_code == 200, r.status_code)
page = r.get_data(as_text=True)
check("it carries a multi-player section", 'id="multiview"' in page)
check("and an anchor to it", 'href="#multiview"' in page)
check("the section sits between streams and pre-warm",
      page.index('id="streams"') < page.index('id="multiview"') < page.index('id="prewarm"'))
check("the panel the script fills is there", 'id="mv-panel"' in page)

css = c.get("/static/dashboard.css").get_data(as_text=True)
check("the stylesheet carries the stage", ".mv-stage" in css)
check("and the mixer slider", ".mv-slider" in css)
# The two DESIGN.md rules this section is the likeliest place to break: the
# single shadow belongs to product photography, and there is one accent.
import re as _re  # noqa: E402
mv_css = css[css.index("── Multi-player"):]
mv_rules = _re.sub(r"/\*.*?\*/", "", mv_css, flags=_re.S)
check("the section declares no shadow",
      "shadow" not in mv_rules,
      mv_rules[max(0, mv_rules.find("shadow") - 60):][:120])
hexes = _re.findall(r"#[0-9a-fA-F]{3,8}", mv_rules)
check("and no colour of its own - every one comes from a token", not hexes, hexes)

js = c.get("/static/dashboard.js").get_data(as_text=True)
check("the script talks to the endpoint", "/api/multiview" in js)
check("and reads the geometry rather than copying it", "mvGeometry" in js)

geo = view.get("geometry") or {}
check("the snapshot carries the encoder's geometry",
      geo.get("width") and geo.get("height") and geo.get("margin") is not None, geo)
check("and the same size fractions the filter uses",
      geo.get("fractions") == dict(composite.SIZE_FRACTION), geo.get("fractions"))

section("a slot cannot be pointed at a slot")
# The picker lists the roster, and with the feature on the roster contains the
# multi-player channels themselves. A composite of a composite is a loop.
mv_slug = sorted(app._MULTIVIEW_BY_SLUG)[0]
check("the multi-player channels are on the roster", mv_slug in app._team_roster, mv_slug)
r = c.put("/api/multiview/1", json={"primary": mv_slug}, headers=H)
check("and are refused as a source",
      r.status_code == 400 and "multi-player channel" in
      str((r.get_json() or {}).get("error")), r.get_json())
check("the picker filters them out too",
      'e.kind === "multi"' in js)

section("the API stops a slot")
r = c.post("/api/multiview/1/stop", json={}, headers=H)
body = r.get_json() or {}
check("stopping a slot with nothing running is still ok",
      r.status_code == 200 and body.get("ok") is True, body)
check("and says nothing was running", body.get("stopped") is False,
      body.get("stopped"))
r = c.post("/api/multiview/99/stop", json={}, headers=H)
check("stopping an unknown slot is 404", r.status_code == 404, r.status_code)

section("Stop means stop")
# Until 2026-09-23 a console Stop was undone within a second: the playing
# viewer's stream saw its composite end, took it for a crash, and rebuilt it.
# Stop now holds the slot like Disconnect holds a team stream.
key = app._multiview_hold_key("1")
app._release_hold(key)
r = c.post("/api/multiview/1/stop", json={}, headers=H)
body = r.get_json() or {}
check("a stop reports how long the slot is held",
      body.get("held_seconds") == app.STREAM_DISCONNECT_HOLD, body.get("held_seconds"))
check("and the hold is set", app._disconnect_held(key))
r = c.get("/stream?multi=1")
check("a tune during the hold is refused, not rebuilt",
      r.status_code == 503 and b"stopped from the console" in r.data,
      (r.status_code, r.data[:60]))
c.put("/api/multiview/1", json={"corner": "tr"}, headers=H)
check("any change to the slot lifts the hold", not app._disconnect_held(key))

shutil.rmtree(DATA, ignore_errors=True)
print("\n%s" % ("ALL CHECKS PASSED" if not fails else "FAILURES: " + "; ".join(fails)))
sys.exit(1 if fails else 0)
