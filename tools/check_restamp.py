"""Restamp after long holes (stream-recovery ticket 01) verification. Not shipped.

Runs inside a scratch-tag image (build one with `docker build -t
streamed-m3u:verify-restamp .`), never against a live source or the live
image. Three steps; /w is a scratch directory that persists between them:

    T=$PWD/tools
    # 1. units, the route, and the transcodes with the image's own ffmpeg
    docker run --rm -e STARTUP_DELAY=0 -v /scratch/w:/w -v $T:/t:ro \
        --entrypoint python streamed-m3u:verify-restamp /t/check_restamp.py
    # 2. the same bytes through Jellyfin's own ffmpeg (its image only; its
    #    -readrate_catchup is newer than Debian's). Real time, so in parallel.
    docker run --rm -e STARTUP_DELAY=0 -v /scratch/w:/w -v $T:/t:ro \
        --entrypoint python streamed-m3u:verify-restamp /t/check_restamp.py \
        --jelly-script > /scratch/w/jelly.sh
    docker run --rm --entrypoint sh -v /scratch/w:/w lscr.io/linuxserver/jellyfin \
        /w/jelly.sh
    # 3. read those back
    docker run --rm -e AV_ONLY=1 -v /scratch/w:/w -v $T:/t:ro \
        --entrypoint python streamed-m3u:verify-restamp /t/check_restamp.py

Three layers, one seam each:

  units   restamp.Restamper: TS bytes in, TS bytes out, built by hand here
          with known timestamps so every expected value is a literal.
  route   the real /stream route against a scripted HLS origin serving
          real ffmpeg-made chunks that fail the messy way (403 after a slow
          attempt, a half-sent chunk, a chunk gone before it was fetched).
  av      what the route sends goes through Jellyfin's ffmpeg line; the beep
          must still land on the flash (tools/check_hole_av.py measures it).
"""

import os
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, "/app")

TICKS = 90000
M33 = (1 << 33) - 1
fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  [%s]" % (detail,)) if detail != "" else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n=== %s ===" % title, flush=True)


# ─── A hand-built transport stream with known timestamps ──────────────────────

VID, AUD = 0x100, 0x101


def _ts5(prefix, v):
    v &= M33
    return bytes([(prefix << 4) | (((v >> 30) & 7) << 1) | 1, (v >> 22) & 0xFF,
                  (((v >> 15) & 0x7F) << 1) | 1, (v >> 7) & 0xFF, ((v & 0x7F) << 1) | 1])


def _pes_packet(pid, sid, pts, dts, cc, pcr, rnd):
    """One PES-start packet. Header bytes are exactly what the spec says; the
    payload is random so a rewrite that strays shows up as a changed byte."""
    hdr = bytearray(b"\x00\x00\x01" + bytes([sid]) + b"\x00\x00" + b"\x80")
    if dts is None:
        hdr += bytes([0x80, 5]) + _ts5(2, pts)
    else:
        hdr += bytes([0xC0, 10]) + _ts5(3, pts) + _ts5(1, dts)
    af = b""
    if pcr is not None:
        base = pcr & M33
        af = bytes([7, 0x10, (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF,
                    (base >> 1) & 0xFF, ((base & 1) << 7) | 0x7E | 0x01, 0x55])
    room = 188 - 4 - len(af)
    payload = bytes(hdr) + bytes(rnd.getrandbits(8) for _ in range(room - len(hdr)))
    return bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF,
                  (0x30 if af else 0x10) | (cc & 0xF)]) + af + payload


def _filler(pid, cc, rnd):
    # Includes a PES start code in the payload on purpose: not a PES start
    # (PUSI clear), so it must be left alone.
    body = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + bytes(rnd.getrandbits(8) for _ in range(188 - 4 - 9))
    return bytes([0x47, pid >> 8, pid & 0xFF, 0x10 | (cc & 0xF)]) + body


def make_chunk(start_s, seconds=6, vbase=0, seed=1, with_b=True):
    """Video at 25 fps (PTS = DTS + 0.08s with reordering), audio every
    1920 ticks, a PCR on every video start. Timestamps begin at start_s."""
    rnd = random.Random(seed)
    out = bytearray()
    t0 = int(round(start_s * TICKS)) + vbase
    cc = {VID: 0, AUD: 0}
    frames = int(seconds * 25)
    for f in range(frames):
        dts = t0 + f * 3600
        pts = dts + (7200 if with_b else 0)
        out += _pes_packet(VID, 0xE0, pts, dts if with_b else None, cc[VID], dts - 5400, rnd)
        cc[VID] += 1
        for _ in range(3):
            out += _filler(VID, cc[VID], rnd)
            cc[VID] += 1
    for a in range(int(seconds * TICKS / 1920)):
        out += _pes_packet(AUD, 0xC0, t0 + a * 1920, None, cc[AUD], None, rnd)
        cc[AUD] += 1
        out += _filler(AUD, cc[AUD], rnd)
        cc[AUD] += 1
    return bytes(out)


def _dec5(b):
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | (b[4] >> 1)


def read_times(body):
    """[(packet index, 'pts'|'dts'|'pcr', value)] for every timestamp."""
    out = []
    for i in range(0, len(body), 188):
        p = body[i:i + 188]
        afc = (p[3] >> 4) & 3
        off = 4
        if afc & 2:
            if p[4] >= 7 and p[5] & 0x10:
                v = (p[6] << 25) | (p[7] << 17) | (p[8] << 9) | (p[9] << 1) | (p[10] >> 7)
                out.append((i // 188, "pcr", v))
            off = 5 + p[4]
        if afc & 1 and p[1] & 0x40 and p[off:off + 3] == b"\x00\x00\x01":
            fl = p[off + 7] >> 6
            if fl & 2:
                out.append((i // 188, "pts", _dec5(p[off + 9:off + 14])))
            if fl == 3:
                out.append((i // 188, "dts", _dec5(p[off + 14:off + 19])))
    return out


def changed_packets(a, b):
    return {i // 188 for i in range(0, len(a), 188) if a[i:i + 188] != b[i:i + 188]}


def first_pts(body, pid=VID):
    for i in range(0, len(body), 188):
        p = body[i:i + 188]
        if ((p[1] & 0x1F) << 8 | p[2]) == pid and p[1] & 0x40:
            return [v for k, kind, v in read_times(p) if kind == "pts"][0]


# ─── Units ────────────────────────────────────────────────────────────────────

def unit_tests():
    import restamp

    section("restamp: shift_chunk")
    c = make_chunk(1000.0)
    before = read_times(c)
    check("fixture has timestamps to rewrite", len(before) > 400, len(before))
    s = restamp.shift_chunk(c, -12 * TICKS)
    after = read_times(s)
    check("every PTS, DTS and PCR moved back by exactly 12s",
          [(i, k, v - 12 * TICKS) for i, k, v in before] == after)
    pes_and_pcr = {i for i, _k, _v in before}
    check("only PES-start and PCR packets differ",
          changed_packets(c, s) == pes_and_pcr, len(changed_packets(c, s)))
    check("size and alignment unchanged", len(s) == len(c))
    check("a zero shift hands back the same object", restamp.shift_chunk(c, 0) is c)
    w = make_chunk(((1 << 33) - 2 * TICKS) / TICKS)      # 2s before the 33-bit wrap
    ws = restamp.shift_chunk(w, 8 * TICKS)               # forward over the wrap
    check("33-bit wrap: forward over it",
          [v for _i, _k, v in read_times(ws)] == [(v + 8 * TICKS) & M33 for _i, _k, v in read_times(w)])
    z = make_chunk(3.0)
    zs = restamp.shift_chunk(z, -12 * TICKS)             # below zero wraps up
    check("33-bit wrap: back past zero",
          [v for _i, _k, v in read_times(zs)] == [(v - 12 * TICKS) & M33 for _i, _k, v in read_times(z)])
    check("shifting there and back is lossless",
          restamp.shift_chunk(zs, 12 * TICKS) == z)
    big = c * 40                                          # ~6 MB, a heavy chunk
    t0 = time.time()
    restamp.shift_chunk(big, -12 * TICKS)
    took = time.time() - t0
    check("a 6 MB chunk shifts in well under a second (it runs on the stream's thread)",
          took < 0.5, "%.0f MB in %.0f ms" % (len(big) / 1e6, took * 1000))

    section("restamp: when it acts")
    c1, c2, c3, c4 = (make_chunk(1000.0 + 6 * n, seed=n) for n in range(4))
    c_after_one = make_chunk(1012.0, seed=7)          # c1, one chunk missing, this
    R = restamp.Restamper()
    out = R.chunk(c1, seq=100, dur=6.0)
    check("first chunk passes through untouched", out is c1 and R.event is None)
    out = R.chunk(c2, seq=101, dur=6.0)
    check("continuous chunks pass through untouched", out is c2 and R.event is None)

    R = restamp.Restamper()
    R.chunk(c1, seq=100, dur=6.0)
    R.missing(seq=101, dur=6.0)
    R.missing(seq=102, dur=6.0)
    out = R.chunk(c4, seq=103, dur=6.0)
    check("two dropped 6s chunks (12s) put the next chunk back by 12s",
          first_pts(out) == first_pts(c4) - 12 * TICKS and first_pts(out) == first_pts(c1) + 6 * TICKS,
          (first_pts(out) - first_pts(c1)) / TICKS)
    check("the restamp is reported with its span", R.event and abs(R.event["span_s"] - 12) < 1e-9, R.event)
    nxt = make_chunk(1000.0 + 6 * 4, seed=9)
    out2 = R.chunk(nxt, seq=104, dur=6.0)
    check("every later chunk keeps the shift", first_pts(out2) == first_pts(nxt) - 12 * TICKS)
    check("... without announcing it again", R.event is None)

    section("restamp: below the threshold nothing changes")
    R = restamp.Restamper()
    R.chunk(c1, seq=100, dur=6.0)
    R.missing(seq=101, dur=6.0)
    out = R.chunk(c_after_one, seq=102, dur=6.0)
    check("one dropped 6s chunk: byte passthrough, same object",
          out is c_after_one and R.event is None)
    R.chunk(make_chunk(1000.0 + 18, seed=5), seq=103, dur=6.0)
    check("... and no shift carried forward",
          first_pts(R.chunk(make_chunk(1000.0 + 24, seed=6), seq=104, dur=6.0)) == first_pts(c1) + 24 * TICKS)
    R = restamp.Restamper()
    R.chunk(make_chunk(1000.0, seconds=4), seq=100, dur=4.0)
    R.missing(seq=101, dur=4.0)
    R.missing(seq=102, dur=4.0)
    c8 = make_chunk(1000.0 + 4 + 8, seconds=4, seed=2)
    check("exactly the threshold (8s) is not over it",
          R.chunk(c8, seq=103, dur=4.0, min_hole_s=8.0) is c8)
    R = restamp.Restamper()
    R.chunk(make_chunk(1000.0, seconds=4), seq=100, dur=4.0)
    R.missing(seq=101, dur=4.0)
    R.missing(seq=102, dur=4.0)
    out = R.chunk(c8, seq=103, dur=4.0, min_hole_s=7.9)
    check("... and just over it is", out is not c8 and R.event is not None)

    section("restamp: the span is the playlist's, never a guess")
    R = restamp.Restamper()
    c1 = make_chunk(500.0, seconds=4, seed=1)
    R.chunk(c1, seq=10, dur=4.0)
    R.missing(seq=11, dur=4.0)
    R.missing(seq=12, dur=4.0)
    R.missing(seq=13, dur=4.0)
    nxt = make_chunk(500.0 + 4 + 12, seconds=4, seed=2)
    out = R.chunk(nxt, seq=14, dur=4.0)
    check("three 4s chunks dropped: back by 12s, not 18s",
          first_pts(out) == first_pts(c1) + 4 * TICKS, (first_pts(out) - first_pts(c1)) / TICKS)

    section("restamp: chunks that left the playlist unseen (no EXTINF for them)")
    R = restamp.Restamper()
    c1 = make_chunk(500.0, seed=1)
    R.chunk(c1, seq=10, dur=6.0)
    nxt = make_chunk(500.0 + 6 + 18, seed=2)          # seq 11, 12, 13 never fetched
    out = R.chunk(nxt, seq=14, dur=6.0)
    check("the missing span is read off the timestamp gap",
          first_pts(out) == first_pts(c1) + 6 * TICKS and abs(R.event["span_s"] - 18) < 0.05, R.event)
    check("... and says where it came from", R.event["from"] == "timestamps")
    R = restamp.Restamper()
    R.chunk(c1, seq=10, dur=6.0)
    nxt = make_chunk(500.0 + 6 + 3, seed=2)
    out = R.chunk(nxt, seq=12, dur=6.0)
    check("a one-chunk skip with a 3s gap stays untouched", out is nxt and R.event is None)
    R = restamp.Restamper()
    R.chunk(c1, seq=10, dur=6.0)
    nxt = make_chunk(500.0 + 6 + 400, seed=2)
    check("an implausible gap (a new timeline, not a hole) is left alone",
          R.chunk(nxt, seq=12, dur=6.0) is nxt)

    section("restamp: playlist and timestamps disagree")
    R = restamp.Restamper()
    R.chunk(c1, seq=10, dur=6.0)
    R.missing(seq=11, dur=6.0)
    R.missing(seq=12, dur=6.0)
    nxt = make_chunk(500.0 + 6, seed=2)               # source restarted its clock: no gap at all
    check("EXTINF says 12s, timestamps say 0s: leave it", R.chunk(nxt, seq=13, dur=6.0) is nxt)

    section("restamp: no hole, no work")
    R = restamp.Restamper()
    R.chunk(c1, seq=10, dur=6.0)
    R.missing(seq=11, dur=6.0)
    R.missing(seq=12, dur=6.0)
    R.reset()
    check("after a reset there is nothing to restamp against",
          R.chunk(nxt, seq=13, dur=6.0) is nxt)
    R = restamp.Restamper()
    R.missing(seq=1, dur=6.0)
    R.missing(seq=2, dur=6.0)
    first = make_chunk(500.0, seed=1)
    check("a hole before anything was sent is no hole", R.chunk(first, seq=3, dur=6.0) is first)
    R = restamp.Restamper()
    audio = restamp.Restamper()
    junk = b"RIFF" + b"\x00" * 300
    R.chunk(c1, seq=10, dur=6.0)
    R.missing(seq=11, dur=6.0)
    R.missing(seq=12, dur=6.0)
    check("a chunk that is not transport stream is left alone", R.chunk(junk, seq=13, dur=6.0) is junk)

    section("restamp: failure leaves the chunk unmodified")
    real = restamp.shift_chunk
    R = restamp.Restamper()
    R.chunk(c1, seq=10, dur=6.0)
    R.missing(seq=11, dur=6.0)
    R.missing(seq=12, dur=6.0)
    nxt = make_chunk(500.0 + 6 + 12, seed=2)

    def boom(*_a, **_k):
        raise RuntimeError("rigged")

    restamp.shift_chunk = boom
    try:
        out = R.chunk(nxt, seq=13, dur=6.0)
        after = R.chunk(make_chunk(500.0 + 30, seed=3), seq=14, dur=6.0)
    finally:
        restamp.shift_chunk = real
    check("a rigged raise sends the chunk as it came", out is nxt)
    check("... and the stream carries on untouched from then on",
          first_pts(after) == first_pts(make_chunk(500.0 + 30, seed=3)))
    check("... counted, for the log", R.failures == 1, R.failures)


# ─── The real route against a scripted origin serving real chunks ────────────

class Origin:
    """A scripted HLS origin. `windows` is what each successive media-playlist
    poll lists, (first, last) chunk numbers; the last one repeats. `plan`
    says how each attempt at a chunk goes, in order, then "ok" forever:
    "403", "partial" (half the bytes, then the connection closes), "slow"
    (delivered over `slow_s`, but whole). Deterministic: nothing depends on
    how soon a poll arrives."""

    def __init__(self, src, windows, plan, slow_s=1.5, chunk_s=6.0):
        self.src, self.windows, self.plan = src, windows, plan
        self.slow_s, self.chunk_s = slow_s, chunk_s
        self.polls = 0
        self.attempts = {}

    def playlist(self):
        first, last = self.windows[min(self.polls, len(self.windows) - 1)]
        self.polls += 1
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6",
                 "#EXT-X-MEDIA-SEQUENCE:%d" % first]
        for n in range(first, last + 1):
            lines += ["#EXTINF:%.3f," % self.chunk_s, "seg%03d.ts" % n]
        return "\n".join(lines) + "\n"


def serve(origin):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
                return self._send(200, origin.playlist().encode(), "application/vnd.apple.mpegurl")
            if leaf.startswith("seg"):
                n = int(leaf[3:6])
                k = origin.attempts[n] = origin.attempts.get(n, 0) + 1
                plan = origin.plan.get(n, [])
                how = plan[k - 1] if k <= len(plan) else "ok"
                if how == "403":
                    return self._send(403, b"expired")
                body = open(origin.src[n], "rb").read()
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


def pull(app, client, origin, port, name, want_bytes, max_s=90.0):
    """The real /stream route, read until `want_bytes` of video arrived."""
    from urllib.parse import quote
    url = "http://127.0.0.1:%d/%s/master.m3u8" % (port, name)
    app.extract_m3u8_via_browser = (
        lambda _e, *a, **k: {"url": url, "headers": {}, "cookies": {},
                             "browser_m3u8_status": 200})
    embed = "http://fake-embed.test/" + name
    origin.polls, origin.attempts = 0, {}
    resp = client.get("/stream?url=" + quote(embed, safe=""), buffered=False)
    data = bytearray()
    t0 = time.time()
    try:
        for item in resp.response:
            if item == app.TS_NULL_PACKET:
                continue
            data += item
            if len(data) >= want_bytes:
                # One more read: the loop's bookkeeping for the last chunk
                # runs when the generator is asked for what follows it.
                next(iter(resp.response), None)
                break
            if time.time() - t0 > max_s:
                break
    finally:
        resp.close()
    return bytes(data), embed


def record_for(app, embed):
    sh = app.streamhealth
    deadline = time.time() + 5
    while time.time() < deadline:
        sh.HISTORY.flush()
        for rec in sh.HISTORY.read(200):
            if rec.get("channel") == embed:
                return rec
        time.sleep(0.1)
    return {}


def route_tests():
    data = tempfile.mkdtemp(prefix="restamp-")
    os.environ.setdefault("STARTUP_DELAY", "0")
    os.environ["TEAMS_FILE"] = os.path.join(data, "teams.json")
    os.environ["LINEUP_FILE"] = os.path.join(data, "lineup.json")
    os.environ["EXTRACT_CACHE_FILE"] = os.path.join(data, "extract_cache.json")
    os.environ["SETTINGS_FILE"] = os.path.join(data, "settings.json")
    os.environ["STREAM_HISTORY_FILE"] = os.path.join(data, "stream_history.jsonl")
    import app
    import restamp
    app.streamhealth.HISTORY.configure(os.environ["STREAM_HISTORY_FILE"])
    client = app.app.test_client()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import check_hole_av as hav
    hav.W = os.environ.get("W", "/w")
    hav.make_source()
    folder = os.path.join(hav.W, "src")
    src = sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".ts"))
    chunks = [open(f, "rb").read() for f in src]
    check("twelve 6s source chunks", len(chunks) == 12, len(chunks))

    # Messy, then fine. Chunk 2 arrives slowly but whole; 3 is dead (403
    # twice) = a 6s hole, below the threshold; 4 arrives half, then whole on
    # the retry; 5 and 6 are dead = a 12s hole; 7 and 8 are fine; 9 and 10
    # left the playlist before they were fetched = a 12s hole nobody has an
    # EXTINF for; 11 is fine.
    plan = {2: ["slow"], 3: ["403", "403"], 4: ["partial"], 5: ["403", "403"], 6: ["403", "403"]}
    windows = [(0, 1), (0, 3), (2, 5), (4, 7), (7, 8), (11, 11)]
    delivered = [0, 1, 2, 4, 7, 8, 11]
    shifts = {0: 0, 1: 0, 2: 0, 4: 0, 7: 12, 8: 12, 11: 24}      # seconds, by hand
    origin = Origin(src, windows, plan)
    port = serve(origin)
    want = sum(len(chunks[n]) for n in delivered)

    def run(label, **flags):
        for k, v in flags.items():
            setattr(app, k, v)
        try:
            return pull(app, client, origin, port, "messy-" + label, want)
        finally:
            app.HOLE_RESTAMP, app.HOLE_RESTAMP_MIN_S = True, 8.0

    section("route: messy source, restamping on")
    on, embed = run("on")
    check("exactly the chunks that were deliverable arrived", len(on) == want, (len(on), want))
    pos, good, detail = 0, True, []
    for n in delivered:
        piece = on[pos:pos + len(chunks[n])]
        pos += len(chunks[n])
        moved = [(i, k, v - shifts[n] * TICKS) for i, k, v in read_times(chunks[n])]
        same = read_times(piece) == moved
        touched = changed_packets(chunks[n], piece)
        same &= touched <= {i for i, _k, _v in moved} and bool(touched) == bool(shifts[n])
        good &= same
        detail.append("%d:%s" % (n, "ok" if same else "WRONG"))
    check("each chunk's timestamps are back by the span missing before it (0 0 0 0 12 12 24 s), "
          "nothing else changed", good, " ".join(detail))
    rec = record_for(app, embed)
    check("health recorded two restamps", rec.get("restamps") == 2, rec.get("restamps"))
    check("... closing 24s in all", abs((rec.get("restamp_s") or 0) - 24) < 0.1, rec.get("restamp_s"))
    check("... naming each (span, where it came from)",
          [(r["span_s"], r["from"]) for r in rec.get("restamp_list", [])]
          == [(12.0, "playlist"), (12.0, "timestamps")], rec.get("restamp_list"))
    check("holes still counted as before: 3 dropped, 2 skipped",
          rec.get("downloads_failed") == 3 and rec.get("skipped") == 2,
          (rec.get("downloads_failed"), rec.get("skipped")))

    section("route: the same source, restamping off")
    off, _ = run("off", HOLE_RESTAMP=False)
    check("off: byte-identical to the chunks as the source sent them",
          off == b"".join(chunks[n] for n in delivered))

    section("route: restamping on, no hole over the threshold")
    saved = (origin.plan, origin.windows)
    origin.plan = {2: ["slow"], 3: ["403", "403"], 4: ["partial"]}
    origin.windows = [(0, 1), (0, 3), (2, 5), (4, 7), (7, 11)]
    want_short = sum(len(chunks[n]) for n in [0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11])
    want, kept = want_short, want
    a, _ = run("short-on")
    b, _ = run("short-off", HOLE_RESTAMP=False)
    check("a lone 6s hole: on is byte-identical to off", a == b and len(a) == want_short,
          (len(a), len(b), want_short))
    origin.plan, origin.windows = saved
    want = kept

    section("route: restamping raising")
    real = restamp.shift_chunk

    def boom(chunk, delta):
        if delta:
            raise RuntimeError("rigged")
        return chunk

    restamp.shift_chunk = boom
    try:
        rigged, _ = run("rigged")
    finally:
        restamp.shift_chunk = real
    check("a rigged raise: the video is what off sends", rigged == off)

    section("route: the threshold is a live setting")
    wide, _ = run("wide", HOLE_RESTAMP_MIN_S=30.0)
    check("min span 30s: nothing restamped", wide == off)

    open(hav.W + "/route_on.ts", "wb").write(on)
    open(hav.W + "/route_off.ts", "wb").write(off)
    open(hav.W + "/route_ctl.ts", "wb").write(b"".join(chunks))
    return hav


JELLY_FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFMPEG_MODES = "plain,copyts,hls_cfr,hls_copyts,hls_igndts,hls_async"
JELLY_MODES = "jelly_ts,jelly_hls"
TAGS = ("ctl", "off", "on")      # no hole at all / holes, restamping off / on


def jelly_script(hav):
    """Shell lines that make the jelly_* transcodes with Jellyfin's own
    ffmpeg (its image has the -readrate_catchup option; the verify image's
    Debian ffmpeg does not). In real time, so in parallel."""
    import shlex
    lines = []
    for tag in TAGS:
        for m in JELLY_MODES.split(","):
            out = "%s/route_%s_%s" % (hav.W, tag, m)
            args = [x.replace("{in}", "%s/route_%s.ts" % (hav.W, tag)).replace("{out}", out)
                    for x in hav.MODES[m]]
            lines.append("%s -hide_banner -loglevel error -y %s &" % (
                JELLY_FFMPEG, " ".join(shlex.quote(a) for a in args)))
    return "\n".join(lines + ["wait"]) + "\n"


def av_tests(hav):
    section("audio/video offset through ffmpeg (beep minus flash, seconds)")
    hav.HOLES = [3, 5, 6, 9, 10]
    modes = os.environ.get("AV_MODES", JELLY_MODES if os.environ.get("AV_ONLY")
                           else FFMPEG_MODES + "," + JELLY_MODES).split(",")
    warm = 3      # an hls + -copyts output starts about -1.44s with no hole at all

    def spread(xs):
        xs = xs[warm:]
        return round(max(xs) - min(xs), 2) if xs else None

    for m in modes:
        res = {}
        for tag in TAGS:
            out = "%s/route_%s_%s" % (hav.W, tag, m)
            ext = ".m3u8" if "hls" in m else ".ts"
            if not m.startswith("jelly"):
                hav.run(["ffmpeg", "-y"] + [x.replace("{in}", "%s/route_%s.ts" % (hav.W, tag))
                                            .replace("{out}", out) for x in hav.MODES[m]])
            elif not os.path.exists(out + ext):
                print("  %s: %s missing; run the jelly script (see the docstring)" % (m, out + ext))
                break
            res[tag] = hav.offsets(out + ext)
        else:
            print("  %-10s no hole, control: %s" % (m, sorted(set(res["ctl"][0]))))
            print("  %-10s restamp off:     %s" % ("", sorted(set(res["off"][0]))))
            print("  %-10s restamp on:      %s  flashes=%d beeps=%d" % (
                "", sorted(set(res["on"][0])), res["on"][1], res["on"][2]))
            on_s, off_s = spread(res["on"][0]), spread(res["off"][0])
            check("%s: with restamping, every beep within 0.1s of the others (after start-up)" % m,
                  len(res["on"][0]) >= 15 and on_s is not None and on_s <= 0.1, on_s)
            if m in ("hls_copyts", "hls_igndts", "jelly_hls"):    # the modes that keep input timestamps
                check("%s: (control) without restamping the audio drifts by the holes" % m,
                      off_s is not None and off_s > 1.0, off_s)


def main():
    if "--jelly-script" in sys.argv:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import check_hole_av as hav
        hav.W = os.environ.get("W", "/w")
        sys.stdout.write(jelly_script(hav))
        return
    if os.environ.get("AV_ONLY"):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import check_hole_av as hav
        hav.W = os.environ.get("W", "/w")
        av_tests(hav)
    else:
        unit_tests()
        av_tests(route_tests())
    print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
