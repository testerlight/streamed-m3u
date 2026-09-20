"""
settings: the schema for every configurable setting, and the file that lets
the console change them.

Three layers, highest priority first:

  1. /data/settings.json    written by the console (or by hand)
  2. the environment        compose, .env, the TrueNAS app config
  3. built-in defaults      the literals in app.py

The file wins so that a change made in the console has an effect. The
console shows the environment value alongside, and sending null for a key
removes it from the file and restores the environment-or-default value.

This module imports nothing from app.py or dashboard.py. app.py calls
`apply(globals(), load())` once, right after its config block and before any
derived structure is built, so overrides take effect at import. The console
calls `validate`, `save` and `apply_live` on a write.

Built-in defaults are deliberately not duplicated here. `apply()` snapshots
every constant's value *before* overlaying the file; that snapshot (BASELINE)
is already environment-or-default because app.py has parsed the environment
by then, and it is what a revert restores.

Run as a script to generate documentation from the schema:

    python settings.py --env-example   # the .env.example file
    python settings.py --markdown      # the README configuration table
"""

import copy
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("settings")

DATA_DIR       = os.getenv("DATA_DIR", "/data")
SETTINGS_FILE  = os.getenv("SETTINGS_FILE", os.path.join(DATA_DIR, "settings.json"))
SCHEMA_VERSION = 1

# ─── Schema ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Setting:
    env: str
    const: str
    group: str
    applies: str            # "live" | "next-refresh" | "restart"
    type: str               # int | float | str | bool | list | url | choice
    description: str
    editable: bool = True   # False: environment only (socket bind, file paths)
    min: Optional[float] = None
    max: Optional[float] = None
    choices: tuple = ()
    allow_empty: bool = False


def _s(env, const, group, applies, type_, description, **kw):
    return Setting(env, const, group, applies, type_, description, **kw)


# `applies` records when a change takes effect. "live": the constant is read
# at call time, so rebinding it works immediately. "next-refresh": read inside
# the playlist rebuild, so it lands on the next cycle. "restart": consumed once
# at startup to seed the roster or build a lookup table.
SCHEMA = [
    # Service
    _s("PORT", "PORT", "Service", "restart", "int",
       "Port the service binds. Must match the published container port.",
       editable=False, min=1, max=65535),
    _s("PUBLIC_BASE_URL", "PUBLIC_BASE_URL", "Service", "live", "url",
       "Address clients use to reach this service, used in every playlist URL. "
       "Empty means derive it from each request, which is right on a shared network.",
       allow_empty=True),
    _s("LOG_LEVEL", "LOG_LEVEL", "Service", "live", "choice",
       "Logging verbosity. Also sets what reaches the events panel.",
       choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    _s("STARTUP_DELAY", "STARTUP_DELAY", "Service", "restart", "int",
       "Pause before the first scrape, giving the VPN tunnel time to come up.",
       min=0, max=300),
    _s("TEAMS_FILE", "TEAMS_FILE", "Service", "restart", "str",
       "Roster file. The only irreplaceable state the service holds.",
       editable=False),
    _s("EXTRACT_CACHE_FILE", "EXTRACT_CACHE_FILE", "Service", "restart", "str",
       "Where resolved stream URLs are persisted across restarts.",
       editable=False),

    # Upstream
    _s("STREAMED_BASE_URL", "BASE_URL", "Upstream", "live", "url",
       "Catalog the service scrapes. Change this when the site moves domain."),
    _s("REQUEST_TIMEOUT", "REQUEST_TIMEOUT", "Upstream", "live", "int",
       "Seconds before an upstream API call is abandoned.", min=1, max=120),
    _s("BUILD_WORKERS", "BUILD_WORKERS", "Upstream", "live", "int",
       "Parallel workers for a playlist build. Modest values avoid rate limits.",
       min=1, max=32),
    _s("REFRESH_SECONDS", "REFRESH_SECONDS", "Upstream", "live", "int",
       "Interval between playlist rebuilds. A change waits out the current interval.",
       min=60, max=86400),

    # Channels
    _s("PREWARM_TEAMS", "PREWARM_TEAMS", "Channels", "restart", "list",
       "Favourites kept hot. Each costs one serial browser launch, so keep it short."),
    _s("EXTRA_ALIAS_TEAMS", "EXTRA_ALIAS_TEAMS", "Channels", "restart", "list",
       "Extra names granted away-side resolution. Adds to the built-in league list."),
    _s("SERIES_CHANNELS", "SERIES_CHANNELS", "Channels", "restart", "list",
       "Racing series that keep a permanent channel through the off-season."),
    _s("FEED_CHANNELS", "FEED_CHANNELS", "Channels", "next-refresh", "list",
       "Always-on feeds, named by the title they display under."),
    _s("POOL_SLOTS", "POOL_SLOTS", "Channels", "next-refresh", "int",
       "Shared channels for one-off events. Lowering it never removes existing channels.",
       min=0, max=16),
    _s("POOL_NAME", "POOL_NAME", "Channels", "restart", "str",
       "Display name for the shared event slots. Renaming mints new channels; the old ones stay."),
    _s("FAVOURITES_GROUP", "FAVOURITES_GROUP", "Channels", "live", "str",
       "Group title favourites are filed under in the playlist."),

    # Pre-warm
    _s("PREWARM_INTERVAL", "PREWARM_INTERVAL", "Pre-warm", "live", "int",
       "How often the pre-warm loop looks for work.", min=10, max=3600),
    _s("PREWARM_MARGIN", "PREWARM_MARGIN", "Pre-warm", "live", "int",
       "Re-warm once a cached entry drops below this much remaining TTL.", min=0, max=86400),
    _s("PREWARM_WINDOW_BEFORE", "PREWARM_WINDOW_BEFORE", "Pre-warm", "live", "int",
       "Minutes before kickoff that a fixture becomes eligible for warming.", min=0, max=1440),
    _s("PREWARM_WINDOW_AFTER", "PREWARM_WINDOW_AFTER", "Pre-warm", "live", "int",
       "Minutes after kickoff that a fixture stays eligible for warming.", min=0, max=1440),

    # Resolution
    _s("BROWSER_TIMEOUT", "BROWSER_TIMEOUT", "Resolution", "live", "int",
       "Ceiling on one headless extraction attempt.", min=5, max=120),
    _s("CASCADE_BUDGET", "CASCADE_BUDGET", "Resolution", "live", "int",
       "Wall-clock ceiling for trying sources. Must stay below Dispatcharr's 60 second grace period.",
       min=10, max=59),
    _s("CASCADE_MAX_ATTEMPTS", "CASCADE_MAX_ATTEMPTS", "Resolution", "live", "int",
       "Hard cap on attempts, independent of the time budget.", min=1, max=9),
    _s("CASCADE_RESERVE", "CASCADE_RESERVE", "Resolution", "live", "int",
       "Headroom required before a further attempt is allowed to start.", min=0, max=59),
    _s("EXTRACT_CACHE_TTL", "EXTRACT_CACHE_TTL", "Resolution", "live", "int",
       "How long a resolved stream URL stays reusable.", min=30, max=86400),
    _s("EXTRACT_CACHE_MAX_ENTRIES", "EXTRACT_CACHE_MAX_ENTRIES", "Resolution", "live", "int",
       "Cache size cap. Oldest entries evict first. Zero means no cap.", min=0, max=10000),
    _s("REQUIRE_AUDIO", "REQUIRE_AUDIO", "Resolution", "live", "bool",
       "Reject a source carrying no audio track and fall through to the next."),
    _s("NO_AUDIO_TTL", "NO_AUDIO_TTL", "Resolution", "live", "int",
       "How long a confirmed silent source is remembered, so it is not retried.",
       min=0, max=86400),

    # Playback
    _s("SEGMENT_TIMEOUT", "SEGMENT_TIMEOUT", "Playback", "live", "int",
       "Per-chunk download ceiling. Must stay above the time a healthy chunk takes.",
       min=5, max=300),
    _s("SEGMENT_PROBE_TIMEOUT", "SEGMENT_PROBE_TIMEOUT", "Playback", "live", "int",
       "Same ceiling for the startup liveness probe. Raising it costs cascade budget.",
       min=1, max=300),
    _s("SEGMENT_RETRIES", "SEGMENT_RETRIES", "Playback", "live", "int",
       "Extra attempts for a failed chunk. Small on purpose: retries drag the live edge.",
       min=0, max=5),
    _s("SEGMENT_MAX_MB", "SEGMENT_MAX_MB", "Playback", "live", "int",
       "Sanity cap on a buffered chunk, since chunks are held in RAM before forwarding.",
       min=1, max=512),
    _s("STREAM_IDLE_TIMEOUT", "STREAM_IDLE_TIMEOUT", "Playback", "live", "int",
       "Seconds without a new segment before a stream is treated as dead.", min=5, max=600),
    _s("STREAM_KEEPALIVE_INTERVAL", "STREAM_KEEPALIVE_INTERVAL", "Playback", "live", "float",
       "Seconds between null packets sent while waiting on a slow chunk, so the client "
       "does not give up.", min=0, max=30),
    _s("STREAM_SEEN_MAX", "STREAM_SEEN_MAX", "Playback", "live", "int",
       "Segment URLs remembered per stream, bounding memory on long sessions.",
       min=10, max=10000),

    # Guide
    _s("EPG_WINDOW_HOURS", "EPG_WINDOW_HOURS", "Guide", "live", "int",
       "How far ahead the guide publishes.", min=1, max=168),
    _s("EPG_BACKFILL_HOURS", "EPG_BACKFILL_HOURS", "Guide", "live", "int",
       "How far back the guide reaches, so in-progress matches still appear.", min=0, max=48),
    _s("EPG_DEFAULT_MINUTES", "EPG_DEFAULT_MINUTES", "Guide", "live", "int",
       "Assumed programme length for sports with no per-sport duration.", min=10, max=1440),

    # Logos
    _s("LOGO_CACHE_TTL", "LOGO_CACHE_TTL", "Logos", "live", "int",
       "How long a successfully fetched logo is served from cache.", min=0, max=604800),
    _s("LOGO_CACHE_FAIL_TTL", "LOGO_CACHE_FAIL_TTL", "Logos", "live", "int",
       "Short retry window after a logo fetch fails.", min=0, max=86400),
    _s("LOGO_CACHE_MAX_ENTRIES", "LOGO_CACHE_MAX_ENTRIES", "Logos", "live", "int",
       "Logo cache size cap.", min=0, max=100000),
]

GROUP_ORDER = ["Service", "Upstream", "Channels", "Pre-warm",
               "Resolution", "Playback", "Guide", "Logos"]

BY_ENV   = {s.env: s for s in SCHEMA}
BY_CONST = {s.const: s for s in SCHEMA}

# Code-only tables that the file may extend. Lists are unioned onto the
# built-in list; dicts are updated over the built-in dict. Neither can remove
# a built-in entry, which keeps a typo in the file from blanking a league.
OVERRIDES = {
    "major_league_teams":       ("MAJOR_LEAGUE_TEAMS", "list"),
    "extra_feed_title_aliases": ("FEED_TITLE_ALIASES", "dict"),
    "feed_slug_overrides":      ("FEED_SLUG_OVERRIDES", "dict"),
    "series_aliases":           ("SERIES_ALIASES", "dict"),
}
_SLUG_MAPS = {"feed_slug_overrides", "series_aliases"}
_SLUG_RE   = re.compile(r"^[a-z0-9-]+$")
_URL_RE    = re.compile(r"^https?://[^/\s]+")

# Filled by apply(): constant -> value before the file overlay.
BASELINE: dict = {}
# Settings (env names) and overrides ("overrides.<key>") changed since
# start that only take effect on the next start.
PENDING_RESTART: set = set()

_doc: dict = {"version": SCHEMA_VERSION, "settings": {}, "overrides": {}}
load_error: Optional[str] = None


# ─── Environment hygiene ──────────────────────────────────────────────────────

def scrub_empty_env(names=None):
    """Drop VAR="" for schema keys. Compose interpolation and uncommented
    .env lines produce empty strings, and int("") would crash the import."""
    removed = []
    for env in (names or BY_ENV):
        if os.environ.get(env) == "":
            del os.environ[env]
            removed.append(env)
    return removed


# ─── Coercion and validation ──────────────────────────────────────────────────

_TRUE  = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no", ""}


def coerce(spec: Setting, raw):
    """Turn a JSON-native or environment-style value into the constant's
    type, checking bounds and choices. Raises ValueError with a message
    fit for a form."""
    t = spec.type
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        v = str(raw).strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        raise ValueError("expected on or off")

    if t in ("int", "float"):
        if isinstance(raw, bool):
            raise ValueError("expected a number")
        try:
            num = float(raw) if t == "float" else int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError("expected a whole number" if t == "int" else "expected a number")
        if spec.min is not None and num < spec.min:
            raise ValueError("must be at least %s" % _fmt(spec.min))
        if spec.max is not None and num > spec.max:
            raise ValueError("must be at most %s" % _fmt(spec.max))
        return num

    if t == "list":
        if isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw]
        else:
            items = [x.strip() for x in str(raw).split(",")]
        return [x for x in items if x]

    if t == "choice":
        v = str(raw).strip().upper()
        if v not in spec.choices:
            raise ValueError("not one of %s" % ", ".join(spec.choices))
        return v

    if t == "url":
        v = str(raw).strip().rstrip("/")
        if not v:
            if spec.allow_empty:
                return ""
            raise ValueError("cannot be empty")
        if not _URL_RE.match(v):
            raise ValueError("expected a URL starting with http:// or https://")
        return v

    v = str(raw).strip()
    if not v and not spec.allow_empty:
        raise ValueError("cannot be empty")
    return v


def _fmt(n):
    return str(int(n)) if float(n).is_integer() else str(n)


def _clean_override(key, table):
    const, kind = OVERRIDES[key]
    if kind == "list":
        if not isinstance(table, (list, tuple)):
            raise ValueError("expected a list of names")
        out = [str(x).strip() for x in table]
        return [x for x in out if x]
    if not isinstance(table, dict):
        raise ValueError("expected a mapping of name to name")
    out = {}
    for k, v in table.items():
        k, v = str(k).strip(), str(v).strip()
        if not k or not v:
            raise ValueError("keys and values cannot be empty")
        if key == "extra_feed_title_aliases":
            k = k.lower()
        elif key in _SLUG_MAPS and not (_SLUG_RE.match(k) and _SLUG_RE.match(v)):
            raise ValueError("%r: slugs use lowercase letters, digits and hyphens only" % k)
        out[k] = v
    return out


def validate(changes: dict, overrides: dict, effective: dict):
    """Check a console write. Returns (clean_settings, clean_overrides, errors).
    Nothing is applied here. `effective` maps env names to current values and
    is used for the cross-field rules."""
    clean, clean_ov, errors = {}, {}, {}
    for env, raw in (changes or {}).items():
        spec = BY_ENV.get(env)
        if spec is None:
            errors[env] = "unknown setting"
        elif not spec.editable:
            errors[env] = "set through the environment only"
        elif raw is None:
            clean[env] = None
        else:
            try:
                clean[env] = coerce(spec, raw)
            except ValueError as e:
                errors[env] = str(e)

    for key, table in (overrides or {}).items():
        if key not in OVERRIDES:
            errors["overrides." + key] = "unknown override"
        elif table is None:
            clean_ov[key] = None
        else:
            try:
                clean_ov[key] = _clean_override(key, table)
            except ValueError as e:
                errors["overrides." + key] = str(e)

    eff = dict(effective or {})
    for env, val in clean.items():
        eff[env] = BASELINE.get(BY_ENV[env].const) if val is None else val
    cross = []
    if eff.get("CASCADE_RESERVE") is not None and eff.get("CASCADE_BUDGET") is not None \
            and eff["CASCADE_RESERVE"] >= eff["CASCADE_BUDGET"]:
        cross.append("CASCADE_RESERVE must be below CASCADE_BUDGET")
    if eff.get("SEGMENT_PROBE_TIMEOUT") is not None and eff.get("SEGMENT_TIMEOUT") is not None \
            and eff["SEGMENT_PROBE_TIMEOUT"] > eff["SEGMENT_TIMEOUT"]:
        cross.append("SEGMENT_PROBE_TIMEOUT cannot exceed SEGMENT_TIMEOUT")
    if eff.get("PREWARM_MARGIN") is not None and eff.get("EXTRACT_CACHE_TTL") is not None \
            and eff["PREWARM_MARGIN"] > eff["EXTRACT_CACHE_TTL"]:
        cross.append("PREWARM_MARGIN cannot exceed EXTRACT_CACHE_TTL")
    if cross:
        errors["_cross"] = cross
    return clean, clean_ov, errors


# ─── File I/O ─────────────────────────────────────────────────────────────────

_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(path):
    with _locks_guard:
        return _locks.setdefault(os.path.abspath(path), threading.Lock())


def _keep_backup(path):
    bak = path + ".bak"
    try:
        if os.path.exists(bak):
            os.remove(bak)
        os.link(path, bak)
    except OSError:
        shutil.copyfile(path, bak)


def atomic_write_json(path, obj, *, backup=True, **dump_kw):
    """Write JSON so a crash never leaves a half-written file: temp file in
    the same directory, fsync, then rename over the target. The previous file
    survives as path.bak."""
    with _lock_for(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, **dump_kw)
            f.flush()
            os.fsync(f.fileno())
        if backup and os.path.exists(path):
            _keep_backup(path)
        os.replace(tmp, path)


def atomic_write_text(path, text, mode=0o600):
    with _lock_for(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def read_json_with_fallback(path, what="file"):
    """Read `path`; on corruption move it aside, log, and try path.bak.
    Returns (data or None, error message or None). Never raises."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        aside = "%s.corrupt-%s" % (path, stamp)
        try:
            os.replace(path, aside)
        except OSError:
            aside = None
        err = "%s unreadable (%s)%s" % (
            what, e, "; moved to %s" % aside if aside else "")
        log.error("%s", err)
        bak = path + ".bak"
        if os.path.exists(bak):
            try:
                with open(bak, encoding="utf-8") as f:
                    data = json.load(f)
                log.warning("Recovered %s from %s", what, bak)
                return data, err
            except (OSError, ValueError) as e2:
                log.error("%s backup also unreadable (%s)", what, e2)
        return None, err


def _normalise(doc):
    out = {"version": SCHEMA_VERSION, "settings": {}, "overrides": {}}
    if isinstance(doc, dict):
        if isinstance(doc.get("settings"), dict):
            out["settings"] = dict(doc["settings"])
        if isinstance(doc.get("overrides"), dict):
            out["overrides"] = {k: v for k, v in doc["overrides"].items()
                                if k in OVERRIDES and v is not None}
    return out


def load(path=SETTINGS_FILE):
    """Load the settings document. Never raises; a corrupt file is moved
    aside and the backup or an empty document is used instead."""
    global _doc, load_error
    data, err = read_json_with_fallback(path, "settings file")
    load_error = err
    _doc = _normalise(data)
    return copy.deepcopy(_doc)


def current():
    return copy.deepcopy(_doc)


def save(doc, path=SETTINGS_FILE):
    global _doc
    doc = _normalise(doc)
    atomic_write_json(path, doc, indent=1, sort_keys=True)
    _doc = doc


# ─── Applying to the running module ───────────────────────────────────────────

def _merge_override(key, builtin, extra):
    _, kind = OVERRIDES[key]
    if kind == "list":
        return list(dict.fromkeys(list(builtin) + list(extra)))
    merged = dict(builtin)
    merged.update(extra)
    return merged


def apply(g: dict, doc=None):
    """Overlay the file onto app.py's module globals at import time.
    Snapshots BASELINE first. Bad values are logged and skipped, never fatal."""
    doc = _normalise(doc if doc is not None else _doc)
    BASELINE.clear()
    for spec in SCHEMA:
        if spec.const in g:
            BASELINE[spec.const] = copy.deepcopy(g[spec.const])
    for key, (const, _) in OVERRIDES.items():
        if const in g:
            BASELINE[const] = copy.deepcopy(g[const])

    applied, skipped = [], {}
    for env, raw in doc["settings"].items():
        spec = BY_ENV.get(env)
        if spec is None:
            skipped[env] = "unknown setting"
        elif not spec.editable:
            skipped[env] = "environment only"
        elif spec.const not in g:
            skipped[env] = "constant not present"
        else:
            try:
                g[spec.const] = coerce(spec, raw)
                applied.append(env)
            except ValueError as e:
                skipped[env] = str(e)

    for key, table in doc["overrides"].items():
        const, _ = OVERRIDES[key]
        if const not in g:
            continue
        try:
            g[const] = _merge_override(key, BASELINE[const], _clean_override(key, table))
            applied.append("overrides." + key)
        except ValueError as e:
            skipped["overrides." + key] = str(e)

    for k, why in skipped.items():
        log.warning("settings.json: ignoring %s (%s)", k, why)
    if applied:
        log.info("settings.json applied: %s", ", ".join(applied))
    return {"applied": applied, "skipped": skipped}


def apply_live(g: dict, clean_settings: dict, clean_overrides: dict):
    """After a validated save: rebind live constants, record the rest as
    pending a restart. A change whose result equals the running value needs
    no restart, so reverting an unapplied edit clears its flag."""
    applied, restart = [], []
    for env, val in clean_settings.items():
        spec = BY_ENV[env]
        value = copy.deepcopy(BASELINE.get(spec.const)) if val is None else val
        if spec.applies in ("live", "next-refresh"):
            g[spec.const] = value
            if env == "LOG_LEVEL":
                logging.getLogger().setLevel(getattr(logging, value, logging.INFO))
            applied.append(env)
            PENDING_RESTART.discard(env)
        elif value == g.get(spec.const):
            PENDING_RESTART.discard(env)
        else:
            restart.append(env)
            PENDING_RESTART.add(env)

    for key, table in clean_overrides.items():
        const, _ = OVERRIDES[key]
        tag = "overrides." + key
        builtin = BASELINE.get(const, g.get(const))
        merged = _merge_override(key, builtin, table or ([] if OVERRIDES[key][1] == "list" else {}))
        if merged == g.get(const):
            PENDING_RESTART.discard(tag)
        else:
            restart.append(tag)
            PENDING_RESTART.add(tag)
    return {"applied": applied, "restart_required": restart}


# ─── Views for the console ────────────────────────────────────────────────────

def display(value):
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(str(v) for v in value) if value else ""
    if isinstance(value, bool):
        return "on" if value else "off"
    return value


def effective_values(g: dict):
    return {s.env: g.get(s.const) for s in SCHEMA}


def describe(g: dict):
    """Everything the console needs to render and edit the configuration."""
    file_settings = _doc["settings"]
    groups = {}
    customised = 0
    for spec in SCHEMA:
        env_raw = os.environ.get(spec.env)
        in_file = spec.env in file_settings
        source = "file" if in_file else ("environment" if env_raw is not None else "default")
        if source != "default":
            customised += 1
        value = g.get(spec.const)
        groups.setdefault(spec.group, []).append({
            "env": spec.env,
            "group": spec.group,
            "description": spec.description,
            "type": spec.type,
            "applies": spec.applies,
            "editable": spec.editable,
            "min": spec.min,
            "max": spec.max,
            "choices": list(spec.choices),
            "allow_empty": spec.allow_empty,
            "value": value,
            "display": display(value),
            "source": source,
            "env_value": env_raw,
            "file_value": file_settings.get(spec.env) if in_file else None,
            "baseline": BASELINE.get(spec.const),
            "restart_pending": spec.env in PENDING_RESTART,
        })
    ordered = [{"group": gname, "settings": groups[gname]}
               for gname in GROUP_ORDER if gname in groups]
    ordered += [{"group": gname, "settings": s}
                for gname, s in sorted(groups.items()) if gname not in GROUP_ORDER]

    overrides = {}
    for key, (const, kind) in OVERRIDES.items():
        builtin = BASELINE.get(const, g.get(const))
        overrides[key] = {
            "kind": kind,
            "constant": const,
            "value": g.get(const),
            "file_value": _doc["overrides"].get(key),
            "builtin_count": len(builtin) if builtin is not None else 0,
            "restart_pending": ("overrides." + key) in PENDING_RESTART,
        }

    return {
        "total": len(SCHEMA),
        "customised": customised,
        "groups": ordered,
        "overrides": overrides,
        "restart_pending": sorted(PENDING_RESTART),
        "load_error": load_error,
        "settings_file": SETTINGS_FILE,
    }


# ─── Documentation generators ─────────────────────────────────────────────────

_ENV_HEADER = """\
# streamed-m3u configuration
#
# Copy this file to .env next to docker-compose.yml and fill in the values you
# need. Every line below is optional except where marked required.
#
# Precedence, highest first:
#   1. /data/settings.json  written by the console when editing is enabled
#   2. this environment     compose, .env, or your platform's app config
#   3. built-in defaults
# The console shows which layer each value came from. An empty value counts as
# unset. PORT, TEAMS_FILE, EXTRACT_CACHE_FILE, DATA_DIR, CONSOLE_PASSWORD,
# PUID and PGID are environment-only and never written to the settings file.

# ── Compose ─────────────────────────────────────────────────────────────────
# Image to run. Build locally or pull a published tag.
#STREAMED_M3U_IMAGE=streamed-m3u:latest
# Host directory that holds each service's persistent data.
#CONFIG_DIR=./config
# User and group the service runs as inside the container. TrueNAS SCALE
# apps conventionally use 568.
#PUID=1000
#PGID=1000
# Host ports.
#STREAMED_M3U_PORT=8787
#DISPATCHARR_PORT=9191
#TZ=UTC

# ── VPN (gluetun) ───────────────────────────────────────────────────────────
# Required unless you use docker-compose.novpn.yml.
#VPN_SERVICE_PROVIDER=airvpn
#VPN_TYPE=wireguard
#WIREGUARD_PRIVATE_KEY=
#WIREGUARD_PRESHARED_KEY=
#WIREGUARD_ADDRESSES=
#SERVER_COUNTRIES=United States

# ── Console ─────────────────────────────────────────────────────────────────
# Set a password to enable editing settings from the browser. Leave unset for
# a read-only console with no login.
#CONSOLE_PASSWORD=
# Send Secure cookies. Only enable behind HTTPS.
#CONSOLE_COOKIE_SECURE=0

# ── Dispatcharr sync container ──────────────────────────────────────────────
# Required for the streamed-m3u-sync service.
#DISPATCHARR_USER=
#DISPATCHARR_PASS=
# Exact names of the M3U account and EPG source you created in Dispatcharr.
#M3U_ACCOUNT_NAME=streamed.pk teams
#EPG_SOURCE_NAME=streamed.pk teams EPG
# Seconds between sync cycles.
#SYNC_INTERVAL=480
"""


def _defaults_from_app():
    """Best effort: import app.py for its built-in defaults. Only works inside
    the image (app.py needs flask and curl_cffi); elsewhere defaults are omitted."""
    try:
        for env in list(BY_ENV):
            os.environ.pop(env, None)
        import app  # noqa: F401
        return {s.env: getattr(app, s.const, None) for s in SCHEMA}
    except Exception:
        return {}


def env_example():
    defaults = _defaults_from_app()
    out = [_ENV_HEADER]
    for gname in GROUP_ORDER:
        out.append("# ── %s %s" % (gname, "─" * max(3, 74 - len(gname))))
        for spec in SCHEMA:
            if spec.group != gname:
                continue
            note = "environment only" if not spec.editable else "applies: %s" % spec.applies
            out.append("# %s (%s)" % (spec.description, note))
            if spec.type in ("int", "float") and (spec.min is not None or spec.max is not None):
                out.append("# Range %s to %s." % (_fmt(spec.min) if spec.min is not None else "any",
                                                  _fmt(spec.max) if spec.max is not None else "any"))
            if spec.choices:
                out.append("# One of: %s." % ", ".join(spec.choices))
            d = defaults.get(spec.env)
            out.append("#%s=%s" % (spec.env, display(d) if d is not None else ""))
        out.append("")
    return "\n".join(out)


def markdown_table():
    defaults = _defaults_from_app()
    lines = ["| Setting | Default | Applies | Description |", "|---|---|---|---|"]
    for gname in GROUP_ORDER:
        lines.append("| **%s** | | | |" % gname)
        for spec in SCHEMA:
            if spec.group != gname:
                continue
            d = defaults.get(spec.env)
            dv = display(d) if d is not None else ""
            applies = "env only" if not spec.editable else spec.applies
            lines.append("| `%s` | `%s` | %s | %s |" % (
                spec.env, dv if dv != "" else "(empty)", applies, spec.description))
    return "\n".join(lines)


if __name__ == "__main__":
    if "--env-example" in sys.argv:
        sys.stdout.write(env_example())
    elif "--markdown" in sys.argv:
        sys.stdout.write(markdown_table() + "\n")
    else:
        sys.stderr.write("usage: settings.py --env-example | --markdown\n")
        sys.exit(2)
