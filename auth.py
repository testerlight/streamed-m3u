"""
auth: optional login for the console.

Disabled unless CONSOLE_PASSWORD is set, in which case the console is
read-only and there is no login page. With a password:

  - `/`, `/api/*` and `/stream/status` require a signed-in session.
    `/stream/status` is included because it exposes resolved CDN URLs with
    their signing tokens.
  - Everything Dispatcharr or a Docker healthcheck consumes stays open:
    `/health`, the playlists, `/epg.xml`, `/stream`, `/logo`, `/teams`,
    `/prewarm`, and `/static/*` (the login page needs the stylesheet).
  - Writes additionally need an `X-CSRF-Token` header matching the token in
    the session, and a JSON body. The cookie is SameSite=Lax as a second layer.

The session key is generated once and kept at /data/.secret_key (mode 0600)
so sessions survive restarts. If /data is not writable the key is ephemeral
and a warning is logged.
"""

import hmac
import logging
import os
import secrets
import threading
import time
from datetime import timedelta

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

import settings

log = logging.getLogger("auth")

CONSOLE_PASSWORD = os.getenv("CONSOLE_PASSWORD", "")
SECRET_KEY_FILE  = os.path.join(settings.DATA_DIR, ".secret_key")

bp = Blueprint("auth", __name__)

# Failed logins per source address: count and time of the last one. Each
# failure sleeps 1, 2, 4, then 8 seconds before answering, which makes
# guessing slow without locking anyone out. Entries age out after ten
# minutes so the dict stays small.
_failures: dict = {}
_failures_lock = threading.Lock()
_FAILURE_TTL = 600


def enabled() -> bool:
    return bool(CONSOLE_PASSWORD)


def logged_in() -> bool:
    return enabled() and session.get("auth") is True


def csrf_token() -> str:
    return session.get("csrf", "") if logged_in() else ""


def _load_or_create_secret() -> str:
    try:
        with open(SECRET_KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        settings.atomic_write_text(SECRET_KEY_FILE, key + "\n", mode=0o600)
    except OSError as e:
        log.warning("Could not persist the session key (%s); "
                    "logins will not survive a restart", e)
    return key


def install(app):
    """Configure sessions and register the login routes. Call before the
    console blueprint is registered."""
    app.secret_key = _load_or_create_secret()
    app.config.update(
        SESSION_COOKIE_NAME="streamed_m3u_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("CONSOLE_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    app.register_blueprint(bp)
    if enabled():
        log.info("Console login enabled")


# ─── Guards ───────────────────────────────────────────────────────────────────

def _wants_html() -> bool:
    """A browser navigation: GET, outside /api/, and either an Accept header
    that admits HTML or no Accept header at all (curl and the test client)."""
    accepts = request.accept_mimetypes
    return (request.method == "GET"
            and not request.path.startswith("/api/")
            and (not accepts or accepts.accept_html))


def deny():
    """The response for an unauthenticated request to a gated route:
    browsers are sent to the login page, everything else gets 401 JSON."""
    if _wants_html():
        nxt = request.full_path.rstrip("?") or "/"
        return redirect(url_for("auth.login", next=nxt))
    return jsonify({"error": "login_required"}), 401


def gate():
    """before_request hook for gated blueprints. None means allowed."""
    if not enabled() or logged_in():
        return None
    return deny()


def _csrf_ok() -> bool:
    sent = request.headers.get("X-CSRF-Token", "")
    have = csrf_token()
    return bool(have) and hmac.compare_digest(sent, have)


def require_write():
    """Guard for endpoints that change state. Returns a response to send
    instead, or None when the request may proceed."""
    if not enabled():
        return jsonify({"error": "console_readonly",
                        "message": "Set CONSOLE_PASSWORD to enable editing."}), 403
    if not logged_in():
        return jsonify({"error": "login_required"}), 401
    if not _csrf_ok():
        return jsonify({"error": "csrf",
                        "message": "Missing or stale request token. Reload the page."}), 403
    if not request.is_json:
        return jsonify({"error": "expected_json"}), 400
    return None


# ─── Login flow ───────────────────────────────────────────────────────────────

def _safe_next(n) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") else "/"


def _record_failure(ip: str) -> int:
    """Count a failed attempt and return how long to stall before replying."""
    now = time.time()
    with _failures_lock:
        for k in [k for k, (_, t) in _failures.items() if now - t > _FAILURE_TTL]:
            del _failures[k]
        count = _failures.get(ip, (0, now))[0] + 1
        _failures[ip] = (count, now)
    return min(2 ** (count - 1), 8)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not enabled():
        return redirect("/")
    nxt = _safe_next(request.values.get("next"))
    if logged_in() and request.method == "GET":
        return redirect(nxt)

    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hmac.compare_digest(pw.encode(), CONSOLE_PASSWORD.encode()):
            session.clear()
            session.permanent = True
            session["auth"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            with _failures_lock:
                _failures.pop(request.remote_addr or "?", None)
            return redirect(nxt)
        time.sleep(_record_failure(request.remote_addr or "?"))
        error = "That password was not accepted."

    return render_template("login.html", error=error, next=nxt)


@bp.route("/logout", methods=["POST"])
def logout():
    if logged_in() and not _csrf_ok():
        return jsonify({"error": "csrf"}), 403
    session.clear()
    if request.accept_mimetypes.accept_html and not request.is_json:
        return redirect(url_for("auth.login"))
    return jsonify({"ok": True})


@bp.route("/api/session")
def api_session():
    """Open on purpose: the page uses it to decide whether editing is
    available, and it reveals nothing beyond the caller's own state."""
    return jsonify({
        "auth_enabled":    enabled(),
        "authenticated":   logged_in(),
        "editing_enabled": logged_in(),
        "csrf_token":      csrf_token() or None,
    })
