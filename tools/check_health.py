"""Stream health (Phases 1-3) verification. Not shipped.

Runs inside an image, with the script bind mounted, against the real app.py:

    # the new image: unit tests, then every scenario with tracking on, with
    # every tracking hook rigged to raise, and with the tracker failing to
    # start at all
    docker run --rm -e STARTUP_DELAY=0 -v $PWD/tools/check_health.py:/tmp/h.py:ro \
        -v /tmp/out:/out <new-image> python /tmp/h.py --out /out/new.json

    # the image it replaces: the same scenarios, no tracking anywhere
    docker run --rm -e STARTUP_DELAY=0 -v $PWD/tools/check_health.py:/tmp/h.py:ro \
        -v /tmp/out:/out <old-image> python /tmp/h.py --baseline --out /out/old.json

    # the proof: identical video, matching timing
    python3 tools/check_health.py --compare /tmp/out/new.json /tmp/out/old.json

Both phases are watch-only, so the claim to prove is that **nothing the
proxy sends changed**. Phase 2 adds a watcher thread raising early warnings;
the tracked run checks each misbehaviour raises the warning it should, and
the rigged run makes the watcher's tick() raise too.

Phase 3 does change *when* video arrives, never *what*: backup requests fetch
the same chunk or playlist, so the video must still be byte-identical to the
old image's in every scenario. In the scenarios where a fix should fire
(FIX_SCENARIOS) the new image may deliver earlier, never later; everywhere
else timing must match both ways. With every fix switched off, timing must
match the old image in the fix scenarios too. Each scenario pulls the real /stream route from a fake HLS
origin on 127.0.0.1 that misbehaves the messy way real sources do: slow
chunks, a source that stalls and then jumps ahead, playlist error bursts, a
chunk that stalls halfway, a truncated chunk, a sequence that restarts lower,
a broadcast that ends. The first attempt's fake failures were too clean
(dead stayed dead, new links worked instantly), which is why it passed its
tests and then made real streams worse.

The origin is deterministic by construction - what a playlist lists does not
depend on exactly when it is polled - so two runs must deliver byte-identical
video. Each pull stops after a fixed number of chunks rather than after a
fixed time, for the same reason.
"""

import argparse
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

D = 1.0                 # seconds of video per chunk
SEG_PACKETS = 200
SEG_BYTES = SEG_PACKETS * 188
FIRST_WINDOW = 3        # what the probe sees: a short, realistic window
LONG_WINDOW = 20        # later polls: long enough that nothing slides out by accident

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  [%s]" % (detail,)) if detail != "" else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n=== %s ===" % title, flush=True)


# ─── Fake origin ──────────────────────────────────────────────────────────────

def seg_body(seq, tag):
    out = bytearray()
    for i in range(SEG_PACKETS):
        out += b"\x47\x41\x00\x10" + struct.pack(">IH", seq, i) + tag * 178
    return bytes(out)


def seg_id(block):
    return "%s%d" % (block[10:11].decode("ascii", "replace"),
                     struct.unpack(">I", block[4:8])[0])


class Scenario:
    """One origin timeline. Every misbehaviour is a function of the chunk
    number or of a fixed point in time that no poll lands near, so what the
    proxy ends up delivering is the same on every run."""

    def __init__(self, name, take, slow=None, stall=None, pl_errors=None,
                 seg_fail=None, restart=None, end=None, window=LONG_WINDOW,
                 pl_hang=None, new_only=False):
        self.name = name
        self.take = take            # chunks to pull before hanging up
        self.slow = slow            # (first, last, seconds per chunk)
        self.stall = stall          # (start_s, end_s, jump)
        self.pl_errors = pl_errors  # (start_s, end_s)
        self.seg_fail = seg_fail or {}   # seq -> "503" | "midstall" | "short" | "hang" | "empty"
        self.restart = restart      # time the sequence restarts lower
        self.end = end              # time the broadcast ends (ENDLIST)
        self.window_len = window    # chunks listed once past the probe
        self.pl_hang = pl_hang      # (start_s, end_s, delay): requests in it hang
        self.new_only = new_only    # not run against the old image
        self.reset()

    def reset(self):
        self.t0 = time.time()
        self.lower = None
        self.attempts = {}

    def first_poll(self):
        self.lower = None

    def now(self):
        return time.time() - self.t0

    def window(self):
        """(tag, first seq, newest seq, media sequence, ended)."""
        t = self.now()
        tag, ended = "A", False
        if self.end is not None and t >= self.end:
            t, ended = self.end, True
        newest = 2 + int(t / D)
        floor = 0
        if self.stall:
            s, e, jump = self.stall
            if s <= t < e:
                newest = 2 + int(s / D)
            elif t >= e:
                newest = 2 + int(s / D) + jump + int((t - e) / D)
                floor = 2 + int(s / D) + jump
        if self.restart is not None and t >= self.restart:
            tag = "B"
            floor = 2 + int(self.restart / D) - 5
            newest = floor + int((t - self.restart) / D)
        if self.lower is None:
            self.lower = max(floor, newest - FIRST_WINDOW + 1)
        first = max(self.lower, floor, newest - self.window_len + 1)
        return tag, first, newest, ended

    def playlist(self):
        t = self.now()
        if self.pl_errors and self.pl_errors[0] <= t < self.pl_errors[1]:
            return 500, ""
        tag, first, newest, ended = self.window()
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1",
                 "#EXT-X-MEDIA-SEQUENCE:%d" % first]
        prefix = "r2/" if tag == "B" else ""
        for n in range(first, newest + 1):
            lines += ["#EXTINF:%.3f," % D, "%sseg%d.ts" % (prefix, n)]
        if ended:
            lines.append("#EXT-X-ENDLIST")
        return 200, "\n".join(lines) + "\n"


PLACEHOLDER = b"RIFF" + struct.pack("<I", 64) + b"WEBPVP8X" + b"\x00" * 56

MASTER = ("#EXTM3U\n"
          "#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080\nv1/index.m3u8\n"
          "#EXT-X-STREAM-INF:BANDWIDTH=700000,RESOLUTION=960x540\nv2/index.m3u8\n")

SCENARIOS = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        sc = SCENARIOS.get(parts[0])
        if sc is None:
            return self._send(404, b"", "text/plain")
        leaf = parts[-1]
        if leaf == "master.m3u8":
            return self._send(200, MASTER.encode(), "application/vnd.apple.mpegurl")
        if leaf == "index.m3u8":
            if sc.pl_hang and sc.pl_hang[0] <= sc.now() < sc.pl_hang[1]:
                time.sleep(sc.pl_hang[2])
            code, text = sc.playlist()
            return self._send(code, text.encode(), "application/vnd.apple.mpegurl")
        if leaf.startswith("seg") and leaf.endswith(".ts"):
            seq = int(leaf[3:-3])
            tag = b"B" if "r2" in parts else b"A"
            key = (tag, seq)
            sc.attempts[key] = sc.attempts.get(key, 0) + 1
            return self._segment(sc, seq, seg_body(seq, tag), sc.attempts[key])
        return self._send(404, b"", "text/plain")

    def _segment(self, sc, seq, body, attempt):
        fail = sc.seg_fail.get(seq)
        if fail == "empty":
            # 2026-10-14: a 200 with a 72-byte empty WebP, every attempt.
            return self._send(200, PLACEHOLDER, "image/webp")
        if attempt != 1:
            fail = None
        if fail == "503":
            return self._send(503, b"busy", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(body)))
        if fail == "short":
            self.send_header("Connection", "close")
        self.end_headers()
        half = len(body) // 2
        if fail == "short":
            self.wfile.write(body[:half])
            self.wfile.flush()
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if fail == "hang":
            self.wfile.flush()
            time.sleep(25.0)
            self.wfile.write(body)
            return
        if fail == "midstall":
            self.wfile.write(body[:half])
            self.wfile.flush()
            time.sleep(6.0)
            self.wfile.write(body[half:])
            return
        if sc.slow and sc.slow[0] <= seq <= sc.slow[1]:
            pieces = 10
            step = len(body) // pieces + 1
            for i in range(0, len(body), step):
                self.wfile.write(body[i:i + step])
                self.wfile.flush()
                time.sleep(sc.slow[2] / pieces)
            return
        self.wfile.write(body)


def start_origin():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def scenario_defs():
    return [
        Scenario("steady", take=12),
        # Downloads at half real time for eight chunks: the cushion drains.
        Scenario("slow", take=18, slow=(6, 13, 2.0)),
        # Source stops publishing for 9s, then resumes 8 chunks further on.
        Scenario("stall", take=13, stall=(5.0, 14.0, 8)),
        # Playlist 500s for 3s; one chunk 503s once; one stalls halfway for
        # 6s; one arrives truncated once. All recover.
        Scenario("errors", take=16, pl_errors=(4.5, 7.5),
                 seg_fail={9: "503", 11: "midstall", 13: "short"}),
        # The sequence restarts five chunks lower, under new URLs.
        # A sliding 5-chunk window, so the media sequence really goes back.
        Scenario("restart", take=15, restart=8.5, window=5),
        # The broadcast ends; the stream runs out on the idle limit.
        Scenario("ended", take=10 ** 6, end=8.5),
        # A playlist request hangs for 7s and then answers: the failure
        # behind all three long freezes of 2026-10-13 (there for ~15s).
        Scenario("hang", take=18, pl_hang=(6.0, 7.0, 7.0)),
        # A chunk's first request sends headers, then nothing for 25s: the
        # 2026-10-14 stall that froze lask for 18.6s (its retry took 0.17s).
        Scenario("chunkhang", take=14, seg_fail={8: "hang"}),
        # Chunks 6, 9 and 10 come back as 72-byte placeholders, every time.
        # The old image forwards the junk, so this runs on the new one only.
        Scenario("empty", take=12, seg_fail={6: "empty", 9: "empty", 10: "empty"},
                 new_only=True),
    ]


# Scenarios where a Phase 3 fix should fire, so the new image may be earlier.
FIX_SCENARIOS = {"errors", "hang", "chunkhang"}


# ─── Pulling through the real app ─────────────────────────────────────────────

def pull(app, client, embed, take, max_s=60.0):
    t0 = time.time()
    resp = client.get("/stream?url=" + quote(embed, safe=""), buffered=False)
    data = bytearray()
    marks = []
    keepalives = 0
    try:
        if resp.status_code != 200:
            return {"status": resp.status_code, "error": resp.get_data(as_text=True)[:200]}
        for item in resp.response:
            if item == app.TS_NULL_PACKET:
                keepalives += 1
                continue
            data += item
            while len(data) >= (len(marks) + 1) * SEG_BYTES:
                marks.append(round(time.time() - t0, 2))
            if len(marks) >= take or time.time() - t0 > max_s:
                break
    finally:
        resp.close()
    whole = bytes(data[:len(marks) * SEG_BYTES])
    ids = [seg_id(whole[i:i + SEG_BYTES]) for i in range(0, len(whole), SEG_BYTES)]
    intact = all(whole[i * SEG_BYTES:(i + 1) * SEG_BYTES]
                 == seg_body(int(sid[1:]), sid[0].encode()) for i, sid in enumerate(ids))
    return {
        "intact": intact,
        "status": 200,
        "segments": [seg_id(whole[i:i + SEG_BYTES])
                     for i in range(0, len(whole), SEG_BYTES)],
        "digest": hashlib.sha256(whole).hexdigest(),
        "trailing_bytes": len(data) - len(whole),
        "marks": marks,
        "keepalives": keepalives,
        "seconds": round(time.time() - t0, 1),
    }


def run_scenarios(app, client, port, label, only=None, health=True):
    out = {}
    for sc in scenario_defs():
        if only and sc.name not in only:
            continue
        if sc.new_only and label == "baseline":
            continue
        key = "%s-%s" % (sc.name, label)
        SCENARIOS[key] = sc
        embed = "http://fake-embed.test/%s" % key
        url = "http://127.0.0.1:%d/%s/master.m3u8" % (port, key)
        app.extract_m3u8_via_browser = (
            lambda _e, *a, _u=url, **k: {"url": _u, "headers": {}, "cookies": {},
                                         "browser_m3u8_status": 200})
        sc.reset()
        res = pull(app, client, embed, sc.take)
        res["health"] = history_for(app, embed) if health else None
        out[sc.name] = res
        print("  %-8s %-12s %3d chunks %5.1fs  %s" % (
            sc.name, label, len(res.get("segments", [])), res.get("seconds", 0),
            res.get("digest", res.get("error", ""))[:16]), flush=True)
    return out


def history_for(app, embed):
    sh = getattr(app, "streamhealth", None)
    if sh is None:
        return None
    deadline = time.time() + 5
    while time.time() < deadline:
        sh.HISTORY.flush()
        for rec in sh.HISTORY.read(500):
            if rec.get("channel") == embed:
                return rec
        time.sleep(0.1)
    return None


# ─── Unit tests (new image only) ──────────────────────────────────────────────

def unit_tests(app):
    import streamhealth as sh

    section("playlist parsing")
    master = sh.parse_playlist(MASTER, "http://h/x/master.m3u8", app.resolve_url)
    check("master detected", master["kind"] == "master")
    check("variants in order", [v["bandwidth"] for v in master["variants"]] == [8000000, 700000],
          master["variants"])
    check("variant resolution", master["variants"][0]["resolution"] == "1920x1080")
    media = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:339243\n"
             "#EXT-X-PROGRAM-DATE-TIME:2026-10-13T23:19:56.363Z\n"
             "#EXTINF:3.200,\na.ts\n#EXTINF:3.200,\nb.ts?token=1\n"
             "#EXT-X-DISCONTINUITY\n#EXTINF:2.000,\nc.ts\n#EXT-X-ENDLIST\n")
    p = sh.parse_playlist(media, "http://h/x/index.m3u8", app.resolve_url)
    segs = p["segments"]
    check("media segments", [s["url"] for s in segs] ==
          ["http://h/x/a.ts", "http://h/x/b.ts?token=1", "http://h/x/c.ts"])
    check("durations", [s["duration"] for s in segs] == [3.2, 3.2, 2.0])
    check("sequence and target", p["media_sequence"] == 339243 and p["target_duration"] == 4.0)
    check("program date time advances", abs(segs[1]["pdt"] - segs[0]["pdt"] - 3.2) < 1e-3 if segs[0]["pdt"] else False)
    check("discontinuity on the right chunk", [s["discontinuity"] for s in segs] == [False, False, True])
    check("endlist", p["endlist"] is True)
    q = sh.parse_playlist("#EXTM3U\n#EXTINF:6,\nx.ts\n", "http://h/i.m3u8", app.resolve_url)
    check("no MEDIA-SEQUENCE means unknown, not 0", q["media_sequence"] is None)
    for junk in ("", "garbage\n\x00\x01", "#EXTINF:abc,\n#EXT-X-MEDIA-SEQUENCE:x\ny.ts"):
        try:
            sh.parse_playlist(junk, "http://h/i.m3u8", app.resolve_url)
            ok = True
        except Exception as e:
            ok = repr(e)
        check("junk playlist does not raise (%r)" % junk[:12], ok is True, ok)
    info = {}
    sh.note_playlist("http://h/x/index.m3u8", media, app.resolve_url, info)
    check("note_playlist fills latest with sequences",
          [s for _k, s, _d in info.get("latest", [])] == [339243, 339244, 339245], info.get("latest"))
    sh.note_playlist("http://h/x/index.m3u8", None, app.resolve_url, {})
    check("note_playlist swallows a bad call", True)

    section("model: cushion, freezes, causes")
    T = [1000.0]
    clock = lambda: T[0]   # noqa: E731
    sh.note_playlist("http://u/p.m3u8",
                     "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:10\n" +
                     "".join("#EXTINF:4.0,\ns%d.ts\n" % n for n in range(10, 30)),
                     app.resolve_url, {})
    u = lambda n: "http://u/s%d.ts" % n   # noqa: E731
    h = sh.StreamHealth("t1", "team:x", "x", "admin1", clock=clock)
    check("starting before the first chunk", h.snapshot()["state"] == "starting")
    h.sending(); h.delivered(u(10), 100)                 # cushion 4, empty at 1004
    T[0] = 1003.0
    h.download_begin(u(11)); h.download_bytes(50)
    T[0] = 1003.5; h.download_bytes(50)
    T[0] = 1006.0
    snap = h.snapshot()
    check("frozen while the cushion is empty", snap["state"] == "frozen", snap["state"])
    check("frozen reason names the download", "Downloading" in snap["reason"], snap["reason"])
    h.download_end(u(11), b"x" * 100)
    h.sending(); h.delivered(u(11), 100)                  # froze 1004..1006 -> slow
    s = h.snapshot()
    check("one freeze, 2s, blamed on slow downloads",
          s["freezes"] == 1 and abs(s["frozen_s"] - 2.0) < 0.05 and "slow" in s["by_cause"],
          (s["freezes"], s["frozen_s"], s["by_cause"]))
    def quick(n):
        h.download_begin(u(n)); h.download_end(u(n), b"x" * 100)
        h.sending(); h.delivered(u(n), 100)

    # Two quick chunks put 12s of cushion in hand, so the next freeze is far
    # enough from the last (more than 1.5 chunks) to count separately.
    quick(12); quick(13)                                  # empty at 1018
    # Source quiet: playlist lists nothing new for 16s.
    h.playlist_begin(); h.playlist_ok({"latest": []}, 0)  # waiting from 1006
    T[0] = 1022.0
    h.playlist_begin(); h.playlist_ok({}, 1)
    h.download_begin(u(14)); T[0] = 1022.2; h.download_bytes(100)
    h.download_end(u(14), b"x" * 100); h.sending(); h.delivered(u(14), 100)
    s = h.snapshot()
    check("source freeze blamed on the source",
          s["freezes"] == 2 and s["by_cause"].get("source", 0) > 3.5, s["by_cause"])
    quick(15); quick(16)                                  # empty at 1034.2
    # A download that needs a second attempt is stalled, not slow.
    T[0] = 1035.0
    h.download_begin(u(17)); h.download_attempt(1)
    T[0] = 1040.0; h.download_attempt(2)
    T[0] = 1042.0; h.download_bytes(100); h.download_end(u(17), b"x" * 100)
    h.sending(); h.delivered(u(17), 100)
    s = h.snapshot()
    check("retried download blamed as stalled", s["by_cause"].get("stalled", 0) > 4, s["by_cause"])
    check("three separate freezes", s["freezes"] == 3, s["freezes"])
    quick(18); quick(19)                                  # empty at 1054
    # Blocked on the player is not a freeze.
    before = s["freezes"]
    T[0] = 1043.0
    h.download_begin(u(20)); h.download_end(u(20), b"x" * 100); h.sending()
    T[0] = 1070.0
    h.delivered(u(20), 100)
    check("player not reading is not a freeze",
          h.snapshot()["freezes"] == before and h.player_waits == 1,
          (h.snapshot()["freezes"], h.player_waits))
    check("finished freezes are closed", h.freeze_count == 3, h.freeze_count)
    # Sequence gap with one known failure: one dropped, the rest expired.
    h.download_begin(u(21)); h.download_end(u(21), None)
    h.download_begin(u(24)); h.download_end(u(24), b"x" * 100); h.delivered(u(24), 100)
    check("skipped counts only chunks that were never fetched",
          h.skipped == 2 and h.downloads_failed == 1, (h.skipped, h.downloads_failed))
    h.delivered(u(22), 100)
    check("an older chunk after a newer one is a jump back", h.backwards == 1, h.backwards)
    h.playlist_ok({"media_sequence": 20}, 0)
    h.playlist_ok({"media_sequence": 12}, 0)
    check("playlist going backwards is counted", h.playlist_backwards == 1)
    h.playlist_ok({"endlist": True}, 0)
    check("ENDLIST noted", h.endlist is True)
    h.playlist_failed(503); h.playlist_failed(503)
    snap = h.snapshot()
    check("playlist errors counted", h.playlist_errors == 2)
    check("timeline has events", len(snap["timeline"]) >= 5, len(snap["timeline"]))
    h.loop_error(ValueError("boom"))
    check("internal error counted", h.internal_errors == 1)
    # Ends while frozen: the open freeze is recorded.
    T[0] = 1100.0
    h.end("idle_timeout", "45s")
    sh.HISTORY.configure(os.path.join(tempfile.mkdtemp(), "h.jsonl"), keep=3)
    h.finish()
    sh.HISTORY.flush()
    rec = sh.HISTORY.read(10)[0]
    check("history record written", rec["id"] == "t1" and rec["end_reason"] == "idle_timeout", rec.get("id"))
    check("open freeze recorded at the end",
          rec["freeze_list"] and rec["freeze_list"][-1]["open"] is True, rec["freeze_list"][-1:])
    check("record carries the speed figures", rec["speed_median"] is not None and rec["speed_p10"] is not None)
    h.finish()
    sh.HISTORY.flush()
    check("finish is idempotent", len(sh.HISTORY.read(10)) == 1)

    section("model: reconnect and replay")
    T[0] = 1110.0
    r = sh.StreamHealth("t2", "team:x", "x", "admin1", clock=clock)
    check("reconnect within 60s detected",
          r.reconnect is not None and r.reconnect["previous_id"] == "t1", r.reconnect)
    r.delivered(u(16), 100); r.delivered(u(18), 100); r.delivered(u(29), 100)
    check("chunks the previous stream already sent count as replayed",
          r.reconnect["replayed_chunks"] == 2, r.reconnect["replayed_chunks"])
    T[0] = 1300.0
    late = sh.StreamHealth("t3", "team:x", "x", "", clock=clock)
    check("no reconnect after the window", late.reconnect is None)

    section("model: stutter")
    T[0] = 2000.0
    st = sh.StreamHealth("t5", "team:y", "y", "", clock=clock)
    st.sending(); st.delivered(u(20), 100)          # cushion 4, empty at 2004
    for n in range(21, 29):                         # each 4s chunk takes 5.5s
        st.download_begin(u(n)); T[0] += 2.75; st.download_bytes(50)
        T[0] += 2.75; st.download_end(u(n), b"x" * 100)
        st.sending(); st.delivered(u(n), 100)
    ss = st.snapshot()
    check("stutter: many short stalls are one freeze",
          ss["freezes"] == 1 and ss["frozen_s"] > 8, (ss["freezes"], ss["frozen_s"]))
    check("stutter: blamed on slow downloads", set(ss["by_cause"]) == {"slow"}, ss["by_cause"])
    check("stutter: speed below real time", ss["speed"] is not None and ss["speed"] < 1, ss["speed"])
    T[0] += 20
    st.sending(); st.delivered(u(29), 100)          # long gap: player label, not a freeze
    st.finish()
    rec = [r for r in sh.HISTORY.read(10) if r["id"] == "t5"]
    sh.HISTORY.flush()
    rec = [r for r in sh.HISTORY.read(10) if r["id"] == "t5"]
    check("stutter: closed with its stall count",
          rec and rec[0]["freeze_list"] and rec[0]["freeze_list"][0]["stalls"] >= 5,
          rec and rec[0]["freeze_list"])

    section("history file")
    path = os.path.join(tempfile.mkdtemp(), "trim.jsonl")
    sh.HISTORY.configure(path, keep=5)
    for i in range(70):
        sh.HISTORY.append({"id": "r%d" % i, "seconds": 1, "freezes": i % 2,
                           "frozen_s": 1.5 * (i % 2), "by_cause": {"slow": 1.5 * (i % 2)},
                           "end_reason": "client_left"})
    sh.HISTORY.flush()
    lines = open(path).read().splitlines()
    check("trimmed to keep", len(lines) <= 5 + 50 and len(lines) >= 5, len(lines))
    with open(path, "a") as f:
        f.write("{not json\n")
    got = sh.HISTORY.read(3)
    check("newest first, corrupt line skipped", [g["id"] for g in got] == ["r69", "r68", "r67"],
          [g["id"] for g in got])
    tot = sh.totals(sh.HISTORY.read(500))
    check("totals add up", tot["freezes"] == sum(1 for g in sh.HISTORY.read(500) if g["freezes"]), tot)

    section("early warnings")
    T[0] = 3000.0
    sh.note_playlist("http://w/p.m3u8",
                     "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n" +
                     "".join("#EXTINF:4.0,\nw%d.ts\n" % n for n in range(1, 40)),
                     app.resolve_url, {})
    wu = lambda n: "http://w/w%d.ts" % n   # noqa: E731
    w = sh.StreamHealth("w1", "team:w", "w", "admin1", clock=clock)
    w.playlist_begin()
    w.playlist_ok({"variants": [{"bandwidth": 8000000, "resolution": "1920x1080"},
                                {"bandwidth": 700000, "resolution": "960x540"}],
                   "target_duration": 4.0}, 3)
    for n in (1, 2, 3):                     # 12s of cushion, empty at 3012
        w.download_begin(wu(n)); w.download_end(wu(n), b"x" * 100)
        w.sending(); w.delivered(wu(n), 100)
    for _ in range(4):
        T[0] += 0.5; w.tick()
    check("steady: nothing raised", not w.warnings and w.snapshot()["state"] == "healthy",
          list(w.warnings))
    # The playlist request hangs at 3004 and answers at 3019: the 2026-10-13 shape.
    T[0] = 3004.0
    w.playlist_begin()
    T[0] = 3007.5; w.tick()
    check("hang: nothing before PLAYLIST_SLOW_S", "playlist_slow" not in w._active)
    T[0] = 3008.0; w.tick()
    snap = w.snapshot()
    check("hang: playlist_slow raised at 4s",
          [x["kind"] for x in snap["warnings"]] == ["playlist_slow"], snap["warnings"])
    check("hang: state struggling with the warning's text",
          snap["state"] == "struggling" and "Playlist request" in snap["reason"], snap["reason"])
    check("hang: warning says what Phase 3 would do",
          "fresh connection" in snap["warnings"][0]["would"], snap["warnings"][0]["would"])
    T[0] = 3012.8; w.tick()
    check("hang: no alarm before the cushion has been empty 1s", w._alarm is None)
    T[0] = 3013.5; w.tick()
    snap = w.snapshot()
    check("hang: freeze alarm once empty for 1s", snap["alarm"] is not None and w.alarms == 1,
          snap["alarm"])
    check("hang: alarm would re-ask for the playlist",
          "playlist" in snap["alarm"]["would"], snap["alarm"])
    check("hang: the alarm is on the timeline, naming the warning",
          any(e["kind"] == "alarm" and "playlist_slow" in e["text"] for e in snap["timeline"]),
          [e["text"] for e in snap["timeline"] if e["kind"] == "alarm"])
    for _ in range(6):
        T[0] += 0.5; w.tick()
    check("hang: one alarm per stall, not per tick", w.alarms == 1, w.alarms)
    T[0] = 3019.0
    w.playlist_ok({}, 1)
    w.download_begin(wu(4)); T[0] = 3019.5; w.download_bytes(100)
    w.download_end(wu(4), b"x" * 100); w.sending(); w.delivered(wu(4), 100)
    w.tick()
    check("hang: warning and alarm clear when video flows",
          not w._active and w._alarm is None, (list(w._active), w._alarm))
    T[0] += 30
    w.download_begin(wu(5)); w.download_end(wu(5), b"x" * 100)
    w.sending(); w.delivered(wu(5), 100)    # closes the freeze episode
    f = w.freezes[-1] if w.freezes else {}
    check("hang: freeze recorded, blamed on the playlist", f.get("cause") == "playlist", f)
    check("hang: warned 4s ahead of the cushion running out",
          f.get("noticed_s") == -4.0 and f.get("warned_by") == ["playlist_slow"], f)
    det = w.snapshot()["detector"]
    check("hang: the warning scored as a hit",
          det["hits"] == 1 and det["false"] == 0 and det["warned_ahead"] == 1, det)
    check("hang: playlist request times recorded",
          w.snapshot()["playlist_fetch"]["slow"] == 1 and w.snapshot()["playlist_fetch"]["max_s"] == 15.0,
          w.snapshot()["playlist_fetch"])
    # A quiet download that recovers well before the cushion runs out.
    base = T[0]
    w.download_begin(wu(6)); T[0] = base + 4.5; w.tick()
    check("quiet download raised", "download_quiet" in w._active, list(w._active))
    w.download_bytes(100); w.download_end(wu(6), b"x" * 100); w.sending(); w.delivered(wu(6), 100)
    w.tick()
    for n in (7, 8, 9, 10, 11, 12, 13):
        w.download_begin(wu(n)); w.download_end(wu(n), b"x" * 100)
        w.sending(); w.delivered(wu(n), 100)
    T[0] += sh.WARN_HORIZON_S + 1; w.tick()
    det = w.snapshot()["detector"]
    check("a warning no freeze followed is a false alarm",
          det["by_kind"]["download_quiet"]["false"] == 1, det["by_kind"])
    # Too slow: three chunks at half real time. The remedy names the variant.
    for n in (14, 15, 16):
        w.download_begin(wu(n)); T[0] += 8; w.download_end(wu(n), b"x" * 100)
        w.sending(); w.delivered(wu(n), 100)
    w.tick()
    slow = w._active.get("too_slow")
    check("too slow raised, would switch to 960x540",
          slow is not None and "960x540" in slow.get("would", ""), slow)
    # Source quiet: polls keep finding nothing for 8s (2 x 4s chunks).
    for n in (17, 18, 19, 20, 21):
        w.download_begin(wu(n)); w.download_end(wu(n), b"x" * 100)
        w.sending(); w.delivered(wu(n), 100)
    w.playlist_begin(); w.playlist_ok({}, 1)
    t_new = T[0]
    for k in range(9):
        T[0] = t_new + k
        w.playlist_begin(); w.playlist_ok({}, 0); w.tick()
    check("source quiet raised after 8s of empty polls", "source_quiet" in w._active,
          list(w._active))
    w.playlist_failed(503); w.tick()
    check("one playlist failure is not a warning", "playlist_failing" not in w._active)
    w.playlist_failed(503); w.tick()
    check("two in a row are", "playlist_failing" in w._active)
    T[0] += 40
    w.finish()
    sh.HISTORY.flush()
    rec = [r for r in sh.HISTORY.read(20) if r["id"] == "w1"]
    rec = rec[0] if rec else {}
    check("record carries the detector and thresholds",
          rec.get("detector", {}).get("raised", 0) >= 5 and rec.get("thresholds", {}).get(
              "playlist_slow_s") == sh.PLAYLIST_SLOW_S, (rec.get("detector"), rec.get("thresholds")))
    check("record lists the warnings", len(rec.get("warning_list") or []) >= 5)
    check("at the end every unhit warning is false, none pending",
          rec.get("detector", {}).get("pending") == 0, rec.get("detector"))
    tot = sh.totals([rec, {"seconds": 10, "freezes": 0}])
    check("totals add the detector, skipping pre-Phase-2 records",
          tot["detector"]["streams"] == 1
          and tot["detector"]["warned_ahead"] == rec["detector"]["warned_ahead"] >= 1,
          tot["detector"])
    check("summary line mentions the warnings", "early warnings" in sh._summary_line(rec))
    no_var = sh.StreamHealth("w2", "", "", "", clock=clock)
    check("too slow with nothing lower on offer says so",
          "no lower quality" in no_var._remedy("too_slow"))

    section("early warnings: tuned after the 2026-10-14 stream")
    T[0] = 6000.0
    v = sh.StreamHealth("v1", "team:v", "v", "", clock=clock)
    v.playlist_begin(); v.playlist_ok({"target_duration": 6.0}, 1)
    v.sending(); v.delivered("http://v/a.ts", 100)
    for _ in range(sh.PLAYLIST_BASELINE_N):          # every request takes 4.4s
        v.playlist_begin(); T[0] += 4.4; v.playlist_ok({}, 0); v.tick()
    check("a stream whose every request takes 4.4s raises nothing",
          "playlist_slow" not in v._counts, dict(v._counts))
    check("its typical request is learned", abs((v._playlist_typical() or 0) - 4.4) < 0.01,
          v._playlist_typical())
    v.playlist_begin(); T[0] += 7.0; v.tick()
    check("7s is within 3s of usual: no warning", "playlist_slow" not in v._active)
    T[0] += 0.5; v.tick()
    w7 = v._active.get("playlist_slow")
    check("7.5s is: warning, saying what usual is",
          w7 is not None and "usually 4.4s" in w7["text"], w7 and w7["text"])
    v.playlist_ok({}, 0)
    f = sh.StreamHealth("v2", "", "", "", clock=clock)
    f.playlist_begin(); f.playlist_ok({}, 1)
    f.sending(); f.delivered("http://v/b.ts", 100)
    for _ in range(sh.PLAYLIST_BASELINE_N):          # IPv4: 0.3s requests
        f.playlist_begin(); T[0] += 0.3; f.playlist_ok({}, 0)
    f.playlist_begin(); T[0] += 3.4; f.tick()
    check("a fast stream's threshold is lower too (0.3s usual: warns at 3.3s)",
          "playlist_slow" in f._active, f._playlist_slow_after())
    f.playlist_ok({}, 0)
    # 250 warnings: the counts are not capped by the 200 kept for scoring, the
    # log and timeline say it once a minute, and none is left pending.
    # Two 4s chunks per 4.5s keep the cushion full, so no freeze makes them hits.
    q = sh.StreamHealth("v3", "", "", "", clock=clock)
    q.sending(); q.delivered("http://v/c.ts", 100); q.delivered("http://v/c.ts", 100)
    for _ in range(250):
        q.download_begin("http://v/d.ts"); T[0] += 4.5; q.tick()
        q.download_bytes(10); q.tick()
        q.download_end("http://v/d.ts", b"x" * 10)
        q.sending(); q.delivered("http://v/d.ts", 10); q.delivered("http://v/d.ts", 10)
    q.end("client_left")
    q.finish()
    d = q._detector(T[0])
    dq = d["by_kind"].get("download_quiet", {})
    check("250 warnings counted as 250, all false",
          dq.get("raised") == 250 and dq.get("false") == 250 and d["pending"] == 0, d)
    warns = [e for e in q.timeline if e["kind"] == "warn"]
    check("timeline carries at most one warning a minute",
          0 < len(warns) <= 250 * 4.5 / sh.WARN_LOG_EVERY_S + 1, len(warns))
    check("and says how many it skipped", any("more since the last one" in e["text"] for e in warns),
          warns[-1:])
    # The alarm's remedy follows what is stuck, not the oldest warning.
    r = sh.StreamHealth("v4", "", "", "", clock=clock)
    r.playlist_begin(); r.playlist_ok({}, 1)
    r.sending()
    for _ in range(3):                                # 12s of cushion
        r.delivered("http://v/e.ts", 100)
    r.playlist_begin(); r.playlist_ok({}, 0)
    T[0] += 7; r.tick()                               # source_quiet up first
    check("source quiet is up before the playlist fails", "source_quiet" in r._active)
    r.playlist_failed(500); r.playlist_failed(500); r.tick()
    T[0] += 6.5; r.tick()                             # cushion empty 1.5s
    check("frozen in playlist errors: would fetch a fresh link, not try another source",
          r._alarm is not None and "fresh stream link" in r._alarm["would"], r._alarm)

    section("Phase 3 records")
    T[0] = 8000.0
    k = sh.StreamHealth("k1", "", "k", "", clock=clock)
    k.recovery("chunk_backup", "fired", "download silent for 4s")
    k.recovery("chunk_backup", "won", "backup finished 0.2s after the backup started")
    k.recovery("quick_retry", "fired"); k.recovery("quick_retry", "ok")
    snap = k.snapshot()
    check("fixes counted by kind and outcome",
          snap["recoveries"] == {"chunk_backup": {"fired": 1, "won": 1},
                                 "quick_retry": {"fired": 1, "ok": 1}}, snap["recoveries"])
    check("fixes on the timeline",
          any(e["kind"] == "recovery" and "Backup chunk request won" in e["text"]
              for e in snap["timeline"]), [e["text"] for e in snap["timeline"]])
    k.finish(); sh.HISTORY.flush()
    kr = [r for r in sh.HISTORY.read(50) if r["id"] == "k1"][0]
    check("fixes in the record and the summary line",
          kr["recoveries"]["chunk_backup"]["won"] == 1
          and "backup chunk request" in sh._summary_line(kr), sh._summary_line(kr))
    check("totals add fixes across streams",
          sh.totals([kr, kr])["recoveries"]["chunk_backup"] == {"fired": 2, "won": 2})

    section("watcher")
    t = sh.track("w3", "team:z", "z", "", clock=time.time)
    check("track() registers with the watcher", t in sh._live and sh._watcher.is_alive())
    t.finish()
    check("finish() unregisters", t not in sh._live)
    T[0] = 5000.0
    stale = sh.StreamHealth("w4", "", "", "", clock=clock)
    sh._register(stale)
    T[0] += sh.STALE_START_S + 1
    stale.tick()
    check("a stream that never started is dropped after STALE_START_S", stale not in sh._live)

    section("guards")
    class Boom(sh.StreamHealth):
        @sh._guarded
        def delivered(self, url, nbytes):
            raise RuntimeError("rigged")
    b = Boom("t4", "", "", "", clock=clock)
    check("a hook that raises returns None", b.delivered("x", 1) is None)
    check("and marks itself failed once", b._hook_failed is True)
    n = sh.track(None, bogus=1)
    check("a tracker that cannot start is the no-op stand-in", n is sh.NULL)
    check("the stand-in swallows every hook", n.delivered("x", 1) is None and n.snapshot() is None)


# ─── Manual source choice ─────────────────────────────────────────────────────

def source_choice_tests(app, client, port):
    """The console picker against four fake sources for one match: A works,
    B works, C sends only empty placeholders, D cannot be extracted."""
    base = "http://127.0.0.1:%d/" % port
    for key, fail in (("srcA", None), ("srcB", None),
                      ("srcC", {n: "empty" for n in range(400)})):
        SCENARIOS[key] = Scenario(key, take=10 ** 6, seg_fail=fail)
        SCENARIOS[key].reset()
    E = {k: "http://fake-embed.test/source-%s" % k for k in "ABCD"}
    urls = {E["A"]: base + "srcA/master.m3u8", E["B"]: base + "srcB/master.m3u8",
            E["C"]: base + "srcC/master.m3u8", E["D"]: None}
    app.extract_m3u8_via_browser = (
        lambda e, *a, **k: {"url": urls[e], "headers": {}, "cookies": {},
                            "browser_m3u8_status": 200} if urls.get(e) else None)
    with app._team_lock:
        app._team_roster["srcteam"] = {"name": "Src Team", "kind": "team", "sport": "test"}
        app._team_map["srcteam"] = {
            "team": "Src Team", "away": "Test", "category": "Live", "sport": "test",
            "poster": "", "starts": int(time.time() * 1000), "kind": "team", "side": "home",
            "match_id": "m1", "match_title": "Src vs Test",
            "streams": [{"embed_url": E["A"], "source": "admin", "stream_no": 1, "hd": True},
                        {"embed_url": E["B"], "source": "golf", "stream_no": 1, "hd": True},
                        {"embed_url": E["C"], "source": "hotel", "stream_no": 1, "hd": True},
                        {"embed_url": E["D"], "source": "admin", "stream_no": 2, "hd": False}]}

    def reader():
        out = {"first": None, "done": threading.Event(), "stop": threading.Event(),
               "t0": time.time()}

        def run():
            resp = client.get("/stream?team=srcteam", buffered=False)
            out["status"] = resp.status_code
            try:
                for item in resp.response:
                    if item != app.TS_NULL_PACKET and out["first"] is None:
                        out["first"] = time.time() - out["t0"]
                    if out["stop"].is_set():
                        break
            finally:
                resp.close()
                out["done"].set()
        threading.Thread(target=run, daemon=True).start()
        return out

    def active(timeout=20.0, label=None):
        end = time.time() + timeout
        while time.time() < end:
            with app._active_streams_lock:
                for r in app._active_streams.values():
                    if r.get("hold_key") == "team:srcteam" and (
                            label is None or r.get("source_label") == label):
                        return r["stream_id"], r.get("source_label")
            time.sleep(0.1)
        return None, None

    def job(states, timeout=40.0):
        end = time.time() + timeout
        while time.time() < end:
            j = app._source_jobs.get("srcteam")
            if j and j["state"] in states:
                return dict(j)
            time.sleep(0.1)
        return dict(app._source_jobs.get("srcteam") or {})

    r1 = reader()
    sid, label = active()
    check("a channel starts on the first source", label == "admin1-HD", label)
    st = client.get("/stream/status").get_json()
    view = next((x.get("source") for x in st["streams"] if x["stream_id"] == sid), None) or {}
    check("/stream/status lists the match's four sources, the playing one marked",
          len(view.get("options") or []) == 4
          and [o["label"] for o in view["options"] if o["playing"]] == ["admin1-HD"], view)

    check("a dead source is accepted for checking", app._switch_source(sid, E["D"])[0] == "ok")
    j = job(("failed", "switching", "done"))
    check("a dead source fails its check, in words", j.get("state") == "failed"
          and "no stream link" in j.get("detail", ""), j)
    check("and the stream is untouched", active(1)[0] == sid and not r1["done"].is_set())

    app._switch_source(sid, E["C"])
    j = job(("failed", "switching", "done"))
    check("a source sending empty placeholders fails its check",
          j.get("state") == "failed" and "empty" in j.get("detail", ""), j)
    check("and the stream is still untouched", active(1)[0] == sid and not r1["done"].is_set())

    check("an unknown source is refused", app._switch_source(sid, "http://elsewhere/x")[0]
          == "unknown_source")
    r = client.post("/api/streams/%s/source" % sid, json={})
    check("the API wants an embed_url", r.status_code == 400, r.status_code)

    check("a good source is accepted", app._switch_source(sid, E["B"])[0] == "ok")
    check("a second switch while one runs is refused",
          app._switch_source(sid, E["A"])[0] == "busy")
    j = job(("switching", "failed", "done"))
    check("the good source passes its check", j.get("state") in ("switching", "done"), j)
    r1["done"].wait(10)
    check("the old stream ended", r1["done"].is_set())
    check("without a disconnect hold, so the reconnect is let in",
          not app._disconnect_held("team:srcteam"))
    r2 = reader()                                   # what Dispatcharr does next
    sid2, label = active(label="golf1-HD")
    deadline = time.time() + 5
    while r2["first"] is None and time.time() < deadline:
        time.sleep(0.05)
    check("the reconnect plays the picked source", label == "golf1-HD", label)
    check("from the checked payload: first video within 1s",
          r2["first"] is not None and r2["first"] < 1.0, r2["first"])
    check("the switch reports done", job(("done",), 5).get("detail") == "Now playing golf1-HD",
          app._source_jobs.get("srcteam"))

    r2["stop"].set(); r2["done"].wait(10)
    r3 = reader()
    sid3, label = active()
    check("the pick sticks across a later reconnect", label == "golf1-HD", label)
    check("picking what is already playing just keeps it",
          app._switch_source(sid3, E["B"])[0] == "ok"
          and "already playing" in job(("done",), 2).get("detail", ""))
    check("back to the normal order", app._clear_source_choice(sid3) == "ok"
          and app._source_pins.get("srcteam") is None)
    r3["stop"].set(); r3["done"].wait(10)
    r4 = reader()
    sid4, label = active()
    check("the next start uses the normal order again", label == "admin1-HD", label)
    r4["stop"].set(); r4["done"].wait(10)

    app._source_pins["srcteam"] = {"embed_url": E["B"], "label": "golf1-HD",
                                   "match_id": "m1", "set_at": time.time(),
                                   "prepared": None, "prepared_at": 0}
    with app._team_lock:
        app._team_map["srcteam"]["match_id"] = "m2"
    check("a new match drops the pick", app._source_pin("srcteam", "m2") is None)
    with app._active_streams_lock:
        app._active_streams["abcdef01"] = {"stream_id": "abcdef01", "hold_key": "url:x",
                                           "embed_url": "x", "multiview": None}
    try:
        check("a ?url= stream has no source choice",
              app._switch_source("abcdef01", E["A"])[0] == "not_team")
    finally:
        with app._active_streams_lock:
            app._active_streams.pop("abcdef01", None)


# ─── Rigs ─────────────────────────────────────────────────────────────────────

HOOKS = ("download_begin", "download_attempt", "download_bytes", "download_end",
         "sending", "delivered", "playlist_begin", "playlist_failed",
         "playlist_ok", "loop_error", "end", "finish", "snapshot", "tick",
         "recovery", "empty_chunk")


def rig_hooks_raise(sh):
    saved = {n: getattr(sh.StreamHealth, n) for n in HOOKS}

    def boom(self, *a, **k):
        raise RuntimeError("rigged")
    for n in HOOKS:
        setattr(sh.StreamHealth, n, sh._guarded(boom))
    saved_parse = sh.parse_playlist
    sh.parse_playlist = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rigged"))

    def undo():
        for n, f in saved.items():
            setattr(sh.StreamHealth, n, f)
        sh.parse_playlist = saved_parse
    return undo


def rig_tracker_fails(sh):
    saved = sh.StreamHealth.__init__

    def boom(self, *a, **k):
        raise RuntimeError("rigged")
    sh.StreamHealth.__init__ = boom

    def undo():
        sh.StreamHealth.__init__ = saved
    return undo


# ─── Expectations for the tracked run ─────────────────────────────────────────

def expect(results):
    section("health matches what the origin did")
    h = {k: (v.get("health") or {}) for k, v in results.items()}
    for k, v in h.items():
        check("%s: history record written" % k, bool(v))
    st, sl, sa, er, rs, en = (h.get(k, {}) for k in
                              ("steady", "slow", "stall", "errors", "restart", "ended"))
    check("steady: no freezes", st.get("freezes") == 0, st.get("freeze_list"))
    check("steady: keeps up", (st.get("speed_p10") or 0) > 1, st.get("speed_p10"))
    check("steady: variant taken is the first offered",
          (st.get("variant") or {}).get("bandwidth") == 8000000 and len(st.get("variants") or []) == 2,
          st.get("variant"))
    check("steady: ended by the player", st.get("end_reason") == "client_left", st.get("end_reason"))
    check("steady: durations came from the playlist", st.get("durations_guessed") == 0,
          st.get("durations_guessed"))
    check("slow: slowest tenth below real time", (sl.get("speed_p10") or 9) < 1, sl.get("speed_p10"))
    check("slow: froze, blamed on slow downloads",
          sl.get("freezes", 0) >= 1 and "slow" in (sl.get("by_cause") or {}), sl.get("by_cause"))
    check("stall: froze, blamed on the source",
          max((sa.get("by_cause") or {"x": 0}).items(), key=lambda kv: kv[1])[0] == "source",
          sa.get("by_cause"))
    check("stall: the 7 chunks it never listed count as skipped", sa.get("skipped") == 7,
          sa.get("skipped"))
    check("errors: playlist errors counted", (er.get("playlist_errors") or 0) >= 1,
          er.get("playlist_errors"))
    check("errors: any freeze is blamed on the download or the playlist",
          set(er.get("by_cause") or {}) <= {"stalled", "slow", "playlist"}, er.get("by_cause"))
    check("errors: nothing dropped", er.get("downloads_failed") == 0, er.get("downloads_failed"))
    check("restart: playlist went backwards", (rs.get("playlist_backwards") or 0) >= 1,
          rs.get("playlist_backwards"))
    check("restart: older chunks sent after newer ones", (rs.get("backwards") or 0) >= 1,
          rs.get("backwards"))
    check("ended: ENDLIST seen", en.get("endlist") is True)
    check("ended: ran out on the idle limit", en.get("end_reason") == "idle_timeout",
          en.get("end_reason"))

    section("early warnings match what the origin did")
    hg = h.get("hang", {})

    def kinds(rec, hit=None):
        return {w["kind"] for w in rec.get("warning_list") or []
                if hit is None or w["hit"] == hit}
    check("steady: no warnings at all", not kinds(st), st.get("warning_list"))
    check("slow: too_slow raised and hit", "too_slow" in kinds(sl, True), sl.get("warning_list"))
    check("stall: source_quiet raised and hit", "source_quiet" in kinds(sa, True),
          sa.get("warning_list"))
    check("errors: the halfway stall raised download_quiet", "download_quiet" in kinds(er),
          er.get("warning_list"))
    check("hang: any freeze is blamed on the playlist",
          not hg.get("by_cause") or max(hg["by_cause"].items(), key=lambda kv: kv[1])[0] == "playlist",
          hg.get("by_cause"))

    section("empty chunks")
    em, er_ = h.get("empty", {}), results.get("empty", {})
    segs = er_.get("segments") or []
    check("empty: the three placeholders counted", em.get("empty_chunks") == 3, em.get("empty_chunks"))
    check("empty: two holes of their length, cause empty",
          [(f["cause"], f["s"]) for f in em.get("freeze_list") or [] if f["cause"] == "empty"]
          == [("empty", 1.0), ("empty", 2.0)], em.get("freeze_list"))
    check("empty: not counted as dropped or skipped",
          em.get("downloads_failed") == 0 and em.get("skipped") == 0,
          (em.get("downloads_failed"), em.get("skipped")))
    check("empty: none of the junk reached the player, chunks intact and in order",
          er_.get("intact") is True and er_.get("trailing_bytes") == 0
          and not {"A6", "A9", "A10"} & set(segs)
          and [int(x[1:]) for x in segs] == sorted(int(x[1:]) for x in segs), segs)

    section("Phase 3 fixes did what the origin called for")
    ch = h.get("chunkhang", {})

    def fixes(rec, kind):
        return (rec.get("recoveries") or {}).get(kind) or {}
    check("chunkhang: a backup chunk request started and won",
          fixes(ch, "chunk_backup").get("won", 0) >= 1, ch.get("recoveries"))
    check("chunkhang: the 25s stall froze for under 4s",
          (ch.get("longest_freeze_s") or 0) < 4, ch.get("freeze_list"))
    check("every pull delivered intact chunks",
          all(v.get("intact") is True for v in results.values()),
          [k for k, v in results.items() if v.get("intact") is not True])
    check("chunkhang: nothing dropped, nothing skipped",
          ch.get("downloads_failed") == 0 and ch.get("skipped") == 0,
          (ch.get("downloads_failed"), ch.get("skipped")))
    check("hang: a backup playlist request started and won",
          fixes(hg, "playlist_backup").get("won", 0) >= 1, hg.get("recoveries"))
    check("hang: the 7s hang froze for under 4.5s (6s without the fix)",
          (hg.get("longest_freeze_s") or 0) < 4.5, hg.get("freeze_list"))
    check("errors: the first playlist error was retried quickly",
          fixes(er, "quick_retry").get("fired", 0) >= 1, er.get("recoveries"))
    check("errors: the halfway stall got a backup that won",
          fixes(er, "chunk_backup").get("won", 0) >= 1, er.get("recoveries"))
    check("slow: slow but flowing downloads never get a backup",
          not fixes(sl, "chunk_backup"), sl.get("recoveries"))
    check("steady: no fix fired", not st.get("recoveries"), st.get("recoveries"))
    check("stall: a source with nothing new triggers no backup",
          not fixes(sa, "chunk_backup") and not fixes(sa, "playlist_backup"), sa.get("recoveries"))
    check("hang: the slow poll is in the playlist figures",
          ((hg.get("playlist_fetch") or {}).get("max_s") or 0) >= 3.9, hg.get("playlist_fetch"))
    # A single stall raises the alarm within FREEZE_MIN_S plus a tick or two.
    # One barely over FREEZE_MIN_S can end before any tick sees it: a miss,
    # recorded as such. A stutter of sub-second stalls never trips the alarm;
    # it is noticed by whichever warning its cause raises.
    import streamhealth as shm
    short = shm.FREEZE_MIN_S + 2 * shm.WATCH_INTERVAL_S
    for name, rec in h.items():
        for f in rec.get("freeze_list") or []:
            n = f.get("noticed_s")
            if f["stalls"] > 1:
                ok, what = n is not None, "stutter noticed"
            elif f["s"] < short:
                ok, what = n is None or n <= 2.0, "short freeze noticed or honestly missed"
            else:
                ok, what = n is not None and n <= 2.0, "freeze noticed within 2s"
            check("%s: %s (at %.0fs, %.1fs)" % (name, what, f["at_s"], f["s"]), ok, f)
        det = rec.get("detector") or {}
        fl = rec.get("freeze_list") or []
        check("%s: every freeze scored, misses included" % name,
              det.get("freezes_scored") == len(fl)
              and det.get("missed") == sum(1 for f in fl if f.get("noticed_s") is None), det)


def compare(new, old, label_new, label_old, tol_max=1.0, tol_median=0.25, earlier_ok=()):
    section("video: %s vs %s" % (label_new, label_old))
    for name in sorted(set(new) | set(old)):
        a, b = new.get(name), old.get(name)
        if a and not b and name in {x.name for x in scenario_defs() if x.new_only}:
            continue
        if not a or not b:
            check("%s: present in both" % name, False)
            continue
        same = a.get("digest") == b.get("digest") and a.get("segments") == b.get("segments")
        if not same and name in earlier_ok:
            # A fix may fetch chunks the old code let slide out of the
            # playlist. Allowed: every chunk intact, in order, and none the
            # old code delivered (up to where the new pull stopped) missing.
            sa, sb = a.get("segments") or [], b.get("segments") or []
            seq = [int(x[1:]) for x in sa]
            last = max(seq) if seq else -1
            missing = [x for x in sb if x[0] == "A" and int(x[1:]) <= last and x not in sa]
            ok = (a.get("intact") is True and seq == sorted(set(seq)) and not missing)
            check("%s: same chunks, intact and in order, plus %d the old code skipped"
                  % (name, len(set(sa) - set(sb))), ok,
                  "missing %s; new %s; old %s" % (missing, sa[:20], sb[:20]))
        else:
            check("%s: byte-identical video (%d chunks)" % (name, len(a.get("segments") or [])),
                  same, "%s vs %s" % ((a.get("segments") or [])[:30], (b.get("segments") or [])[:30])
                  if not same else "")
        ma, mb = a.get("marks") or [], b.get("marks") or []
        if name in earlier_ok:
            # Once a fix has caught up, the 2s playlist polls fall at other
            # moments than the old code's, so a chunk published just before
            # an old poll can land one poll later than it did. Allowed: no
            # chunk later than one poll interval, and earlier on the median.
            late = sorted(x - y for x, y in zip(ma, mb))
            if late:
                med = late[len(late) // 2]
                check("%s: earlier on the median (%+.2fs), never a poll later (worst %+.2fs), "
                      "up to %.1fs earlier" % (name, med, late[-1], -late[0]),
                      med <= tol_median and late[-1] <= 2.0 + tol_median)
            continue
        deltas = sorted(abs(x - y) for x, y in zip(ma, mb))
        if deltas:
            med = deltas[len(deltas) // 2]
            check("%s: delivery timing matches (median %.2fs, worst %.2fs)" % (name, med, deltas[-1]),
                  med <= tol_median and deltas[-1] <= tol_max)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true",
                    help="old image: scenarios only, no tracking expected")
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", nargs=2, metavar=("NEW", "OLD"))
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    if args.compare:
        new = json.load(open(args.compare[0]))
        old = json.load(open(args.compare[1]))
        compare(new["runs"]["tracked"], old["runs"]["baseline"], "new image", "old image",
                earlier_ok=FIX_SCENARIOS)
        if "fixes_off" in new["runs"]:
            compare(new["runs"]["fixes_off"], {k: v for k, v in old["runs"]["baseline"].items()
                                               if k in new["runs"]["fixes_off"]},
                    "new image, every fix off", "old image")
        print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
        sys.exit(1 if fails else 0)

    data = tempfile.mkdtemp(prefix="health-")
    os.environ.setdefault("STARTUP_DELAY", "0")
    os.environ["STREAM_IDLE_TIMEOUT"] = "12"
    os.environ["TEAMS_FILE"] = os.path.join(data, "teams.json")
    os.environ["LINEUP_FILE"] = os.path.join(data, "lineup.json")
    os.environ["EXTRACT_CACHE_FILE"] = os.path.join(data, "extract_cache.json")
    os.environ["SETTINGS_FILE"] = os.path.join(data, "settings.json")
    os.environ["STREAM_HISTORY_FILE"] = os.path.join(data, "stream_history.jsonl")
    sys.path.insert(0, "/app")
    import app  # noqa: E402

    client = app.app.test_client()
    port = start_origin()
    only = set(filter(None, args.only.split(",")))
    sh = getattr(app, "streamhealth", None)
    result = {"image": "baseline" if args.baseline else "new", "runs": {}}

    if args.baseline:
        if sh is not None:
            print("note: this image has streamhealth; running as baseline anyway")
        section("scenarios (baseline)")
        result["runs"]["baseline"] = run_scenarios(app, client, port, "baseline", only,
                                                   health=False)
    else:
        if sh is None:
            sys.exit("this image has no streamhealth module; use --baseline")
        unit_tests(app)
        sh.HISTORY.configure(os.environ["STREAM_HISTORY_FILE"])
        section("scenarios, tracked")
        tracked = run_scenarios(app, client, port, "tracked", only)
        result["runs"]["tracked"] = tracked
        expect(tracked)

        section("scenarios, every tracking hook rigged to raise")
        undo = rig_hooks_raise(sh)
        try:
            rigged = run_scenarios(app, client, port, "rigged", only, health=False)
        finally:
            undo()
        result["runs"]["rigged"] = rigged
        compare(tracked, rigged, "tracked", "hooks raising")

        section("scenarios with every Phase 3 fix switched off")
        saved = (app.CHUNK_BACKUP_AFTER, app.PLAYLIST_BACKUP_AFTER, app.PLAYLIST_RETRY_DELAY)
        app.CHUNK_BACKUP_AFTER, app.PLAYLIST_BACKUP_AFTER, app.PLAYLIST_RETRY_DELAY = 0, 0, 2
        try:
            off_names = FIX_SCENARIOS & (only or FIX_SCENARIOS)
            off = run_scenarios(app, client, port, "fixes-off", off_names) if off_names else {}
        finally:
            app.CHUNK_BACKUP_AFTER, app.PLAYLIST_BACKUP_AFTER, app.PLAYLIST_RETRY_DELAY = saved
        result["runs"]["fixes_off"] = off
        for name, rec in off.items():
            check("%s with fixes off: none fired" % name,
                  not ((rec.get("health") or {}).get("recoveries")),
                  (rec.get("health") or {}).get("recoveries"))

        section("scenario, tracker fails to start")
        if only and "errors" not in only:
            print("  (skipped: --only)")
        undo = rig_tracker_fails(sh)
        try:
            nostart = (run_scenarios(app, client, port, "nostart", {"errors"}, health=False)
                       if not only or "errors" in only else {})
        finally:
            undo()
        result["runs"]["nostart"] = nostart
        if "errors" in tracked:
            compare({"errors": tracked["errors"]}, nostart, "tracked", "no tracker")

        section("reconnect")
        sc = Scenario("reconnect", take=8)
        SCENARIOS["reconnect"] = sc
        url = "http://127.0.0.1:%d/reconnect/master.m3u8" % port
        app.extract_m3u8_via_browser = (
            lambda _e, *a, **k: {"url": url, "headers": {}, "cookies": {},
                                 "browser_m3u8_status": 200})
        embed = "http://fake-embed.test/reconnect"
        first = pull(app, client, embed, 8)
        sc.first_poll()
        second = pull(app, client, embed, 5)
        sh.HISTORY.flush()
        recs = [r for r in sh.HISTORY.read(50) if r.get("channel") == embed]
        latest = recs[0] if recs else {}
        rc = latest.get("reconnect") or {}
        check("second stream recognised as a reconnect", bool(rc), latest.get("reconnect"))
        check("it replayed chunks the first had sent", (rc.get("replayed_chunks") or 0) >= 1,
              rc.get("replayed_chunks"))
        check("both pulls delivered video", first.get("segments") and second.get("segments"))

        section("IPv4 on the stream path")
        from curl_cffi import CurlOpt
        check("UPSTREAM_IPV4 on by default", app.UPSTREAM_IPV4 is True)
        check("it asks curl for IPv4 only",
              app._upstream_curl_options() == {CurlOpt.IPRESOLVE: 1}, app._upstream_curl_options())
        app.UPSTREAM_IPV4 = False
        check("switched off live, the library default is back", app._upstream_curl_options() is None)
        app.UPSTREAM_IPV4 = True
        seen = []
        real_get = app.cf_requests.get

        def spy(url, *a, **k):
            seen.append((url.rsplit("/", 1)[-1], k.get("curl_options")))
            return real_get(url, *a, **k)
        app.cf_requests.get = spy
        try:
            sc = Scenario("ipv4", take=6)
            SCENARIOS["ipv4"] = sc
            url4 = "http://127.0.0.1:%d/ipv4/master.m3u8" % port
            app.extract_m3u8_via_browser = (
                lambda _e, *a, **k: {"url": url4, "headers": {}, "cookies": {},
                                     "browser_m3u8_status": 200})
            pulled = pull(app, client, "http://fake-embed.test/ipv4", 6)
        finally:
            app.cf_requests.get = real_get
        leaves = {u for u, _o in seen}
        check("a stream made playlist, probe and chunk requests",
              {"master.m3u8", "index.m3u8"} <= leaves and any(x.endswith(".ts") for x in leaves)
              and len(pulled.get("segments") or []) == 6, sorted(leaves)[:8])
        check("every one of them was IPv4 only",
              all(o == {CurlOpt.IPRESOLVE: 1} for _u, o in seen),
              [x for x in seen if x[1] != {CurlOpt.IPRESOLVE: 1}][:3])

        section("manual source choice")
        source_choice_tests(app, client, port)

        section("status endpoint")
        r = client.get("/api/streams/history?limit=5")
        body = r.get_json() if r.status_code == 200 else {}
        check("/api/streams/history answers", r.status_code == 200, r.status_code)
        check("history has totals and records", "totals" in body and len(body.get("records", [])) == 5)
        st = client.get("/stream/status").get_json()
        check("/stream/status still answers with no streams", st.get("active_streams") == 0)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=1)
    print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
