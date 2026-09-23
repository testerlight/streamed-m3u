"""Multi-view input feeders: one live stream, written into one FIFO.

A composite needs two byte streams arriving at the same time, and it needs to
be able to swap what is on either of them without the encoder noticing. A FIFO
gives exactly that: the encoder opens it once and reads until told to stop,
while this side decides, chunk by chunk, whose bytes go in. Swapping a fixture
becomes "write different bytes", not "restart ffmpeg" — which is what keeps
the miniplayer changeable mid-game without a black frame.

**Where the bytes come from.** Each feeder reads `/stream?team=<slug>` from
this same service over loopback, rather than calling the streaming code
directly. That is deliberate. The segment path already solves the three
hardest problems in this codebase — whole chunks buffered before anything is
forwarded (gotcha #16), the fake WebP wrapper stripped (#17), and segments
deduped by identity rather than by signed URL (#18) — and every one of those
is preserved here by construction, because not a line of it is touched. The
cost is a loopback hop, which at ~10 Mbit/s is nothing, and the benefit is
that a composite leg behaves *exactly* like any other viewer, including the
cascade, the extract cache and the keepalive padding.

It also means each leg appears in /stream/status on its own, tagged with the
slot and role it is feeding, so a stalling input is visible rather than hidden
inside the encoder.

Nothing here knows about ffmpeg. This module fills FIFOs; Phase 5 adds the
process that drains them.
"""

import errno
import logging
import os
import select
import subprocess
import threading
import time

log = logging.getLogger("composite")

# Chunk size for moving bytes from the HTTP response into the FIFO. Large
# enough that the syscall overhead is irrelevant, small enough that a stop
# request is noticed promptly.
CHUNK = 65536

# How long to wait for a reader to appear on a FIFO before giving up on one
# write cycle. The encoder opens its end a moment after we create ours, so
# this only has to outlast process start.
READER_WAIT = 30.0

# How long a single write may wait for a reader that has stopped draining
# before the feeder checks its stop flag again. Bounded on purpose: a blocked
# write must never be able to wedge the thread forever.
WRITE_POLL = 1.0

# Ceiling for the retry back-off on a source that will not resolve. Each
# attempt costs the far side a whole cascade, so this climbs rather than
# hammering a channel that is simply off the air.
MAX_BACKOFF = 60.0

# How long an input may produce nothing before we cut it and reconnect. A
# healthy /stream?team= never goes quiet for this long even mid-chunk: it
# pads with null packets every STREAM_KEEPALIVE_INTERVAL precisely so its
# reader can tell a slow download from a dead one. Silence past this is the
# source having gone away without saying so.
SOURCE_STALL_TIMEOUT = 15.0

# How long to wait on a reply from the encoder's control socket. Generous
# for what is a local round trip to a filter graph, because the cost of
# giving up early is a control that silently did nothing.
ZMQ_TIMEOUT = 3.0

# How long a watched composite may produce nothing before it is torn down and
# rebuilt. An input whose stream has changed under it never recovers - see the
# note on build_filter - so once the picture stops there is nothing to wait
# for, and the viewer's stream re-attaches to the replacement.
OUTPUT_STALL_TIMEOUT = 20.0

# Encoder complaints that are expected rather than interesting. Inputs are
# live and chunked - a whole segment lands at once, so its packets share an
# arrival timestamp and then jump - and every source switch restarts the
# stream's own clock. The demuxer says so loudly. Kept in the ring buffer for
# /stream/status, but logged at debug so they cannot bury a real fault or
# flood the console's event feed.
BENIGN_STDERR = (
    "DTS discontinuity",
    "Non-monotonous DTS",
    "timestamp discontinuity",
)


def make_fifo(path):
    """Create (or replace) a FIFO at `path`. Returns the path.

    Replaced rather than reused: names carry a per-composite token so a
    collision should be impossible, but a FIFO left behind by a killed process
    may still have a reader attached, and inheriting that reader would feed
    the wrong encoder.
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.mkfifo(path, 0o600)
    return path


def remove_fifo(path):
    try:
        os.unlink(path)
    except (FileNotFoundError, TypeError):
        pass
    except OSError as e:
        log.warning("Could not remove FIFO %s: %s", path, e)


def _open_write_nonblocking(path, stop, deadline=None):
    """Open the write end without blocking on a missing reader.

    Opening a FIFO for writing blocks until someone opens the read end. That
    is the classic way to wedge a thread here, so it is opened non-blocking
    and retried: ENXIO simply means "no reader yet".

    With no deadline this waits until stopped, which is what a feeder wants.
    ffmpeg opens its inputs **sequentially** - it will not touch the second
    FIFO until the first has been opened and probed - so the second feeder
    routinely waits out a whole cold extraction before its reader appears.
    Giving up on a timer there leaves that input silently unfed for the life
    of the composite, which is how a missing miniplayer looks like a bug in
    the encoder rather than in the waiting.
    """
    while not stop.is_set() and (deadline is None or time.time() < deadline):
        try:
            return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno != errno.ENXIO:
                raise
            stop.wait(0.1)
    return None


def make_filler(path, width=1920, height=1080, fps=60, seconds=2,
                ffmpeg="ffmpeg"):
    """Generate the black-and-silence clip a feeder plays when it has nothing.

    ffmpeg blocks probing an input that has produced no data, so an input
    which is merely *empty* - nothing selected, or a cold extraction still in
    flight - would hang the whole graph at startup rather than showing a hole.
    Keeping both inputs fed at all times means the filter graph never changes
    shape and never stalls, which is also what lets a secondary be added or
    removed without restarting the encoder.

    Encoded with libx264 rather than VAAPI: it is two seconds of black, the
    cost is irrelevant, and the filler must be producible even on a box where
    the render node is missing.
    """
    if os.path.exists(path):
        return path
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i",
        "color=c=black:s=%dx%d:r=%d" % (width, height, fps),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", str(fps), "-c:a", "aac", "-b:a", "64k",
        "-f", "mpegts", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not os.path.exists(path):
        raise RuntimeError("filler generation failed (%s): %s"
                           % (proc.returncode,
                              proc.stderr.decode("utf-8", "replace")[-400:]))
    log.info("Multi-view filler generated: %s (%d bytes)",
             path, os.path.getsize(path))
    return path


class _FillerSource:
    """Loops a clip at roughly its natural bitrate.

    Paced deliberately. Written as fast as the pipe accepts it, a two-second
    clip would put minutes of black into the encoder's input buffer in a
    blink, and the moment a real source arrived the composite would be that
    far behind the live edge.
    """

    def __init__(self, path, stop):
        with open(path, "rb") as fh:
            self._data = fh.read()
        self._stop = stop
        # Bytes per second implied by the clip itself, so pacing follows
        # whatever make_filler produced rather than a guessed constant.
        self._rate = max(1, len(self._data) // 2)
        self._closed = False

    def __iter__(self):
        step = 32768
        while not self._closed and not self._stop.is_set():
            for off in range(0, len(self._data), step):
                if self._closed or self._stop.is_set():
                    return
                chunk = self._data[off:off + step]
                yield chunk
                time.sleep(len(chunk) / float(self._rate))

    def close(self):
        self._closed = True


class _HttpSource:
    """Iterable over a streaming response, closable from another thread.

    Closing the *response* rather than the generator matters: delivering
    GeneratorExit into a generator that is currently running on the feeder
    thread raises "generator already executing". Closing the socket makes the
    in-flight read fail instead, which is what unwinds a stop or a switch
    while a chunk is still arriving.
    """

    def __init__(self, resp):
        self._resp = resp

    def __iter__(self):
        for chunk in self._resp.iter_content(chunk_size=CHUNK):
            if chunk:
                yield chunk

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass


class Feeder:
    """Keeps one FIFO fed from one channel, and can change which channel.

    The FIFO is opened once and held open across source changes, so a reader
    never sees EOF just because the fixture changed.
    """

    def __init__(self, fifo_path, slot_id, role, base_url,
                 open_source=None, stop=None, filler=None):
        self.fifo_path = fifo_path
        self.slot_id = str(slot_id)
        self.role = role                      # "primary" | "secondary"
        self.base_url = base_url.rstrip("/")
        # Injectable so the harness can drive a feeder without a live stream;
        # production always uses the loopback reader below.
        self._open_source = open_source or self._http_source
        self._stop = stop or threading.Event()
        # Played whenever there is no source. None keeps the older behaviour
        # of simply going quiet, which the feeder-only checks rely on.
        self.filler = filler

        self._lock = threading.Lock()
        self._want = None                     # slug the feeder should be on
        self._current = None                  # slug it is actually on
        self._switch = threading.Event()
        self._thread = None
        self._active = None                   # live source, for interrupting

        self.bytes_written = 0
        self.filler_bytes = 0
        self.connects = 0
        self._backoff = 2.0
        self._pending = None
        self._pre_retry_at = 0.0
        self.stalls = 0
        # Once a real stream has gone into this FIFO, ffmpeg has probed it
        # and its decoder is configured for that stream. Anything else spliced
        # on afterwards - filler, another channel - is not decodable on this
        # input and freezes the whole graph, so nothing else ever goes in.
        self.carried_source = False
        self.source_errors = 0
        self.reader_gone = False
        self.last_write_at = 0.0

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def start(self, slug=None):
        if self._thread is not None:
            raise RuntimeError("feeder already started")
        make_fifo(self.fifo_path)
        with self._lock:
            self._want = slug or None
        self._thread = threading.Thread(
            target=self._run, name="mvfeed-%s-%s" % (self.slot_id, self.role),
            daemon=True)
        self._thread.start()
        # The worker cannot watch itself: when a source stalls, that is the
        # thread stuck in the read. So the watch is its own.
        threading.Thread(
            target=self._watch, name="mvwatch-%s-%s" % (self.slot_id, self.role),
            daemon=True).start()
        return self

    def _watch(self):
        """Cut a source that has gone quiet, so the graph cannot freeze.

        ffmpeg will not produce a frame without both of its inputs, so one
        silent source stops the composite dead rather than degrading it - and
        neither this feeder (blocked in a read that never returns) nor the
        encoder (patiently waiting) can notice on its own. Cheap: one
        timestamp comparison every couple of seconds.
        """
        while not self._stop.is_set():
            self._stop.wait(2.0)
            if self._stop.is_set():
                return
            idle = self.live_source_idle()
            if idle is not None and idle > SOURCE_STALL_TIMEOUT:
                self.drop_stalled()
                continue
            # The backstop. Nothing at all should keep this FIFO quiet for
            # long - not a source, not a connect, not a blocked write - and
            # whatever has, the encoder is frozen until it stops. Cut
            # everything this feeder could be waiting on by raising the
            # switch: the worker owns _pending and will abandon it safely,
            # which a second thread reaching in could not.
            quiet = self.quiet_for()
            if quiet is not None and quiet > SOURCE_STALL_TIMEOUT * 2:
                self.stalls += 1
                log.warning("Multi-view slot %s %s: nothing written for "
                            "%.0fs, cutting whatever it is waiting on",
                            self.slot_id, self.role, quiet)
                self._close_active()
                self._switch.set()

    def switch(self, slug):
        """Point this feeder at a different channel, or at nothing.

        The FIFO stays open across the change; only the bytes flowing into it
        differ. That is the whole reason this class exists.
        """
        with self._lock:
            if (slug or None) == self._want:
                return False
            self._want = slug or None
        self._switch.set()
        # Same reasoning as stop(): without closing the live source the change
        # waits out however long the current chunk takes to arrive.
        self._close_active()
        log.info("Multi-view slot %s %s -> %s", self.slot_id, self.role,
                 slug or "(none)")
        return True

    def stop(self, timeout=15.0):
        self._stop.set()
        self._switch.set()
        # Closing the live source unblocks a read that is otherwise sitting
        # in the socket. Upstream buffers a whole chunk before forwarding any
        # of it (gotcha #16), so between bursts the connection can legitimately
        # be silent for ~13s; without this, stopping would wait that out.
        self._close_active()
        t = self._thread
        if t is not None:
            t.join(timeout)
            if t.is_alive():
                log.warning("Feeder %s/%s did not stop within %.0fs",
                            self.slot_id, self.role, timeout)
        remove_fifo(self.fifo_path)
        self._thread = None

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def stats(self):
        with self._lock:
            want = self._want
            current = self._current
        return {
            "slot": self.slot_id,
            "role": self.role,
            "slug": current,
            "wanted": want,
            "fifo": self.fifo_path,
            "bytes_written": self.bytes_written,
            "filler_bytes": self.filler_bytes,
            "connects": self.connects,
            "source_errors": self.source_errors,
            # Times this input went quiet without closing and had to be cut.
            # Anything other than zero means the far side is unwell.
            "stalls": self.stalls,
            "reader_gone": self.reader_gone,
            "idle_seconds": (round(time.time() - self.last_write_at, 1)
                             if self.last_write_at else None),
        }

    # ─── Sources ──────────────────────────────────────────────────────────

    def _http_source(self, slug):
        """Iterator of bytes for `slug`, read back over loopback.

        Tagged with a header rather than a query parameter so the URL — and
        therefore the operator disconnect-hold key — stays exactly what a
        normal viewer would send.
        """
        import requests

        url = "%s/stream?team=%s" % (self.base_url, slug)
        resp = requests.get(
            url, stream=True, timeout=(10, 60),
            headers={"X-Multiview-Slot": "%s/%s" % (self.slot_id, self.role)})
        if resp.status_code != 200:
            resp.close()
            raise RuntimeError("upstream %s for %s" % (resp.status_code, slug))
        self.connects += 1
        return _HttpSource(resp)

    # ─── Worker ───────────────────────────────────────────────────────────

    def _run(self):
        fd = None
        try:
            # Resolve while waiting for the reader, not after it. The two
            # waits are independent, and doing them one after the other is
            # what stacks the composite's two cold starts - see _prepare.
            while not self._stop.is_set():
                fd = _open_write_nonblocking(self.fifo_path, self._stop,
                                             deadline=time.time() + 0.5)
                if fd is not None:
                    break
                with self._lock:
                    want = self._want
                if want:
                    self._prepare(want)
            if fd is None:
                return

            while not self._stop.is_set():
                with self._lock:
                    slug = self._want
                    self._current = slug
                self._switch.clear()

                if not slug:
                    if self.filler:
                        # Black rather than silence on the wire: an input that
                        # stops producing entirely stalls the encoder.
                        self._pump_filler(fd)
                    else:
                        self._switch.wait(WRITE_POLL)
                    continue

                if not self._pump(fd, slug):
                    break
                # _pump returns on a switch, a stop, or the source ending.
                # Going quiet here is not an option: an input that stops
                # producing stalls the entire graph, so the reconnect runs
                # underneath filler rather than underneath silence. This side
                # goes black for the length of the reconnect and the other
                # keeps playing, which is the agreed behaviour for a source
                # that has gone away.
                if not self._switch.is_set() and not self._stop.is_set():
                    with self._lock:
                        nxt = self._want
                    if not nxt:
                        continue
                    self._begin_connect(nxt)
                    while (not self._stop.is_set() and not self._switch.is_set()
                           and self._pending is not None
                           and self._pending["thread"].is_alive()):
                        self._pump_filler(fd, until=time.time() + 1.0)
        except Exception:
            log.exception("Multi-view feeder %s/%s failed",
                          self.slot_id, self.role)
        finally:
            self._abandon_pending()
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _prepare(self, slug):
        """Keep a connect in flight for `slug` while we wait for our reader.

        Retried on the same back-off the pump uses, because a first attempt
        that fails fast - an upstream 503 while the roster is still
        refreshing, say - would otherwise waste the entire wait and leave us
        exactly where we started when the reader finally arrives.
        """
        pending = self._pending
        if pending is None:
            self._begin_connect(slug)
            return
        if pending["thread"].is_alive() or "error" not in pending["result"]:
            return                      # still resolving, or landed and held
        now = time.time()
        if not self._pre_retry_at:
            wait = self._backoff
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            self._pre_retry_at = now + wait
            log.debug("Multi-view slot %s %s: early connect to %s failed (%s),"
                      " retrying in %.0fs", self.slot_id, self.role, slug,
                      pending["result"]["error"], wait)
            return
        if now >= self._pre_retry_at:
            self._pre_retry_at = 0.0
            self._pending = None        # failed, so there is nothing to close
            self._begin_connect(slug)

    def _begin_connect(self, slug):
        """Start resolving `slug` on its own thread, if we are not already.

        Called before the FIFO has a reader as well as after, because the two
        waits are independent and stacking them is expensive. ffmpeg opens its
        inputs one after another and will not touch the second until the first
        is open *and probed* - and probing needs the first source to be
        streaming already. So a feeder that waits for its reader before it
        starts resolving makes the composite's two cold starts strictly
        sequential: measured live at 50s to first picture, with the secondary
        not beginning its own extraction until the 30s mark. Resolving while
        we wait for the reader overlaps them instead.

        Nothing is wasted if the reader never comes: the pending connect is
        abandoned on the way out and closed when it lands.
        """
        pending = self._pending
        if pending is not None and pending["slug"] == slug:
            return
        self._abandon_pending()
        result = {}

        def _resolve():
            try:
                result["source"] = self._open_source(slug)
            except Exception as e:                        # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=_resolve, daemon=True,
                             name="mvconnect-%s-%s" % (self.slot_id, self.role))
        t.start()
        self._pending = {"slug": slug, "thread": t, "result": result}

    def _abandon_pending(self):
        """Let go of a connect we no longer want.

        It cannot simply be dropped: it is a real upstream connection that
        will eventually land, and an unclosed one holds a slot on the far
        side. A watcher closes it whenever it arrives.
        """
        pending = self._pending
        self._pending = None
        if pending is None:
            return

        def _reap():
            pending["thread"].join()
            src = pending["result"].get("source")
            close = getattr(src, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass

        threading.Thread(target=_reap, daemon=True).start()

    def _connect(self, slug, fd=None):
        """Wait for `slug` to resolve without blocking a switch or a stop.

        A cold channel can take most of CASCADE_BUDGET to produce its first
        byte, and that whole time is spent inside one blocking call. Waiting
        on it directly means a switch made while it is in flight is not seen
        until it returns - measured live at a full 60s of a feeder ignoring
        both switch() and stop(). So the connect runs on its own thread and
        this one polls, exactly as _fetch_with_keepalive does in app.py for
        the same reason.

        Adopts a connect already in flight for the same slug, which is the
        usual case: _run starts one before waiting for the reader.

        Given `fd`, plays filler for as long as the wait lasts. That is not a
        nicety: a connect can hang for as long as the far side lets it, and an
        input that stops producing freezes the *whole* graph, not just its own
        side of the picture. Measured live at five minutes of a frozen
        composite with no error logged anywhere, because from every local
        point of view nothing was wrong.

        Returns (source, abandoned).
        """
        self._begin_connect(slug)
        pending = self._pending
        t, result = pending["thread"], pending["result"]
        while t.is_alive():
            if self._stop.is_set() or self._switch.is_set():
                self._abandon_pending()
                log.info("Multi-view slot %s %s: abandoning connect to %s",
                         self.slot_id, self.role, slug)
                return None, True
            # Deliberately quiet, and `fd` is carried only to make that a
            # decision rather than an oversight. Writing filler here would
            # give ffmpeg a stream to probe that is not the one it is about
            # to receive, and the input never recovers - see §12.
            t.join(0.25)
        self._pending = None
        self._pre_retry_at = 0.0

        if "error" in result:
            raise result["error"]
        return result.get("source"), False

    def _pump_filler(self, fd, until=None):
        """Play the filler until something changes, or until `until` passes.

        Refuses on an input that has already carried a real stream. Filler is
        for an input that has no channel at all and never will - an
        unconfigured miniplayer - and splicing it behind a source that stopped
        does not produce black, it produces a frozen composite.
        """
        if self.carried_source or not self.filler:
            remaining = (until - time.time()) if until else WRITE_POLL
            self._switch.wait(max(0.0, min(WRITE_POLL, remaining)))
            return
        source = _FillerSource(self.filler, self._stop)
        with self._lock:
            self._active = source
        try:
            for chunk in source:
                if self._stop.is_set() or self._switch.is_set():
                    break
                if until is not None and time.time() >= until:
                    break
                if not self._write_all(fd, chunk):
                    self.reader_gone = True
                    return
                self.filler_bytes += len(chunk)
        finally:
            with self._lock:
                self._active = None
            source.close()

    def _pump(self, fd, slug):
        """Stream `slug` into `fd`. False means the feeder should stop."""
        try:
            source, abandoned = self._connect(slug, fd)
        except Exception as e:
            self.source_errors += 1
            # Back off progressively. Every attempt costs the far side a whole
            # cascade, browser extraction included, so a channel that is simply
            # off the air must not be retried every two seconds forever.
            wait = self._backoff
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            log.warning("Multi-view slot %s %s: cannot read %s (%s), retrying "
                        "in %.0fs", self.slot_id, self.role, slug, e, wait)
            # Filler, not silence. The encoder needs bytes on this input or
            # the whole graph stalls waiting for them, and black is exactly
            # the right picture for a source that has gone away: this side
            # goes dark while the other keeps playing.
            self._pump_filler(fd, until=time.time() + wait)
            return True
        if abandoned or source is None:
            return not self._stop.is_set()
        self._backoff = 2.0
        with self._lock:
            self._active = source

        try:
            for chunk in source:
                if self._stop.is_set():
                    return False
                if self._switch.is_set():
                    return True
                self.carried_source = True
                if not self._write_all(fd, chunk):
                    # The reader went away. Nothing this feeder does will
                    # help; the supervisor owns restarting the encoder.
                    self.reader_gone = True
                    log.warning("Multi-view slot %s %s: reader closed %s",
                                self.slot_id, self.role, self.fifo_path)
                    return False
        except Exception as e:
            # A stop or a switch closes the socket under the read on purpose;
            # that is the mechanism, not a fault, and must not be counted as
            # a source error or logged as one.
            if self._stop.is_set() or self._switch.is_set():
                log.debug("Multi-view slot %s %s: %s closed for a change",
                          self.slot_id, self.role, slug)
            else:
                self.source_errors += 1
                log.warning("Multi-view slot %s %s: source %s ended (%s)",
                            self.slot_id, self.role, slug, e)
        finally:
            with self._lock:
                self._active = None
            close = getattr(source, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass
        return True

    def live_source_idle(self):
        """Seconds since a *live* source last produced. None if on filler.

        Named by what it excludes rather than what it matches: filler is a
        loop of a local file and never stalls, so a feeder playing it is not
        the thing to cut. Anything else is a real source and is watched.
        """
        with self._lock:
            source = self._active
        if source is None or isinstance(source, _FillerSource):
            return None
        return time.time() - self.last_write_at

    def quiet_for(self):
        """Seconds since this feeder wrote a byte, whatever it was doing."""
        if not self.last_write_at:
            return None
        return time.time() - self.last_write_at

    def drop_stalled(self):
        """Cut a source that has gone quiet so the pump can reconnect.

        A source that stops producing without closing leaves this feeder
        blocked in a read that will never return and its FIFO silent - and
        since ffmpeg cannot run the filter graph without both inputs, that
        freezes the whole composite, not just one side of it. Measured live:
        a secondary that stalled thirteen seconds in held the composite for
        55 minutes, output frozen, while the primary poured gigabytes into a
        queue nobody could use.
        """
        self.stalls += 1
        log.warning("Multi-view slot %s %s: %s has produced nothing for %.0fs,"
                    " cutting it", self.slot_id, self.role, self._current,
                    time.time() - self.last_write_at)
        self._close_active()

    def _close_active(self):
        """Close whatever source is live, so a blocked read unwinds now."""
        with self._lock:
            source = self._active
        close = getattr(source, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass

    def _write_all(self, fd, data):
        """Write every byte, or return False if the reader has gone.

        select() with a timeout rather than a blocking write: a reader that
        stops draining must not be able to wedge this thread past a stop
        request. The loop simply comes back around and re-checks.
        """
        view = memoryview(data)
        while view:
            if self._stop.is_set():
                return False
            try:
                _r, writable, _x = select.select([], [fd], [], WRITE_POLL)
            except (OSError, ValueError):
                return False
            if not writable:
                continue
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                continue
            except BrokenPipeError:
                return False
            except OSError as e:
                if e.errno in (errno.EPIPE, errno.EBADF):
                    return False
                raise
            view = view[written:]
            self.bytes_written += written
            self.last_write_at = time.time()
        return True


class SlotFeeders:
    """The pair of feeders behind one composite slot."""

    def __init__(self, slot_id, fifo_dir, base_url, open_source=None):
        self.slot_id = str(slot_id)
        # A token per instance, not per slot. Tearing a composite down is not
        # instant - feeders have to unwind out of blocking writes - and the
        # reaper drops the slot before that finishes, so a viewer who retunes
        # immediately used to get a composite whose FIFOs the *previous* one
        # then deleted out from under it. With a name nobody else can guess,
        # a late cleanup can only ever remove its own.
        self.token = os.urandom(4).hex()
        self.primary = Feeder(
            os.path.join(fifo_dir, "mv%s-primary-%s.ts" % (slot_id, self.token)),
            slot_id, "primary", base_url, open_source)
        self.secondary = Feeder(
            os.path.join(fifo_dir, "mv%s-secondary-%s.ts" % (slot_id, self.token)),
            slot_id, "secondary", base_url, open_source)

    def start(self, slot):
        self.primary.start(slot.get("primary") or None)
        self.secondary.start(slot.get("secondary") or None)
        return self

    def apply(self, slot):
        """Point both feeders at what the slot now says. Returns what moved."""
        moved = []
        if self.primary.switch(slot.get("primary") or None):
            moved.append("primary")
        if self.secondary.switch(slot.get("secondary") or None):
            moved.append("secondary")
        return moved

    def stop(self):
        self.primary.stop()
        self.secondary.stop()

    def stats(self):
        return {"primary": self.primary.stats(),
                "secondary": self.secondary.stats()}


# ─── The composite ────────────────────────────────────────────────────────────
# One ffmpeg per slot: two FIFOs in, one MPEG-TS out. The graph shape never
# changes while it runs - that is what Phase 7's live commands depend on - so
# corner, size and the mixer are set as starting values here and retargeted
# later rather than rebuilt.

SIZE_FRACTION = {"small": 0.22, "medium": 0.30, "large": 0.40}
CORNER_MARGIN = 32


def pip_geometry(corner, size, width, height, margin=CORNER_MARGIN):
    """(w, h, x, y) for the miniplayer. Even dimensions: H.264 needs them."""
    frac = SIZE_FRACTION.get(size, SIZE_FRACTION["medium"])
    w = int(width * frac) // 2 * 2
    h = int(w * height / float(width)) // 2 * 2
    x = margin if corner in ("tl", "bl") else width - w - margin
    y = margin if corner in ("tl", "tr") else height - h - margin
    return w, h, x, y


def _filter_value(value):
    """Escape a filter option value that contains ':'. Twice.

    The string is unescaped once by the filtergraph parser and again by the
    individual filter's option parser, so a colon that has to survive both
    needs two backslashes. One produces `No option name near '//127.0.0.1'`,
    which reads like a malformed URL rather than an escaping bug.
    """
    return value.replace("\\", "\\\\").replace(":", "\\\\:")


def _gain(value):
    """A 0-100 mixer setting as a filter gain."""
    try:
        return max(0, min(100, int(value))) / 100.0
    except (TypeError, ValueError):
        return 0.0


# Instance names for the filters Phase 7 drives. Named here rather than
# spelled into the graph string, so the graph and the commands cannot drift
# apart - a command to a name that does not exist is answered "Success" by
# some builds and is then very hard to see.
PIP_SCALE, PIP_OVERLAY = "scale@pip", "overlay@pip"
VOL_PRIMARY, VOL_SECONDARY = "volume@pri", "volume@sec"


def layout_commands(slot, width, height, previous=None):
    """The commands that move a running graph to `slot`'s layout.

    Ordered so the miniplayer is never momentarily off the edge: growing, the
    move goes first (a small player at the new position is always inside);
    shrinking, the resize goes first. The two commands land a millisecond
    apart, so this is worth at most one frame - but it costs nothing.
    """
    w, h, x, y = pip_geometry(slot.get("corner"), slot.get("size"), width, height)
    resize = [(PIP_SCALE, "width", str(w)), (PIP_SCALE, "height", str(h))]
    move = [(PIP_OVERLAY, "x", str(x)), (PIP_OVERLAY, "y", str(y))]
    if previous is not None:
        pw, _ph, _px, _py = pip_geometry(previous.get("corner"),
                                         previous.get("size"), width, height)
        if w > pw:
            return move + resize
    return resize + move


def audio_commands(slot):
    """The commands that set a running graph's mix."""
    audio = slot.get("audio") or {}
    return [(VOL_PRIMARY, "volume", "%.3f" % _gain(audio.get("primary", 100))),
            (VOL_SECONDARY, "volume", "%.3f" % _gain(audio.get("secondary", 0)))]


def build_filter(slot, width, height, fps, control=None):
    """The filter graph, as one -filter_complex string.

    Both branches are normalised to the output's size and rate before the
    overlay. Live sources disagree on both - measured in Phase 1, an NFL
    1080p59.94 primary against an MLB 720p30 secondary - and overlaying
    mismatched rates drifts.

    `zmq` sits on the main branch: it is a passthrough that costs nothing and
    is where every live change arrives. The filters it drives carry explicit
    instance names, because a command is addressed by name - an unnamed filter
    cannot be retargeted, and the four that can change while playing are
    exactly the four named here.
    """
    w, h, x, y = pip_geometry(slot.get("corner"), slot.get("size"), width, height)
    audio = slot.get("audio") or {}
    pa = _gain(audio.get("primary", 100))
    sa = _gain(audio.get("secondary", 0))
    ctl = ("zmq@ctl=bind_address=%s," % _filter_value(control)) if control else ""
    return (
        "[0:v]hwdownload,format=nv12,format=yuv420p,"
        "scale=%d:%d,fps=%d,%s" % (width, height, fps, ctl) +
        "null[m];"
        "[1:v]hwdownload,format=nv12,format=yuv420p,"
        "%s=%d:%d,fps=%d[p];"
        "[m][p]%s=x=%d:y=%d:eof_action=pass,format=nv12,hwupload[v];"
        "[0:a]%s=%.3f,aresample=async=1[a0];"
        "[1:a]%s=%.3f,aresample=async=1[a1];"
        "[a0][a1]amix=inputs=2:duration=first:normalize=0[a]"
        % (PIP_SCALE, w, h, fps, PIP_OVERLAY, x, y,
           VOL_PRIMARY, pa, VOL_SECONDARY, sa)
    )


def build_command(slot, primary_fifo, secondary_fifo, *, width, height, fps,
                  encoder, qp, render_node, ffmpeg="ffmpeg", control=None):
    """Full argv for one composite."""
    args = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-fflags", "+genpts"]
    if encoder == "vaapi":
        # Named explicitly, and reused by both inputs and the filter graph.
        # Letting each input create its own device leaves ffmpeg picking one
        # for the filters "by default", which it warns about and which is not
        # a thing to leave to chance on a box with more than one node.
        args += ["-init_hw_device", "vaapi=va:%s" % render_node,
                 "-filter_hw_device", "va"]
    for fifo in (primary_fifo, secondary_fifo):
        if encoder == "vaapi":
            args += ["-hwaccel", "vaapi", "-hwaccel_device", "va",
                     "-hwaccel_output_format", "vaapi"]
        # Wallclock timestamps, not the stream's own. Each input is a live
        # feed whose internal clock restarts every time the source changes or
        # the filler loops; replaying those values gives "DTS out of order"
        # and corrupt-packet complaints. Arrival time is the only clock that
        # is continuous across a switch.
        args += ["-use_wallclock_as_timestamps", "1",
                 "-thread_queue_size", "1024", "-f", "mpegts", "-i", fifo]

    if encoder == "vaapi":
        graph = build_filter(slot, width, height, fps, control=control)
        venc = ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(qp)]
    else:
        # Software fallback: the same graph without the hardware hops.
        graph = (build_filter(slot, width, height, fps, control=control)
                 .replace("hwdownload,format=nv12,format=yuv420p,", "format=yuv420p,")
                 .replace(",format=nv12,hwupload[v]", "[v]"))
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(qp)]

    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]"]
    args += venc
    args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000",
             "-f", "mpegts", "-flush_packets", "1", "pipe:1"]
    return args


class Composite:
    """One slot's encoder, its two feeders, and its viewers."""

    def __init__(self, slot_id, slot, *, fifo_dir, base_url, filler,
                 width=1920, height=1080, fps=60, encoder="vaapi", qp=23,
                 render_node="/dev/dri/renderD128", ffmpeg="ffmpeg",
                 idle_timeout=60):
        self.slot_id = str(slot_id)
        self.slot = dict(slot)
        self.width, self.height, self.fps = width, height, fps
        self.encoder, self.qp = encoder, qp
        self.render_node, self.ffmpeg = render_node, ffmpeg
        self.idle_timeout = idle_timeout

        self.feeders = SlotFeeders(slot_id, fifo_dir, base_url)
        self.feeders.primary.filler = filler
        self.feeders.secondary.filler = filler

        self.proc = None
        self.started_at = None
        self.bytes_out = 0
        self.viewers = 0
        self.last_viewer_at = time.time()
        self.exit_code = None
        self.benign_lines = 0
        self.first_byte_at = None
        # One control socket per composite, beside its FIFOs and named with
        # the same token. A Unix socket rather than a TCP port: this container
        # shares a network namespace with everything else behind the VPN, so a
        # port here is a port everything else can reach and can collide with.
        self.control_path = os.path.join(
            fifo_dir, "mv%s-ctl-%s.zmq" % (self.slot_id, self.feeders.token))
        self.control = "ipc://%s" % self.control_path
        self.commands_sent = 0
        self.commands_failed = 0
        self.last_command_error = None
        self.needs_resend = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stderr = []

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        # Feeders first: the FIFOs must exist and something must be ready to
        # write before ffmpeg tries to open and probe them.
        self.feeders.start(self.slot)
        cmd = build_command(
            self.slot, self.feeders.primary.fifo_path,
            self.feeders.secondary.fifo_path,
            width=self.width, height=self.height, fps=self.fps,
            encoder=self.encoder, qp=self.qp, render_node=self.render_node,
            ffmpeg=self.ffmpeg, control=self.control)
        log.info("Multi-view slot %s starting encoder (%s, %dx%d@%d, qp=%d)",
                 self.slot_id, self.encoder, self.width, self.height,
                 self.fps, self.qp)
        log.debug("Multi-view slot %s argv: %s", self.slot_id, " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        self.started_at = time.time()
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name="mvstderr-%s" % self.slot_id).start()
        return self

    def _drain_stderr(self):
        """Surface encoder complaints instead of letting the pipe fill."""
        proc = self.proc
        if proc is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._stderr.append(line)
                del self._stderr[:-20]
                if self._stop.is_set():
                    # We closed the output pipe on purpose; the broken-pipe
                    # complaints that follow are the shutdown working.
                    log.debug("Multi-view slot %s (stopping): %s",
                              self.slot_id, line)
                elif any(b in line for b in BENIGN_STDERR):
                    self.benign_lines += 1
                    log.debug("Multi-view slot %s: %s", self.slot_id, line)
                else:
                    log.warning("Multi-view slot %s: %s", self.slot_id, line)
        except Exception:
            pass

    def hold(self):
        """Count a viewer that is waiting rather than reading yet.

        A cold start takes most of a minute - two resolutions, and ffmpeg
        opens its inputs sequentially so they cannot overlap - and for all of
        it `read()` has not been called, so the composite looks unwatched.
        Without this the reaper would stop an encoder in the middle of its own
        startup at any `MULTIVIEW_IDLE_TIMEOUT` shorter than the cold start,
        which the settings schema happily allows (the floor is 5s).
        """
        with self._lock:
            self.viewers += 1

    def release(self):
        with self._lock:
            self.viewers -= 1
            self.last_viewer_at = time.time()

    def read(self, stop=None):
        """Yield the composite's bytes. One viewer per call.

        `stop` is the caller's own event, set when its client has gone away.
        Polled rather than blocked on: a wedged encoder would otherwise leave
        this thread inside read() forever, still counted as an audience, and a
        composite nobody is watching could then never be reaped. Measured
        live at four phantom viewers on one slot.
        """
        self.hold()
        try:
            while not self._stop.is_set() and not (stop and stop.is_set()):
                proc = self.proc
                if proc is None:
                    break
                try:
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if not ready:
                        continue
                    chunk = proc.stdout.read(CHUNK)
                except (ValueError, OSError):
                    # stop() closes the pipe under us, and with a rebuild that
                    # is the ordinary way a read ends rather than an accident.
                    # The reader treats it as this composite being over and
                    # goes looking for its replacement.
                    break
                if not chunk:
                    break
                if self.first_byte_at is None:
                    # The moment the picture actually exists. Everything
                    # before it is padding on the wire, and this is the only
                    # honest measure of how long a cold tune takes.
                    self.first_byte_at = time.time()
                    log.info("Multi-view slot %s: first output after %.1fs",
                             self.slot_id, self.first_byte_at - self.started_at)
                self.bytes_out += len(chunk)
                yield chunk
        finally:
            self.release()

    # ─── Live control ─────────────────────────────────────────────────────

    def command(self, target, command, arg):
        """Send one command to the running graph. Returns (ok, reply).

        A fresh socket per call. REQ sockets have a strict send/recv turn
        order and a timed-out one is left in a state the next caller inherits;
        at the rate a person moves a miniplayer around, opening one is free.
        """
        if not self.alive():
            return False, "encoder is not running"
        if not os.path.exists(self.control_path):
            # The zmq filter binds when the filter graph is configured, which
            # is only after both inputs have been probed - so for the first
            # seconds of a composite there is nothing listening. Checked
            # rather than waited on: connecting to an absent ipc endpoint does
            # not fail, it queues, and the send then costs a full ZMQ_TIMEOUT
            # per command with the caller blocked behind it.
            return False, "control socket is not up yet"
        try:
            import zmq
        except ImportError as e:                          # pragma: no cover
            return False, "pyzmq missing (%s)" % e

        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.REQ)
        try:
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.RCVTIMEO, int(ZMQ_TIMEOUT * 1000))
            sock.setsockopt(zmq.SNDTIMEO, int(ZMQ_TIMEOUT * 1000))
            sock.connect(self.control)
            sock.send_string("%s %s %s" % (target, command, arg))
            reply = sock.recv_string()
        except Exception as e:                            # noqa: BLE001
            self.commands_failed += 1
            self.last_command_error = "%s %s %s: %s" % (target, command, arg, e)
            log.warning("Multi-view slot %s: control %s %s %s failed (%s)",
                        self.slot_id, target, command, arg, e)
            return False, str(e)
        finally:
            sock.close()

        self.commands_sent += 1
        # ffmpeg answers "<errno> <text>"; 0 is the only success.
        ok = reply.startswith("0 ")
        if not ok:
            self.commands_failed += 1
            self.last_command_error = "%s %s %s: %s" % (target, command, arg,
                                                        reply.strip())
            log.warning("Multi-view slot %s: control %s %s %s rejected (%s)",
                        self.slot_id, target, command, arg, reply.strip())
        else:
            log.debug("Multi-view slot %s: %s %s %s",
                      self.slot_id, target, command, arg)
        return ok, reply.strip()

    def _send_all(self, commands):
        """Send a batch, reporting how many landed. Never raises.

        A rejected command is logged and skipped, not fatal: the stream is
        worth more than the setting. The console sees the failure through
        `last_command_error`, and the value is still stored, so it takes
        effect on the next start either way.

        Abandons the batch on the first *timeout*, because a timeout means the
        graph is not running frames - nothing is draining the output, or the
        inputs have gone quiet - and the zmq filter only services its socket
        between frames. Trying the remaining five would cost five more
        timeouts on a caller that is often the reaper thread. The retry
        catches them once frames move again.
        """
        ok = 0
        for target, command, arg in commands:
            landed, reply = self.command(target, command, arg)
            if landed:
                ok += 1
            elif "temporarily unavailable" in reply.lower() or "timed out" in reply.lower():
                log.info("Multi-view slot %s: graph is not consuming, "
                         "deferring %d of %d commands", self.slot_id,
                         len(commands) - ok, len(commands))
                break
        # A partial application is the dangerous state: the stored value has
        # moved on and the picture has not, and nothing would ever look
        # again. Flagged so the reaper retries rather than leaving the two
        # permanently out of step.
        self.needs_resend = ok < len(commands)
        return ok, len(commands)

    def apply(self, slot):
        """Move a running composite to `slot`, without restarting it.

        Three different mechanisms, which is why this is not a one-liner:
        sources move by retargeting the feeders, layout and mix move by
        commanding the filter graph, and nothing here touches ffmpeg's
        lifecycle. That is the whole point - the output must not break.

        Returns a dict of what changed.
        """
        previous, self.slot = self.slot, dict(slot)
        changed = {"sources": [], "layout": False, "audio": False,
                   "commands": (0, 0), "restart": False}

        moved = [role for role in ("primary", "secondary")
                 if (previous.get(role) or "") != (slot.get(role) or "")]
        if moved and self.alive():
            # Retargeting a feeder was the plan, and it does not work: a
            # different stream spliced onto an input ffmpeg has already probed
            # freezes the composite, permanently and silently. Verified live -
            # a healthy 13.8 MB composite stopped dead the instant its
            # secondary changed and never produced another byte. So a source
            # change rebuilds the encoder, which costs a re-buffer and keeps
            # the picture honest. Layout and the mix are unaffected and stay
            # seamless.
            changed["sources"] = moved
            changed["restart"] = True
            return changed

        changed["sources"] = self.feeders.apply(slot)

        if not self.alive():
            # Nothing to command. The new values are stored and will be the
            # graph's starting values whenever it next starts.
            return changed

        commands = []
        if (previous.get("corner") != slot.get("corner")
                or previous.get("size") != slot.get("size")):
            changed["layout"] = True
            commands += layout_commands(slot, self.width, self.height,
                                        previous=previous)
        if (previous.get("audio") or {}) != (slot.get("audio") or {}):
            changed["audio"] = True
            commands += audio_commands(slot)
        if commands:
            changed["commands"] = self._send_all(commands)
        return changed

    def resend(self):
        """Push the whole current layout and mix at the graph.

        Used after anything that could have left the two out of step, and by
        the console as a "make it look like what it says" button.
        """
        sent = self._send_all(
            layout_commands(self.slot, self.width, self.height)
            + audio_commands(self.slot))
        return sent

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def idle_for(self):
        with self._lock:
            if self.viewers > 0:
                return 0.0
        return time.time() - self.last_viewer_at

    def stop(self):
        """Stop the encoder and both feeders. Safe to call twice."""
        self._stop.set()
        proc, self.proc = self.proc, None
        if proc is not None:
            self.exit_code = proc.poll()
            if self.exit_code is None:
                # Close the output pipe first. ffmpeg blocked writing into a
                # pipe nobody is draining does not act on SIGTERM, so without
                # this every stop ends in a kill.
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.warning("Multi-view slot %s: encoder ignored "
                                "terminate, killing", self.slot_id)
                    proc.kill()
                    proc.wait(timeout=5)
                self.exit_code = proc.returncode
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass
        self.feeders.stop()
        # The control socket outlives ffmpeg the way a FIFO does, and a stale
        # one in a tmpfs is litter rather than state.
        try:
            os.unlink(self.control_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove control socket %s: %s",
                        self.control_path, e)
        log.info("Multi-view slot %s stopped (exit=%s, %.1f MB out)",
                 self.slot_id, self.exit_code, self.bytes_out / 1e6)

    def stats(self):
        up = time.time() - self.started_at if self.started_at else 0
        return {
            "slot": self.slot_id,
            "alive": self.alive(),
            "encoder": self.encoder,
            "output": "%dx%d@%d" % (self.width, self.height, self.fps),
            "qp": self.qp,
            "uptime_seconds": round(up, 1),
            # How long this composite took to produce its first byte. None
            # while it is still starting, which is also how you tell a slow
            # start from a stalled one.
            "startup_seconds": (round(self.first_byte_at - self.started_at, 1)
                                if self.first_byte_at and self.started_at
                                else None),
            "bytes_out": self.bytes_out,
            "mbit_per_s": round(self.bytes_out * 8 / 1e6 / up, 2) if up > 1 else None,
            "viewers": self.viewers,
            "idle_seconds": round(self.idle_for(), 1),
            "exit_code": self.exit_code,
            "primary": self.slot.get("primary"),
            "secondary": self.slot.get("secondary"),
            "corner": self.slot.get("corner"),
            "size": self.slot.get("size"),
            "audio": self.slot.get("audio"),
            "inputs": self.feeders.stats(),
            "last_errors": list(self._stderr[-3:]),
            # Expected timestamp grumbles, counted rather than logged. A
            # climbing number here is normal; a non-empty last_errors is not.
            "benign_lines": self.benign_lines,
            # Live control. A climbing `failed` with the stream still running
            # is the signature of a graph that stopped listening - the setting
            # is stored and the picture is stale.
            "commands_sent": self.commands_sent,
            "commands_failed": self.commands_failed,
            "last_command_error": self.last_command_error,
            "needs_resend": self.needs_resend,
        }


class Manager:
    """Holds the running composites and enforces how many may run."""

    # The fields that describe what a composite should look like. "updated"
    # is deliberately not among them: it changes on every write and would
    # make every reconcile look like a change.
    SHAPE = ("primary", "secondary", "corner", "size", "audio")

    def __init__(self, reconcile=None, **defaults):
        # `reconcile` reads the stored configuration for a slot. Given one,
        # the document on disk becomes the authority and a running composite
        # is brought back into line with it - so any writer at all gets live
        # application, and a change can never be persisted but not shown.
        self.reconcile = reconcile
        self.defaults = defaults
        self._slots = {}
        self._seen = {}             # slot -> (bytes_out, when it last moved)
        self._lock = threading.Lock()
        self._reaper = None
        self._stop = threading.Event()

    def get(self, slot_id):
        with self._lock:
            return self._slots.get(str(slot_id))

    def start(self, slot_id, slot, max_active=1):
        """Start (or return) the composite for this slot.

        Returns (composite, error). Refuses rather than quietly starting a
        second encoder: one composite is about a third of this box, and two
        would spoil both rather than serving either.
        """
        slot_id = str(slot_id)
        with self._lock:
            existing = self._slots.get(slot_id)
            if existing is not None and existing.alive():
                return existing, None
            if existing is not None:
                existing.stop()
                self._slots.pop(slot_id, None)
            live = [s for s in self._slots.values() if s.alive()]
            if len(live) >= max_active:
                return None, ("another composite is already running (slot %s)"
                              % live[0].slot_id)
            comp = Composite(slot_id, slot, **self.defaults)
            self._slots[slot_id] = comp
        try:
            comp.start()
        except Exception as e:
            log.exception("Multi-view slot %s failed to start", slot_id)
            comp.stop()
            with self._lock:
                self._slots.pop(slot_id, None)
            return None, str(e)
        self._ensure_reaper()
        return comp, None

    def apply(self, slot_id, slot, max_active=1):
        """Move a running composite to `slot`. Quiet if none is running.

        Returns what changed, so a caller can tell "the picture moved" from
        "the setting was recorded for next time" - the console needs that
        distinction to say anything honest.
        """
        comp = self.get(slot_id)
        if comp is None or not comp.alive():
            return {"running": False, "sources": [], "layout": False,
                    "audio": False, "commands": (0, 0), "restart": False}
        changed = comp.apply(slot)
        changed["running"] = True
        if changed.get("restart"):
            log.info("Multi-view slot %s: %s changed, rebuilding the encoder",
                     slot_id, " and ".join(changed["sources"]))
            self.stop(slot_id)
            self.start(slot_id, slot, max_active=max_active)
        return changed

    def stop(self, slot_id):
        with self._lock:
            comp = self._slots.pop(str(slot_id), None)
        if comp is not None:
            comp.stop()
        return comp is not None

    def stop_all(self):
        self._stop.set()
        with self._lock:
            slots = list(self._slots.values())
            self._slots.clear()
        for comp in slots:
            comp.stop()

    def stats(self):
        with self._lock:
            return [c.stats() for c in self._slots.values()]

    def _ensure_reaper(self):
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._stop.clear()
        self._reaper = threading.Thread(target=self._reap, daemon=True,
                                        name="mvreaper")
        self._reaper.start()

    def _reap(self):
        """Retire composites nobody is watching, and ones that have died.

        An encoder must never outlive its audience: it is the most expensive
        thing this service does, and a forgotten one costs a third of the box
        indefinitely.
        """
        while not self._stop.is_set():
            self._stop.wait(5.0)
            for slot_id, comp in list(self._slots.items()):
                if not comp.alive():
                    log.warning("Multi-view slot %s: encoder exited (%s)",
                                slot_id, comp.exit_code)
                    self.stop(slot_id)
                elif comp.idle_for() > comp.idle_timeout:
                    log.info("Multi-view slot %s: no viewer for %.0fs, stopping",
                             slot_id, comp.idle_for())
                    self.stop(slot_id)
                elif self._output_stalled(comp):
                    # Nothing is coming out and nothing will: an input has
                    # changed under ffmpeg and that input is gone for good.
                    # Tearing it down is what lets the viewer's stream
                    # re-attach to a working one.
                    log.warning("Multi-view slot %s: no output for %.0fs, "
                                "rebuilding", slot_id, OUTPUT_STALL_TIMEOUT)
                    self.stop(slot_id)
                else:
                    self._reconcile_one(slot_id, comp)

    def _output_stalled(self, comp):
        """True when a watched composite has stopped producing entirely."""
        if comp.viewers <= 0 or comp.first_byte_at is None:
            return False            # not started yet, or nobody is watching
        seen = self._seen.get(comp.slot_id)
        now = time.time()
        if seen is None or seen[0] != comp.bytes_out:
            self._seen[comp.slot_id] = (comp.bytes_out, now)
            return False
        return now - seen[1] > OUTPUT_STALL_TIMEOUT

    def _reconcile_one(self, slot_id, comp):
        """Bring one running composite back into line with what is stored."""
        if self.reconcile is None:
            return
        try:
            stored = self.reconcile(slot_id)
        except Exception:                                 # noqa: BLE001
            log.exception("Multi-view slot %s: could not read its "
                          "configuration", slot_id)
            return
        if not stored:
            return
        if all(stored.get(k) == comp.slot.get(k) for k in self.SHAPE):
            if comp.needs_resend:
                log.info("Multi-view slot %s: retrying a change that did not "
                         "fully land", slot_id)
                comp.resend()
            return
        log.info("Multi-view slot %s: applying stored configuration", slot_id)
        try:
            changed = comp.apply(stored)
        except Exception:                                 # noqa: BLE001
            log.exception("Multi-view slot %s: could not apply its "
                          "configuration", slot_id)
            return
        if changed.get("restart"):
            # A channel changed, which cannot be done to a running graph.
            # Stopping is the whole job: the viewer's stream re-attaches and
            # starts a fresh one from the stored configuration, so the rebuild
            # happens on the thread that is actually waiting for the picture
            # rather than on the reaper.
            log.info("Multi-view slot %s: %s changed, stopping so it can be "
                     "rebuilt", slot_id, " and ".join(changed["sources"]))
            self.stop(slot_id)
