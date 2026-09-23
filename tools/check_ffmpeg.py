"""Verification harness for the multi-view composite capability. Not shipped.

Run inside a container built from this directory, with the script bind mounted
and the render node attached:

    RGID=$(stat -c '%g' /dev/dri/renderD128)
    docker run --rm --device /dev/dri --group-add "$RGID" \
        -v $PWD/tools/check_ffmpeg.py:/tmp/check_ffmpeg.py:ro \
        <image> python /tmp/check_ffmpeg.py

Add `--user 568:568` to check the capability as the service actually runs it
rather than as root, which is the failure this catches most often.

Checks only what the composite needs (see docs/internal/PENDING_multiview.md):
the binary, the filters that must accept *live* commands, the VAAPI encoder,
pyzmq, and whether the render node is usable by the current user.

The live-command checks are the point of this script. overlay_qsv and vpp_qsv
accept commands and silently ignore them, so "no error" proves nothing; each
command check runs the graph twice, identically except for the command, and
compares output digests. Identical output means the command was ignored. A
future ffmpeg or driver change that quietly removes command support would
otherwise surface as a dead control in the console with nothing in the logs.

Without a usable render node the hardware checks report SKIP and the script
still exits 0, so it stays useful on a box with no iGPU.
"""

import os
import shutil
import subprocess
import sys

RENDER_NODE = os.environ.get("RENDER_NODE", "/dev/dri/renderD128")
QP = "23"

fails = []
skips = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  [%s]" % detail) if detail != "" else ""))
    if not cond:
        fails.append(name)


def skip(name, why):
    print("SKIP %s  [%s]" % (name, why))
    skips.append(name)


def section(title):
    print("\n=== %s ===" % title)


def run(args, timeout=120):
    """Return (rc, stdout+stderr). Never raises; a timeout is a failure."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ss" % timeout
    except Exception as exc:                                  # noqa: BLE001
        return 1, repr(exc)


def ff(*args, **kw):
    return run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
               + list(args), **kw)


def digest(args):
    """Run an ffmpeg graph to the md5 muxer and return its digest, or None.

    rawvideo keeps this independent of encoder determinism: the comparison is
    of decoded pixels, not of a compressed bitstream.
    """
    rc, out = ff(*(list(args) + ["-c:v", "rawvideo", "-f", "md5", "-"]))
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("MD5="):
            return line.strip()
    return None


# ─── Binaries ─────────────────────────────────────────────────────────────────
section("binaries")
for tool in ("ffmpeg", "ffprobe"):
    check("%s on PATH" % tool, shutil.which(tool) is not None)

rc, out = run(["ffmpeg", "-hide_banner", "-version"])
version = out.splitlines()[0] if rc == 0 and out.splitlines() else "unavailable"
check("ffmpeg reports a version", rc == 0, version)

rc, pyzmq_out = run([sys.executable, "-c",
                     "import zmq; print(zmq.__version__, zmq.zmq_version())"])
check("pyzmq importable", rc == 0, pyzmq_out.strip() or "missing")

print("\nrunning as uid=%d gid=%d groups=%s" % (os.getuid(), os.getgid(), os.getgroups()))


# ─── Filters and encoders ─────────────────────────────────────────────────────
section("filters and encoders")
rc, filters = run(["ffmpeg", "-hide_banner", "-filters"])
rc2, encoders = run(["ffmpeg", "-hide_banner", "-encoders"])


def has_filter(name):
    return any(line.split()[1:2] == [name] for line in filters.splitlines())


# scale/overlay/volume carry the composite; zmq is how it is steered live;
# amix sums the two audio branches for the "Both" mixer preset.
for name in ("scale", "overlay", "volume", "amix", "zmq", "azmq",
             "sendcmd", "asendcmd", "hwupload", "hwdownload"):
    check("filter %s" % name, has_filter(name))

check("encoder h264_vaapi", "h264_vaapi" in encoders)
check("encoder aac", " aac " in encoders)

# Informational only. QSV is faster but its filters are not commandable, so the
# composite does not use it; this line exists so a future reader knows whether
# the option was present and rejected, or simply absent.
print("note: h264_qsv %s (unused: QSV filters ignore live commands)"
      % ("present" if "h264_qsv" in encoders else "absent"))


# ─── Live command support ─────────────────────────────────────────────────────
# The feature's controls are: miniplayer corner (overlay x/y), miniplayer size
# (scale w/h) and the audio mixer (volume). Each must be changeable on a
# running graph. Prove it by effect, not by absence of an error.
section("live command support (software filters)")

SRC_MAIN = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:d=2"]
SRC_PIP = ["-f", "lavfi", "-i", "testsrc2=size=80x60:rate=10:d=2"]

base_overlay = "[0:v][1:v]overlay=x=200:y=150"
cmd_overlay = ("[0:v]sendcmd=c='1.0 overlay x 0'[m];[m][1:v]overlay=x=200:y=150")

d_base = digest(SRC_MAIN + SRC_PIP + ["-filter_complex", base_overlay])
d_cmd = digest(SRC_MAIN + SRC_PIP + ["-filter_complex", cmd_overlay])
check("overlay x is commandable",
      d_base is not None and d_cmd is not None and d_base != d_cmd,
      "identical output means the command was ignored" if d_base == d_cmd else "")

base_scale = "[1:v]scale=80:60[p];[0:v][p]overlay=x=200:y=150"
cmd_scale = ("[1:v]sendcmd=c='1.0 scale width 40',scale=80:60[p];"
             "[0:v][p]overlay=x=200:y=150")
d_base = digest(SRC_MAIN + SRC_PIP + ["-filter_complex", base_scale])
d_cmd = digest(SRC_MAIN + SRC_PIP + ["-filter_complex", cmd_scale])
check("scale width is commandable",
      d_base is not None and d_cmd is not None and d_base != d_cmd,
      "identical output means the command was ignored" if d_base == d_cmd else "")

SINE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]


def audio_digest(af):
    rc, out = ff(*(SINE + ["-af", af, "-f", "md5", "-"]))
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("MD5="):
            return line.strip()
    return None


a_base = audio_digest("volume=1.0")
a_cmd = audio_digest("asendcmd=c='1.0 volume volume 0.2',volume=1.0")
check("volume is commandable",
      a_base is not None and a_cmd is not None and a_base != a_cmd,
      "identical output means the command was ignored" if a_base == a_cmd else "")

# The zmq filter is the transport those commands arrive on in production.
# Initialising it proves libzmq is built in and the bind succeeds.
rc, out = ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=5:d=1",
             "-vf", "zmq", "-f", "null", "-", timeout=60)
check("zmq filter initialises", rc == 0, out.strip().splitlines()[-1] if rc else "")


# ─── Hardware ─────────────────────────────────────────────────────────────────
section("hardware (VAAPI)")
if not os.path.exists(RENDER_NODE):
    skip("render node present", "%s missing, pass --device /dev/dri" % RENDER_NODE)
    skip("render node readable as this user", "no render node")
    skip("h264_vaapi encodes", "no render node")
    skip("composite graph runs end to end", "no render node")
elif not os.access(RENDER_NODE, os.R_OK | os.W_OK):
    check("render node present", True, RENDER_NODE)
    # The symptom this catches is misleading: ffmpeg reports "No VA display
    # found", not a permission error. The fix is group_add with the host's
    # render gid on the service, not a change to the host user.
    check("render node readable as this user", False,
          "uid=%d lacks access to %s; add the host render gid via group_add"
          % (os.getuid(), RENDER_NODE))
    skip("h264_vaapi encodes", "render node not accessible")
    skip("composite graph runs end to end", "render node not accessible")
else:
    check("render node present", True, RENDER_NODE)
    check("render node readable as this user", True, "uid=%d" % os.getuid())

    # CQP, not -b:v: the free iHD driver supports no other rate control here.
    rc, out = ff("-vaapi_device", RENDER_NODE,
                 "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:d=2",
                 "-vf", "format=nv12,hwupload",
                 "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", QP,
                 "-f", "null", "-")
    check("h264_vaapi encodes (CQP)", rc == 0,
          out.strip().splitlines()[-1] if rc else "qp=%s" % QP)

    # The real shape: two inputs, software scale+overlay between hwdownload and
    # hwupload, VAAPI encode, muxed to TS. Synthetic sources, real graph.
    graph = ("[0:v]format=yuv420p[m];"
             "[1:v]format=yuv420p,scale=576:324[p];"
             "[m][p]overlay=x=W-w-32:y=H-h-32,format=nv12,hwupload[v]")
    # Muxed to a file, not to stdout: TS is binary and would be captured as
    # such, and the point here is that the graph completes, not the bytes.
    rc, out = ff("-vaapi_device", RENDER_NODE,
                 "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:d=3",
                 "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:d=3",
                 "-filter_complex", graph, "-map", "[v]",
                 "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", QP,
                 "-y", "-f", "mpegts", os.devnull, timeout=180)
    check("composite graph runs end to end", rc == 0,
          out.strip().splitlines()[-1] if rc else "1080p PiP, VAAPI encode")


# ─── Result ───────────────────────────────────────────────────────────────────
print("\n%d checks failed, %d skipped" % (len(fails), len(skips)))
if fails:
    print("failed: %s" % ", ".join(fails))
sys.exit(1 if fails else 0)
