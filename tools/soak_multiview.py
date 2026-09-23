#!/usr/bin/env python3
"""Multi-view soak through Dispatcharr, run on the HOST. Not shipped.

Plays a Multi-Player channel exactly the way Jellyfin does - through
Dispatcharr's TS proxy - for as long as asked, optionally with an ordinary
channel playing alongside, optionally stepping through every control, and
reports what matters for the Phase 12 gate:

  - the longest gap the viewer saw, and whether the stream ever ended
  - Dispatcharr's own verdict: "unhealthy", forced switches, read timeouts
  - encoder exits and re-attaches, from streamed-m3u's log
  - CPU (the streamed-m3u container, and host load) and GPU (Video engine)

    python3 tools/soak_multiview.py --minutes 12 --controls \\
        --secondary-swap arizona-diamondbacks --primary-swap athletics \\
        --alongside "San Diego Padres"

    python3 tools/soak_multiview.py --minutes 200          # a full game

Needs: docker and intel_gpu_top (both on the TrueNAS host). --controls
drives the console API through tools/multiview_api.py, piped into the
container, so the console password never leaves it. Dispatcharr's /output/m3u is read to
find channel URLs, so no Dispatcharr login is used at all - its token
endpoint rate-limits.
"""

import argparse
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

DISPATCHARR = "http://127.0.0.1:9191"
MVAPI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "multiview_api.py")


def channel_urls():
    """name and tvg-id -> Dispatcharr proxy URL, from its public M3U output."""
    body = urllib.request.urlopen(DISPATCHARR + "/output/m3u", timeout=30).read().decode()
    out, meta = {}, None
    for line in body.splitlines():
        if line.startswith("#EXTINF"):
            meta = line
        elif line.startswith("http") and meta:
            name = meta.rsplit(",", 1)[-1].strip()
            out[name] = line.strip()
            m = re.search(r'tvg-id="([^"]+)"', meta)
            if m:
                out[m.group(1)] = line.strip()
            meta = None
    return out


class Viewer(threading.Thread):
    def __init__(self, label, url):
        super().__init__(daemon=True)
        self.label, self.url = label, url
        self.stop = threading.Event()
        self.bytes = 0
        self.first = None
        self.last = None
        self.max_gap = 0.0
        self.max_gap_at = None
        self.ended = None
        self.started = time.time()

    def run(self):
        try:
            with urllib.request.urlopen(self.url, timeout=120) as r:
                while not self.stop.is_set():
                    chunk = r.read1(65536)
                    now = time.time()
                    if not chunk:
                        self.ended = "stream ended at %.0fs" % (now - self.started)
                        return
                    if self.first is None:
                        self.first = now - self.started
                    elif now - self.last > self.max_gap:
                        self.max_gap = now - self.last
                        self.max_gap_at = now - self.started
                    self.last = now
                    self.bytes += len(chunk)
        except Exception as e:  # noqa: BLE001
            self.ended = "error at %.0fs: %s" % (time.time() - self.started, str(e)[:120])

    def summary(self):
        dur = (self.last or time.time()) - self.started
        return ("%-22s first byte %5.1fs | %6.1f MB | ~%.2f Mbit/s | longest gap %.1fs%s | %s"
                % (self.label, self.first or -1, self.bytes / 1e6,
                   self.bytes * 8 / max(dur, 1) / 1e6, self.max_gap,
                   (" at %.0fs" % self.max_gap_at) if self.max_gap_at else "",
                   self.ended or "still playing when stopped"))


class Sampler(threading.Thread):
    """CPU of the streamed-m3u container and host load, every `every` s."""
    def __init__(self, every=10):
        super().__init__(daemon=True)
        self.every = every
        self.stop = threading.Event()
        self.cpu, self.load = [], []

    def run(self):
        while not self.stop.is_set():
            r = subprocess.run(["docker", "stats", "--no-stream", "--format",
                                "{{.CPUPerc}}", "streamed-m3u"],
                               capture_output=True, text=True)
            try:
                self.cpu.append(float(r.stdout.strip().rstrip("%")))
            except ValueError:
                pass
            self.load.append(float(open("/proc/loadavg").read().split()[0]))
            self.stop.wait(self.every)


def gpu_video_busy(path):
    """Mean and peak Video-engine busy % from an intel_gpu_top -J log."""
    raw = open(path).read()
    vals = [float(v) for v in re.findall(
        r'"Video/0":\s*\{\s*"busy":\s*([0-9.]+)', raw)]
    return (sum(vals) / len(vals), max(vals), len(vals)) if vals else (None, None, 0)


def mv(*args):
    r = subprocess.run(["docker", "exec", "-i", "streamed-m3u", "python", "-"] + list(args),
                       stdin=open(MVAPI), capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (r.stdout + r.stderr).strip()[-200:]}


def controls(log, secondary_swap, primary_swap):
    """Every control once, mid-game, with time between for the picture to settle."""
    steps = [("corner " + c, {"corner": c}, 25) for c in ("tl", "tr", "bl", "br")]
    steps += [("size " + z, {"size": z}, 25) for z in ("small", "medium", "large")]
    for label, a in (("sound: main only", (100, 0)), ("sound: miniplayer only", (0, 100)),
                     ("sound: both", (100, 35)), ("sound: none", (0, 0)),
                     ("sound: back to main", (100, 0))):
        steps.append((label, {"audio": {"primary": a[0], "secondary": a[1]}}, 20))
    if secondary_swap:
        steps.append(("swap miniplayer -> " + secondary_swap, {"secondary": secondary_swap}, 75))
    if primary_swap:
        steps.append(("swap main -> " + primary_swap, {"primary": primary_swap}, 75))
    for label, change, settle in steps:
        t = time.strftime("%H:%M:%S")
        res = mv("put", json.dumps(change))
        ch = res.get("changed") or {}
        log.append("%s  %-30s live=%s layout=%s audio=%s rebuilt=%s %s" % (
            t, label, ch.get("running"), ch.get("layout"), ch.get("audio"),
            ch.get("rebuilt"), res.get("error", "")))
        print(log[-1], flush=True)
        time.sleep(settle)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="Multi-Player 1",
                    help="channel name as Dispatcharr outputs it")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--controls", action="store_true")
    ap.add_argument("--secondary-swap")
    ap.add_argument("--primary-swap")
    ap.add_argument("--alongside", help="an ordinary channel to play at the same time")
    args = ap.parse_args()

    urls = channel_urls()
    if args.channel not in urls:
        raise SystemExit("channel %r not in Dispatcharr's output" % args.channel)
    started = time.time()
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    gpu_log = "/tmp/soak_gpu_%d.json" % int(started)
    gpu = subprocess.Popen(["intel_gpu_top", "-J", "-s", "5000", "-o", gpu_log],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sampler = Sampler()
    sampler.start()
    viewers = [Viewer("Multi-Player", urls[args.channel])]
    if args.alongside:
        viewers.append(Viewer(args.alongside, urls[args.alongside]))
    for v in viewers:
        v.start()

    log = []
    deadline = started + args.minutes * 60
    if args.controls:
        # Let the composite reach picture before touching it.
        while viewers[0].first is None and time.time() < deadline:
            time.sleep(2)
        time.sleep(30)
        controls(log, args.secondary_swap, args.primary_swap)
    while time.time() < deadline and viewers[0].ended is None:
        time.sleep(5)

    for v in viewers:
        v.stop.set()
    sampler.stop.set()
    gpu.terminate()
    time.sleep(2)

    dlog = subprocess.run(["docker", "logs", "--since", since, "dispatcharr"],
                          capture_output=True, text=True)
    dtext = dlog.stdout + dlog.stderr
    slog = subprocess.run(["docker", "logs", "--since", since, "streamed-m3u"],
                          capture_output=True, text=True)
    stext = slog.stdout + slog.stderr

    def count(text, pattern):
        return len(re.findall(pattern, text, re.I))

    mean_gpu, peak_gpu, n_gpu = gpu_video_busy(gpu_log)
    print("\n=== soak: %.1f minutes ===" % ((time.time() - started) / 60))
    for v in viewers:
        print(v.summary())
    print("Dispatcharr   unhealthy %d | switch %d | read timeouts %d"
          % (count(dtext, r"unhealthy"), count(dtext, r"switching to|switched to|forced switch"),
             count(dtext, r"Read timed out")))
    print("streamed-m3u  encoder gone %d | rebuilds %d | stalled inputs cut %d | commands failed %d"
          % (count(stext, r"encoder gone"), count(stext, r"\[rebuild \d+\]"),
             count(stext, r"stalled|no data for"), count(stext, r"control .* failed")))
    if sampler.cpu:
        print("CPU           streamed-m3u mean %.0f%% peak %.0f%% (100%% = one core) | host load mean %.2f peak %.2f"
              % (sum(sampler.cpu) / len(sampler.cpu), max(sampler.cpu),
                 sum(sampler.load) / len(sampler.load), max(sampler.load)))
    if n_gpu:
        print("GPU           Video engine mean %.0f%% peak %.0f%% (%d samples)" % (mean_gpu, peak_gpu, n_gpu))
    if log:
        print("controls:\n  " + "\n  ".join(log))


if __name__ == "__main__":
    main()
