"""Multi-view slot configuration.

A multi-view slot is a permanent channel carrying two fixtures at once: a
primary filling the frame and an optional secondary in a miniplayer corner.
Which fixtures those are lives here, in /data/multiview.json, and never on a
roster row — the channel is a shelf, and what sits on it changes without the
channel's address, number or guide link changing with it.

The document is slot-keyed, and a slot id is a string because JSON object
keys are strings; "1" and 1 must not become two different slots.

    {"version": 1,
     "slots": {"1": {"primary": "kansas-city-chiefs",
                     "secondary": "baltimore-orioles",
                     "corner": "br", "size": "medium",
                     "audio": {"primary": 100, "secondary": 0},
                     "updated": "2026-09-20T22:04:11Z"}}}

Three rules are enforced here rather than trusted to the caller, because this
file is hand-editable and a console is not the only way in:

  - A secondary without a primary is dropped. The miniplayer is defined
    relative to a main picture; without one there is nothing to overlay onto,
    and a slot in that state would look configured while being unplayable.
  - Unknown slugs are dropped. A fixture removed from the roster leaves the
    slot unconfigured rather than pointing the encoder at nothing.
  - Gains are clamped, corners and sizes fall back to their defaults. A bad
    value degrades one field instead of failing the slot.

Nothing here starts, stops or talks to an encoder. This module only answers
"what should slot N be showing", and is safe to import with the feature off.
"""

import copy
import logging
import os
import threading
import time

import settings

log = logging.getLogger("multiview")

VERSION = 1

# Miniplayer corners, as stored. The console shows them as a 2x2 grid.
CORNERS = ("tl", "tr", "bl", "br")
DEFAULT_CORNER = "br"

# Miniplayer sizes, as a fraction of the composite's width. Kept as names
# rather than numbers so the console and the filter graph cannot disagree
# about what "medium" means.
SIZES = {"small": 0.22, "medium": 0.30, "large": 0.40}
DEFAULT_SIZE = "medium"

# Sound follows the primary by default: the secondary is something you glance
# at, not something you listen to, until you say otherwise.
DEFAULT_AUDIO = {"primary": 100, "secondary": 0}

MULTIVIEW_FILE = os.getenv("MULTIVIEW_FILE", os.path.join(settings.DATA_DIR, "multiview.json"))

_lock = threading.Lock()
_doc = None
_loaded = False


def empty_slot():
    return {"primary": "", "secondary": "", "corner": DEFAULT_CORNER,
            "size": DEFAULT_SIZE, "audio": dict(DEFAULT_AUDIO), "updated": ""}


def empty_doc(slots=0):
    return {"version": VERSION,
            "slots": {str(i + 1): empty_slot() for i in range(max(0, int(slots or 0)))}}


def slot_ids(count):
    return [str(i + 1) for i in range(max(0, int(count or 0)))]


def _slug(value, known=None):
    """A roster slug, or "" if absent or not a channel we know about."""
    slug = str(value or "").strip().lower()
    if not slug:
        return ""
    if known is not None and slug not in known:
        return ""
    return slug


def _gain(value, fallback):
    try:
        num = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return max(0, min(100, num))


def normalize_slot(raw, known=None):
    """Coerce one slot into a valid configuration. Never raises."""
    out = empty_slot()
    if not isinstance(raw, dict):
        return out

    out["primary"] = _slug(raw.get("primary"), known)
    secondary = _slug(raw.get("secondary"), known)
    # The miniplayer only exists over a main picture. This also covers the
    # case where the primary was dropped just above for being unknown.
    out["secondary"] = secondary if out["primary"] else ""
    # A slug cannot be shown against itself; the second copy would be
    # indistinguishable and would cost a whole extra decode to say nothing.
    if out["secondary"] and out["secondary"] == out["primary"]:
        out["secondary"] = ""

    corner = str(raw.get("corner") or "").strip().lower()
    out["corner"] = corner if corner in CORNERS else DEFAULT_CORNER
    size = str(raw.get("size") or "").strip().lower()
    out["size"] = size if size in SIZES else DEFAULT_SIZE

    audio = raw.get("audio")
    audio = audio if isinstance(audio, dict) else {}
    out["audio"] = {
        "primary": _gain(audio.get("primary"), DEFAULT_AUDIO["primary"]),
        "secondary": _gain(audio.get("secondary"), DEFAULT_AUDIO["secondary"]),
    }
    # Nothing to hear from a miniplayer that is not there.
    if not out["secondary"]:
        out["audio"]["secondary"] = 0

    updated = str(raw.get("updated") or "").strip()
    out["updated"] = updated[:40]
    return out


def normalize(raw, slots=None, known=None):
    """Coerce a loaded object into a document. Unknown keys dropped.

    `slots` is the configured slot count. When given, the document is squared
    up to exactly that many slots: extras are dropped and missing ones are
    created empty, so the rest of the service can index a slot without
    checking whether it exists. When None, whatever slots are present are
    kept, which is what a plain file read wants.
    """
    out = {"version": VERSION, "slots": {}}
    src = raw.get("slots") if isinstance(raw, dict) else None
    src = src if isinstance(src, dict) else {}

    if slots is None:
        ids = [str(k) for k in src.keys() if str(k).isdigit()]
        ids.sort(key=int)
    else:
        ids = slot_ids(slots)

    for sid in ids:
        out["slots"][sid] = normalize_slot(src.get(sid) or src.get(int(sid)), known)

    try:
        out["version"] = int(raw.get("version") or VERSION)
    except (TypeError, ValueError, AttributeError):
        out["version"] = VERSION
    return out


def configured(slot):
    """Whether this slot has enough to play. A primary is the whole bar."""
    return bool((slot or {}).get("primary"))


def sources(slot):
    """Slugs this slot needs resolved, primary first. Empty when unconfigured."""
    slot = slot or {}
    out = []
    if slot.get("primary"):
        out.append(slot["primary"])
    if slot.get("secondary"):
        out.append(slot["secondary"])
    return out


def touch(slot):
    """Stamp a slot as just changed. Returns the same dict."""
    slot["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return slot


# ─── Persistence ──────────────────────────────────────────────────────────────
# Same discipline as the roster and settings: atomic write with a .bak kept,
# and a corrupt file moved aside rather than read. A slot configuration is
# cheap to recreate, so the fallback chain ends at empty rather than trying
# hard to recover something half-parsed.

def load(path=None, slots=None, known=None):
    """Load the document. Missing or unreadable file => empty. Never raises.

    `known` is the set of roster slugs; anything outside it is dropped, so a
    slot pointing at a channel that no longer exists comes back unconfigured
    rather than sending the encoder after nothing.
    """
    path = path or MULTIVIEW_FILE
    if not os.path.exists(path):
        return empty_doc(slots or 0)
    data, err = settings.read_json_with_fallback(path, "multiview")
    if data is None:
        return empty_doc(slots or 0)
    return normalize(data, slots, known)


def save(doc, path=None, slots=None):
    path = path or MULTIVIEW_FILE
    settings.atomic_write_json(path, normalize(doc, slots), indent=1, sort_keys=True)


def current():
    ensure_loaded()
    with _lock:
        return copy.deepcopy(_doc)


def reload(path=None, slots=None, known=None):
    """Re-read from disk into the process cache."""
    global _doc, _loaded
    doc = load(path, slots, known)
    with _lock:
        _doc = doc
        _loaded = True
    return copy.deepcopy(doc)


def ensure_loaded():
    with _lock:
        loaded = _loaded
    if not loaded:
        reload()


def persist(doc, path=None, slots=None):
    """Write then adopt."""
    global _doc, _loaded
    clean = normalize(doc, slots)
    save(clean, path, slots)
    with _lock:
        _doc = clean
        _loaded = True
    return copy.deepcopy(clean)


def get_slot(sid):
    """One slot from the in-memory document, or an empty one."""
    doc = current()
    return (doc.get("slots") or {}).get(str(sid)) or empty_slot()


def set_slot(sid, slot, path=None, slots=None, known=None):
    """Replace one slot and persist the whole document.

    Returns the cleaned slot as stored, so a caller can show what actually
    landed rather than what it asked for — the two differ whenever a rule
    above fired.
    """
    sid = str(sid)
    doc = current()
    doc.setdefault("slots", {})
    doc["slots"][sid] = touch(normalize_slot(slot, known))
    clean = persist(doc, path, slots)
    return (clean.get("slots") or {}).get(sid) or empty_slot()


def merge_slot(sid, changes, path=None, slots=None, known=None):
    """Apply a partial change to one slot, keeping what it does not mention.

    The counterpart to set_slot, and the one almost every caller wants. A
    console sends "the corner is now top left", not a whole configuration, and
    putting that through set_slot silently drops the channels it did not
    mention — which does not look like a bad request, it looks like the stream
    dying for no reason. The audio map merges the same way, so setting one
    gain does not mute the other.
    """
    sid = str(sid)
    base = dict(get_slot(sid))
    if isinstance(changes, dict):
        for key in ("primary", "secondary", "corner", "size"):
            if key in changes:
                base[key] = changes[key]
        incoming = changes.get("audio")
        if isinstance(incoming, dict):
            audio = dict(base.get("audio") or {})
            for key in ("primary", "secondary"):
                if key in incoming:
                    audio[key] = incoming[key]
            base["audio"] = audio
    return set_slot(sid, base, path=path, slots=slots, known=known)


def clear_slot(sid, path=None, slots=None):
    """Return a slot to unconfigured. The channel stays; its contents go."""
    return set_slot(sid, empty_slot(), path, slots)
