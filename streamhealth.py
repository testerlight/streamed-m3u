"""streamhealth: watch-only health tracking for proxied streams.

Phase 1 of the stream-health work (.scratch/stream-health/spec.md). It
observes and never acts: nothing here may change what a stream sends, or when.

Two rules follow from that, and both are load-bearing:

- **Every hook is guarded.** The stream loop answers any exception with a
  two-second idle, so an unguarded bug in here would itself cause the freezes
  this module exists to measure. A failing hook logs once per stream and
  returns None; the stream never sees it.
- **Hooks do no I/O.** The history line is written by a background thread
  after the stream has ended.

The model
---------
The *cushion* is how many seconds of video have been sent ahead of a player
that started at our first byte and plays in real time. Each delivered chunk
adds its EXTINF duration and the wall clock drains it. When it reaches zero
the picture is, as far as this side can tell, frozen. Dispatcharr and Jellyfin
buffer a little of their own, so the real player is slightly more patient
than this model: the estimate errs towards reporting a freeze, never towards
hiding one.

While a stream runs, its wall clock is labelled with what the loop was doing
(downloading, waiting for the source, fetching the playlist, sending). A
freeze's cause is the label that covered most of it. Time spent blocked on a
player that is not reading is recorded, but it is not a freeze: that is the
player's choice, not a failure on this side.
"""

import json
import logging
import os
import queue
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime
from functools import wraps

log = logging.getLogger("streamhealth")

# Starvation shorter than this is scheduling noise and is ignored.
STARVE_MIN_S = 0.3
# Stalls closer together than this are one freeze to a viewer: below real
# time a stream plays a chunk, stops, plays a chunk. Counted one stall at a
# time, each gap can fall under any threshold and the stutter vanishes from
# the figures. The window is this or 1.5 chunk lengths, whichever is longer,
# so a 6s-chunk source stuttering every chunk still reads as one bad patch.
MERGE_S = 3.0
# A freeze is an episode with at least this much starved time in it.
FREEZE_MIN_S = 1.0
# A download that receives nothing for this long is stalled, not slow.
STALL_GAP_S = 5.0
# A new stream for the same channel this soon after one ended is a reconnect.
RECONNECT_WINDOW_S = 60.0
# Chunks in the rolling speed figure.
SPEED_WINDOW = 5
# Below this many seconds of cushion a stream is about to freeze.
LOW_CUSHION_S = 2.0
# Used only when a playlist gives neither EXTINF nor a target duration.
DEFAULT_DURATION_S = 4.0
TIMELINE_MAX = 40
FREEZE_LIST_MAX = 50
SAMPLES_MAX = 5000
HISTORY_KEEP = 500

CAUSES = {
    "slow":     "Downloads too slow",
    "stalled":  "Download stalled or failing",
    "source":   "Source not publishing",
    "playlist": "Playlist slow or failing",
    "internal": "Internal error",
    "unknown":  "Unknown",
}

END_REASONS = {
    "client_left":       "Player disconnected",
    "console":           "Stopped from the console",
    "idle_timeout":      "Idle limit: no new video",
    "playlist_failures": "Playlist failed repeatedly",
    "fatal":             "Source link dead",
}

# What the loop was doing -> what a freeze during it is blamed on. "sending"
# is absent on purpose: blocked on the player is not a freeze.
_LABEL_CAUSE = {
    "download":         "slow",
    "download_stalled": "stalled",
    "waiting":          "source",
    "playlist":         "playlist",
    "playlist_error":   "playlist",
    "internal":         "internal",
    "starting":         "unknown",
}


# ─── Configuration ────────────────────────────────────────────────────────────

_key_fn = None


def configure(key_fn=None, history_path=None, keep=HISTORY_KEEP):
    """Wire in app.py's segment identity and the history file location.

    key_fn must be the same _segment_key the dedup uses, so a chunk the
    playlist re-signed is still recognised as the same chunk here.
    """
    global _key_fn
    if key_fn is not None:
        _key_fn = key_fn
    if history_path:
        HISTORY.configure(history_path, keep)


def _key(url):
    try:
        return _key_fn(url) if _key_fn else url
    except Exception:
        return url


# ─── Playlist parsing ─────────────────────────────────────────────────────────

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _num(value, cast):
    try:
        return cast(str(value).strip().strip('"'))
    except (TypeError, ValueError):
        return None


def _pdt(value):
    try:
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v).timestamp()
    except (AttributeError, ValueError):
        return None


def parse_playlist(text, base_url, resolve):
    """Read the tags get_segments() ignores. Pure; raises nothing it can help.

    Classifies lines exactly as get_segments does (a URI whose path contains
    .m3u8 is a sub-playlist), so the segments here are the segments it
    returns, in the same order.
    """
    out = {"kind": "media", "variants": [], "target_duration": None,
           "media_sequence": None, "endlist": False, "segments": []}
    dur = None
    clock = None
    disc = False
    variant = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            tag, _, value = line.partition(":")
            if tag == "#EXT-X-STREAM-INF":
                attrs = dict(_ATTR_RE.findall(value))
                variant = {"bandwidth": _num(attrs.get("BANDWIDTH"), int),
                           "resolution": attrs.get("RESOLUTION") or None}
            elif tag == "#EXT-X-TARGETDURATION":
                out["target_duration"] = _num(value, float)
            elif tag == "#EXT-X-MEDIA-SEQUENCE":
                out["media_sequence"] = _num(value, int)
            elif tag == "#EXTINF":
                dur = _num(value.split(",", 1)[0], float)
            elif tag == "#EXT-X-PROGRAM-DATE-TIME":
                clock = _pdt(value)
            elif tag == "#EXT-X-DISCONTINUITY":
                disc = True
            elif tag == "#EXT-X-ENDLIST":
                out["endlist"] = True
            continue
        url = resolve(base_url, line)
        if ".m3u8" in url.split("?")[0]:
            out["kind"] = "master"
            out["variants"].append(variant or {"bandwidth": None, "resolution": None})
            variant = None
            continue
        out["segments"].append({"url": url, "index": len(out["segments"]),
                                "duration": dur, "pdt": clock,
                                "discontinuity": disc})
        if clock is not None and dur:
            clock += dur
        dur = None
        disc = False
    if out["kind"] == "master" and out["segments"]:
        out["kind"] = "media"   # get_segments only recurses when no segments
    return out


# Segment identity -> (sequence, duration, discontinuity). Global rather than
# per stream so the probe's playlist (fetched before any stream exists) still
# supplies durations for the first chunks a stream sends.
_meta_lock = threading.Lock()
_meta = OrderedDict()
_META_MAX = 5000


def note_playlist(url, text, resolve, info=None):
    """Called by get_segments for every playlist it reads. Never raises."""
    try:
        p = parse_playlist(text, url, resolve)
        if p["kind"] == "master":
            if info is not None and "variants" not in info:
                info["variants"] = p["variants"]
            return
        ms = p["media_sequence"]
        target = p["target_duration"]
        latest = []
        with _meta_lock:
            for s in p["segments"]:
                k = _key(s["url"])
                seq = ms + s["index"] if ms is not None else None
                d = s["duration"] or target
                _meta[k] = (seq, d, s["discontinuity"])
                _meta.move_to_end(k)
                latest.append((k, seq, d))
            while len(_meta) > _META_MAX:
                _meta.popitem(last=False)
        if info is not None:
            info.update(target_duration=target, media_sequence=ms,
                        endlist=p["endlist"], latest=latest)
    except Exception:
        _warn_once("parse", "Playlist parse failed (tracking only)")


_warned = set()


def _warn_once(key, msg):
    if key in _warned:
        return
    _warned.add(key)
    try:
        log.warning("%s; playback is unaffected", msg, exc_info=True)
    except Exception:
        pass


# ─── Reconnect memory ─────────────────────────────────────────────────────────

_recent_lock = threading.Lock()
_recent = {}


def _remember_ended(channel_key, now, reason, stream_id, keys):
    if not channel_key:
        return
    with _recent_lock:
        _recent[channel_key] = {"ended": now, "reason": reason,
                                "id": stream_id, "keys": keys}
        for k in [k for k, v in _recent.items()
                  if now - v["ended"] > RECONNECT_WINDOW_S]:
            _recent.pop(k, None)


def _match_reconnect(channel_key, now):
    if not channel_key:
        return None
    with _recent_lock:
        r = _recent.get(channel_key)
    if not r or now - r["ended"] > RECONNECT_WINDOW_S:
        return None
    return {"after_s": round(now - r["ended"], 1), "previous_id": r["id"],
            "previous_reason": r["reason"], "replayed_chunks": 0,
            "replayed_s": 0.0, "_keys": set(r["keys"])}


# ─── Per-stream tracker ───────────────────────────────────────────────────────

def _guarded(fn):
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            try:
                if not self._hook_failed:
                    self._hook_failed = True
                    log.warning("Stream health tracking error in %s (id=%s); "
                                "playback is unaffected", fn.__name__,
                                getattr(self, "id", "?"), exc_info=True)
            except Exception:
                pass
            return None
    return wrapper


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


def _r(v, n=1):
    return None if v is None else round(v, n)


class StreamHealth:
    """Everything known about one stream's health. Thread-safe.

    Hooks are called from the response generator and from the download worker
    thread; snapshot() from request threads. One RLock covers all of it, and
    no hook holds it for longer than a few dictionary operations.
    """

    def __init__(self, stream_id, channel_key="", channel="", source="",
                 multiview=None, concurrency=None, clock=time.time):
        self._hook_failed = False
        self._lock = threading.RLock()
        self._now = clock
        now = clock()
        self.id = stream_id
        self.channel_key = channel_key
        self.channel = channel
        self.source = source
        self.multiview = multiview
        self.started = now
        self._concurrency = concurrency
        self.concurrent_max = 1

        self._spans = deque(maxlen=2000)
        self._label = "starting"
        self._label_since = now
        self._spans.append([now, "starting"])

        self._cushion = 0.0
        self._cushion_at = None
        self.first_delivery = None
        self.last_delivery = None
        self.video_s = 0.0
        self.chunks = 0
        self.bytes = 0
        self.durations_guessed = 0

        self._episode = None
        self._last_dur = None
        self.blips = 0
        self.blip_s = 0.0
        self.freeze_count = 0
        self.frozen_s = 0.0
        self.by_cause = {}
        self.freezes = []
        self.longest_freeze = 0.0
        self.player_waits = 0
        self.player_wait_s = 0.0

        self._dl = None
        self._speeds = deque(maxlen=SPEED_WINDOW)
        self._ratios = deque(maxlen=SAMPLES_MAX)
        self._mbits = deque(maxlen=SAMPLES_MAX)
        self.downloads_failed = 0
        self._failed_since = 0

        self.playlist_errors = 0
        self._pl_run = 0
        self._pl_status = None
        self.last_new_chunk_at = None
        self.target_duration = None
        self.variants = None
        self._last_ms = None
        self._latest = []
        self.endlist = False
        self.playlist_backwards = 0

        self._last_seq = None
        self.skipped = 0
        self.skipped_s = 0.0
        self.backwards = 0
        self.discontinuities = 0
        self.behind_now = None
        self._behind = deque(maxlen=SAMPLES_MAX)

        self.internal_errors = 0
        self._seen_errors = set()

        self.end_reason = None
        self.end_detail = None
        self.ended = None
        self.timeline = deque(maxlen=TIMELINE_MAX)
        self._keys = deque(maxlen=12)

        self.reconnect = _match_reconnect(channel_key, now)
        self._event("start", "Started" + (" on %s" % source if source else ""))
        if self.reconnect:
            r = self.reconnect
            msg = ("Reconnected %.0fs after stream %s ended (%s)"
                   % (r["after_s"], r["previous_id"],
                      END_REASONS.get(r["previous_reason"], r["previous_reason"])))
            self._event("reconnect", msg)
            log.info("%s: %s", self._who(), msg)

    # ── internals (caller holds the lock) ────────────────────────────────────

    def _who(self):
        return "Stream %s (%s)" % (self.id, self.channel or "?")

    def _event(self, kind, text, now=None):
        self.timeline.append({"ts": round(now or self._now(), 1),
                              "kind": kind, "text": text})

    def _enter(self, label, now):
        if label == self._label:
            return
        self._spans.append([now, label])
        self._label = label
        self._label_since = now

    def _relabel(self, since, label):
        for span in self._spans:
            if span[0] >= since and span[1] in ("download", "download_stalled"):
                span[1] = label
        if self._label in ("download", "download_stalled"):
            self._label = label

    def _attribute(self, start, end):
        out = {}
        spans = list(self._spans)
        for i, (t0, label) in enumerate(spans):
            t1 = spans[i + 1][0] if i + 1 < len(spans) else end
            a, b = max(t0, start), min(t1, end)
            if b > a:
                out[label] = out.get(label, 0.0) + (b - a)
        return out

    def _duration(self, key):
        with _meta_lock:
            m = _meta.get(key)
        if m and m[1]:
            return m[1], m[0], m[2]
        self.durations_guessed += 1
        return (self.target_duration or DEFAULT_DURATION_S,
                m[0] if m else None, bool(m and m[2]))

    def _starved(self, start, end, still_frozen=False):
        """One stretch with the cushion empty. Returns True if it counted."""
        length = end - start
        if length < STARVE_MIN_S:
            return False
        spent = self._attribute(start, end)
        player = spent.pop("sending", 0.0)
        blamed = {}
        for label, secs in spent.items():
            cause = _LABEL_CAUSE.get(label, "unknown")
            blamed[cause] = blamed.get(cause, 0.0) + secs
        total = sum(blamed.values())
        if total <= 0 or player >= total:
            self.player_waits += 1
            self.player_wait_s += length
            self._event("player", "Player not reading for %.0fs" % length, end)
            return False
        ep = self._episode
        if ep is not None and start - ep["last_end"] > self._merge_window():
            self._close_episode()
            ep = None
        if ep is None:
            ep = self._episode = {"start": start, "last_end": end,
                                  "starved": 0.0, "stalls": 0, "blamed": {},
                                  "open": False}
        ep["last_end"] = end
        ep["starved"] += length
        ep["stalls"] += 1
        ep["open"] = still_frozen
        # Scaled so an episode's causes add up to its starved time.
        for cause, secs in blamed.items():
            ep["blamed"][cause] = ep["blamed"].get(cause, 0.0) + secs * length / total
        return True

    def _merge_window(self):
        chunk = self.target_duration or self._last_dur or 0.0
        return max(MERGE_S, 1.5 * chunk)

    def _close_episode(self):
        ep, self._episode = self._episode, None
        if ep is None:
            return
        if ep["starved"] < FREEZE_MIN_S:
            self.blips += 1
            self.blip_s += ep["starved"]
            return
        cause = max(ep["blamed"], key=ep["blamed"].get)
        self.freeze_count += 1
        self.frozen_s += ep["starved"]
        self.longest_freeze = max(self.longest_freeze, ep["starved"])
        for c, secs in ep["blamed"].items():
            self.by_cause[c] = self.by_cause.get(c, 0.0) + secs
        span = ep["last_end"] - ep["start"]
        if len(self.freezes) < FREEZE_LIST_MAX:
            self.freezes.append({"at_s": round(ep["start"] - self.started, 1),
                                 "s": round(ep["starved"], 1),
                                 "span_s": round(span, 1),
                                 "stalls": ep["stalls"], "cause": cause,
                                 "open": ep["open"]})
        shape = ("" if ep["stalls"] == 1 else
                 " (%d stalls over %.0fs)" % (ep["stalls"], span))
        text = "%s %.1fs%s: %s" % ("Frozen at end," if ep["open"] else "Froze",
                                   ep["starved"], shape, CAUSES[cause])
        self._event("freeze", text, ep["last_end"])
        log.warning("%s froze for %.1fs%s%s: %s", self._who(), ep["starved"],
                    shape, " (still frozen when it ended)" if ep["open"] else "",
                    CAUSES[cause])

    def _speed(self):
        if not self._speeds:
            return None, None
        v = sum(s[0] for s in self._speeds)
        t = sum(s[1] for s in self._speeds)
        b = sum(s[2] for s in self._speeds)
        if t <= 0:
            return None, None
        return v / t, b * 8 / t / 1e6

    def _cushion_now(self, now):
        if self._cushion_at is None:
            return None, 0.0
        empty_at = self._cushion_at + self._cushion
        if now < empty_at:
            return empty_at - now, 0.0
        return 0.0, now - empty_at

    def _activity_text(self, now):
        label = self._label
        held = now - self._label_since
        if label in ("download", "download_stalled") and self._dl:
            dl = self._dl
            quiet = now - dl["last"]
            mb = dl["bytes"] / 1e6
            if dl["attempts"] > 1:
                return "Retrying a chunk (attempt %d)" % dl["attempts"]
            if quiet >= STALL_GAP_S:
                return "Download stalled: no data for %.0fs" % quiet
            return "Downloading a chunk for %.0fs (%.1f MB so far)" % (
                now - dl["began"], mb)
        if label in ("waiting", "playlist"):
            since = self.last_new_chunk_at or self.started
            return "Source has not published a new chunk for %.0fs" % (now - since)
        if label == "playlist_error":
            return "Playlist failing (%s, %d in a row)" % (
                self._pl_status, self._pl_run)
        if label == "internal":
            return "Internal error, retrying"
        if label == "sending":
            return "Waiting on the player to read (%.0fs)" % held
        return "Starting"

    # ── hooks: downloads (worker thread) ─────────────────────────────────────

    @_guarded
    def download_begin(self, url):
        with self._lock:
            now = self._now()
            self._dl = {"url": url, "began": now, "last": now, "bytes": 0,
                        "attempts": 1, "gap": 0.0}
            self._enter("download", now)

    @_guarded
    def download_attempt(self, attempt):
        with self._lock:
            dl = self._dl
            if dl is None or attempt <= 1:
                return
            now = self._now()
            dl["gap"] = max(dl["gap"], now - dl["last"])
            dl["last"] = now
            dl["attempts"] = attempt
            self._enter("download_stalled", now)

    @_guarded
    def download_bytes(self, n):
        with self._lock:
            dl = self._dl
            if dl is None:
                return
            now = self._now()
            gap = now - dl["last"]
            if gap > dl["gap"]:
                dl["gap"] = gap
            dl["last"] = now
            dl["bytes"] += n

    @_guarded
    def download_end(self, url, body):
        with self._lock:
            dl, self._dl = self._dl, None
            if dl is None:
                return
            now = self._now()
            dl["gap"] = max(dl["gap"], now - dl["last"])
            secs = now - dl["began"]
            if body is None or dl["attempts"] > 1 or dl["gap"] >= STALL_GAP_S:
                self._relabel(dl["began"], "download_stalled")
            if body is None:
                self.downloads_failed += 1
                self._failed_since += 1
                self._event("dropped", "Chunk dropped after %d attempt(s) and %.0fs"
                            % (dl["attempts"], secs), now)
                return
            dur, _seq, _disc = self._duration(_key(url))
            secs = max(secs, 0.001)
            self._speeds.append((dur, secs, len(body)))
            self._ratios.append(dur / secs)
            self._mbits.append(len(body) * 8 / secs / 1e6)

    # ── hooks: delivery (generator thread) ───────────────────────────────────

    @_guarded
    def sending(self):
        with self._lock:
            self._enter("sending", self._now())

    @_guarded
    def delivered(self, url, nbytes):
        with self._lock:
            now = self._now()
            key = _key(url)
            dur, seq, disc = self._duration(key)

            starved = False
            if self._cushion_at is None:
                self.first_delivery = now
                cushion = dur
            else:
                empty_at = self._cushion_at + self._cushion
                if now > empty_at:
                    starved = self._starved(empty_at, now)
                    cushion = dur
                else:
                    cushion = (empty_at - now) + dur
            if (not starved and self._episode is not None
                    and now - self._episode["last_end"] > self._merge_window()):
                self._close_episode()
            self._last_dur = dur
            self._cushion = cushion
            self._cushion_at = now
            self.video_s += dur
            self.chunks += 1
            self.bytes += nbytes
            self.last_delivery = now

            if seq is not None and self._last_seq is not None:
                if seq > self._last_seq + 1:
                    missing = seq - self._last_seq - 1
                    expired = max(0, missing - self._failed_since)
                    if expired:
                        self.skipped += expired
                        self.skipped_s += expired * dur
                        msg = ("Skipped %d chunk(s) (~%.0fs): they left the "
                               "playlist before they were fetched"
                               % (expired, expired * dur))
                        self._event("skip", msg, now)
                        log.info("%s: %s", self._who(), msg)
                elif seq <= self._last_seq:
                    self.backwards += 1
                    msg = ("Sent an older chunk: sequence %d after %d"
                           % (seq, self._last_seq))
                    self._event("backwards", msg, now)
                    log.info("%s: %s", self._who(), msg)
            if seq is not None:
                self._last_seq = seq
            self._failed_since = 0
            if disc:
                self.discontinuities += 1

            behind = None
            if self._latest:
                if seq is not None and self._latest[-1][1] is not None:
                    behind = sum(d or 0 for _k, s, d in self._latest
                                 if s is not None and s > seq)
                else:
                    keys = [k for k, _s, _d in self._latest]
                    if key in keys:
                        behind = sum(d or 0 for _k, _s, d
                                     in self._latest[keys.index(key) + 1:])
            if behind is not None:
                self.behind_now = behind
                self._behind.append(behind)

            r = self.reconnect
            if r is not None and self.chunks <= 12:
                if key in r["_keys"]:
                    r["replayed_chunks"] += 1
                    r["replayed_s"] += dur
                if self.chunks == 12 and r["replayed_chunks"]:
                    self._replay_note(now)
            self._keys.append(key)

            if self._concurrency is not None:
                try:
                    self.concurrent_max = max(self.concurrent_max,
                                              int(self._concurrency()))
                except Exception:
                    pass

    def _replay_note(self, now):
        r = self.reconnect
        msg = ("Replayed %d chunk(s) (~%.0fs) already sent before the reconnect"
               % (r["replayed_chunks"], r["replayed_s"]))
        self._event("replay", msg, now)
        log.info("%s: %s", self._who(), msg)

    # ── hooks: playlist (generator thread) ───────────────────────────────────

    @_guarded
    def playlist_begin(self):
        with self._lock:
            self._enter("playlist", self._now())

    @_guarded
    def playlist_failed(self, status, empty=False):
        with self._lock:
            now = self._now()
            self.playlist_errors += 1
            self._pl_run += 1
            self._pl_status = "empty playlist" if empty else "HTTP %s" % status
            if self._pl_run == 1:
                self._event("playlist_error", "Playlist error (%s)" % self._pl_status, now)
            self._enter("playlist_error", now)

    @_guarded
    def playlist_ok(self, info, new_count):
        with self._lock:
            now = self._now()
            if self._pl_run:
                self._event("playlist_ok", "Playlist recovered after %d error(s)"
                            % self._pl_run, now)
                self._pl_run = 0
            info = info or {}
            if info.get("target_duration"):
                self.target_duration = info["target_duration"]
            if self.variants is None and info.get("variants"):
                self.variants = info["variants"]
            ms = info.get("media_sequence")
            if ms is not None:
                if self._last_ms is not None and ms < self._last_ms:
                    self.playlist_backwards += 1
                    msg = ("Playlist went backwards: sequence %d after %d"
                           % (ms, self._last_ms))
                    self._event("backwards", msg, now)
                    log.info("%s: %s", self._who(), msg)
                self._last_ms = ms
            if info.get("latest"):
                self._latest = info["latest"]
            if info.get("endlist") and not self.endlist:
                self.endlist = True
                self._event("endlist", "Source says the broadcast has ended", now)
                log.info("%s: source playlist has ended (ENDLIST)", self._who())
            if new_count:
                self.last_new_chunk_at = now
            else:
                self._enter("waiting", now)

    @_guarded
    def loop_error(self, exc):
        with self._lock:
            now = self._now()
            self.internal_errors += 1
            self._enter("internal", now)
            msg = "%s: %s" % (type(exc).__name__, exc)
            if msg in self._seen_errors or len(self._seen_errors) >= 10:
                return
            self._seen_errors.add(msg)
            self._event("internal", "Internal error: %s" % msg[:160], now)
        log.warning("%s: stream loop error, retrying in 2s: %s",
                    self._who(), msg, exc_info=exc)

    # ── hooks: end ───────────────────────────────────────────────────────────

    @_guarded
    def end(self, reason, detail=None):
        with self._lock:
            if self.end_reason is None:
                self.end_reason = reason
                self.end_detail = detail

    @_guarded
    def finish(self):
        """Close the books. Called once from the stream's finally block."""
        with self._lock:
            if self.ended is not None:
                return
            now = self._now()
            self.ended = now
            if self.end_reason is None:
                self.end_reason = "client_left"
            _cushion, frozen = self._cushion_now(now)
            if self._cushion_at is not None and frozen > 0:
                self._starved(now - frozen, now, still_frozen=True)
            self._close_episode()
            r = self.reconnect
            if r is not None and r["replayed_chunks"] and self.chunks < 12:
                self._replay_note(now)
            self._event("end", self._end_text(), now)
            record = self._record(now)
            keys = list(self._keys)
        _remember_ended(self.channel_key, now, self.end_reason, self.id, keys)
        log.info("%s summary: %s", self._who(), _summary_line(record))
        HISTORY.append(record)

    def _end_text(self):
        text = END_REASONS.get(self.end_reason, self.end_reason)
        return "%s (%s)" % (text, self.end_detail) if self.end_detail else text

    # ── reading ──────────────────────────────────────────────────────────────

    def _state(self, now, cushion, frozen, speed):
        if self.first_delivery is None:
            return "starting", "Waiting for the first chunk"
        if frozen >= FREEZE_MIN_S:
            if self._label == "sending":
                return "struggling", self._activity_text(now)
            return "frozen", self._activity_text(now)
        if self._pl_run:
            return "struggling", self._activity_text(now)
        if speed is not None and speed < 1.0:
            return "struggling", "Downloads at %.2fx real time" % speed
        if cushion is not None and cushion < LOW_CUSHION_S:
            return "struggling", "Cushion low: %.1fs" % cushion
        return "healthy", ""

    def _totals(self):
        """Closed freezes plus the one in progress, if it already counts."""
        count, frozen, longest = self.freeze_count, self.frozen_s, self.longest_freeze
        causes = dict(self.by_cause)
        ep = self._episode
        if ep is not None and ep["starved"] >= FREEZE_MIN_S:
            count += 1
            frozen += ep["starved"]
            longest = max(longest, ep["starved"])
            for c, secs in ep["blamed"].items():
                causes[c] = causes.get(c, 0.0) + secs
        return count, frozen, longest, causes

    def _common(self, now):
        speed, link = self._speed()
        count, frozen, longest, causes = self._totals()
        video_mbit = (self.bytes * 8 / self.video_s / 1e6) if self.video_s else None
        variant = self.variants[0] if self.variants else None
        r = self.reconnect
        return {
            "speed": _r(speed, 2),
            "link_mbit": _r(link),
            "video_mbit": _r(video_mbit),
            "video_s": _r(self.video_s),
            "freezes": count,
            "frozen_s": _r(frozen),
            "longest_freeze_s": _r(longest),
            "by_cause": {k: _r(v) for k, v in causes.items()},
            "blips": self.blips,
            "blip_s": _r(self.blip_s),
            "player_waits": self.player_waits,
            "player_wait_s": _r(self.player_wait_s),
            "downloads_failed": self.downloads_failed,
            "skipped": self.skipped,
            "skipped_s": _r(self.skipped_s),
            "backwards": self.backwards,
            "playlist_backwards": self.playlist_backwards,
            "discontinuities": self.discontinuities,
            "playlist_errors": self.playlist_errors,
            "internal_errors": self.internal_errors,
            "endlist": self.endlist,
            "target_duration": self.target_duration,
            "durations_guessed": self.durations_guessed,
            "variant": variant,
            "variants": self.variants,
            "concurrent_max": self.concurrent_max,
            "reconnect": None if r is None else {
                k: (_r(v) if isinstance(v, float) else v)
                for k, v in r.items() if not k.startswith("_")},
        }

    @_guarded
    def snapshot(self):
        """The live view, for /stream/status."""
        with self._lock:
            now = self._now()
            cushion, frozen = self._cushion_now(now)
            out = self._common(now)
            state, reason = self._state(now, cushion, frozen, out["speed"])
            out.update({
                "state": state,
                "reason": reason,
                "cushion_s": _r(cushion),
                "frozen_for_s": _r(frozen) if frozen >= FREEZE_MIN_S else 0,
                "behind_live_s": _r(self.behind_now),
                "activity": self._label,
                "activity_s": _r(now - self._label_since),
                "timeline": list(self.timeline),
            })
            return out

    def _record(self, now):
        out = self._common(now)
        out.update({
            "v": 1,
            "id": self.id,
            "channel": self.channel,
            "channel_key": self.channel_key,
            "source": self.source,
            "multiview": self.multiview,
            "started": _r(self.started),
            "ended": _r(now),
            "seconds": _r(now - self.started),
            "first_chunk_s": _r(self.first_delivery - self.started)
                             if self.first_delivery else None,
            "end_reason": self.end_reason,
            "end_detail": self.end_detail,
            "end_text": self._end_text(),
            "chunks": self.chunks,
            "bytes": self.bytes,
            "speed_median": _r(_median(self._ratios), 2),
            "speed_p10": _r(_pct(self._ratios, 0.1), 2),
            "link_mbit_median": _r(_median(self._mbits)),
            "link_mbit_p10": _r(_pct(self._mbits, 0.1)),
            "behind_live_median_s": _r(_median(self._behind)),
            "behind_live_max_s": _r(max(self._behind) if self._behind else None),
            "freeze_list": list(self.freezes),
        })
        out.pop("speed", None)
        out.pop("link_mbit", None)
        return out


def _summary_line(rec):
    parts = ["%.0fs" % (rec["seconds"] or 0), "%d chunks" % rec["chunks"]]
    if rec["freezes"]:
        causes = ", ".join("%s %.0fs" % (CAUSES.get(k, k), v)
                           for k, v in sorted(rec["by_cause"].items(),
                                              key=lambda kv: -kv[1])
                           if v >= 0.5)
        parts.append("%d freeze(s) totalling %.0fs (%s)"
                     % (rec["freezes"], rec["frozen_s"], causes))
    else:
        parts.append("no freezes")
    if rec["speed_median"] is not None:
        parts.append("speed %.2fx median, %.2fx slowest tenth"
                     % (rec["speed_median"], rec["speed_p10"]))
    if rec["video_mbit"] is not None and rec["link_mbit_median"] is not None:
        parts.append("video %.1f Mbit/s over a %.1f Mbit/s link"
                     % (rec["video_mbit"], rec["link_mbit_median"]))
    jumps = (rec.get("backwards") or 0) + (rec.get("playlist_backwards") or 0)
    for n, label in ((rec.get("skipped"), "chunk(s) skipped"),
                     (jumps, "jump(s) back"),
                     (rec.get("downloads_failed"), "chunk(s) dropped"),
                     (rec.get("playlist_errors"), "playlist error(s)"),
                     (rec.get("internal_errors"), "internal error(s)")):
        if n:
            parts.append("%d %s" % (n, label))
    rc = rec.get("reconnect")
    if rc:
        parts.append("reconnect %.0fs after %s, replayed %d chunk(s)"
                     % (rc["after_s"], rc["previous_id"], rc["replayed_chunks"]))
    parts.append("ended: %s" % rec["end_text"])
    return "; ".join(parts)


class _Null:
    """Stands in when a tracker could not be built. Every hook is a no-op."""

    def __getattr__(self, name):
        return _noop


def _noop(*_args, **_kwargs):
    return None


NULL = _Null()


def track(*args, **kwargs):
    """Build a tracker, or the no-op stand-in if that fails. Never raises."""
    try:
        return StreamHealth(*args, **kwargs)
    except Exception:
        _warn_once("track", "Stream health tracker could not start")
        return NULL


# ─── History ──────────────────────────────────────────────────────────────────

class _History:
    """Append-only JSON lines, trimmed to the last `keep` records.

    Writes go through one daemon thread, so a slow or failing disk can delay
    the history but never a stream. Reads are cached on (mtime, size).
    """

    def __init__(self):
        self.path = None
        self.keep = HISTORY_KEEP
        self._q = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()          # the file and the read cache
        self._start_lock = threading.Lock()    # the writer thread only
        self._count = None
        self._cache = (None, [])

    def configure(self, path, keep=HISTORY_KEEP):
        self.path = path
        self.keep = max(1, int(keep))
        self._count = None
        self._cache = (None, [])

    def append(self, record):
        if not self.path:
            return
        # Never self._lock: a stream's finally block calls this, and that lock
        # is held for the whole of a disk write.
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="stream-history")
                self._thread.start()
        self._q.put(record)

    def flush(self, timeout=5.0):
        end = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.02)

    def _run(self):
        while True:
            record = self._q.get()
            try:
                self._write(record)
            except Exception:
                _warn_once("history", "Could not write stream history to %s" % self.path)
            finally:
                self._q.task_done()

    def _write(self, record):
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            if self._count is None:
                self._count = len(self._lines())
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
            self._count += 1
            if self._count > self.keep + 50:
                lines = self._lines()[-self.keep:]
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                os.replace(tmp, self.path)
                self._count = len(lines)

    def _lines(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [ln if ln.endswith("\n") else ln + "\n"
                        for ln in f if ln.strip()]
        except FileNotFoundError:
            return []

    def read(self, limit=100):
        """Newest first. Unparseable lines are skipped, not fatal."""
        if not self.path:
            return []
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if self._cache[0] != stamp:
                records = []
                for ln in self._lines():
                    try:
                        records.append(json.loads(ln))
                    except ValueError:
                        continue
                self._cache = (stamp, records)
            records = self._cache[1]
        return list(reversed(records[-limit:]))


HISTORY = _History()


def totals(records):
    """Roll a list of history records up into what the console headlines."""
    out = {"streams": len(records), "seconds": 0.0, "freezes": 0,
           "frozen_s": 0.0, "by_cause": {}, "ended": {}, "reconnects": 0,
           "replayed_chunks": 0, "skipped": 0, "with_freezes": 0}
    for r in records:
        out["seconds"] += r.get("seconds") or 0
        out["freezes"] += r.get("freezes") or 0
        out["frozen_s"] += r.get("frozen_s") or 0
        out["skipped"] += r.get("skipped") or 0
        if r.get("freezes"):
            out["with_freezes"] += 1
        for k, v in (r.get("by_cause") or {}).items():
            out["by_cause"][k] = round(out["by_cause"].get(k, 0) + (v or 0), 1)
        reason = r.get("end_reason") or "unknown"
        out["ended"][reason] = out["ended"].get(reason, 0) + 1
        rc = r.get("reconnect")
        if rc:
            out["reconnects"] += 1
            out["replayed_chunks"] += rc.get("replayed_chunks") or 0
    out["seconds"] = round(out["seconds"], 1)
    out["frozen_s"] = round(out["frozen_s"], 1)
    return out
