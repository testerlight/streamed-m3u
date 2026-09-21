#!/usr/bin/env python3
"""
dispatcharr_sync.py
-------------------
Keeps Dispatcharr in step with the streamed-m3u *team* playlist.

One cycle per invocation by default, for cron or a one-off run. With
--loop it runs forever, sleeping SYNC_INTERVAL seconds between cycles, which
is how the streamed-m3u-sync container uses it. Loop mode never exits on a
failed cycle: Dispatcharr may simply be restarting, and the next cycle
retries from a fresh login.

Four jobs, in order:

1. REFRESH  - Re-pull the team M3U so Dispatcharr sees the current stream list.
              The playlist is never filtered; every roster entry stays a stream.
2. SYNC     - Create a channel for any in-lineup stream that does not have one.
3. EPG      - Re-import the guide so fixtures stay current.
4. LINEUP   - Set hidden_from_output so only the Jellyfin lineup is visible.
              Never deletes a channel.

It deliberately DELETES NOTHING. In the team-channel model a channel is a
permanent shelf for whichever fixture that team happens to have; deleting it
when a game ends would recycle channel numbers and shuffle the Jellyfin guide,
which is the exact problem this design exists to solve. A team with no game
shows "No game scheduled" from the EPG instead of disappearing.

(The previous version managed the old per-match account and had to delete
finished games. That account was retired 2026-08-25; the deletion logic went
with it. See _backups/phase9-legacy-* for the exported state.)

Configuration (environment variables):
    DISPATCHARR_URL      default http://dispatcharr:9191, the compose service
                         name; set it explicitly when running on a host
    DISPATCHARR_USER     required
    DISPATCHARR_PASS     required
    M3U_ACCOUNT_NAME     Exact name of the team M3U account
    EPG_SOURCE_NAME      Exact name of the team EPG source ("" to skip)
    SYNC_INTERVAL        seconds between cycles in --loop mode (default 480)
    STREAMED_M3U_URL     base URL of streamed-m3u for /teams?all=1
                         (default http://gluetun:8787; novpn: http://streamed-m3u:8787)

Exit codes: 0 cycle ok, 1 cycle failed (one-shot mode), 2 not configured.
"""

import argparse
import os
import sys
import time
import logging
from urllib.parse import parse_qs, urlparse

import requests

# ─── Configuration ────────────────────────────────────────────────────────────
DISPATCHARR_URL  = os.getenv("DISPATCHARR_URL",  "http://dispatcharr:9191").rstrip("/")
DISPATCHARR_USER = os.getenv("DISPATCHARR_USER", "")
DISPATCHARR_PASS = os.getenv("DISPATCHARR_PASS", "")
M3U_ACCOUNT_NAME = os.getenv("M3U_ACCOUNT_NAME", "streamed.pk teams")
EPG_SOURCE_NAME  = os.getenv("EPG_SOURCE_NAME",  "streamed.pk teams EPG")
# Seconds to let Dispatcharr finish ingesting the playlist before we look at it.
REFRESH_SETTLE   = int(os.getenv("REFRESH_SETTLE", "10"))
# Seconds to let asynchronous bulk channel creation commit.
CREATE_SETTLE    = int(os.getenv("CREATE_SETTLE", "15"))
# Where this container reaches streamed-m3u. It must not share gluetun's
# namespace (gotcha #7); gluetun:8787 is the compose-network address.
STREAMED_M3U_URL = os.getenv("STREAMED_M3U_URL", "http://gluetun:8787").rstrip("/")

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Auth ─────────────────────────────────────────────────────────────────────
session = requests.Session()


def login():
    r = session.post(
        f"{DISPATCHARR_URL}/api/accounts/token/",
        json={"username": DISPATCHARR_USER, "password": DISPATCHARR_PASS},
        timeout=10,
    )
    r.raise_for_status()
    session.headers.update({"Authorization": f"Bearer {r.json()['access']}"})
    log.info("Authenticated with Dispatcharr")


def ensure_auth():
    """Re-login if the token is missing or expired."""
    try:
        if session.get(f"{DISPATCHARR_URL}/api/accounts/users/me/",
                       timeout=5).status_code == 401:
            login()
    except Exception:
        login()


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _paged(path, **params):
    """Yield every result across Dispatcharr's paginated endpoints."""
    page = 1
    while True:
        r = session.get(f"{DISPATCHARR_URL}{path}",
                        params={**params, "page": page, "page_size": 500},
                        timeout=20)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        for item in results:
            yield item
        if isinstance(data, dict) and data.get("next"):
            page += 1
        else:
            break


def get_m3u_account_id():
    r = session.get(f"{DISPATCHARR_URL}/api/m3u/accounts/", timeout=10)
    r.raise_for_status()
    for acct in r.json():
        if acct.get("name") == M3U_ACCOUNT_NAME:
            return acct["id"]
    raise RuntimeError(f"M3U account '{M3U_ACCOUNT_NAME}' not found")


def trigger_refresh(account_id):
    r = session.post(f"{DISPATCHARR_URL}/api/m3u/refresh/{account_id}/",
                     timeout=15)
    if r.status_code in (200, 202, 204):
        log.info("M3U refresh triggered for account %d", account_id)
        return True
    log.warning("Refresh returned %d: %s", r.status_code, r.text[:200])
    return False


def get_all_streams(account_id):
    """Every stream on this account.

    Ordered by name explicitly: Dispatcharr's own default here is descending,
    which would otherwise flow through into the order channels get created.
    """
    return list(_paged("/api/channels/streams/",
                       m3u_account=account_id, ordering="name"))


def list_channels():
    """Every channel, including hidden_from_output.

    Dispatcharr's list defaults to visibility_filter=active and omits
    hidden rows. Sync would then treat a hidden channel as missing, create
    a duplicate on the next +, and leave the original hidden forever
    (gotcha #26).
    """
    return list(_paged("/api/channels/channels/", visibility_filter="all"))


def get_existing_channel_stream_ids():
    """Stream IDs that already have a channel, across all accounts."""
    seen = set()
    for ch in list_channels():
        for sid in (ch.get("stream_ids") or ch.get("streams") or []):
            seen.add(sid.get("id") if isinstance(sid, dict) else sid)
    return seen


def create_channels_for_streams(stream_ids):
    if not stream_ids:
        return
    r = session.post(
        f"{DISPATCHARR_URL}/api/channels/channels/from-stream/bulk/",
        json={"stream_ids": stream_ids},
        timeout=60,
    )
    if r.status_code in (200, 201, 202):
        log.info("Created channels for %d new team(s)", len(stream_ids))
    else:
        log.warning("Bulk create returned %d: %s", r.status_code, r.text[:200])


def get_epg_source_id():
    """Numeric id of the team EPG source, or None if it is not configured."""
    r = session.get(f"{DISPATCHARR_URL}/api/epg/sources/", timeout=10)
    r.raise_for_status()
    for src in r.json():
        if src.get("name") == EPG_SOURCE_NAME:
            return src["id"]
    log.warning("EPG source '%s' not found", EPG_SOURCE_NAME)
    return None


def refresh_epg(source_id):
    """Re-import the guide so new fixtures appear without waiting an hour.

    The id MUST be sent in the body as "id". Posting to this endpoint with no
    body is silently useless: the worker dispatches with source None, logs
    "EPG source with ID None not found", and parses nothing. That failure mode
    is invisible from the HTTP response, which still returns 202.
    """
    if not source_id:
        return
    try:
        r = session.post(f"{DISPATCHARR_URL}/api/epg/import/",
                         json={"id": source_id}, timeout=30)
        if r.status_code in (200, 202, 204):
            log.info("EPG import triggered for source %d", source_id)
        else:
            log.warning("EPG import returned %d: %s",
                        r.status_code, r.text[:200])
    except Exception as e:
        log.warning("EPG import failed: %s", e)


def stream_team_slug(stream):
    """Slug from a stream URL's /stream?team= query, never the display name."""
    url = (stream or {}).get("url") or ""
    if not url:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    vals = qs.get("team") or []
    slug = (vals[0] or "").strip() if vals else ""
    return slug or None


def fetch_lineup_map(base_url=None):
    """slug -> in_lineup from streamed-m3u /teams?all=1."""
    origin = (base_url or STREAMED_M3U_URL).rstrip("/")
    r = requests.get(origin + "/teams?all=1", timeout=30)
    r.raise_for_status()
    data = r.json()
    roster = data.get("roster") or {}
    return {slug: bool((entry or {}).get("in_lineup")) for slug, entry in roster.items()}


def _channel_sort_key(ch):
    """Lowest channel_number, then lowest id. Missing number sorts last."""
    num = ch.get("channel_number")
    try:
        num = float(num)
    except (TypeError, ValueError):
        num = float("inf")
    try:
        cid = int(ch.get("id"))
    except (TypeError, ValueError):
        cid = 0
    return (num, cid)


def visibility_diffs(channels, in_lineup, slug_of):
    """Channel rows whose hidden_from_output does not match the lineup.

    channels: iterable of Dispatcharr channel dicts.
    in_lineup: slug -> bool.
    slug_of: channel dict -> slug or None. Unknown / non-streamed channels
    are skipped. Only diffs are returned, ready for edit/bulk.

    One visible channel per in-lineup slug: the lowest channel_number
    (then lowest id) stays shown and extras stay hidden, so a duplicate
    created before gotcha #26 does not reappear on the next + .
    """
    groups = {}
    for ch in channels:
        slug = slug_of(ch)
        if not slug or slug not in in_lineup:
            continue
        groups.setdefault(slug, []).append(ch)

    diffs = []
    for slug, rows in groups.items():
        if not in_lineup[slug]:
            for ch in rows:
                if not ch.get("hidden_from_output"):
                    diffs.append({"id": ch["id"], "hidden_from_output": True})
            continue
        keeper_id = min(rows, key=_channel_sort_key)["id"]
        for ch in rows:
            want_hidden = ch["id"] != keeper_id
            if bool(ch.get("hidden_from_output")) != want_hidden:
                diffs.append({"id": ch["id"], "hidden_from_output": want_hidden})
    return diffs


def apply_lineup_visibility(in_lineup, slug_by_stream_id):
    """Bulk-edit hidden_from_output so it matches in_lineup. Diffs only."""

    def slug_of(ch):
        tvg = str(ch.get("tvg_id") or "")
        if not tvg.startswith("streamed."):
            return None
        for sid in (ch.get("stream_ids") or ch.get("streams") or []):
            key = sid.get("id") if isinstance(sid, dict) else sid
            slug = slug_by_stream_id.get(key)
            if slug:
                return slug
        # Fallback: streamed.team.<slug> / streamed.feed.<slug>
        parts = tvg.split(".", 2)
        return parts[2] if len(parts) == 3 else None

    diffs = visibility_diffs(list_channels(), in_lineup, slug_of)
    if not diffs:
        log.info("Lineup visibility: no changes")
        return 0
    hide_n = sum(1 for d in diffs if d["hidden_from_output"])
    show_n = len(diffs) - hide_n
    r = session.patch(
        f"{DISPATCHARR_URL}/api/channels/channels/edit/bulk/",
        json=diffs,
        timeout=60,
    )
    if r.status_code in (200, 201, 202, 204):
        log.info("Lineup visibility: hid %d, showed %d", hide_n, show_n)
        return len(diffs)
    log.warning("Lineup bulk edit returned %d: %s", r.status_code, r.text[:200])
    return 0


# ─── Cycle ────────────────────────────────────────────────────────────────────
def link_epg_data(source_id):
    """Attach guide data to channels that were created without it.

    Dispatcharr links a channel to its EPG entry at creation time by matching
    tvg_id. A channel created BEFORE its EPG entry exists therefore stays
    unlinked forever: the guide data is present and the channel is present,
    but nothing joins them, so the channel shows an empty guide. That is
    exactly what happens the first time a new team or feed appears, because
    the playlist entry and the EPG entry are both brand new that cycle.

    This repairs any such channel, and is a cheap no-op once all are linked.
    """
    if not source_id:
        return
    try:
        r = session.get(f"{DISPATCHARR_URL}/api/epg/epgdata/",
                        params={"page_size": 10000}, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows = data.get("results", data) if isinstance(data, dict) else data
        emap = {x["tvg_id"]: x["id"] for x in rows
                if x.get("epg_source") == source_id}
    except Exception as e:
        log.warning("Could not read EPG data rows: %s", e)
        return

    linked = 0
    for ch in list_channels():
        tvg = str(ch.get("tvg_id") or "")
        if not tvg.startswith("streamed.") or ch.get("epg_data_id"):
            continue
        eid = emap.get(tvg)
        if not eid:
            continue
        try:
            session.patch(
                f"{DISPATCHARR_URL}/api/channels/channels/{ch['id']}/",
                json={"epg_data_id": eid}, timeout=20)
            linked += 1
        except Exception as e:
            log.warning("Could not link %s: %s", tvg, e)
    if linked:
        log.info("Linked guide data to %d channel(s)", linked)


def run_cycle(account_id):
    trigger_refresh(account_id)
    time.sleep(REFRESH_SETTLE)

    streams = get_all_streams(account_id)
    log.info("Team account holds %d stream(s)", len(streams))

    slug_by_stream_id = {}
    for s in streams:
        slug = stream_team_slug(s)
        if slug:
            slug_by_stream_id[s["id"]] = slug

    try:
        in_lineup = fetch_lineup_map()
    except Exception as e:
        log.warning("Could not read lineup from streamed-m3u: %s", e)
        in_lineup = None

    existing = get_existing_channel_stream_ids()
    if in_lineup is None:
        new_ids = [s["id"] for s in streams if s["id"] not in existing]
    else:
        new_ids = [s["id"] for s in streams
                   if s["id"] not in existing
                   and in_lineup.get(slug_by_stream_id.get(s["id"]), False)]

    if new_ids:
        log.info("Creating channels for %d in-lineup stream(s)", len(new_ids))
        create_channels_for_streams(new_ids)
        # Bulk creation is asynchronous (HTTP 202); let it commit before the
        # guide is imported and linked below.
        time.sleep(CREATE_SETTLE)
    else:
        log.info("Every in-lineup stream already has a channel")

    source_id = get_epg_source_id()
    refresh_epg(source_id)
    link_epg_data(source_id)

    if in_lineup is not None:
        apply_lineup_visibility(in_lineup, slug_by_stream_id)


def run_once() -> bool:
    """One full cycle. Logs and returns False on failure rather than raising,
    so loop mode keeps going while Dispatcharr is down or restarting."""
    log.info("Dispatcharr team sync -> %s | account: %s",
             DISPATCHARR_URL, M3U_ACCOUNT_NAME)
    try:
        login()
        account_id = get_m3u_account_id()
        log.info("Team M3U account id: %d", account_id)
        ensure_auth()
        run_cycle(account_id)
        log.info("Cycle complete")
        return True
    except Exception as e:
        log.error("Cycle failed: %s", e)
        return False


def main():
    ap = argparse.ArgumentParser(
        description="Keep Dispatcharr channels in step with the streamed-m3u team playlist.")
    ap.add_argument("--loop", action="store_true",
                    help="run forever, sleeping --interval seconds between cycles")
    ap.add_argument("--interval", type=int,
                    default=int(os.getenv("SYNC_INTERVAL", "480")),
                    help="seconds between cycles in --loop mode (default: SYNC_INTERVAL or 480)")
    args = ap.parse_args()

    missing = [n for n, v in (("DISPATCHARR_USER", DISPATCHARR_USER),
                              ("DISPATCHARR_PASS", DISPATCHARR_PASS)) if not v]
    if missing:
        log.error("Not configured: set %s in the environment", " and ".join(missing))
        sys.exit(2)

    if not args.loop:
        sys.exit(0 if run_once() else 1)

    interval = max(1, args.interval)
    log.info("Loop mode: one cycle every %ds", interval)
    while True:
        run_once()
        log.info("Next cycle in %ds", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
