"""Cushion and playlist padding (stream-recovery ticket 02) verification. Not shipped.

Runs inside a scratch-tag image (`docker build -t streamed-m3u:verify-cushion .`),
never against a live source or the live image. Scripted origins only.

    T=$PWD/tools
    # 1. units, tracker, the real route (about 2 minutes)
    docker run --rm -e STARTUP_DELAY=0 -v $T:/t:ro --entrypoint python \
        streamed-m3u:verify-cushion /t/check_cushion.py
    # 2. one messy, time-driven scenario, measured from the player's side;
    #    runs on the old image too (settings only apply to the new one)
    docker run --rm -e STARTUP_DELAY=0 -v $T:/t:ro -v /tmp/cushion:/out \
        --entrypoint python <image> /t/check_cushion.py --scenario hang \
        --window 60 --cushion 30 --padding 1 --out /out/new-60-30-1.json
    # 3. the same deterministic script through old and new: off must be
    #    byte-identical with matching timing
    ... /t/check_cushion.py --identity --window 60 --cushion 0 --padding 0 --out ...
    python3 tools/check_cushion.py --compare new.json old.json

Seams (chosen without a user to agree them, one line each):

  units   app._cushion_cut(durations, cap): the arithmetic of where a start
          falls. Catches off-by-one at the cap and unknown durations; misses
          everything that happens around it.
  tracker streamhealth.StreamHealth through its public hooks and snapshot:
          what is recorded for the start and that a start behind live is
          not reported as trouble. Misses the wiring in app.py.
  route   the real /stream route against a scripted HLS origin serving
          chunks with real timestamps: bytes, order, timing, padding, and
          the interactions with dedup, empty chunks, restamping, the idle
          timeout and failing hooks. Catches wiring; misses real providers.
  player  (scenario mode) what a real-time player would have seen of the
          bytes the route sent: freezes, cushion, silence on the wire.
"""

import argparse
import hashlib
import json
import os
import random
import socket
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TICKS = 90000
D = 5.0                 # seconds of video per chunk
N0 = 100                # first chunk number

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  [%s]" % (detail,)) if detail != "" else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n=== %s ===" % title, flush=True)


# ─── Chunks with real, continuous timestamps ─────────────────────────────────

_chunks = {}


def chunk_bytes(n):
    """Chunk n: D seconds of PES/PCR timestamps starting at (n - N0) * D, so a
    run of chunks is one continuous timeline. Cached; ~180 KB each."""
    if n not in _chunks:
        import check_restamp as cr
        _chunks[n] = cr.make_chunk((n - N0) * D, seconds=int(D), seed=n)
    return _chunks[n]


def chunk_id(unit):
    """Which chunk a received unit is, from its second packet: filler, random
    per seed, and never rewritten by restamping."""
    for n, body in list(_chunks.items()):
        if body[188:376] == unit[188:376]:
            return n
    return None


def split(data, lo=N0, hi=N0 + 400):
    """[(chunk number or None, bytes)] for a received stream (all chunks the
    same length)."""
    size = len(chunk_bytes(N0))
    return [(chunk_id(data[i:i + size]), data[i:i + size])
            for i in range(0, len(data) - size + 1, size)]


# ─── The scripted origin ──────────────────────────────────────────────────────

class Origin:
    """Poll-driven HLS origin: what each successive media-playlist poll lists
    is fixed, (first, last) chunk numbers, the last entry repeating. So two
    runs list the same things whatever their timing, and the delivered bytes
    can be compared exactly.

    plan  {chunk: [how, ...]} how each attempt at a chunk goes, then "ok":
          "403", "partial" (half, then the connection closes), "slow"
          (delivered over `slow_s`), "empty" (a 72-byte placeholder, always).
    hang  {poll index: seconds} that playlist request sleeps first.
    """

    def __init__(self, windows, plan=None, hang=None, slow_s=1.5, signed=False):
        self.windows, self.plan, self.hang = windows, plan or {}, hang or {}
        self.slow_s, self.signed = slow_s, signed
        self.polls = 0
        self.attempts = {}
        self.lock = threading.Lock()

    def reset(self):
        self.polls, self.attempts = 0, {}

    def playlist(self):
        with self.lock:
            k = self.polls
            self.polls += 1
        first, last = self.windows[min(k, len(self.windows) - 1)]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:%d" % D,
                 "#EXT-X-MEDIA-SEQUENCE:%d" % first]
        for n in range(first, last + 1):
            lines += ["#EXTINF:%.3f," % D,
                      "seg%05d.ts" % n + (("?token=%d&X-Amz-Date=%d" % (k, k)) if self.signed else "")]
        return k, "\n".join(lines) + "\n"


def serve(origin):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            leaf = self.path.split("?")[0].rsplit("/", 1)[-1]
            if leaf == "master.m3u8":
                return self._send(200, b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\nindex.m3u8\n",
                                  "application/vnd.apple.mpegurl")
            if leaf == "index.m3u8":
                k, text = origin.playlist()
                if k in origin.hang:
                    time.sleep(origin.hang[k])
                return self._send(200, text.encode(), "application/vnd.apple.mpegurl")
            if leaf.startswith("seg"):
                n = int(leaf[3:8])
                with origin.lock:
                    k = origin.attempts[n] = origin.attempts.get(n, 0) + 1
                plan = origin.plan.get(n, [])
                how = plan[k - 1] if k <= len(plan) else "ok"
                if how == "403":
                    return self._send(403, b"expired")
                if how == "empty":
                    return self._send(200, b"RIFF" + struct.pack("<I", 64) + b"WEBPVP8X" + b"\0" * 56,
                                      "image/webp")
                body = chunk_bytes(n)
                if how == "wrap":       # the fake 42-byte WebP header (gotcha #17)
                    body = b"RIFF\x00\x00\x00\x00WEBPVP8L" + b"\0" * 26 + body
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(body)))
                if how == "partial":
                    self.send_header("Connection", "close")
                self.end_headers()
                if how == "partial":
                    self.wfile.write(body[:len(body) // 2])
                    self.wfile.flush()
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                if how == "slow":
                    step = len(body) // 10 + 1
                    for i in range(0, len(body), step):
                        self.wfile.write(body[i:i + step])
                        self.wfile.flush()
                        time.sleep(origin.slow_s / 10)
                    return
                self.wfile.write(body)
                return
            self._send(404, b"")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# ─── Pulling through the real route ──────────────────────────────────────────

class Pull:
    """One pull of /stream. Keeps every item with its arrival time."""

    def __init__(self):
        self.items = []         # (seconds since start, bytes)
        self.video = b""
        self.nulls = 0
        self.ended = None       # seconds, when the stream ended by itself
        self.started = None
        self.embed = None


def pull(app, client, origin, port, name, want_chunks=None, max_s=60.0, pace=None,
         until=None):
    """Read the real /stream route until `want_chunks` whole chunks arrived
    (or `until(pull)` is true, or the stream ends, or max_s). Null packets
    are counted and kept out of `video`. `pace` seconds are slept after each
    chunk (a slow consumer)."""
    url = "http://127.0.0.1:%d/%s/master.m3u8" % (port, name)
    app.extract_m3u8_via_browser = (
        lambda _e, *a, **k: {"url": url, "headers": {}, "cookies": {},
                             "browser_m3u8_status": 200})
    embed = "http://fake-embed.test/" + name
    origin.reset()
    size = len(chunk_bytes(N0))
    p = Pull()
    p.embed = embed
    p.started = t0 = time.time()
    resp = client.get("/stream?url=" + quote(embed, safe=""), buffered=False)
    buf = bytearray()
    try:
        for item in resp.response:
            now = time.time() - t0
            p.items.append((now, item))
            if item == app.TS_NULL_PACKET:
                p.nulls += 1
            else:
                buf += item
                done = len(buf) // size
                if pace and done and len(buf) % size < len(item):
                    time.sleep(pace)
                if want_chunks is not None and done >= want_chunks:
                    # One more read: the loop's bookkeeping for the last
                    # chunk runs when the generator is asked for what follows.
                    next(iter(resp.response), None)
                    break
            if until and until(p):
                break
            if now > max_s:
                break
        else:
            p.ended = time.time() - t0
    finally:
        resp.close()
    p.video = bytes(buf)
    return p


def record_for(app, embed):
    sh = app.streamhealth
    deadline = time.time() + 6
    while time.time() < deadline:
        sh.HISTORY.flush()
        for rec in sh.HISTORY.read(300):
            if rec.get("channel") == embed:
                return rec
        time.sleep(0.1)
    return {}


# ─── Units ────────────────────────────────────────────────────────────────────

def unit_tests(app):
    section("units: where the start falls (app._cushion_cut)")
    cut = app._cushion_cut
    check("a 60s window, 5s chunks, 30s cap: drop the oldest 6", cut([5.0] * 12, 30) == 6)
    check("a 20s window under a 30s cap: drop nothing", cut([5.0] * 4, 30) == 0)
    check("exactly the cap: drop nothing", cut([5.0] * 6, 30) == 0)
    check("one chunk over the cap: drop one", cut([5.0] * 7, 30) == 1)
    check("a chunk that would straddle the cap is dropped ('up to' 30s)", cut([5.0] * 6, 29) == 1)
    check("a cap below one chunk still keeps the newest chunk", cut([5.0] * 3, 1) == 2)
    check("cap 0 is off: drop nothing", cut([5.0] * 12, 0) == 0)
    check("a negative cap is off", cut([5.0] * 12, -5) == 0)
    check("unknown durations: do not guess, drop nothing", cut([5.0, None, 5.0], 6) == 0)
    check("zero duration is unknown too", cut([5.0, 0, 5.0], 6) == 0)
    check("an empty list drops nothing", cut([], 30) == 0)
    check("float EXTINFs at the cap are kept (4.1s x 3 = 12.3s, cap 12.3)",
          cut([4.1] * 5, 12.3) == 2)
    check("mixed durations: newest first until the cap",
          cut([6.0, 6.0, 3.0, 3.0, 3.0, 3.0], 12) == 2)
    check("garbage in, nothing dropped", cut(None, 30) == 0 and cut(["x"], 30) == 0)


# ─── The tracker ──────────────────────────────────────────────────────────────

def tracker_tests(app):
    import logging
    import streamhealth as sh

    section("tracker: a start behind live")
    tmp = tempfile.mkdtemp(prefix="cushion-h-")
    sh.HISTORY.configure(os.path.join(tmp, "h.jsonl"))
    base = "http://o.test/x/index.m3u8"
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:100\n" + "".join(
        "#EXTINF:5.000,\ns%d.ts\n" % n for n in range(100, 112))
    info = {}
    sh.note_playlist(base, text, app.resolve_url, info)
    urls = ["http://o.test/x/s%d.ts" % n for n in range(100, 112)]

    class Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, rec):
            self.lines.append(rec.getMessage())

    cap = Cap()
    logging.getLogger("streamhealth").addHandler(cap)
    logging.getLogger("streamhealth").setLevel(logging.INFO)

    T = [5000.0]
    h = sh.StreamHealth("cush1", channel="c", source="s", clock=lambda: T[0])
    h.start_window(window_s=60.0, wanted_s=30.0, start_s=30.0, trimmed=6)
    for u in urls[6:]:
        h.download_begin(u)
        T[0] += 0.2
        h.download_end(u, b"x" * 1000)
        h.sending()
        h.delivered(u, 1000)
    h.playlist_ok(info, 0)
    h.start_done()
    snap = h.snapshot()
    check("the window and the start are on the live view",
          snap.get("window_s") == 60.0 and snap.get("cushion_wanted_s") == 30.0
          and snap.get("start_trimmed_chunks") == 6, (snap.get("window_s"),
          snap.get("cushion_wanted_s"), snap.get("start_trimmed_chunks")))
    check("achieved cushion measured after the burst (about 30s, not negative)",
          snap.get("start_cushion_s") is not None and 28 <= snap["start_cushion_s"] <= 30,
          snap.get("start_cushion_s"))
    check("cushion now is positive and about 30s", snap["cushion_s"] is not None
          and 27 <= snap["cushion_s"] <= 30, snap["cushion_s"])
    check("a start 30s back is healthy: not frozen, not struggling",
          snap["state"] == "healthy", (snap["state"], snap["reason"]))
    check("no freeze counted for the start", snap["freezes"] == 0 and snap["blips"] == 0)
    check("'behind live' is not reported for the burst the start itself caused",
          (snap.get("behind_live_s") or 0) < 0.1, snap.get("behind_live_s"))
    check("the timeline says how far back it started and why",
          any("30" in e["text"] and "60" in e["text"] for e in snap["timeline"]
              if e["kind"] == "cushion"), [e["text"] for e in snap["timeline"]])
    check("... and so does the log",
          any("30" in l and "60" in l and "behind live" in l for l in cap.lines), cap.lines[-2:])
    T[0] += 1000
    snap = h.snapshot()
    check("the cushion never goes negative, however long the source is silent",
          snap["cushion_s"] == 0 or snap["cushion_s"] is None or snap["cushion_s"] >= 0,
          snap["cushion_s"])
    h.end("client_left")
    h.finish()
    sh.HISTORY.flush()
    rec = [r for r in sh.HISTORY.read(5) if r.get("id") == "cush1"][0]
    check("the history line carries window, wanted, achieved and trimmed",
          (rec.get("window_s"), rec.get("cushion_wanted_s"), rec.get("start_trimmed_chunks"))
          == (60.0, 30.0, 6) and 28 <= (rec.get("start_cushion_s") or 0) <= 30,
          (rec.get("window_s"), rec.get("cushion_wanted_s"), rec.get("start_cushion_s")))
    check("... and behind_live_max_s is not the start burst (0, not 25)",
          (rec.get("behind_live_max_s") or 0) < 0.1, rec.get("behind_live_max_s"))

    # The window is the source's, whatever the setting: a short window caps it.
    T = [6000.0]
    short = "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:300\n" + "".join(
        "#EXTINF:5.000,\nw%d.ts\n" % n for n in range(300, 304))
    sh.note_playlist("http://o.test/y/index.m3u8", short, app.resolve_url, {})
    h2 = sh.StreamHealth("cush2", channel="c2", clock=lambda: T[0])
    cap.lines.clear()
    h2.start_window(window_s=20.0, wanted_s=30.0, start_s=20.0, trimmed=0)
    check("a window shorter than the wish is logged as the limit",
          any("20" in l and "30" in l and ("only" in l or "limit" in l) for l in cap.lines), cap.lines)
    h2.end("client_left")
    h2.finish()

    section("tracker: a hook that raises changes nothing")
    h3 = sh.StreamHealth("cush3", channel="c3", clock=lambda: 7000.0)
    sh.StreamHealth.start_window.__wrapped__ if False else None
    try:
        h3.start_window(window_s="garbage", wanted_s=None, start_s=object(), trimmed="x")
        h3.start_done()
        ok = True
    except Exception as e:
        ok = repr(e)
    check("bad arguments to the new hooks are swallowed", ok is True, ok)
    h3.end("client_left")
    h3.finish()
    logging.getLogger("streamhealth").removeHandler(cap)


# ─── The route ────────────────────────────────────────────────────────────────

def route_tests(app, client):
    import restamp  # noqa: F401
    import check_restamp as cr

    def run(origin, name, **kw):
        return pull(app, client, origin, serve(origin), name, **kw)

    def ids(p):
        return [n for n, _b in split(p.video)]

    def setting(**kw):
        for k, v in kw.items():
            setattr(app, k, v)

    app.STREAM_KEEPALIVE_INTERVAL = 0.5
    base = dict(BEHIND_LIVE_S=30.0, PLAYLIST_KEEPALIVE=True, HOLE_RESTAMP=True,
                HOLE_RESTAMP_MIN_S=8.0, STREAM_IDLE_TIMEOUT=45)

    # The first poll is the probe: 12 chunks = a 60s window. Then it slides.
    big = [(N0, N0 + 11), (N0, N0 + 11), (N0 + 1, N0 + 12), (N0 + 2, N0 + 13)]

    section("route: a 60s window under a 30s setting starts 30s back")
    setting(**base)
    o = Origin(big)
    p = run(o, "cap", want_chunks=8)
    got = ids(p)
    check("starts at the chunk 30s from the live edge and goes on in order",
          got == list(range(N0 + 6, N0 + 14)), got)
    check("the chunks cut from the start never turn up later (marked seen)",
          not any(n < N0 + 6 for n in got), got)
    bytes_ok = p.video == b"".join(chunk_bytes(n) for n in got)
    check("what arrived is the source's bytes, untouched (no restamp for a start further back)",
          bytes_ok)
    rec = record_for(app, p.embed)
    check("recorded: window 60s, wanted 30s, trimmed 6, achieved ~30s",
          rec.get("window_s") == 60.0 and rec.get("cushion_wanted_s") == 30.0
          and rec.get("start_trimmed_chunks") == 6 and 25 <= (rec.get("start_cushion_s") or 0) <= 30,
          (rec.get("window_s"), rec.get("cushion_wanted_s"), rec.get("start_trimmed_chunks"),
           rec.get("start_cushion_s")))
    check("no restamp, no skip, no freeze from starting further back",
          not rec.get("restamps") and not rec.get("skipped") and not rec.get("freezes"),
          (rec.get("restamps"), rec.get("skipped"), rec.get("freezes")))
    check("behind-live in the history is not the start", (rec.get("behind_live_max_s") or 0) <= 5.1,
          rec.get("behind_live_max_s"))

    section("route: setting 0 is today: the whole window")
    setting(**dict(base, BEHIND_LIVE_S=0))
    p0 = run(Origin(big), "cap0", want_chunks=14)
    check("starts at the oldest chunk the playlist offers", ids(p0)[0] == N0, ids(p0)[:3])
    check("... and sends all 12 then the two that follow, in order", ids(p0) == list(range(N0, N0 + 14)),
          ids(p0))
    rec0 = record_for(app, p0.embed)
    check("off: no cushion fields on the record", rec0.get("start_cushion_s") is None
          and rec0.get("start_trimmed_chunks") in (None, 0), (rec0.get("start_cushion_s"),
          rec0.get("start_trimmed_chunks")))

    section("route: a window shorter than the setting is the limit")
    setting(**base)
    small = [(N0, N0 + 3), (N0, N0 + 3), (N0 + 1, N0 + 4)]
    ps = run(Origin(small), "small", want_chunks=5)
    check("a 20s window: all four chunks, then on", ids(ps) == list(range(N0, N0 + 5)), ids(ps))
    recs = record_for(app, ps.embed)
    check("recorded: window 20s, wanted 30s, nothing trimmed, achieved ~20s",
          recs.get("window_s") == 20.0 and recs.get("cushion_wanted_s") == 30.0
          and not recs.get("start_trimmed_chunks") and 15 <= (recs.get("start_cushion_s") or 0) <= 20,
          (recs.get("window_s"), recs.get("start_trimmed_chunks"), recs.get("start_cushion_s")))
    setting(**dict(base, BEHIND_LIVE_S=0))
    ps0 = run(Origin(small), "small0", want_chunks=5)
    check("a 20s window: the bytes are the same as with the setting at 0", ps.video == ps0.video)

    section("route: sequential after a stall, never a jump to the live edge")
    setting(**base)
    # The playlist slides 3 chunks while we were not looking (a stall): the
    # next poll still has the chunks we have not sent. All must come, in order.
    stall = [(N0, N0 + 3), (N0 + 1, N0 + 6), (N0 + 4, N0 + 9)]
    pst = run(Origin(stall), "stall", want_chunks=9)
    check("oldest-first: every chunk still listed is sent, none passed over",
          ids(pst) == [N0, N0 + 1, N0 + 2, N0 + 3, N0 + 4, N0 + 5, N0 + 6, N0 + 7, N0 + 8], ids(pst))
    # Chunks that really left the window are lost; the next one is the oldest
    # still there, not the newest.
    gone = [(N0, N0 + 3), (N0 + 6, N0 + 9)]
    pg = run(Origin(gone), "gone", want_chunks=8)
    check("chunks that left the window are skipped; the oldest left is next, not the newest",
          ids(pg) == [N0, N0 + 1, N0 + 2, N0 + 3, N0 + 6, N0 + 7, N0 + 8, N0 + 9], ids(pg))

    section("route: a hole inside the burst is still a hole (restamp unchanged)")
    setting(**base)
    # Two dead chunks in the 30s we start: a 10s span, over the 8s threshold.
    dead = Origin(big, plan={N0 + 8: ["403", "403"], N0 + 9: ["403", "403"]})
    pd = run(dead, "burst-hole", want_chunks=6)
    got = ids(pd)
    check("six chunks arrive: the first four of the burst, then the two after the hole",
          got[:2] == [N0 + 6, N0 + 7] and len(got) >= 6, got)
    rec = record_for(app, pd.embed)
    check("one restamp of 10s, as before; none from the start", rec.get("restamps") == 1
          and abs((rec.get("restamp_s") or 0) - 10) < 0.1, (rec.get("restamps"), rec.get("restamp_s")))
    after = split(pd.video)[2][1]
    first = [v for _i, k, v in cr.read_times(after) if k == "pts"][0]
    want = int((got[2] - N0) * D * TICKS) + 7200 - int(10 * TICKS)
    check("the chunk after the hole has its timestamps back by the missing 10s",
          abs(first - want) <= 2, (first, want))

    section("route: empty placeholders in the burst are dropped as before")
    pe = run(Origin(big, plan={N0 + 7: ["empty"] * 3}), "burst-empty", want_chunks=7)
    got = ids(pe)
    check("the placeholder is dropped; the chunks around it are untouched",
          N0 + 7 not in got and got[:2] == [N0 + 6, N0 + 8], got)
    rec = record_for(app, pe.embed)
    check("counted as an empty chunk, not a restamp (5s hole)", rec.get("empty_chunks") == 1
          and not rec.get("restamps"), (rec.get("empty_chunks"), rec.get("restamps")))
    pe1 = run(Origin(big, plan={N0 + 6: ["empty"] * 3}), "burst-empty-first", want_chunks=6)
    check("the very first chunk of the start being a placeholder: start at the next",
          ids(pe1)[0] == N0 + 7, ids(pe1))

    section("route: dedup and the wrapper strip with the trimmed start")
    # Every poll re-signs every URL (token and X-Amz-Date change): the cut
    # chunks must still be recognised as seen, or they come back out of order.
    pw = run(Origin(big, plan={N0 + 6: ["wrap"] * 9, N0 + 8: ["wrap"] * 9}, signed=True),
             "burst-signed", want_chunks=8)
    check("re-signed URLs: the cut chunks do not come back, nothing twice",
          ids(pw) == list(range(N0 + 6, N0 + 14)), ids(pw))
    check("wrapped chunks in the burst are stripped to the source's bytes, as before",
          pw.video == b"".join(chunk_bytes(n) for n in ids(pw)))
    recw = record_for(app, pw.embed)
    check("... and not mistaken for holes", not recw.get("restamps") and not recw.get("skipped")
          and not recw.get("empty_chunks"), (recw.get("restamps"), recw.get("skipped")))

    section("route: messy burst (slow, half-sent, then fine)")
    pm = run(Origin(big, plan={N0 + 7: ["slow"], N0 + 9: ["partial"]}), "burst-messy", want_chunks=8)
    check("the slow chunk arrives whole, the half-sent one is retried whole",
          ids(pm) == list(range(N0 + 6, N0 + 14)), ids(pm))
    check("... and the bytes are the source's", pm.video == b"".join(chunk_bytes(n) for n in ids(pm)))

    section("route: the trim fails closed")
    real_cut = app._cushion_cut

    def boom(*_a, **_k):
        raise RuntimeError("rigged")

    app._cushion_cut = boom
    try:
        pb = run(Origin(big), "cut-rigged", want_chunks=12)
    finally:
        app._cushion_cut = real_cut
    check("a raise in the trim: today's start (the whole window), same bytes",
          ids(pb) == list(range(N0, N0 + 12)), ids(pb))

    real_sw = app.streamhealth.StreamHealth.start_window
    app.streamhealth.StreamHealth.start_window = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rigged"))
    try:
        ph = run(Origin(big), "hook-rigged", want_chunks=8)
    finally:
        app.streamhealth.StreamHealth.start_window = real_sw
    check("the tracker's start hook raising: the same video as when it works",
          ph.video == p.video)

    section("route: padding while a playlist request runs")
    setting(**base)
    windows = [(N0, N0 + 3), (N0, N0 + 3), (N0 + 1, N0 + 4), (N0 + 2, N0 + 5), (N0 + 3, N0 + 6)]
    hang = {1: 3.5}                  # the second request (the first poll in the loop) hangs 3.5s
    on = run(Origin(windows, hang=hang), "pad-on", want_chunks=7)
    off_flags = dict(base, PLAYLIST_KEEPALIVE=False)
    setting(**off_flags)
    off = run(Origin(windows, hang=hang), "pad-off", want_chunks=7)
    setting(**base)

    def longest_gap(p):
        ts = [t for t, _it in p.items]
        return max((b - a) for a, b in zip(ts, ts[1:])) if len(ts) > 1 else 0

    check("on: the 3.5s hang is padded (null packets, longest silence under 1.5s)",
          on.nulls >= 3 and longest_gap(on) < 1.5, (on.nulls, round(longest_gap(on), 2)))
    check("off: the same hang is 3.5s of silence on the wire",
          longest_gap(off) >= 3.0, round(longest_gap(off), 2))
    check("the video is identical, padding on or off", on.video == off.video and len(on.video) > 0)
    only_nulls = all(it == app.TS_NULL_PACKET for t, it in on.items
                     if len(it) == 188 and it[:4] == b"\x47\x1f\xff\x10")
    check("padding is null packets only (PID 0x1FFF, no payload, creates no video)", only_nulls)
    check("padding never lands mid-chunk: every unit of video is a whole chunk",
          [n for n, _b in split(on.video)] == [n for n, _b in split(off.video)]
          and None not in [n for n, _b in split(on.video)])
    rec_on = record_for(app, on.embed)
    check("health's video/cushion are untouched by padding (no freeze from it)",
          rec_on.get("video_s") == len(split(on.video)) * D
          or abs((rec_on.get("video_s") or 0) - len(split(on.video)) * D) < 0.01,
          (rec_on.get("video_s"), len(split(on.video))))

    setting(**dict(base, STREAM_KEEPALIVE_INTERVAL=0))
    app.STREAM_KEEPALIVE_INTERVAL = 0
    z = run(Origin(windows, hang={1: 2.0}), "pad-zero", want_chunks=6)
    app.STREAM_KEEPALIVE_INTERVAL = 0.5
    check("STREAM_KEEPALIVE_INTERVAL=0 still means no padding at all", z.nulls == 0, z.nulls)

    section("route: the playlist request raising or the stream stopping while it hangs")
    real_gs = app.get_segments
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            time.sleep(1.2)
            raise RuntimeError("rigged playlist")
        return real_gs(*a, **k)

    app.get_segments = flaky
    try:
        pf = run(Origin(windows), "pl-raise", want_chunks=7, max_s=40)
    finally:
        app.get_segments = real_gs
    check("a raise inside a padded playlist request is handled as today (idle 2s, carry on)",
          len(ids(pf)) >= 7 and pf.video == b"".join(chunk_bytes(n) for n in ids(pf)), ids(pf))
    recf = record_for(app, pf.embed)
    check("... and counted as an internal error, as today", recf.get("internal_errors") == 1,
          recf.get("internal_errors"))

    section("route: the 45s idle limit is not extended by padding")
    setting(**dict(base, STREAM_IDLE_TIMEOUT=6))
    quiet = Origin([(N0, N0 + 1)], hang={k: 1.5 for k in range(1, 60)})
    pq = run(quiet, "idle", max_s=30)
    check("a silent source still ends the stream by itself, on time", pq.ended is not None
          and 6 <= pq.ended <= 11, pq.ended)
    check("... with padding flowing the whole time", pq.nulls >= 3, pq.nulls)
    recq = record_for(app, pq.embed)
    check("... recorded as the idle limit", recq.get("end_reason") == "idle_timeout", recq.get("end_reason"))
    setting(**base)

    section("route: a consumer that reads slowly (Jellyfin -re behind Dispatcharr)")
    ps2 = run(Origin(big), "slow-reader", want_chunks=6, pace=0.6)
    rec = record_for(app, ps2.embed)
    check("a 30s burst read at one chunk per 0.6s: in order, whole, no freeze counted",
          ids(ps2) == list(range(N0 + 6, N0 + 12)) and not rec.get("freezes"),
          (ids(ps2), rec.get("freezes")))
    check("... the wait is the player's, not the source's", not rec.get("by_cause"), rec.get("by_cause"))


# ─── Scenario mode: a live, time-driven source, measured from the player ────

SEG_PACKETS = 200
SEG_BYTES = SEG_PACKETS * 188


def seg_body(n):
    out = bytearray()
    for i in range(SEG_PACKETS):
        out += b"\x47\x41\x00\x10" + struct.pack(">IH", n, i) + b"A" * 178
    return bytes(out)


class Live:
    """A live source on a real clock. Chunk n is published at a time fixed by
    the clock, the playlist lists the last `window` seconds of them, and the
    failures are scheduled in seconds since the pull began:

      errors   (a, b)         playlist answers HTTP 500 from a to b
      silent   (a, b)         nothing new is published from a to b; the
                              backlog appears at b (the video is continuous,
                              it was only late)
      hang     (at, seconds)  the first playlist request after `at` sleeps
      slow     {chunk: s}     that chunk downloads over s seconds, whole
      partial  [chunk]        that chunk's first attempt is half-sent
    """

    def __init__(self, window_s, errors=None, silent=None, hang=None, slow=None,
                 partial=None):
        self.window_s, self.nwin = window_s, int(window_s / D)
        self.errors, self.silent, self.hang = errors, silent, hang
        self.slow, self.partial = slow or {}, set(partial or [])
        self.reset()

    def reset(self):
        self.t0 = time.time()
        self.hung = False
        self.tried = set()

    def now(self):
        return time.time() - self.t0

    def newest(self, t=None):
        t = self.now() if t is None else t
        if self.silent and self.silent[0] <= t < self.silent[1]:
            t = self.silent[0]
        return N0 + self.nwin - 1 + int(t / D)


def serve_live(live):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            leaf = self.path.split("?")[0].rsplit("/", 1)[-1]
            t = live.now()
            if leaf == "master.m3u8":
                return self._send(200, b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\nindex.m3u8\n",
                                  "application/vnd.apple.mpegurl")
            if leaf == "index.m3u8":
                if live.hang and not live.hung and t >= live.hang[0]:
                    live.hung = True
                    time.sleep(live.hang[1])
                    t = live.now()
                if live.errors and live.errors[0] <= t < live.errors[1]:
                    return self._send(500, b"busy")
                newest = live.newest(t)
                first = max(N0, newest - live.nwin + 1)
                lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:%d" % D,
                         "#EXT-X-MEDIA-SEQUENCE:%d" % first]
                for n in range(first, newest + 1):
                    lines += ["#EXTINF:%.3f," % D, "seg%05d.ts" % n]
                return self._send(200, ("\n".join(lines) + "\n").encode(),
                                  "application/vnd.apple.mpegurl")
            if leaf.startswith("seg"):
                n = int(leaf[3:8])
                if n > live.newest(t):
                    return self._send(404, b"not yet")
                body = seg_body(n)
                first_try = n not in live.tried
                live.tried.add(n)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(body)))
                if n in live.partial and first_try:
                    self.send_header("Connection", "close")
                self.end_headers()
                if n in live.partial and first_try:
                    self.wfile.write(body[:len(body) // 2])
                    self.wfile.flush()
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                if n in live.slow:
                    step = len(body) // 10 + 1
                    for i in range(0, len(body), step):
                        self.wfile.write(body[i:i + step])
                        self.wfile.flush()
                        time.sleep(live.slow[n] / 10)
                    return
                self.wfile.write(body)
                return
            self._send(404, b"")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def scenario(name, window_s):
    f = N0 + int(window_s / D) - 1          # the newest chunk when the pull begins
    if name == "messy":
        # A playlist 500 for 10s, a source silent 12s, a request that hangs
        # 13s (the playlist timeout is 15s), one slow chunk, one half-sent.
        return Live(window_s, errors=(25, 35), silent=(55, 67), hang=(85, 13.0),
                    slow={f + 9: 3.0}, partial=[f + 20]), 120
    if name == "silent":
        # The source publishes nothing for 30s (the idle limit is 45s) and then
        # delivers the backlog: video that is late, not missing, if the stream
        # still has the chunks. Only a cushion that long (and a window that
        # long) survives it. (A playlist that answers 500 for more than ~12s
        # ends the stream outright, 6 failures in a row: not a cushion matter.)
        return Live(window_s, silent=(30, 60)), 100
    if name == "hang":
        # Just the hang, 13s, so padding is the only difference.
        return Live(window_s, hang=(30, 13.0)), 70
    raise SystemExit("unknown scenario " + name)


def player_view(p):
    """What a real-time player makes of what the route sent. It starts when the
    first chunk is complete and plays chunks in the order they arrived, each
    once whole; if the next is not whole by the time the last one ends, the
    picture is frozen until it is. Chunks missing from the sequence are holes
    (video that never existed on our side), reported separately."""
    arrivals, total = [], 0
    last_item, gaps, first_byte = None, [], None
    for t, item in p.items:
        if first_byte is None:
            first_byte = t
        if last_item is not None:
            gaps.append(t - last_item)
        last_item = t
        if len(item) == 188 and item[:4] == b"\x47\x1f\xff\x10":
            continue
        total += len(item)
        while (len(arrivals) + 1) * SEG_BYTES <= total:
            arrivals.append(t)
    units = [p.video[i * SEG_BYTES:(i + 1) * SEG_BYTES] for i in range(len(arrivals))]
    seqs = [struct.unpack(">I", u[4:8])[0] for u in units]
    out = {"first_byte_s": round(first_byte, 2) if first_byte is not None else None,
           "chunks": len(arrivals), "first_chunk": seqs[0] if seqs else None,
           "last_chunk": seqs[-1] if seqs else None, "nulls": p.nulls,
           "wire_gap_max_s": round(max(gaps), 2) if gaps else None,
           "wire_silent_over_10s": sum(1 for g in gaps if g > 10)}
    if not arrivals:
        return out
    holes = sum(max(0, b - a - 1) for a, b in zip(seqs, seqs[1:]))
    ends, freezes, cushions = None, [], []
    for t in arrivals:
        if ends is None:
            ends = t + D
            continue
        if t > ends + 1e-9:
            freezes.append((round(ends, 1), round(t - ends, 1)))
            ends = t + D
            cushions.append(0.0)
        else:
            cushions.append(ends - t)
            ends += D
    burst = [t for t in arrivals if t <= arrivals[0] + 3.0]
    long_f = [f for f in freezes if f[1] >= 1.0]
    out.update({
        "burst_chunks": len(burst), "burst_video_s": len(burst) * D,
        "holes": holes, "holes_s": holes * D,
        "freezes": len(long_f), "frozen_s": round(sum(f[1] for f in long_f), 1),
        "longest_freeze_s": max([f[1] for f in long_f], default=0.0),
        "freeze_list": long_f,
        "cushion_min_s": round(min(cushions[len(burst) - 1:] or [0]), 1),
        "cushion_median_s": round(sorted(cushions)[len(cushions) // 2], 1) if cushions else None,
    })
    return out


def run_scenario(args):
    app = boot_app()
    client = app.app.test_client()
    app.BEHIND_LIVE_S = float(args.cushion)
    app.PLAYLIST_KEEPALIVE = bool(int(args.padding))
    app.STREAM_IDLE_TIMEOUT = 45
    live, seconds = scenario(args.scenario, args.window)
    port = serve_live(live)
    name = "%s-%s-%s-%s" % (args.scenario, args.window, args.cushion, args.padding)
    url = "http://127.0.0.1:%d/%s/master.m3u8" % (port, name)
    app.extract_m3u8_via_browser = (
        lambda _e, *a, **k: {"url": url, "headers": {}, "cookies": {},
                             "browser_m3u8_status": 200})
    embed = "http://fake-embed.test/" + name
    live.reset()
    p = Pull()
    p.embed = embed
    t0 = time.time()
    resp = client.get("/stream?url=" + quote(embed, safe=""), buffered=False)
    buf = bytearray()
    try:
        for item in resp.response:
            now = time.time() - t0
            p.items.append((now, item))
            if item == app.TS_NULL_PACKET:
                p.nulls += 1
            else:
                buf += item
            if now > seconds:
                break
    finally:
        resp.close()
    p.video = bytes(buf)
    out = {"image": os.environ.get("IMAGE", "?"), "args": vars(args), "view": player_view(p)}
    if getattr(app, "streamhealth", None) is not None:
        rec = record_for(app, embed)
        out["health"] = {k: rec.get(k) for k in (
            "freezes", "frozen_s", "longest_freeze_s", "by_cause", "skipped", "skipped_s",
            "downloads_failed", "window_s", "cushion_wanted_s", "start_cushion_s",
            "start_trimmed_chunks", "first_chunk_s", "playlist_errors")}
    print(json.dumps(out))
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)


def report(files):
    cols = ("first_byte_s", "burst_video_s", "holes_s", "freezes", "frozen_s",
            "longest_freeze_s", "cushion_min_s", "wire_gap_max_s", "wire_silent_over_10s", "nulls")
    print("%-30s" % "run" + "".join("%-11s" % c[:10] for c in cols))
    for f in files:
        d = json.load(open(f))
        a, v = d["args"], d["view"]
        label = "%s %s w%d c%s pad%s" % (a["scenario"], d.get("image", "?"), a["window"],
                                         a["cushion"], a["padding"])
        print("%-30s" % label + "".join("%-11s" % v.get(c) for c in cols))
        h = d.get("health") or {}
        if h.get("window_s") is not None or h.get("start_cushion_s") is not None:
            print("%-30s tracker: window %ss, wanted %ss, start cushion %ss, trimmed %s, "
                  "freezes %s (%ss)" % ("", h.get("window_s"), h.get("cushion_wanted_s"),
                                         h.get("start_cushion_s"), h.get("start_trimmed_chunks"),
                                         h.get("freezes"), h.get("frozen_s")))


# ─── Identity mode: the same deterministic script, old and new ───────────────

def run_identity(args):
    app = boot_app()
    client = app.app.test_client()
    app.BEHIND_LIVE_S = float(args.cushion)
    app.PLAYLIST_KEEPALIVE = bool(int(args.padding))
    app.STREAM_KEEPALIVE_INTERVAL = 1.0
    app.HOLE_RESTAMP = True
    if args.identity == "big":
        # A 60s window at the probe, then stalls that let chunks slide out.
        windows = [(N0, N0 + 11), (N0, N0 + 11), (N0 + 1, N0 + 12), (N0 + 7, N0 + 18),
                   (N0 + 12, N0 + 23), (N0 + 17, N0 + 28)]
        want = 20
    else:
        windows = [(N0, N0 + 3), (N0, N0 + 3), (N0 + 1, N0 + 4), (N0 + 4, N0 + 7),
                   (N0 + 6, N0 + 9), (N0 + 9, N0 + 12)]
        want = 11
    # Messy: slow, dead twice, half-sent, a request that hangs 2.5s.
    origin = Origin(windows, plan={N0 + 8: ["slow"], N0 + 10: ["403", "403"],
                                   N0 + 13: ["403", "403"], N0 + 14: ["slow"],
                                   N0 + 16: ["partial"]},
                    hang={3: 2.5}, slow_s=1.5)
    p = pull(app, client, origin, serve(origin), "identity-" + args.identity,
             want_chunks=want, max_s=90)
    marks, total, size = [], 0, len(chunk_bytes(N0))
    for t, item in p.items:
        if item == app.TS_NULL_PACKET:
            continue
        total += len(item)
        while (len(marks) + 1) * size <= total:
            marks.append(round(t, 2))
    ids = [n for n, _b in split(p.video)]
    out = {"image": os.environ.get("IMAGE", "?"), "args": vars(args), "segments": ids,
           "digest": hashlib.sha256(p.video[:len(ids) * size]).hexdigest(), "marks": marks,
           "nulls": p.nulls}
    print("%s: %d chunks %s.. %s nulls=%d" % (args.identity, len(ids), ids[:3],
                                              out["digest"][:16], p.nulls))
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)


def compare(new_f, old_f, tol_median=0.25, tol_max=1.0):
    a, b = json.load(open(new_f)), json.load(open(old_f))
    same = a["digest"] == b["digest"] and a["segments"] == b["segments"]
    check("byte-identical video (%d chunks): %s vs %s" % (len(a["segments"]), new_f, old_f), same,
          "%s vs %s" % (a["segments"], b["segments"]) if not same else "")
    deltas = sorted(abs(x - y) for x, y in zip(a["marks"], b["marks"]))
    if deltas:
        med = deltas[len(deltas) // 2]
        check("delivery timing matches (median %.2fs, worst %.2fs)" % (med, deltas[-1]),
              med <= tol_median and deltas[-1] <= tol_max)


# ─── Main ─────────────────────────────────────────────────────────────────────

def boot_app():
    data = tempfile.mkdtemp(prefix="cushion-")
    os.environ.setdefault("STARTUP_DELAY", "0")
    os.environ["TEAMS_FILE"] = os.path.join(data, "teams.json")
    os.environ["LINEUP_FILE"] = os.path.join(data, "lineup.json")
    os.environ["EXTRACT_CACHE_FILE"] = os.path.join(data, "extract_cache.json")
    os.environ["SETTINGS_FILE"] = os.path.join(data, "settings.json")
    os.environ["STREAM_HISTORY_FILE"] = os.path.join(data, "stream_history.jsonl")
    import app
    app.streamhealth.HISTORY.configure(os.environ["STREAM_HISTORY_FILE"])
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="")
    ap.add_argument("--identity", default="")
    ap.add_argument("--window", type=float, default=20)
    ap.add_argument("--cushion", default="30")
    ap.add_argument("--padding", default="1")
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", nargs=2)
    ap.add_argument("--report", nargs="+")
    args = ap.parse_args()
    if args.report:
        return report(args.report)
    if args.compare:
        compare(*args.compare)
        print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
        sys.exit(1 if fails else 0)
    if args.scenario:
        return run_scenario(args)
    if args.identity:
        return run_identity(args)
    app = boot_app()
    unit_tests(app)
    tracker_tests(app)
    route_tests(app, app.app.test_client())
    print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
