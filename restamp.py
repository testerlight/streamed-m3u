"""restamp: close the timestamp jump a long hole leaves in the stream.

When chunks go missing (dropped after failed downloads, empty placeholders,
or gone from the playlist before they were fetched) the next chunk's PTS, DTS
and PCR jump forward by the missing span. A jump over ffmpeg's 10s threshold
is not repaired by Jellyfin's transcode when it keeps input timestamps
(`-copyts`, `igndts`): audio stays that far out of step with the picture until
the input is reopened. The discontinuity flag does not help, since ffmpeg
reacts to the size of the jump and does not read the flag. Measured in
`.scratch/stream-recovery/issues/01-hole-discontinuity.md`.

The fix is to close the jump here: once a hole longer than the threshold has
been crossed, every PTS, DTS and PCR in the next chunk and all later ones is
moved back by the hole's length, so the timeline stays continuous. Only
PES-start and PCR packets are rewritten. Shorter holes stay byte passthrough.

The span of a hole comes from the playlist (EXTINF of the dropped chunks) and
is cross-checked against the timestamps. For chunks that left the playlist
before they were fetched there is no EXTINF, so the timestamp gap itself is
the span. Nothing here assumes a chunk length.

Fail-safe in every direction: any doubt (not transport stream, no timestamps,
playlist and timestamps disagree, a gap too big to be a hole) leaves the chunk
exactly as it came, and so does any exception. Pure Python, no I/O, no
imports from app.py.
"""

import logging

log = logging.getLogger("restamp")

TICKS = 90000                 # PTS/DTS/PCR base clock, per second
M33 = (1 << 33) - 1           # the clocks are 33 bits and wrap
HALF = 1 << 32

DEFAULT_MIN_HOLE_S = 8.0      # ffmpeg's timestamp-jump threshold is 10s
MAX_HOLE_S = 120.0            # beyond this it is a new timeline, not a hole
AGREE_ABS_S = 1.0             # playlist vs timestamps: the larger of this ...
AGREE_REL = 0.2               # ... and this share of the span
SCAN_PACKETS = 2000           # head and tail of a chunk read to find its edges

# PES streams without the optional header (and so without timestamps).
_NO_HEADER = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF}


def _signed(v):
    """A 33-bit difference as a signed number."""
    v &= M33
    return v - (1 << 33) if v >= HALF else v


def _aligned(body):
    return len(body) >= 188 and body[0] == 0x47 and len(body) % 188 == 0 \
        and all(body[i] == 0x47 for i in range(0, len(body), 188))


def _enc5(prefix, v):
    v &= M33
    return bytes([(prefix << 4) | (((v >> 30) & 7) << 1) | 1, (v >> 22) & 0xFF,
                  (((v >> 15) & 0x7F) << 1) | 1, (v >> 7) & 0xFF, ((v & 0x7F) << 1) | 1])


def _dec5(b):
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | (b[4] >> 1)


def shift_chunk(body, delta):
    """body with every PTS, DTS and PCR moved by delta ticks (90 kHz).

    Only PES-start packets and packets carrying a PCR are touched; every
    other byte is the input's. Returns the same object for a zero shift.
    Raises on a body that is not 188-aligned transport stream.
    """
    if not delta:
        return body
    if not _aligned(body):
        raise ValueError("not 188-aligned transport stream")
    b = bytearray(body)
    for i in range(0, len(b), 188):
        afc = (b[i + 3] >> 4) & 3
        if b[i + 3] & 0xC0:                       # scrambled: leave it
            continue
        off = i + 4
        if afc & 2:
            alen = b[i + 4]
            if alen >= 7 and b[i + 5] & 0x10:     # PCR present
                base = ((b[i + 6] << 25) | (b[i + 7] << 17) | (b[i + 8] << 9)
                        | (b[i + 9] << 1) | (b[i + 10] >> 7))
                base = (base + delta) & M33
                b[i + 6] = (base >> 25) & 0xFF
                b[i + 7] = (base >> 17) & 0xFF
                b[i + 8] = (base >> 9) & 0xFF
                b[i + 9] = (base >> 1) & 0xFF
                b[i + 10] = ((base & 1) << 7) | (b[i + 10] & 0x7F)
            off = i + 5 + alen
        if afc & 1 and b[i + 1] & 0x40 and off + 19 <= i + 188 \
                and b[off:off + 3] == b"\x00\x00\x01" and b[off + 3] not in _NO_HEADER:
            flags = b[off + 7] >> 6
            if flags & 2:
                p = off + 9
                b[p:p + 5] = _enc5(3 if flags == 3 else 2, _dec5(b[p:p + 5]) + delta)
                if flags == 3:
                    b[p + 5:p + 10] = _enc5(1, _dec5(b[p + 5:p + 10]) + delta)
    return bytes(b)


def _pes_starts(body, first, last):
    """[(pid, stream_id, pts)] for PES starts in packets [first, last)."""
    out = []
    for n in range(first, last):
        i = n * 188
        if not body[i + 1] & 0x40:
            continue
        afc = (body[i + 3] >> 4) & 3
        if not afc & 1:
            continue
        off = i + 4 + (1 + body[i + 4] if afc & 2 else 0)
        if off + 14 > i + 188 or body[off:off + 3] != b"\x00\x00\x01" \
                or body[off + 3] in _NO_HEADER or not body[off + 7] & 0x80:
            continue
        out.append((((body[i + 1] & 0x1F) << 8) | body[i + 2], body[off + 3],
                    _dec5(body[off + 9:off + 14])))
    return out


def _edges(body, pid=None):
    """(pid, first PTS, end PTS) of a chunk, or None: where its timeline
    starts and where the next chunk should pick up (last PTS plus one frame).

    The picture's PID if there is one, otherwise whichever stream starts
    first. Read from the head and the tail of the chunk only.
    """
    n = len(body) // 188
    head = _pes_starts(body, 0, min(n, SCAN_PACKETS))
    if not head:
        return None
    if pid is None:
        video = [h for h in head if 0xE0 <= h[1] <= 0xEF]
        pid = (video or head)[0][0]
    mine = [h[2] for h in head if h[0] == pid]
    if not mine:
        return None
    ref = mine[0]
    start = ref + min(_signed(p - ref) for p in mine)
    tail = [h[2] for h in _pes_starts(body, max(0, n - SCAN_PACKETS), n) if h[0] == pid]
    if not tail:
        return None
    ref = tail[0]
    rel = sorted({_signed(p - ref) for p in tail})
    steps = sorted(b - a for a, b in zip(rel, rel[1:]) if b > a)
    frame = steps[len(steps) // 2] if steps else 0
    return pid, start & M33, (ref + rel[-1] + frame) & M33


class Restamper:
    """One stream's restamping. Feed it every chunk the stream loop sees.

    missing(seq, dur)       a chunk was dropped or came back empty
    chunk(body, seq, dur)   a chunk is about to be sent; returns what to send

    `event` is set by chunk() to a dict (span_s, from, missing) when that
    chunk was restamped, else None. `seq` is the chunk's media sequence number
    and `dur` its EXTINF in seconds; either may be None.
    """

    def __init__(self):
        self.failures = 0
        self.event = None
        self.reset()

    def reset(self):
        """Forget the stream so far (switched off, or a new timeline)."""
        self._offset = 0            # ticks every chunk is moved back by
        self._started = False       # something was sent already
        self._last_seq = None
        self._end = None            # (pid, end PTS) of the last chunk, source clock
        self._dropped = 0
        self._dropped_s = 0.0
        self._dropped_unknown = False
        self._skipped = 0
        self.broken = False

    @property
    def offset_s(self):
        return self._offset / TICKS

    def _note_seq(self, seq):
        if seq is None:
            return
        last = self._last_seq
        if last is not None and seq > last + 1:
            self._skipped += seq - last - 1
        if last is None or seq > last:
            self._last_seq = seq

    def missing(self, seq=None, dur=None):
        try:
            self._note_seq(seq)
            self._dropped += 1
            if dur:
                self._dropped_s += dur
            else:
                self._dropped_unknown = True
        except Exception:
            self._fail("missing")

    def _hole(self, edges, dur, min_hole_s):
        """(span in seconds, where it came from, chunks missing) or None."""
        count = self._dropped + self._skipped
        if not count or not self._started or self._end is None:
            return None
        known = self._dropped_s if self._skipped == 0 and not self._dropped_unknown else None
        gap = _signed(edges[1] - self._end[1]) / TICKS if edges else None
        if known is not None:
            if gap is not None and abs(gap - known) > max(AGREE_ABS_S, AGREE_REL * known):
                return None
            span, how = known, "playlist"
        elif gap is not None and 0 < gap <= min(MAX_HOLE_S, count * 2 * (dur or 10.0) + 1):
            span, how = gap, "timestamps"
        else:
            return None
        if span <= min_hole_s or span > MAX_HOLE_S:
            return None
        return span, how, count

    def chunk(self, body, seq=None, dur=None, min_hole_s=DEFAULT_MIN_HOLE_S):
        """What to send in place of `body`. Never raises."""
        self.event = None
        if self.broken:
            return body
        try:
            if not _aligned(body):
                return body              # not transport stream: not ours to touch
            self._note_seq(seq)
            edges = _edges(body, self._end[0] if self._end else None)
            hole = self._hole(edges, dur, min_hole_s)
            if hole is not None:
                span, how, count = hole
                self._offset += int(round(span * TICKS))
                self.event = {"span_s": span, "from": how, "missing": count,
                              "offset_s": self.offset_s}
            self._dropped = self._skipped = 0
            self._dropped_s = 0.0
            self._dropped_unknown = False
            self._started = True
            edges = edges or _edges(body)       # the picture's PID may have changed
            if edges:
                self._end = (edges[0], edges[2])
            return shift_chunk(body, -self._offset)
        except Exception:
            self.event = None
            self._fail("chunk")
            return body

    def _fail(self, where):
        self.failures += 1
        self.broken = True
        if self.failures == 1:
            log.warning("Restamp failed in %s; chunks pass through unmodified "
                        "from here on", where, exc_info=True)
