"""Audio/video offset after a hole (stream-recovery ticket 01). Not shipped.

Builds a TS stream whose picture flashes and whose audio beeps at the same
instants every 2s, cuts it into 6s chunks the way an HLS source does, drops
some chunks (a hole), and transcodes what is left with ffmpeg the way
Jellyfin does. It then reads the output back and prints how far the beep
trails the flash before and after the hole. With a flag, the first chunk
after the hole carries a TS discontinuity indicator on every elementary
stream (see `mark()`; the prototype of what the ticket asks for).

Run inside a scratch-tag image (it has ffmpeg), never against a live source:

    docker run --rm -e HOLES=3 [-e MARK=1] --entrypoint python \
        -v $PWD/tools/check_hole_av.py:/tmp/h.py:ro -v /tmp/holeav:/w \
        <image> /tmp/h.py /w

HOLES is a comma list of chunk numbers to drop ("none" for a control run).
MARK=1 runs each transcode a second time with the flag. RESTAMP=1 instead
shifts every later chunk's PTS/DTS/PCR back by the hole's length, so the
timeline has no jump at all. A real capture can
stand in for the synthetic one: put its chunks in /w/src/seg000.ts,
seg001.ts, ... (flashes and beeps are then not known, so the offsets mean
nothing; only the synthetic source can be measured).
"""

import collections
import glob
import os
import re
import subprocess
import sys

W = sys.argv[1] if len(sys.argv) > 1 else "/w"
CHUNK, TOTAL = 6, 72
_h = os.environ.get("HOLES", "3")
RESTAMP = bool(os.environ.get("RESTAMP"))
HOLES = [] if _h == "none" else [int(x) for x in _h.split(",")]


def run(args):
    r = subprocess.run(args, capture_output=True)
    if r.returncode:
        sys.stderr.write(r.stderr.decode()[-2000:])
        raise SystemExit(1)
    return r


def mark(b, scan=2000):
    """Before the chunk's first packet of each elementary-stream PID, insert
    an adaptation-only packet carrying discontinuity_indicator. Nothing in the
    chunk is rewritten and it stays 188-aligned."""
    pids = {}
    for i in range(min(len(b) // 188, scan)):
        p = b[i * 188:(i + 1) * 188]
        if p[0] != 0x47:
            return b
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        if pid in pids or pid < 0x20 or pid == 0x1FFF or not afc & 1 or not p[1] & 0x40:
            continue
        off = 4 + (1 + p[4] if afc & 2 else 0)
        if p[off:off + 3] == b"\x00\x00\x01":
            pids[pid] = p[3] & 0x0F
    out = bytearray()
    for pid, cc in pids.items():
        pkt = bytearray(b"\xff" * 188)
        pkt[0:6] = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x20 | ((cc - 1) & 0x0F), 183, 0x80])
        out += pkt
    return bytes(out) + bytes(b)


M33 = (1 << 33) - 1

def enc_ts(prefix, v):
    v &= M33
    return bytes([(prefix << 4) | (((v >> 30) & 7) << 1) | 1, (v >> 22) & 0xFF,
                  (((v >> 15) & 0x7F) << 1) | 1, (v >> 7) & 0xFF, ((v & 0x7F) << 1) | 1])

def dec_ts(b):
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | (b[4] >> 1)

def shift(b, delta):
    """Move every PTS, DTS and PCR in a chunk by delta (90 kHz ticks)."""
    b = bytearray(b)
    for i in range(0, len(b) - 187, 188):
        if b[i] != 0x47: continue
        afc = (b[i+3] >> 4) & 3
        off = i + 4
        if afc & 2:
            alen = b[i+4]
            if alen >= 7 and b[i+5] & 0x10:      # PCR present
                base = (b[i+6] << 25) | (b[i+7] << 17) | (b[i+8] << 9) | (b[i+9] << 1) | (b[i+10] >> 7)
                base = (base + delta) & M33
                b[i+6] = (base >> 25) & 0xFF; b[i+7] = (base >> 17) & 0xFF
                b[i+8] = (base >> 9) & 0xFF;  b[i+9] = (base >> 1) & 0xFF
                b[i+10] = ((base & 1) << 7) | (b[i+10] & 0x7F)
            off = i + 5 + alen
        if afc & 1 and b[i+1] & 0x40 and b[off:off+3] == b"\x00\x00\x01":
            flags = b[off+7] >> 6
            if flags & 2:
                p = off + 9
                pts = dec_ts(b[p:p+5]) + delta
                b[p:p+5] = enc_ts(3 if flags == 3 else 2, pts)
                if flags == 3:
                    d = dec_ts(b[p+5:p+10]) + delta
                    b[p+5:p+10] = enc_ts(1, d)
    return bytes(b)

def make_source():
    d = os.path.join(W, "src")
    os.makedirs(d, exist_ok=True)
    if glob.glob(d + "/seg*.ts"):
        return
    beep = "sin(2*PI*1000*t)*lt(mod(t,2),0.2)"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25",
         "-f", "lavfi", "-i", "aevalsrc='%s|%s':s=48000" % (beep, beep),
         "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,2),0.2)'",
         "-t", str(TOTAL), "-c:v", "libx264", "-preset", "veryfast",
         "-g", str(CHUNK * 25), "-keyint_min", str(CHUNK * 25), "-sc_threshold", "0",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
         "-f", "segment", "-segment_time", str(CHUNK), "-segment_format", "mpegts",
         d + "/seg%03d.ts"])


def build(path, flag):
    out = bytearray()
    gone = 0
    for i, s in enumerate(sorted(glob.glob(W + "/src/seg*.ts"))):
        if i in HOLES:
            gone += 1
            continue
        b = open(s, "rb").read()
        if RESTAMP and gone:
            b = shift(b, -gone * CHUNK * 90000)
        if flag and HOLES and i == HOLES[-1] + 1:
            b = mark(b)
        out += b
    open(path, "wb").write(out)


def events(path, kind):
    """Onset times (s) of flashes (video) or beeps (audio) in a media file."""
    if kind == "v":
        run(["ffmpeg", "-y", "-i", path, "-map", "0:v", "-vf",
             "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=" + W + "/v.txt",
             "-f", "null", "-"])
        txt, key, on = open(W + "/v.txt").read(), "YAVG", lambda v: v > 128
    else:
        run(["ffmpeg", "-y", "-i", path, "-map", "0:a", "-af",
             "asetnsamples=n=480:p=0,astats=metadata=1:reset=1,"
             "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=" + W + "/a.txt",
             "-f", "null", "-"])
        txt, key, on = open(W + "/a.txt").read(), "RMS_level", lambda v: v > -30
    pts = [float(x) for x in re.findall(r"pts_time:([-\d.]+)", txt)]
    val = [-120.0 if x in ("-inf", "nan") else float(x) for x in re.findall(key + r"=(\S+)", txt)]
    ev, prev = [], False
    for p, v in zip(pts, val):
        if on(v) and not prev:
            ev.append(round(p, 3))
        prev = on(v)
    return ev


def analyse(label, path):
    v, a = events(path, "v"), events(path, "a")
    exp = [t for t in range(0, TOTAL, 2) if t // CHUNK not in HOLES]
    n = min(len(v), len(a), len(exp))
    after = (HOLES[-1] + 1) * CHUNK if HOLES else TOTAL
    pre = sorted({round(a[k] - v[k], 2) for k in range(n) if exp[k] < (HOLES[0] * CHUNK if HOLES else TOTAL)})
    post = sorted({round(a[k] - v[k], 2) for k in range(n) if exp[k] >= after})
    print("%-34s flashes=%d beeps=%d of %d | A-V before hole %s | after %s" %
          (label, len(v), len(a), len(exp), pre, post))


JELLY = ["-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", "-ac", "2", "-b:a", "128k"]
HLS = ["-f", "hls", "-hls_time", "6", "-hls_segment_type", "mpegts", "-hls_list_size", "0",
       "-hls_playlist_type", "event"]
CP = ["-copyts", "-avoid_negative_ts", "disabled", "-max_muxing_queue_size", "2048"]
GP = ["-fflags", "+genpts", "-i", "{in}"]
MODES = {
    "plain":      ["-i", "{in}"] + JELLY + ["-f", "mpegts", "{out}.ts"],
    "copyts":     GP + JELLY + CP + ["-f", "mpegts", "{out}.ts"],
    "hls_cfr":    GP + JELLY + ["-fps_mode", "cfr"] + HLS + ["{out}.m3u8"],
    "hls_copyts": GP + JELLY + CP + ["-fps_mode", "cfr"] + HLS + ["{out}.m3u8"],
    "hls_igndts": ["-fflags", "+genpts+igndts", "-i", "{in}"] + JELLY + CP + HLS + ["{out}.m3u8"],
    "hls_async":  GP + JELLY + ["-af", "aresample=async=1:first_pts=0"] + HLS + ["{out}.m3u8"],
}

if __name__ == "__main__":
    make_source()
    print("holes (chunk numbers, %ds each): %s" % (CHUNK, HOLES or "none"))
    for tag, flag in (("no flag", False), ("flag", True)) if os.environ.get("MARK") else (("no flag", False),):
        p = W + "/in_%s.ts" % tag.replace(" ", "")
        build(p, flag)
        for m, args in MODES.items():
            out = W + "/out_%s_%s" % (tag.replace(" ", ""), m)
            run(["ffmpeg", "-y"] + [x.replace("{in}", p).replace("{out}", out) for x in args])
            analyse("%s / %s" % (tag, m), out + (".m3u8" if "hls" in m else ".ts"))
        if flag:
            r = subprocess.run(["ffmpeg", "-v", "warning", "-i", p, "-f", "null", "-"], capture_output=True)
            print("demuxer warnings with flag: %d" % len(r.stderr.decode().splitlines()))
