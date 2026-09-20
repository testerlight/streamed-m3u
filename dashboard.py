"""
dashboard: the operations console for streamed-m3u.

Serves the HTML page at `/` plus the JSON endpoints the page polls. Read-only
by default; with CONSOLE_PASSWORD set (see auth.py) a signed-in session can
also change settings.

The page consumes the pre-existing diagnostic endpoints (`/health`, `/teams`,
`/prewarm`, `/stream/status`) directly, so those keep their exact shapes and
stay usable from curl. The endpoints here cover what had no endpoint at all:

  GET /api/overview   Aggregate first-paint payload: uptime, refresh timing,
                      roster counts, cache sizes, pending restarts.
  GET /api/config     Every setting with its effective value, its source
                      (settings file, environment, or default), its type and
                      bounds, and whether the caller may edit it.
  PUT /api/settings   Validate, save and apply a change. Needs a signed-in
                      session and a CSRF token. See settings.py for the
                      precedence rules and auth.py for the guards.
  GET /api/cache      Extraction cache contents plus logo and no-audio stats.
  GET /api/events     Ring buffer of recent log records, newest first.

Wiring, from app.py:

    auth.install(app)
    dashboard.register_dashboard(
        app,
        runtime=_dash_runtime,
        caches=_dash_caches,
        config_values=lambda: globals(),
        apply_settings=lambda s, o: settings.apply_live(globals(), s, o),
    )

`register_dashboard` also attaches a logging handler to the root logger to
populate the event ring buffer, so no existing log call sites need editing.
"""

import os
import time
import logging
import threading
from collections import deque

from flask import Blueprint, jsonify, render_template, request

import auth
import settings

bp = Blueprint("dashboard", __name__)

# Wall-clock and monotonic process start. Monotonic drives uptime so a host
# clock adjustment cannot produce a negative or wildly wrong figure.
_PROCESS_START_WALL = time.time()
_PROCESS_START_MONO = time.monotonic()

VERSION = os.getenv("STREAMED_M3U_VERSION", "dev")

# Providers injected by register_dashboard. Kept module-level rather than
# imported from app.py, because app.py runs as __main__ and importing it back
# would execute a second copy of the module.
_runtime_provider = None
_caches_provider = None
_config_provider = None
_apply_settings = None


# ─── Event ring buffer ────────────────────────────────────────────────────────
# A logging.Handler rather than explicit emit() calls at every interesting
# site: the service already logs extraction attempts, cascade failures, stream
# starts and refresh results, so capturing records gets the whole event stream
# without touching a single existing code path.

class _EventBuffer(logging.Handler):
    def __init__(self, capacity: int = 500):
        super().__init__()
        self._events = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    # Werkzeug logs one line per HTTP request. The console polls several
    # endpoints every few seconds, so leaving those in would fill the buffer
    # with the console observing itself and push out the records worth seeing.
    _MUTED = ("werkzeug",)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in self._MUTED:
            return
        try:
            message = record.getMessage()
        except Exception:
            # A broken format string in some other module must never take the
            # service down through the logging path.
            return
        with self._lock:
            self._seq += 1
            self._events.append({
                "seq": self._seq,
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            })

    def snapshot(self, limit: int = 200, min_level: str = "") -> list:
        threshold = logging.getLevelName(min_level.upper()) if min_level else 0
        if not isinstance(threshold, int):
            threshold = 0
        with self._lock:
            events = list(self._events)
        if threshold:
            events = [
                e for e in events
                if logging.getLevelName(e["level"]) >= threshold
            ]
        events.reverse()  # newest first
        return events[:limit]

    def counts(self) -> dict:
        with self._lock:
            events = list(self._events)
        out = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        for e in events:
            if e["level"] in out:
                out[e["level"]] += 1
        return out


EVENTS = _EventBuffer()


# ─── Routes ───────────────────────────────────────────────────────────────────

@bp.route("/")
def console():
    return render_template(
        "dashboard.html",
        version=VERSION,
        auth_enabled=auth.enabled(),
        logged_in=auth.logged_in(),
        csrf_token=auth.csrf_token(),
    )


@bp.route("/api/overview")
def api_overview():
    data = _runtime_provider() if _runtime_provider else {}
    uptime = time.monotonic() - _PROCESS_START_MONO
    data["service"] = {
        "version": VERSION,
        "started_at": _PROCESS_START_WALL,
        "uptime_s": round(uptime, 1),
        "pid": os.getpid(),
    }
    data["events"] = EVENTS.counts()
    data["restart_pending"] = sorted(settings.PENDING_RESTART)
    data["settings_load_error"] = settings.load_error
    data["now"] = time.time()
    return jsonify(data)


def _config_payload():
    values = _config_provider() if _config_provider else {}
    data = settings.describe(values)
    data["editable"] = auth.logged_in()
    data["auth"] = {"enabled": auth.enabled(), "logged_in": auth.logged_in()}
    return data


@bp.route("/api/config")
def api_config():
    return jsonify(_config_payload())


@bp.route("/api/settings", methods=["PUT"])
def api_settings():
    """Validate, save, then apply. Nothing is written if validation fails,
    and nothing is applied if the save fails, so the file and the running
    process never disagree about what was accepted."""
    denied = auth.require_write()
    if denied:
        return denied

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected_json"}), 400
    changes = payload.get("settings") or {}
    overrides = payload.get("overrides") or {}
    if not isinstance(changes, dict) or not isinstance(overrides, dict):
        return jsonify({"ok": False, "error": "expected_json"}), 400

    values = _config_provider() if _config_provider else {}
    clean, clean_ov, errors = settings.validate(
        changes, overrides, settings.effective_values(values))
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    doc = settings.current()
    for env, val in clean.items():
        if val is None:
            doc["settings"].pop(env, None)
        else:
            doc["settings"][env] = val
    for key, table in clean_ov.items():
        if table is None:
            doc["overrides"].pop(key, None)
        else:
            doc["overrides"][key] = table
    try:
        settings.save(doc)
    except OSError as e:
        return jsonify({"ok": False, "error": "save_failed", "message": str(e)}), 500

    result = (_apply_settings(clean, clean_ov) if _apply_settings
              else {"applied": [], "restart_required": []})
    return jsonify({
        "ok": True,
        "applied": result["applied"],
        "restart_required": result["restart_required"],
        "restart_pending": sorted(settings.PENDING_RESTART),
        "config": _config_payload(),
    })


@bp.route("/api/cache")
def api_cache():
    return jsonify(_caches_provider() if _caches_provider else {})


@bp.route("/api/events")
def api_events():
    try:
        limit = max(1, min(int(request.args.get("limit", 200)), 500))
    except (TypeError, ValueError):
        limit = 200
    level = request.args.get("level", "")
    return jsonify({
        "counts": EVENTS.counts(),
        "capacity": EVENTS._events.maxlen,
        "events": EVENTS.snapshot(limit=limit, min_level=level),
    })


def register_dashboard(flask_app, *, runtime, caches, config_values,
                       apply_settings=None, capture_logs=True):
    """Attach the console to a Flask app.

    runtime / caches / config_values are zero-argument callables provided by
    app.py, which owns the locks guarding the state they read. apply_settings
    is (clean_settings, clean_overrides) -> result, used by the write endpoint
    to rebind live constants in app.py's namespace.
    """
    global _runtime_provider, _caches_provider, _config_provider, _apply_settings
    _runtime_provider = runtime
    _caches_provider = caches
    _config_provider = config_values
    _apply_settings = apply_settings

    if capture_logs:
        root = logging.getLogger()
        if EVENTS not in root.handlers:
            root.addHandler(EVENTS)

    # With a password configured the whole console, page and API alike,
    # requires a session. Without one it stays open and read-only.
    if auth.enabled():
        bp.before_request(auth.gate)

    flask_app.register_blueprint(bp)
    return bp
