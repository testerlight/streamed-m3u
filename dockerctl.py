"""Docker Engine API helper for the console Restart control.

The service cannot reach the LAN (it shares gluetun's network), but a Unix
socket is not LAN traffic. Mount `/var/run/docker.sock` read-only and the
entrypoint grants the runtime user the socket's group so this module can
talk to the daemon.

Only the names in RESTART_CONTAINERS can be targeted; the request body is
never consulted. This process used to SIGTERM PID 1 (tini) to bounce
itself; that is EPERM after the entrypoint drops to PUID, because tini
stays root. Both this container and the others go through the Engine
`/restart` endpoint. The HTTP 202 leaves first; the self-restart runs on
a background thread so the daemon killing us is not a deadlock.
"""

import http.client
import logging
import os
import socket
import threading
import time
from urllib.parse import quote

log = logging.getLogger("dockerctl")

SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
SELF_CONTAINER = os.getenv("SELF_CONTAINER", "streamed-m3u").strip()


def _parse_names(raw: str) -> list:
    return [n.strip() for n in (raw or "").split(",") if n.strip()]


CONTAINERS = _parse_names(
    os.getenv("RESTART_CONTAINERS", "streamed-m3u,streamed-m3u-sync")
)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


def available() -> bool:
    """True when the socket exists and this process can read and write it."""
    return os.path.exists(SOCKET) and os.access(SOCKET, os.R_OK | os.W_OK)


def describe() -> dict:
    return {
        "available": available(),
        "socket": SOCKET,
        "containers": list(CONTAINERS),
        "self": SELF_CONTAINER,
    }


def _request(method: str, path: str, timeout: float = 30):
    conn = _UnixHTTPConnection(SOCKET, timeout=timeout)
    try:
        conn.request(method, path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, body
    finally:
        conn.close()


def restart_one(name: str, timeout: int = 10) -> dict:
    """Restart one allowlisted container by name. Never accepts a caller name
    that is not in RESTART_CONTAINERS.

    Restarting ourselves cuts the Engine connection as the container dies;
    that interrupt is success, not a failure.
    """
    if name not in CONTAINERS:
        return {"name": name, "ok": False, "error": "not_allowlisted"}
    if not available():
        return {"name": name, "ok": False, "error": "docker_unavailable"}
    path = "/containers/%s/restart?t=%d" % (quote(name, safe=""), timeout)
    try:
        status, body = _request("POST", path, timeout=timeout + 15)
    except OSError as e:
        if name == SELF_CONTAINER:
            log.info("Restart of %s interrupted (container exiting): %s", name, e)
            return {"name": name, "ok": True, "interrupted": True}
        log.warning("Docker restart of %s failed: %s", name, e)
        return {"name": name, "ok": False, "error": str(e)}
    if status in (204, 200):
        log.info("Restarted container %s", name)
        return {"name": name, "ok": True}
    snippet = body.decode("utf-8", "replace")[:200] if body else ""
    log.warning("Docker restart of %s returned %s %s", name, status, snippet)
    return {"name": name, "ok": False, "error": "http_%s" % status,
            "detail": snippet}


def exit_self() -> None:
    """Last resort if the Engine call cannot be issued. This uid cannot
    signal tini, but it can end its own process; tini then exits and
    Docker's restart policy starts us again."""
    log.info("Exiting this process so Docker can restart the container")
    os._exit(0)


def restart_services(self_exit=None, delay: float = 0.6) -> dict:
    """Restart the allowlisted containers.

    Others go through the Engine API immediately. This container is
    restarted on a background thread after `delay` seconds so the HTTP
    202 can leave first. `self_exit` is injectable so the verification
    harness never touches Docker or this process.
    """
    if not available():
        return {
            "ok": False,
            "error": "docker_unavailable",
            "message": "The Docker socket is not mounted, so services cannot "
                       "be restarted from the console.",
            "restart": describe(),
        }

    results = []
    for name in CONTAINERS:
        if name == SELF_CONTAINER:
            continue
        results.append(restart_one(name))

    def _later():
        time.sleep(delay)
        if self_exit is not None:
            try:
                self_exit()
            except OSError as e:
                log.error("Could not run injected self-exit: %s", e)
            return
        result = restart_one(SELF_CONTAINER)
        if not result.get("ok"):
            log.error("Engine restart of %s failed (%s); exiting instead",
                      SELF_CONTAINER, result.get("error"))
            exit_self()

    threading.Thread(target=_later, name="self-restart", daemon=True).start()
    return {
        "ok": True,
        "restarting": list(CONTAINERS),
        "results": results,
        "restart": describe(),
    }
