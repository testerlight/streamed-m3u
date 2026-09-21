"""Jellyfin lineup membership.

A roster slug is either on the lineup or not. That decision is computed from
/data/lineup.json; it is never stored on a roster row. The playlist still
emits every roster entry — hiding happens in Dispatcharr via
hidden_from_output, so streams stay and channel numbers stay put.

Two policies:

  all        every slug is in except those in exclude. New teams appear in
             Jellyfin automatically. This is also the implicit document when
             the file is missing (this box today).
  allowlist  only the slugs in include. New teams stay streams until added.

The first write materializes the file. Do not copy seed/lineup.json onto an
existing /data — that seed is majors-only and is installed only on the same
first-boot path as seed/teams.json.
"""

import copy
import logging
import os
import threading

import settings

log = logging.getLogger("lineup")

VERSION = 1
POLICIES = ("all", "allowlist")

LINEUP_FILE = os.getenv("LINEUP_FILE", os.path.join(settings.DATA_DIR, "lineup.json"))
SEED_LINEUP_FILE = os.getenv("SEED_LINEUP_FILE", "/app/seed/lineup.json")

_lock = threading.Lock()
_doc = None
_implicit = True
_loaded = False


def implicit_all():
    return {"version": VERSION, "policy": "all", "include": [], "exclude": []}


def normalize(raw):
    """Coerce a loaded object into a lineup document. Unknown keys dropped."""
    out = implicit_all()
    if not isinstance(raw, dict):
        return out
    policy = raw.get("policy")
    if policy in POLICIES:
        out["policy"] = policy
    out["include"] = _slug_list(raw.get("include"))
    out["exclude"] = _slug_list(raw.get("exclude"))
    try:
        out["version"] = int(raw.get("version") or VERSION)
    except (TypeError, ValueError):
        out["version"] = VERSION
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
    doc = doc or implicit_all()
    if doc.get("policy") == "allowlist":
        return slug in set(doc.get("include") or [])
    return slug not in set(doc.get("exclude") or [])


def apply_op(doc, op, slugs):
    """Return a new document after add/remove of slugs.

    + on allowlist adds to include; − removes from include.
    + / − on all only edit exclude (+ clears, − adds).
    The caller writes the result; this does not persist.
    """
    if op not in ("add", "remove"):
        raise ValueError("op must be add or remove")
    wanted = _slug_list(slugs)
    if not wanted:
        raise ValueError("no slugs")
    out = normalize(doc)
    include = list(out["include"])
    exclude = list(out["exclude"])
    if out["policy"] == "allowlist":
        if op == "add":
            have = set(include)
            for slug in wanted:
                if slug not in have:
                    include.append(slug)
                    have.add(slug)
        else:
            drop = set(wanted)
            include = [s for s in include if s not in drop]
        out["include"] = include
        return out
    if op == "remove":
        have = set(exclude)
        for slug in wanted:
            if slug not in have:
                exclude.append(slug)
                have.add(slug)
    else:
        drop = set(wanted)
        exclude = [s for s in exclude if s not in drop]
    out["exclude"] = exclude
    return out


def load(path=None):
    """Load the lineup. Missing file => implicit all. Never raises.

    Returns (doc, implicit). implicit is True when no file was used, so a
    later write is what first materializes it.
    """
    path = path or LINEUP_FILE
    if not os.path.exists(path):
        return implicit_all(), True
    data, err = settings.read_json_with_fallback(path, "lineup")
    if data is None:
        return implicit_all(), True
    return normalize(data), False


def save(doc, path=None):
    path = path or LINEUP_FILE
    settings.atomic_write_json(path, normalize(doc), indent=1, sort_keys=True)


def current():
    """In-memory document and whether it is still implicit (no file yet)."""
    ensure_loaded()
    with _lock:
        return copy.deepcopy(_doc), _implicit


def reload(path=None):
    """Re-read from disk into the process cache."""
    global _doc, _implicit, _loaded
    doc, implicit = load(path)
    with _lock:
        _doc = doc
        _implicit = implicit
        _loaded = True
    return copy.deepcopy(doc), implicit


def ensure_loaded():
    with _lock:
        loaded = _loaded
    if not loaded:
        reload()


def persist(doc, path=None):
    """Write then adopt. Used by the first − and every later console edit."""
    global _doc, _implicit, _loaded
    clean = normalize(doc)
    save(clean, path)
    with _lock:
        _doc = clean
        _implicit = False
        _loaded = True
    return copy.deepcopy(clean)


def install_seed(lineup_path=None, seed_path=None, teams_existed=False):
    """Copy the bundled allowlist only on the same first-boot as the roster.

    teams_existed must be False — the caller checks TEAMS_FILE before copying
    the roster. An existing /data is never given the seed lineup, even if
    lineup.json is missing (that absence means implicit-all on this box).
    """
    if teams_existed:
        return False
    lineup_path = lineup_path or LINEUP_FILE
    seed_path = seed_path or SEED_LINEUP_FILE
    if os.path.exists(lineup_path) or not os.path.exists(seed_path):
        return False
    try:
        os.makedirs(os.path.dirname(lineup_path) or ".", exist_ok=True)
        data, err = settings.read_json_with_fallback(seed_path, "seed lineup")
        if data is None:
            return False
        clean = normalize(data)
        save(clean, lineup_path)
        reload(lineup_path)
        log.info("Installed seed lineup (%s) -> %s",
                 clean.get("policy"), lineup_path)
        return True
    except Exception as e:
        log.warning("Could not install seed lineup: %s", e)
        return False


def seed_document(slugs, comment=None):
    """Allowlist document for seed/lineup.json. Majors only."""
    doc = {
        "version": VERSION,
        "policy": "allowlist",
        "include": _slug_list(slugs),
        "exclude": [],
    }
    if comment:
        doc["comment"] = comment
    return doc


def count_in(doc, slugs):
    return sum(1 for s in slugs if in_lineup(doc, s))
