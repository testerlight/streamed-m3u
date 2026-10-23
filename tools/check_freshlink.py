"""Fresh-link retry for a 403 (stream-recovery ticket 03) verification. Not shipped.

Runs inside a scratch-tag image (`docker build -t streamed-m3u:verify-403 .`),
never against a live source or the live image. Scripted origins only.

    T=$PWD/tools
    # 1. units, tracker, the real route (about 3 minutes)
    docker run --rm -e STARTUP_DELAY=0 -v $T:/t:ro --entrypoint python \
        streamed-m3u:verify-403 /t/check_freshlink.py
    # 2. the same deterministic script through old and new: switch off must be
    #    byte-identical with matching timing (runs on the old image too)
    docker run --rm -e STARTUP_DELAY=0 -v $T:/t:ro -v /tmp/fl:/out --entrypoint python \
        <image> /t/check_freshlink.py --identity messy --switch 0 --out /out/new.json
    python3 tools/check_freshlink.py --compare new.json old.json

Seams (chosen without a user to agree them, one line each):

  units   app._fresh_link_url / app._link_changes: which listed URL is "the
          same chunk" and what differs. Pure; catches wrong matching by
          sequence; misses the wiring.
  tracker streamhealth.StreamHealth.fresh_link through its public hook and
          snapshot: the counters and the record. Misses app.py.
  route   the real /stream route against a scripted HLS origin whose chunks
          have real timestamps and whose playlist can re-sign, fail or hang
          on the re-read: bytes, order, timing, padding, counters, log, and
          the interactions with dedup, empty chunks, restamping, the idle
          timeout and failing hooks. Catches wiring; misses real providers.
  identity the route again with the switch off, old image against new.
"""

import argparse
import hashlib
import json
import logging
import os
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_cushion as cc          # noqa: E402  (chunks, pull, boot_app: shared)
from check_cushion import N0, D, chunk_bytes, split, pull, record_for, boot_app  # noqa: E402

fails = cc.fails


def check(name, cond, detail=""):
    cc.check(name, cond, detail)


def section(title):
    cc.section(title)


# ─── Units ────────────────────────────────────────────────────────────────────

def unit_tests(app):
    section("units: which listed URL is the same chunk (app._fresh_link_url)")
    f = app._fresh_link_url
    A = "https://cdn.test/b/%s~x.image?x-expires=1&x-signature=AAA"
    B = "https://cdn.test/b/%s~x.image?x-expires=1&x-signature=BBB"
    segs = [A % "c1", A % "c2", A % "c3"]
    check("same sequence, same URL: 'same'", f(segs, [10, 11, 12], 11, A % "c2") == ("same", A % "c2"))
    check("same sequence, new signature: 'differs', and it is the listed URL",
          f([A % "c1", B % "c2", A % "c3"], [10, 11, 12], 11, A % "c2") == ("differs", B % "c2"))
    check("same sequence, a different object: 'differs'",
          f([A % "c1", A % "zz", A % "c3"], [10, 11, 12], 11, A % "c2") == ("differs", A % "zz"))
    check("the chunk has left the playlist: 'not_listed'",
          f([A % "c3"], [12], 11, A % "c2") == ("not_listed", None))
    check("another chunk with a new signature is not this chunk",
          f([B % "c1", A % "c2", B % "c3"], [10, 11, 12], 11, A % "c2") == ("same", A % "c2"))
    check("unknown sequence: matched by object (path), and a new signature differs",
          f([A % "c1", B % "c2"], [None, None], None, A % "c2") == ("differs", B % "c2"))
    check("unknown sequence and the object is gone: 'not_listed'",
          f([A % "c1"], [None], None, A % "c2") == ("not_listed", None))
    check("sequence list of the wrong length falls back to the object, never guesses",
          f([B % "c2", A % "c3"], [11], 11, A % "c2") == ("differs", B % "c2"))
    check("an empty playlist is 'not_listed'", f([], [], 11, A % "c2") == ("not_listed", None))
    check("never raises on junk", f(None, None, None, None) == ("not_listed", None))

    section("units: what differs (app._link_changes)")
    c = app._link_changes
    check("identical: nothing", c(A % "c2", A % "c2") == [])
    check("signature only", c(A % "c2", B % "c2") == ["x-signature"])
    check("object differs", "path" in c(A % "c2", A % "zz"))
    check("a new parameter is named", c(A % "c2", (A % "c2") + "&t=9") == ["t"])
    check("values are never returned, names only", all("AAA" not in x and "BBB" not in x
          for x in c(A % "c2", B % "c2")))


# ─── Tracker ──────────────────────────────────────────────────────────────────

def tracker_tests(app):
    import streamhealth as sh

    section("tracker: fresh-link attempts are counted and recorded")
    tmp = tempfile.mkdtemp(prefix="fl-h-")
    sh.HISTORY.configure(os.path.join(tmp, "h.jsonl"))

    class Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, rec):
            self.lines.append(rec.getMessage())

    cap = Cap()
    logging.getLogger("streamhealth").addHandler(cap)
    logging.getLogger("streamhealth").setLevel(logging.INFO)

    T = [7000.0]
    h = sh.StreamHealth("fl1", channel="c", source="s", clock=lambda: T[0])
    s = h.snapshot().get("fresh_link")
    check("nothing attempted: all zero on the live view",
          s is not None and s.get("attempted") == 0 and s.get("url_differed") == 0
          and s.get("succeeded") == 0, s)

    h.fresh_link("reread", "same", seq=5, secs=0.3)
    s = h.snapshot()["fresh_link"]
    check("same URL on the re-read: attempted 1, differed 0, succeeded 0",
          (s["attempted"], s["url_differed"], s["succeeded"], s["same_url"]) == (1, 0, 0, 1), s)

    h.fresh_link("reread", "differs", seq=6, secs=0.4, changed=["x-signature"])
    h.fresh_link("retry", "ok")
    s = h.snapshot()["fresh_link"]
    check("a different URL that then works: differed 1, succeeded 1",
          (s["attempted"], s["url_differed"], s["succeeded"]) == (2, 1, 1), s)

    h.fresh_link("reread", "differs", seq=7, secs=0.4, changed=["path"])
    h.fresh_link("retry", "failed")
    s = h.snapshot()["fresh_link"]
    check("a different URL that also fails: differed 2, succeeded 1, retry_failed 1",
          (s["attempted"], s["url_differed"], s["succeeded"], s["retry_failed"]) == (3, 2, 1, 1), s)

    h.fresh_link("reread", "not_listed", seq=8, secs=0.2)
    h.fresh_link("reread", "failed", seq=9, secs=15.0)
    s = h.snapshot()["fresh_link"]
    check("chunk gone from the playlist and a re-read that failed are counted apart",
          (s["not_listed"], s["reread_failed"], s["attempted"]) == (1, 1, 5), s)
    h.fresh_link("reread", "skipped")
    s = h.snapshot()["fresh_link"]
    check("a skipped attempt (time budget used) is not an attempt",
          (s["skipped"], s["attempted"]) == (1, 5), s)

    lines = [l for l in cap.lines if "another link" in l]
    check("every attempt that found a different URL is logged, with what differed",
          any("x-signature" in l for l in lines) and any("path" in l for l in lines), lines)
    check("the log never carries a signature value", not any("AAA" in l or "BBB" in l for l in lines))
    ev = [e for e in h.snapshot()["timeline"] if e["kind"] == "freshlink"]
    check("the timeline carries them", len(ev) >= 3, len(ev))

    h.finish()
    rec = record_for_tracker(sh, h)
    check("the history line carries the counters and the list",
          rec.get("fresh_link", {}).get("attempted") == 5
          and len(rec.get("fresh_link_list", [])) >= 3, (rec.get("fresh_link"), rec.get("fresh_link_list")))
    lst = rec.get("fresh_link_list", [])
    check("a list entry says what happened and how the second try went",
          any(e.get("seq") == 6 and e.get("how") == "differs" and e.get("result") == "ok" for e in lst), lst)

    section("tracker: many 'same' results do not flood the timeline or the log")
    T2 = [8000.0]
    h2 = sh.StreamHealth("fl2", channel="c2", source="s", clock=lambda: T2[0])
    before = len(cap.lines)
    for i in range(300):
        T2[0] += 0.5
        h2.fresh_link("reread", "same", seq=i, secs=0.3)
    s = h2.snapshot()
    ev = [e for e in s["timeline"] if e["kind"] == "freshlink"]
    check("300 same-URL results: counted in full", s["fresh_link"]["same_url"] == 300
          and s["fresh_link"]["attempted"] == 300, s["fresh_link"])
    check("... but at most a handful of timeline events and log lines (first, then once a minute)",
          len(ev) <= 5 and len(cap.lines) - before <= 5, (len(ev), len(cap.lines) - before))
    check("... and a bounded list", len(h2._record(T2[0]).get("fresh_link_list", [])) <= sh.FREEZE_LIST_MAX)

    section("tracker: the hook cannot raise")
    try:
        h.fresh_link("nonsense", object(), seq="x", secs="y", changed=3)
        h.fresh_link(None, None)
        ok = True
    except Exception:
        ok = False
    check("junk arguments are swallowed", ok)
    check("the no-op stand-in has the hook", sh.NULL.fresh_link("reread", "same") is None)


def record_for_tracker(sh, h):
    sh.HISTORY.flush()
    for rec in sh.HISTORY.read(50):
        if rec.get("id") == h.id:
            return rec
    return {}


# ─── A scripted origin whose links can be re-signed ──────────────────────────

class FLOrigin:
    """Poll-driven HLS origin. The media playlist poll k lists windows[k] (the
    last repeating); every chunk URL carries a signature, "a" until the chunk
    is re-signed ("b") from poll resign[n] on. The signature is not one
    `_segment_key` strips (like the TikTok x-signature), so a re-signed link
    looks like a new chunk to the dedup, exactly as on the real source.

    dead   {(chunk, sig): how or [how per attempt, last repeating]} - how is
           403, 404, 500, "empty" (72-byte placeholder), "partial", "slow",
           "hang" (40s then 403). A link not listed here is fine.
    pl     {poll k: how} - 500, ("hang", seconds) (sleep, then answer), or
           ("hang500", seconds).
    """

    def __init__(self, windows, resign=None, dead=None, pl=None, slow_s=1.0):
        self.windows, self.resign = windows, resign or {}
        self.dead, self.pl, self.slow_s = dead or {}, pl or {}, slow_s
        self.polls = 0
        self.attempts = {}
        self.log = []
        self.lock = threading.Lock()
        self.t0 = time.time()

    def reset(self):
        self.polls, self.attempts, self.log = 0, {}, []
        self.t0 = time.time()

    def sig(self, n, k):
        return "b" if n in self.resign and k >= self.resign[n] else "a"

    def playlist(self):
        with self.lock:
            k = self.polls
            self.polls += 1
            self.log.append((round(time.time() - self.t0, 2), "poll", k))
        first, last = self.windows[min(k, len(self.windows) - 1)]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:%d" % D,
                 "#EXT-X-MEDIA-SEQUENCE:%d" % first]
        for n in range(first, last + 1):
            lines += ["#EXTINF:%.3f," % D,
                      "seg%05d.ts?x-signature=%s&x-expires=4102444800" % (n, self.sig(n, k))]
        return k, "\n".join(lines) + "\n"

    def n_attempts(self, n, sig):
        return self.attempts.get((n, sig), 0)


def serve_fl(origin):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/plain"):
            try:            # the client may have timed out and gone
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self):
            path, _, query = self.path.partition("?")
            leaf = path.rsplit("/", 1)[-1]
            if leaf == "master.m3u8":
                return self._send(200, b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\nindex.m3u8\n",
                                  "application/vnd.apple.mpegurl")
            if leaf == "index.m3u8":
                k, text = origin.playlist()
                how = origin.pl.get(k)
                if isinstance(how, tuple):
                    time.sleep(how[1])
                    how = 500 if how[0] == "hang500" else None
                if how == 500:
                    return self._send(500, b"oops")
                return self._send(200, text.encode(), "application/vnd.apple.mpegurl")
            if leaf.startswith("seg"):
                n = int(leaf[3:8])
                sig = dict(q.split("=") for q in query.split("&")).get("x-signature", "?")
                with origin.lock:
                    k = origin.attempts[(n, sig)] = origin.attempts.get((n, sig), 0) + 1
                    origin.log.append((round(time.time() - origin.t0, 2), "seg", (n, sig, k)))
                plan = origin.dead.get((n, sig))
                if plan is None:
                    how = "ok"
                else:
                    seq = plan if isinstance(plan, list) else [plan]
                    how = seq[min(k, len(seq)) - 1]
                if how in (403, 404, 500):
                    return self._send(how, b"no")
                if how == "hang":
                    time.sleep(40)
                    return self._send(403, b"no")
                if how == "empty":
                    return self._send(200, b"RIFF" + struct.pack("<I", 64) + b"WEBPVP8X" + b"\0" * 56,
                                      "image/webp")
                body = chunk_bytes(n)
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
                        import socket
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


def longest_gap(p):
    ts = [t for t, _it in p.items]
    return max((b - a) for a, b in zip(ts, ts[1:])) if len(ts) > 1 else 0


# Poll 0 is the probe (its window is the start); the first poll after a failed
# chunk is the re-read; later polls bring new chunks.
WIN = [(N0, N0 + 3), (N0, N0 + 3), (N0 + 1, N0 + 4), (N0 + 2, N0 + 5), (N0 + 3, N0 + 6),
       (N0 + 4, N0 + 7), (N0 + 5, N0 + 8), (N0 + 6, N0 + 9), (N0 + 7, N0 + 10)]
BIG = [(N0, N0 + 11), (N0, N0 + 11), (N0 + 1, N0 + 12), (N0 + 2, N0 + 13), (N0 + 3, N0 + 14),
       (N0 + 4, N0 + 15), (N0 + 5, N0 + 16)]
A, B = "a", "b"
BASE = dict(FRESH_LINK_RETRY=True, BEHIND_LIVE_S=30.0, PLAYLIST_KEEPALIVE=True,
            HOLE_RESTAMP=True, HOLE_RESTAMP_MIN_S=8.0, STREAM_IDLE_TIMEOUT=45,
            SEGMENT_TIMEOUT=30, SEGMENT_RETRIES=1)


def route_tests(app, client):
    win, big = WIN, BIG

    def run(origin, name, **kw):
        return pull(app, client, origin, serve_fl(origin), name, **kw)

    def ids(p):
        return [n for n, _b in split(p.video)]

    def whole(p):
        """What arrived is exactly the source's bytes for the chunks it names."""
        return p.video == b"".join(chunk_bytes(n) for n in ids(p))

    def setting(**kw):
        for k, v in kw.items():
            setattr(app, k, v)

    def fresh(rec):
        return rec.get("fresh_link") or {}

    app.STREAM_KEEPALIVE_INTERVAL = 0.5
    base = BASE
    setting(**base)

    section("route: 403, and the re-read lists a new link that works")
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403})
    p = run(o, "fresh-ok", want_chunks=8)
    check("every chunk arrives, in order, nothing twice (the new link is not fetched again as 'new')",
          ids(p) == list(range(N0, N0 + 8)), ids(p))
    check("... and they are the source's bytes", whole(p))
    check("the dead link was tried twice (as today), the new one once",
          (o.n_attempts(N0 + 1, A), o.n_attempts(N0 + 1, B)) == (2, 1),
          (o.n_attempts(N0 + 1, A), o.n_attempts(N0 + 1, B)))
    rec = record_for(app, p.embed)
    fl = fresh(rec)
    check("recorded: attempted 1, url_differed 1, succeeded 1",
          (fl.get("attempted"), fl.get("url_differed"), fl.get("succeeded")) == (1, 1, 1), fl)
    check("... and no hole: nothing dropped, skipped, restamped or sent backwards",
          not rec.get("downloads_failed") and not rec.get("skipped") and not rec.get("restamps")
          and not rec.get("backwards"), (rec.get("downloads_failed"), rec.get("skipped"),
                                          rec.get("restamps"), rec.get("backwards")))
    lst = rec.get("fresh_link_list") or [{}]
    check("the list says which parameter changed, not its value",
          lst[0].get("changed") == ["x-signature"] and lst[0].get("result") == "ok"
          and lst[0].get("seq") is not None, lst)
    check("the extra work was quick (a re-read and one download, well under a second)",
          lst[0].get("reread_s") is not None and lst[0]["reread_s"] < 1.0, lst[0].get("reread_s"))
    check("no freeze from it", not rec.get("freezes"), rec.get("freezes"))

    section("route: 403, and the re-read lists the same link: no retry, a hole as today")
    o = FLOrigin(win, dead={(N0 + 1, A): 403})
    p = run(o, "fresh-same", want_chunks=7)
    check("the chunk is dropped; the rest arrive in order",
          ids(p) == [N0] + list(range(N0 + 2, N0 + 8)), ids(p))
    check("exactly today's two attempts on the dead link; no third try", o.n_attempts(N0 + 1, A) == 2,
          o.n_attempts(N0 + 1, A))
    rec = record_for(app, p.embed)
    fl = fresh(rec)
    check("recorded: attempted 1, same_url 1, url_differed 0, succeeded 0",
          (fl.get("attempted"), fl.get("same_url"), fl.get("url_differed"), fl.get("succeeded")) == (1, 1, 0, 0), fl)
    check("a hole as before: one dropped chunk, no restamp (5s)", rec.get("downloads_failed") == 1
          and not rec.get("restamps"), (rec.get("downloads_failed"), rec.get("restamps")))
    setting(**dict(base, FRESH_LINK_RETRY=False))
    q = run(FLOrigin(win, dead={(N0 + 1, A): 403}), "fresh-same-off", want_chunks=7)
    setting(**base)
    check("the same bytes as with the switch off", q.video == p.video and len(p.video) > 0)

    section("route: the re-read itself fails")
    o = FLOrigin(win, dead={(N0 + 1, A): 403}, pl={1: 500})
    p = run(o, "reread-500", want_chunks=7)
    check("playlist answers 500 to the re-read: the chunk is a hole, the stream goes on in order",
          ids(p) == [N0] + list(range(N0 + 2, N0 + 8)), ids(p))
    check("the dead link was tried twice, never a third time",
          o.n_attempts(N0 + 1, A) == 2 and o.n_attempts(N0 + 1, B) == 0)
    rec = record_for(app, p.embed)
    fl = fresh(rec)
    check("recorded: attempted 1, reread_failed 1", (fl.get("attempted"), fl.get("reread_failed")) == (1, 1), fl)
    check("the stream did not end on it", rec.get("end_reason") not in ("playlist_failures", "fatal"),
          rec.get("end_reason"))

    section("route: the re-read hangs; extra work is bounded and padded")
    setting(**dict(base, SEGMENT_TIMEOUT=8))
    o = FLOrigin(win, dead={(N0 + 1, A): 403}, pl={1: ("hang", 30)})
    t0 = time.time()
    p = run(o, "reread-hang", want_chunks=6, max_s=40)
    took = time.time() - t0
    check("the chunk is dropped and the stream carries on", N0 + 1 not in ids(p) and len(ids(p)) >= 6, ids(p))
    check("the hang was cut at half the segment budget (4s of 8s), not left to the 15s request timeout",
          took < 12, round(took, 1))
    check("null packets kept flowing through it (longest silence on the wire under 1.5s)",
          p.nulls >= 4 and longest_gap(p) < 1.5, (p.nulls, round(longest_gap(p), 2)))
    rec = record_for(app, p.embed)
    check("recorded as a failed re-read", fresh(rec).get("reread_failed") == 1, rec.get("fresh_link"))
    check("the idle limit was not what ended it", rec.get("end_reason") != "idle_timeout", rec.get("end_reason"))
    setting(**base)

    section("route: the re-read is slow but works")
    setting(**dict(base, SEGMENT_TIMEOUT=8))
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403}, pl={1: ("hang", 2.0)})
    p = run(o, "reread-slow", want_chunks=7, max_s=40)
    check("a 2s re-read inside the budget still recovers the chunk", ids(p) == list(range(N0, N0 + 7)), ids(p))
    check("padded while it ran", p.nulls >= 2 and longest_gap(p) < 1.5, (p.nulls, round(longest_gap(p), 2)))
    rec = record_for(app, p.embed)
    check("recorded as recovered, with the re-read's time",
          fresh(rec).get("succeeded") == 1
          and 1.8 <= ((rec.get("fresh_link_list") or [{}])[0].get("reread_s") or 0) <= 3.0,
          (rec.get("fresh_link"), rec.get("fresh_link_list")))
    setting(**base)

    section("route: the new link is also dead")
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403, (N0 + 1, B): 404})
    p = run(o, "fresh-404", want_chunks=7)
    check("a 404 on the new link: a hole, in order", ids(p) == [N0] + list(range(N0 + 2, N0 + 8)), ids(p))
    check("the new link was tried once, and not again when later polls list it",
          o.n_attempts(N0 + 1, B) == 1, o.n_attempts(N0 + 1, B))
    rec = record_for(app, p.embed)
    fl = fresh(rec)
    check("recorded: differed 1, succeeded 0, retry_failed 1",
          (fl.get("url_differed"), fl.get("succeeded"), fl.get("retry_failed")) == (1, 0, 1), fl)
    check("counted as a dropped chunk like any other", rec.get("downloads_failed") == 1, rec.get("downloads_failed"))

    section("route: the new link answers with an empty placeholder")
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403, (N0 + 1, B): "empty"})
    p = run(o, "fresh-empty", want_chunks=7)
    rec = record_for(app, p.embed)
    check("handled as an empty chunk (hole, counted as empty), not retried further",
          N0 + 1 not in ids(p) and rec.get("empty_chunks") == 1 and o.n_attempts(N0 + 1, B) == 1,
          (ids(p), rec.get("empty_chunks"), o.n_attempts(N0 + 1, B)))

    section("route: several 403s in a row")
    o = FLOrigin(win, resign={N0 + 1: 1, N0 + 2: 2, N0 + 3: 3},
                 dead={(N0 + 1, A): 403, (N0 + 2, A): 403, (N0 + 3, A): 403})
    p = run(o, "row-all", want_chunks=8)
    rec = record_for(app, p.embed)
    check("three dead links in a row, each re-signed: all recovered, in order, no restamp",
          ids(p) == list(range(N0, N0 + 8)) and not rec.get("restamps") and whole(p), ids(p))
    check("recorded: 3 attempted, 3 differed, 3 succeeded",
          tuple(fresh(rec).get(k) for k in ("attempted", "url_differed", "succeeded")) == (3, 3, 3),
          rec.get("fresh_link"))
    o = FLOrigin(win, resign={N0 + 2: 2}, dead={(N0 + 1, A): 403, (N0 + 2, A): 403, (N0 + 3, A): 403})
    p = run(o, "row-mixed", want_chunks=7)
    rec = record_for(app, p.embed)
    check("only the middle one re-signed: it comes back, the outer two are holes",
          ids(p) == [N0, N0 + 2] + list(range(N0 + 4, N0 + 9)), ids(p))
    check("... recorded: 3 attempted, 1 differed, 1 succeeded, 2 same",
          tuple(fresh(rec).get(k) for k in ("attempted", "url_differed", "succeeded", "same_url"))
          == (3, 1, 1, 2), rec.get("fresh_link"))
    check("... the two holes are not adjacent, so no restamp (5s each)", not rec.get("restamps"), rec.get("restamps"))
    o = FLOrigin(win, dead={(N0 + 1, A): 403, (N0 + 2, A): 403})
    p = run(o, "row-restamp", want_chunks=7)
    rec = record_for(app, p.embed)
    check("two holes in a row, nothing recovered: the 10s restamp happens as before",
          rec.get("restamps") == 1 and abs((rec.get("restamp_s") or 0) - 10) < 0.1,
          (rec.get("restamps"), rec.get("restamp_s")))
    o = FLOrigin(win, resign={N0 + 1: 1, N0 + 2: 2}, dead={(N0 + 1, A): 403, (N0 + 2, A): 403})
    p = run(o, "row-restamp-fixed", want_chunks=7)
    rec = record_for(app, p.embed)
    check("the same two, recovered on fresh links: no hole, so no restamp, and untouched bytes",
          not rec.get("restamps") and ids(p) == list(range(N0, N0 + 7)) and whole(p),
          (rec.get("restamps"), ids(p)))
    setting(**dict(base, SEGMENT_TIMEOUT=8))
    o = FLOrigin(win, dead={(N0 + 1, A): 403, (N0 + 2, A): 403, (N0 + 3, A): 403},
                 pl={1: ("hang", 30), 2: ("hang", 30)})
    t0 = time.time()
    p = run(o, "row-hang", want_chunks=6, max_s=60)
    took = time.time() - t0
    rec = record_for(app, p.embed)
    fl = fresh(rec)
    check("three hanging re-reads in a row cost about one segment budget (8s) in total, not 3 x 15s",
          took < 16, round(took, 1))
    check("... later attempts are skipped once the budget is used (recorded as skipped)",
          fl.get("skipped", 0) >= 1 and fl.get("attempted", 0) <= 3, fl)
    check("... padding the whole time", longest_gap(p) < 1.5, round(longest_gap(p), 2))
    setting(**base)

    section("route: a 403 on the very first chunk of the start")
    o = FLOrigin(big, resign={N0 + 6: 1}, dead={(N0 + 6, A): 403})
    p = run(o, "first-ok", want_chunks=8)
    check("fresh link works: the start is the chunk 30s back, as without the 403",
          ids(p) == list(range(N0 + 6, N0 + 14)) and whole(p), ids(p))
    o = FLOrigin(big, resign={N0 + 6: 1}, dead={(N0 + 6, A): 403, (N0 + 6, B): 403})
    p = run(o, "first-dead", want_chunks=7)
    rec = record_for(app, p.embed)
    check("fresh link dead too: the first chunk is a hole, the stream starts at the next, no restamp "
          "(nothing had been sent)", ids(p)[0] == N0 + 7 and not rec.get("restamps"), (ids(p), rec.get("restamps")))
    o = FLOrigin(big, dead={(N0 + 6, A): 403})
    p = run(o, "first-same", want_chunks=7)
    check("same link: starts at the next, as today", ids(p)[0] == N0 + 7, ids(p))

    section("route: the switch off is today's two attempts")
    setting(**dict(base, FRESH_LINK_RETRY=False))
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403})
    p = run(o, "off", want_chunks=7)
    rec = record_for(app, p.embed)
    check("no re-read, no third attempt: 2 attempts on the dead link", o.n_attempts(N0 + 1, A) == 2,
          o.n_attempts(N0 + 1, A))
    check("nothing recorded about it", not fresh(rec).get("attempted"), rec.get("fresh_link"))
    setting(**base)

    section("route: SEGMENT_RETRIES=0 and a 403 on the only attempt")
    setting(SEGMENT_RETRIES=0)
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403})
    p = run(o, "retries0", want_chunks=7)
    check("the one try failed with 403: the fresh link is the second", ids(p) == list(range(N0, N0 + 7)), ids(p))
    setting(SEGMENT_RETRIES=1)

    section("route: only a 403 earns it")
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 404})
    p = run(o, "only-403-404", want_chunks=7)
    check("a 404 is not a stale link: no re-read", not fresh(record_for(app, p.embed)).get("attempted")
          and ids(p)[:2] == [N0, N0 + 2])
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): [403, "partial"]})
    p = run(o, "only-403-last", want_chunks=7)
    check("a 403 then a half-sent body: the last failure was not a 403, no re-read",
          not fresh(record_for(app, p.embed)).get("attempted") and ids(p)[:2] == [N0, N0 + 2], ids(p))

    section("route: hooks and helpers that raise fall back to today")
    real_url = app._fresh_link_url
    app._fresh_link_url = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rigged"))
    try:
        o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403})
        p = run(o, "rig-url", want_chunks=7)
    finally:
        app._fresh_link_url = real_url
    check("the matcher raising: a hole and two attempts, the stream carries on",
          ids(p)[:3] == [N0, N0 + 2, N0 + 3] and o.n_attempts(N0 + 1, A) == 2, ids(p))
    real_hook = app.streamhealth.StreamHealth.fresh_link
    app.streamhealth.StreamHealth.fresh_link = app.streamhealth._guarded(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rigged")))
    try:
        o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403})
        p = run(o, "rig-hook", want_chunks=8)
    finally:
        app.streamhealth.StreamHealth.fresh_link = real_hook
    check("the tracker hook raising: the chunk is still recovered, same video",
          ids(p) == list(range(N0, N0 + 8)) and whole(p), ids(p))
    real_gs = app.get_segments

    def gs_boom(*a, **k):
        if k.get("deadline") is not None:
            raise RuntimeError("rigged")
        return real_gs(*a, **k)

    app.get_segments = gs_boom
    try:
        o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403})
        p = run(o, "rig-get", want_chunks=7)
    finally:
        app.get_segments = real_gs
    check("the re-read raising: a hole as today, stream carries on",
          ids(p)[:3] == [N0, N0 + 2, N0 + 3], ids(p))

    section("route: no padding configured (STREAM_KEEPALIVE_INTERVAL=0)")
    app.STREAM_KEEPALIVE_INTERVAL = 0
    o = FLOrigin(win, resign={N0 + 1: 1}, dead={(N0 + 1, A): 403})
    p = run(o, "no-pad", want_chunks=7)
    app.STREAM_KEEPALIVE_INTERVAL = 0.5
    check("works inline too", ids(p) == list(range(N0, N0 + 7)) and p.nulls == 0, (ids(p), p.nulls))

    section("route: the idle limit after a recovery")
    setting(STREAM_IDLE_TIMEOUT=6)
    quiet = FLOrigin([(N0, N0 + 3), (N0, N0 + 3), (N0 + 1, N0 + 3)], resign={N0 + 1: 1},
                     dead={(N0 + 1, A): 403}, pl={k: ("hang", 1.5) for k in range(3, 80)})
    p = run(quiet, "idle-after", max_s=40)
    rec = record_for(app, p.embed)
    check("a recovered chunk, then a source that goes quiet: the idle limit still ends it on time",
          p.ended is not None and 6 <= p.ended <= 16 and rec.get("end_reason") == "idle_timeout",
          (p.ended, rec.get("end_reason")))
    check("... and the recovered chunk was delivered", N0 + 1 in ids(p), ids(p))
    setting(**base)


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_identity(args):
    """One deterministic messy script through the route, recorded for compare().
    `messy` has dead links (403 that never recovers, and a re-signed one
    listed later), a half-sent chunk, a slow one and a hung playlist request;
    `clean` has the same without any 403. Run with the switch off on the old
    and the new image (byte-identical, same timing), and `clean` with it on."""
    app = boot_app()
    client = app.app.test_client()
    app.FRESH_LINK_RETRY = bool(int(args.switch))     # an unused name on the old image
    app.BEHIND_LIVE_S = 30.0
    app.PLAYLIST_KEEPALIVE = True
    app.STREAM_KEEPALIVE_INTERVAL = 1.0
    app.HOLE_RESTAMP = True
    windows = [(N0, N0 + 3), (N0, N0 + 3), (N0 + 1, N0 + 4), (N0 + 4, N0 + 7), (N0 + 6, N0 + 9),
               (N0 + 9, N0 + 12), (N0 + 11, N0 + 14), (N0 + 13, N0 + 16)]
    dead = {(N0 + 5, A): "slow", (N0 + 7, A): ["partial"], (N0 + 10, A): ["slow"]}
    if args.identity == "messy":
        dead.update({(N0 + 1, A): 403, (N0 + 4, A): 403, (N0 + 8, A): 403, (N0 + 9, A): 403})
    origin = FLOrigin(windows, resign={N0 + 1: 3} if args.identity == "messy" else {},
                      dead=dead, pl={3: ("hang", 1.5)}, slow_s=1.5)
    want = 11 if args.identity == "messy" else 14
    p = pull(app, client, origin, serve_fl(origin), "identity-" + args.identity,
             want_chunks=want, max_s=90)
    marks, total, size = [], 0, len(chunk_bytes(N0))
    for t, item in p.items:
        if item == app.TS_NULL_PACKET:
            continue
        total += len(item)
        while (len(marks) + 1) * size <= total:
            marks.append(round(t, 2))
    got = [n for n, _b in split(p.video)]
    out = {"image": os.environ.get("IMAGE", "?"), "args": vars(args), "segments": got,
           "digest": hashlib.sha256(p.video[:len(got) * size]).hexdigest(), "marks": marks,
           "nulls": p.nulls}
    print("%s (switch %s): %d chunks %s.. %s nulls=%d" % (args.identity, args.switch, len(got), got[:4],
                                                        out["digest"][:16], p.nulls))
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", default="")
    ap.add_argument("--switch", default="0")
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", nargs=2)
    args = ap.parse_args()
    if args.compare:
        cc.compare(*args.compare)
        print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
        sys.exit(1 if fails else 0)
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
