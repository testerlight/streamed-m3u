"""Does the composite play at real speed when its inputs arrive in bursts?

Not shipped. Run inside the image (about a minute):

    docker run --rm -v $PWD/tools/check_pacing.py:/tmp/p.py:ro <image> python /tmp/p.py

    MODE=smooth           control: the same material arriving evenly
    PRI_PHASE, SEC_PHASE  seconds before each input's source starts
    KEEP=/dir             keep the captured output (mount /dir)
    VERBOSE=1             ffmpeg at verbose, nothing filtered

Before the fix (2026-09-23): 2% of frames advanced normally, a 5s freeze,
36s of swing against the real clock. After: 100%, 2 frames, 0.3s.

Why this exists. A team stream reaches the composite the way the proxy
delivers it: a whole HLS segment - about five seconds of video, ~4 MB - in a
few milliseconds, then nothing but keepalive padding until the next one. The
composite stamps its inputs by arrival time (pitfall #14), so a burst is five
seconds of video stamped as one instant, and the two inputs burst at
unrelated moments. Nothing in Phases 0-9 measured motion, only bytes; the
first person to watch it called it unwatchable (2026-09-23).

This reproduces that delivery exactly - three segments of backlog, then one
segment every five seconds, null packets between - with a picture whose top
strip encodes its own frame number. Reading the strip back out of the
composite's output says, frame by frame, whether the picture advanced by
exactly one source frame (smooth), stood still (a freeze) or jumped (a skip),
and parsing the output's PTS against the arrival clock says whether it is
delivered at real speed.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, "/app")
import composite  # noqa: E402

W, H, F = 640, 360, 30
SEG = 5.0
# The top strip encodes the frame number mod 32 as luma 32 + 6*(n mod 32):
# 32..218, inside video range (16..235) so nothing clips and every step is
# recoverable after two encodes.
BASE, STEP, CYCLE = 32, 6, 32
DATA = tempfile.mkdtemp(prefix="pacing-")
NULL = b"\x47\x1f\xff\x10" + b"\xff" * 184
fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  [%s]" % (detail,)) if detail != "" else ""), flush=True)
    if not cond:
        fails.append(name)


def segments(name, seconds=60, tone=440):
    """Five-second MPEG-TS segments of a picture that numbers its frames."""
    out = os.path.join(DATA, name)
    os.makedirs(out)
    vsrc = ("color=c=gray:s=%dx%d:r=%d,"
            "geq=lum='if(lt(Y,40),%d+%d*mod(N,%d),128)':cb=128:cr=128"
            % (W, H, F, BASE, STEP, CYCLE))
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", vsrc,
         "-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000" % tone,
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
         "-pix_fmt", "yuv420p", "-g", str(int(F * SEG)), "-keyint_min", str(int(F * SEG)),
         "-sc_threshold", "0", "-c:a", "aac", "-f", "segment",
         "-segment_time", str(SEG), "-segment_format", "mpegts",
         os.path.join(out, "seg%03d.ts")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return [open(os.path.join(out, f), "rb").read() for f in sorted(os.listdir(out))]


class BurstSource:
    """Serves segments the way the proxy does: backlog first, then one whole
    segment every SEG seconds, a null packet each second in between."""

    def __init__(self, segs, backlog=3, phase=0.0):
        self.segs, self.backlog, self.phase = segs, backlog, phase
        self.closed = False

    def __iter__(self):
        time.sleep(self.phase)
        t0 = time.time()
        for seg in self.segs[:self.backlog]:
            for i in range(0, len(seg), composite.CHUNK):
                yield seg[i:i + composite.CHUNK]
        nxt = t0 + SEG
        for seg in self.segs[self.backlog:]:
            while time.time() < nxt and not self.closed:
                time.sleep(min(1.0, max(0.0, nxt - time.time())))
                if time.time() < nxt:
                    yield NULL
            if self.closed:
                return
            for i in range(0, len(seg), composite.CHUNK):
                yield seg[i:i + composite.CHUNK]
            nxt += SEG
        while not self.closed:              # a live source does not end
            time.sleep(1.0)
            yield NULL

    def close(self):
        self.closed = True


class SmoothSource(BurstSource):
    """Control: the same segments, each spread evenly over its own five
    seconds, no backlog. What a well-behaved live source looks like. Run with
    MODE=smooth to check the measurement itself on known-good input."""

    def __iter__(self):
        time.sleep(self.phase)
        for seg in self.segs:
            pieces = [seg[i:i + 188 * 50] for i in range(0, len(seg), 188 * 50)]
            for piece in pieces:
                if self.closed:
                    return
                yield piece
                time.sleep(SEG / len(pieces))
        while not self.closed:
            time.sleep(1.0)
            yield NULL


def video_pts(ts_bytes_iter):
    """(arrival, pts) for each video PES start, parsed as it arrives."""
    buf = b""
    for arrival, chunk in ts_bytes_iter:
        buf += chunk
        n = len(buf) // 188 * 188
        for i in range(0, n, 188):
            p = buf[i:i + 188]
            if p[0] != 0x47 or not (p[1] & 0x40):
                continue
            afc = (p[3] >> 4) & 3
            off = 4 + (1 + p[4] if afc & 2 else 0)
            if not (afc & 1) or off + 14 > 188:
                continue
            pes = p[off:]
            if pes[0:3] != b"\x00\x00\x01" or not (0xE0 <= pes[3] <= 0xEF) or not (pes[7] & 0x80):
                continue
            b = pes[9:14]
            yield arrival, (((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15
                            | b[3] << 7 | b[4] >> 1) / 90000.0
        buf = buf[n:]


print("building sources ...", flush=True)
PRI = segments("pri", tone=440)
SEC = segments("sec", tone=1000)
filler = composite.make_filler(os.path.join(DATA, "filler.ts"), width=W, height=H, fps=F)
# MODE=filler: both inputs loop a short clip for the whole run, as they do
# before a source connects and as an unconfigured miniplayer does for good.
# The main clip numbers its frames, so a stall shows as a hold.
FILLER_ONLY = os.environ.get("MODE") == "filler"
if FILLER_ONLY:
    loop_clip = os.path.join(DATA, "loop.ts")
    with open(loop_clip, "wb") as fh:
        fh.write(PRI[int(os.environ.get("LOOP_SEG", "0"))])
fifo_dir = os.path.join(DATA, "fifo")
os.makedirs(fifo_dir)

# SECONDARY=none: main picture only, so the miniplayer input runs on the
# looping filler the whole time - the unconfigured-miniplayer case, where a
# gap at every filler loop would freeze the whole composite.
NO_SECONDARY = os.environ.get("SECONDARY") == "none"
slot = {"primary": "" if FILLER_ONLY else "p",
        "secondary": "" if (NO_SECONDARY or FILLER_ONLY) else "s", "corner": "br",
        "size": "small", "audio": {"primary": 100, "secondary": 0}}
comp = composite.Composite("9", slot, fifo_dir=fifo_dir, base_url="http://127.0.0.1:1",
                           filler=filler, width=W, height=H, fps=F, encoder="cpu",
                           qp=18, idle_timeout=600)
SOURCE = SmoothSource if os.environ.get("MODE") == "smooth" else BurstSource
print("source shape:", SOURCE.__name__, flush=True)
PRI_PHASE = float(os.environ.get("PRI_PHASE", "0.0"))
SEC_PHASE = float(os.environ.get("SEC_PHASE", "2.3"))
if FILLER_ONLY:
    comp.feeders.primary.filler = loop_clip
comp.feeders.primary._open_source = lambda slug: SOURCE(PRI, phase=PRI_PHASE)
comp.feeders.secondary._open_source = lambda slug: SOURCE(SEC, phase=SEC_PHASE)
if os.environ.get("TEE"):
    # Diagnostic: keep a copy of exactly what each feeder writes to its FIFO.
    _orig_write_all = composite.Feeder._write_all

    def _tee(self, fd, data, _w=_orig_write_all):
        with open(os.path.join(os.environ["TEE"], "%s.ts" % self.role), "ab") as fh:
            fh.write(data)
        return _w(self, fd, data)
    composite.Feeder._write_all = _tee
if os.environ.get("VERBOSE"):
    _bc = composite.build_command

    def _verbose(*a, **k):
        argv = _bc(*a, **k)
        i = argv.index("-loglevel")
        argv[i + 1] = "verbose"
        return argv
    composite.build_command = _verbose
    composite.BENIGN_STDERR = ()
comp.start()

RUN = 48.0
captured, stop = [], threading.Event()
t_start = time.time()


def reader():
    for chunk in comp.read(stop):
        captured.append((time.time() - t_start, chunk))


threading.Thread(target=reader, daemon=True).start()
time.sleep(RUN)
stop.set()
comp.stop()
time.sleep(1)

# KEEP=/some/dir keeps the capture and the feeders' view of it, for looking at.
KEEP = os.environ.get("KEEP")
print("feeders:", {r: {k: v for k, v in st.items() if k in (
    "bytes_written", "filler_bytes", "connects", "stalls", "source_errors",
    "pace_reanchors", "pace_late")} for r, st in comp.feeders.stats().items()}, flush=True)
out_path = os.path.join(KEEP or DATA, "out.ts")
with open(out_path, "wb") as fh:
    for _a, c in captured:
        fh.write(c)
print("captured %.1f MB in %.0fs" % (os.path.getsize(out_path) / 1e6, RUN), flush=True)

# ── delivery against the real clock ───────────────────────────────────────
pts = list(video_pts(captured))
check("the composite produced video", len(pts) > F * 10, len(pts))
if pts:
    a0, p0 = pts[0]
    settled = [(a, p) for a, p in pts if a - a0 >= 12.0]
    lead = [(p - p0) - (a - a0) for a, p in settled]
    swing = (max(lead) - min(lead)) if lead else 99
    print("  video lead over the real clock after 12s: min %+.1fs max %+.1fs"
          % (min(lead), max(lead)) if lead else "  no settled video")
    check("delivered at real speed: the lead swings by under 1.5s", swing < 1.5,
          round(swing, 2))

# ── motion, frame by frame ────────────────────────────────────────────────
# Read the strip as raw luma (no range conversion), recover each output
# frame's source frame number mod CYCLE, and look at how it steps.
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", out_path, "-map", "0:v",
                      "-vf", "crop=64:24:8:8", "-pix_fmt", "yuv420p",
                      "-f", "rawvideo", "-"], capture_output=True).stdout
fsz = 64 * 24 * 3 // 2                     # one yuv420p frame; luma first
idx = []
for i in range(0, len(raw) - fsz + 1, fsz):
    y = raw[i:i + 64 * 24]
    idx.append(round((sum(y) / len(y) - BASE) / STEP) % CYCLE)
print("  source frame (mod %d) every half second, from 12s:" % CYCLE,
      idx[F * 12::F // 2][:40])
# The miniplayer, for comparison: small, bottom right, 32 px margin. Its
# strip is the top 40/360 of a 140x78 picture at (468, 250).
raw2 = subprocess.run(["ffmpeg", "-v", "error", "-i", out_path, "-map", "0:v",
                       "-vf", "crop=64:4:500:251", "-pix_fmt", "yuv420p",
                       "-f", "rawvideo", "-"], capture_output=True).stdout
f2 = 64 * 4 * 3 // 2
mini = [round((sum(raw2[i:i + 256]) / 256 - BASE) / STEP) % CYCLE
        for i in range(0, len(raw2) - f2 + 1, f2)]
print("  miniplayer source frame every half second, from 12s:",
      mini[F * 12::F // 2][:40])
idx = idx[F * 12:]                         # judge after startup
steps = [(idx[i + 1] - idx[i]) % CYCLE for i in range(len(idx) - 1)]
smooth = steps.count(1)
holds = steps.count(0)
if FILLER_ONLY:
    # One jump per loop of the clip is the clip starting again, not a fault.
    joins = sum(1 for st in steps if st not in (0, 1))
    smooth += joins
    print("  loop joins counted as smooth: %d" % joins)
run, longest = 0, 0
for st in steps:
    run = run + 1 if st == 0 else 0
    longest = max(longest, run)
if steps:
    print("  frames judged %d: advanced by one %d (%.0f%%), held %d, skipped %d"
          % (len(steps), smooth, 100.0 * smooth / len(steps), holds,
             len(steps) - smooth - holds))
check("motion is smooth: at least 95% of frames advance by exactly one",
      bool(steps) and smooth >= 0.95 * len(steps),
      "%.0f%%" % (100.0 * smooth / len(steps)) if steps else "no frames")
check("no freeze longer than three frames", longest <= 3, "%d frames" % longest)

print("\n%s" % ("PACING OK" if not fails else "PACING FAILED: " + "; ".join(fails)))
sys.exit(1 if fails else 0)
