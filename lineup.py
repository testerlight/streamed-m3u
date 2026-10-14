"""Jellyfin lineup membership.

A roster slug is either on the lineup or not. That decision is computed from
the allowlist in /data/lineup.json; it is never stored on a roster row. The
playlist still emits every roster entry, so every team stays a Dispatcharr
stream. Only allowlisted slugs get a Dispatcharr channel, and so only they
reach Jellyfin. Nothing is ever added automatically: a team seen for the
first time stays a stream until someone presses + in the console.

A missing file means the bundled default set (seed/lineup.json), never
"everything". The file keeps `"policy": "allowlist"` so that an older build
reading it can never mistake it for its own show-everything mode.

Startup runs `prepare()`, which installs the defaults when the file is
missing and converts a legacy `policy: "all"` file once, into an allowlist of
exactly the slugs that were visible at that moment.
"""

import copy
import logging
import os
import shutil
import threading

import settings

log = logging.getLogger("lineup")

VERSION = 1
POLICY = "allowlist"

LINEUP_FILE = os.getenv("LINEUP_FILE", os.path.join(settings.DATA_DIR, "lineup.json"))
SEED_LINEUP_FILE = os.getenv("SEED_LINEUP_FILE", "/app/seed/lineup.json")

_lock = threading.Lock()
_doc = None
_loaded = False


def empty():
    return {"version": VERSION, "policy": POLICY, "include": []}


def normalize(raw):
    """Coerce a loaded object into a lineup document. Unknown keys dropped.

    A legacy `policy: "all"` document cannot be converted here (that needs the
    roster), so it reads as an empty allowlist until `prepare()` has run.
    """
    out = empty()
    if isinstance(raw, dict):
        out["include"] = _slug_list(raw.get("include"))
    return out


def _slug_list(value):
    if not isinstance(value, (list, tuple)):
        return []
    seen, out = set(), []
    for item in value:
        slug = str(item or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def in_lineup(doc, slug):
    """Whether this slug should be a visible Dispatcharr channel."""
    if not slug:
        return False
    return slug in set((doc or {}).get("include") or [])


def apply_op(doc, op, slugs):
    """Return a new document after add/remove of slugs.

    The caller writes the result; this does not persist.
    """
    if op not in ("add", "remove"):
        raise ValueError("op must be add or remove")
    wanted = _slug_list(slugs)
    if not wanted:
        raise ValueError("no slugs")
    out = normalize(doc)
    if op == "add":
        have = set(out["include"])
        out["include"].extend(s for s in wanted if s not in have)
    else:
        drop = set(wanted)
        out["include"] = [s for s in out["include"] if s not in drop]
    return out


def default_doc(seed_path=None):
    """The bundled default lineup. Empty when the seed is missing or unreadable."""
    seed_path = seed_path or SEED_LINEUP_FILE
    data, _err = settings.read_json_with_fallback(seed_path, "seed lineup")
    return normalize(data)


def load(path=None):
    """Load the lineup. Missing or unreadable => the bundled defaults.

    Never raises. Writing is what materializes the file.
    """
    path = path or LINEUP_FILE
    data, _err = settings.read_json_with_fallback(path, "lineup")
    if data is None:
        return default_doc()
    return normalize(data)


def save(doc, path=None):
    path = path or LINEUP_FILE
    settings.atomic_write_json(path, normalize(doc), indent=1, sort_keys=True)


def current():
    """In-memory document."""
    ensure_loaded()
    with _lock:
        return copy.deepcopy(_doc)


def reload(path=None):
    """Re-read from disk into the process cache."""
    doc = load(path)
    return _adopt(doc)


def ensure_loaded():
    with _lock:
        loaded = _loaded
    if not loaded:
        reload()


def _adopt(doc):
    global _doc, _loaded
    clean = normalize(doc)
    with _lock:
        _doc = clean
        _loaded = True
    return copy.deepcopy(clean)


def persist(doc, path=None):
    """Write then adopt. Used by every console edit."""
    clean = normalize(doc)
    save(clean, path)
    return _adopt(clean)


def prepare(roster_slugs, path=None, seed_path=None):
    """Startup: leave a valid allowlist on disk, then load it. Returns what it did.

    "seeded"    no usable file: the bundled defaults were written.
    "migrated"  a legacy `policy: "all"` file: converted to an allowlist of
                every roster slug it was showing (roster minus exclude), so
                Jellyfin sees exactly what it saw before. The old file is
                kept beside it as `<name>.pre-allowlist`.
    "ok"        already an allowlist; untouched.
    "unsaved"   a change was needed but the write failed; the in-memory
                lineup is still the right one and the next start retries.

    Run after the roster is loaded and the feed/series/pool slugs are seeded,
    so a slug created this boot still counts as visible under a legacy file.
    """
    path = path or LINEUP_FILE
    data, _err = settings.read_json_with_fallback(path, "lineup")
    if isinstance(data, dict) and data.get("policy") != "all":
        _adopt(data)
        return "ok"
    if isinstance(data, dict):
        hidden = set(_slug_list(data.get("exclude")))
        doc = normalize({"include": [s for s in roster_slugs if s not in hidden]})
        action = "migrated"
    else:
        doc = default_doc(seed_path)
        action = "seeded"
    try:
        if action == "migrated":
            keep = path + ".pre-allowlist"
            if not os.path.exists(keep):
                shutil.copyfile(path, keep)
        persist(doc, path)
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)
        _adopt(doc)
        action = "unsaved"
    log.info("Lineup %s: %d channel(s) on the Jellyfin allowlist",
             action, len(doc["include"]))
    return action


def count_in(doc, slugs):
    return sum(1 for s in slugs if in_lineup(doc, s))
