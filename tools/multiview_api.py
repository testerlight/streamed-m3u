"""Console API helper for the Phase 12 live test. Runs INSIDE the streamed-m3u
container (docker exec -i streamed-m3u python - <args>), so the console
password is read from that container's environment and never leaves it.

    python - get                         print the slot 1 snapshot (compact)
    python - put '<json>' [slot]         PUT /api/multiview/<slot>
    python - stop [slot]                 POST /api/multiview/<slot>/stop
"""
import json
import os
import sys

import requests

BASE = "http://127.0.0.1:8787"
s = requests.Session()
s.post(BASE + "/login", data={"password": os.environ["CONSOLE_PASSWORD"], "next": "/"},
       timeout=15)
token = (s.get(BASE + "/api/session", timeout=15).json() or {}).get("csrf_token", "")
H = {"X-CSRF-Token": token, "Content-Type": "application/json"}


def compact(snap, slot="1"):
    for sl in snap.get("slots") or []:
        if sl["id"] != slot:
            continue
        run = sl.get("running") or {}
        out = {
            "primary": (sl["primary"]["slug"], sl["primary"]["warm"], sl["primary"]["status"]),
            "secondary": (sl["secondary"]["slug"], sl["secondary"]["warm"], sl["secondary"]["status"]),
            "corner": sl["corner"], "size": sl["size"], "audio": sl["audio"],
            "configured": sl["configured"],
        }
        if run:
            out["running"] = {k: run.get(k) for k in (
                "uptime_seconds", "startup_seconds", "viewers", "mbit_per_s", "encoder",
                "commands_sent", "commands_failed")}
            out["inputs"] = {r: (i["slug"], i["stalls"], i["source_errors"])
                             for r, i in (run.get("inputs") or {}).items()}
        if "changed" in snap:
            out["changed"] = snap["changed"]
        return out


cmd = sys.argv[1] if len(sys.argv) > 1 else "get"
slot = sys.argv[3] if cmd == "put" and len(sys.argv) > 3 else (
    sys.argv[2] if cmd == "stop" and len(sys.argv) > 2 else "1")
if cmd == "get":
    r = s.get(BASE + "/api/multiview", timeout=15)
elif cmd == "put":
    r = s.put(BASE + "/api/multiview/" + slot, data=sys.argv[2], headers=H, timeout=30)
elif cmd == "stop":
    r = s.post(BASE + "/api/multiview/%s/stop" % slot, data="{}", headers=H, timeout=60)
else:
    sys.exit("unknown command")
body = r.json()
if r.status_code != 200:
    print(r.status_code, body)
    sys.exit(1)
print(json.dumps(compact(body, slot)))
