"""Pull one real stream through a scratch image and report its health. Not shipped.

    docker run --rm --network container:gluetun -e STARTUP_DELAY=0 \
        -v $PWD/tools/check_health_live.py:/tmp/live.py:ro <image> \
        python /tmp/live.py <embed-url> [seconds]

Joins gluetun's namespace so it sees the same upstream path as production,
but binds no port: the app runs in-process behind Flask's test client, with
its own throwaway /data, so it cannot collide with the running service.
It does share the VPN with anyone watching; check nothing is playing first.

Reports the health snapshot every 15s, the history record at the end, and
whether the captured MPEG-TS kept its 188-byte alignment throughout.
"""

import json
import os
import sys
import tempfile
import threading
import time
from urllib.parse import quote

EMBED = sys.argv[1]
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 150

DATA = tempfile.mkdtemp(prefix="live-")
for name, leaf in (("TEAMS_FILE", "teams.json"), ("LINEUP_FILE", "lineup.json"),
                   ("EXTRACT_CACHE_FILE", "extract_cache.json"),
                   ("SETTINGS_FILE", "settings.json"),
                   ("STREAM_HISTORY_FILE", "stream_history.jsonl")):
    os.environ[name] = os.path.join(DATA, leaf)
os.environ.pop("CONSOLE_PASSWORD", None)
sys.path.insert(0, "/app")
import app  # noqa: E402

done = threading.Event()


def sample():
    c = app.app.test_client()
    while not done.wait(15):
        st = c.get("/stream/status").get_json() or {}
        for s in st.get("streams", []):
            h = s.get("health") or {}
            print("  t=%4.0fs %-10s speed %-6s link %-5s Mbit/s video %-4s Mbit/s cushion %-5s "
                  "behind %-5s freezes %s (%ss) %s" % (
                      s["elapsed_seconds"], h.get("state"), h.get("speed"), h.get("link_mbit"),
                      h.get("video_mbit"), h.get("cushion_s"), h.get("behind_live_s"),
                      h.get("freezes"), h.get("frozen_s"), h.get("reason") or ""), flush=True)


threading.Thread(target=sample, daemon=True).start()
client = app.app.test_client()
t0 = time.time()
resp = client.get("/stream?url=" + quote(EMBED, safe=""), buffered=False)
print("HTTP %s after %.1fs" % (resp.status_code, time.time() - t0), flush=True)
buf = bytearray()
keepalives = 0
try:
    for item in resp.response:
        if item == app.TS_NULL_PACKET:
            keepalives += 1
        buf += item
        if time.time() - t0 > SECONDS:
            break
finally:
    resp.close()
    done.set()

app.streamhealth.HISTORY.flush()
recs = app.streamhealth.HISTORY.read(5)
print("\ncaptured %.1f MB, %d keepalive packets" % (len(buf) / 1e6, keepalives))

# Alignment: the first offset where 0x47 holds for 10 strides, then every
# 188 bytes from there. Null packets are valid TS, so they keep the stride.
off = next((i for i in range(min(len(buf), 188 * 50))
            if all(i + k * 188 < len(buf) and buf[i + k * 188] == 0x47 for k in range(10))), None)
if off is None:
    print("TS: no alignment found")
else:
    breaks = 0
    i = off
    while i < len(buf):
        if buf[i] != 0x47:
            breaks += 1
            nxt = next((j for j in range(i, min(len(buf), i + 188 * 4))
                        if buf[j] == 0x47 and (j + 188 >= len(buf) or buf[j + 188] == 0x47)), None)
            if nxt is None:
                break
            i = nxt
            continue
        i += 188
    print("TS: aligned at offset %d, %d packets, %d sync breaks" % (off, (len(buf) - off) // 188, breaks))

if recs:
    r = recs[0]
    keep = ("channel", "source", "seconds", "first_chunk_s", "chunks", "video_s", "freezes",
            "frozen_s", "by_cause", "blips", "blip_s", "player_waits", "speed_median",
            "speed_p10", "link_mbit_median", "link_mbit_p10", "video_mbit",
            "behind_live_median_s", "behind_live_max_s", "variant", "variants",
            "target_duration", "durations_guessed", "skipped", "backwards",
            "playlist_backwards", "discontinuities", "playlist_errors", "downloads_failed",
            "internal_errors", "end_reason", "freeze_list", "playlist_fetch", "recoveries",
            "detector")
    print("\nhistory record:")
    print(json.dumps({k: r.get(k) for k in keep}, indent=1))
else:
    print("no history record")
