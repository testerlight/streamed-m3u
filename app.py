"""
streamed-m3u: Polls the streamed.pk public API and serves a live M3U playlist
for consumption by Dispatcharr (or any IPTV client).

Uses Playwright/Chromium to intercept the real .m3u8 URL from embed pages,
since the stream token is generated client-side in JavaScript.

Endpoints:
  GET /playlist.m3u          - Full M3U playlist
  GET /stream?url=<embedUrl> - Launches headless browser, intercepts .m3u8, proxies TS
  GET /stream/status         - JSON list of all currently active streams with stats
  GET /health                - JSON status
  GET /                      - Operations console (dashboard.py); editable
                               when CONSOLE_PASSWORD is set (auth.py)
  GET /api/overview          - Aggregate status, uptime, cache sizes
  GET /api/config            - Every setting with value, source and bounds
  PUT /api/settings          - Change settings (session + CSRF; settings.py)
  GET /api/lineup            - Jellyfin lineup policy, counts and groups
  PUT /api/lineup            - Add or remove slugs/groups (session + CSRF)
  GET /api/cache             - Extraction, logo and no-audio cache contents
  POST /api/cache/extract/refresh
                             - Force a new Chromium extract for one cache entry
  POST /api/cache/extract/clear
                             - Drop one extract-cache entry
  GET /api/events            - Recent log records, newest first
  POST /api/streams/<id>/disconnect
                             - Stop one active proxy session (console)
  POST /api/restart          - Restart this service and streamed-m3u-sync
  GET /login, POST /logout   - Console session (only with CONSOLE_PASSWORD)

Configuration precedence: /data/settings.json > environment > defaults.
See settings.py.
"""

import os
import re
import time
import uuid
import signal
import logging
import shutil
import subprocess
import threading
import json
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import requests
# curl_cffi impersonates Chrome's TLS/JA3 fingerprint. The strmd.st CDN
# (DDoS-Guard) rejects urllib3's fingerprint with 403 even when headers and
# cookies match exactly; Chrome impersonation gets 200. Used for all CDN
# manifest/segment fetches. Plain `requests` is kept for the streamed.pk API,
# which is not fingerprint-protected.
from curl_cffi import requests as cf_requests
from datetime import datetime, timezone
from urllib.parse import quote
from flask import Flask, Response, jsonify, request, has_request_context

import auth
import dashboard
import dockerctl
import lineup
import settings as _settings

# Compose interpolation and uncommented .env lines hand us VAR="" for unset
# values; int("") would fail below. Treat empty as unset.
_settings.scrub_empty_env()

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_URL          = os.getenv("STREAMED_BASE_URL", "https://streamed.pk")
REFRESH_SECONDS   = int(os.getenv("REFRESH_SECONDS", "480"))
REQUEST_TIMEOUT   = int(os.getenv("REQUEST_TIMEOUT", "10"))
# Concurrency for the playlist build. Each match needs one API call per
# source; serially that was ~400-700 round-trips over the VPN and took
# >5 minutes per cycle. Keep this modest - the streamed.pk API is not
# fingerprint-protected but it can still rate-limit.
BUILD_WORKERS     = int(os.getenv("BUILD_WORKERS", "8"))
PORT              = int(os.getenv("PORT", "8787"))
# The address clients use to reach this service, embedded in every playlist
# URL. Empty means derive it from each request's Host header, which is right
# whenever clients and the service share a network. Set it explicitly when
# they do not: a reverse proxy, a different hostname, a remapped port.
PUBLIC_BASE_URL   = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
# Marker written into the cached legacy playlist, which is built on a
# background thread with no request to derive a host from. playlist() swaps
# it for the real origin at serve time. quote(..., safe="") encodes braces,
# so no URL inside the playlist can ever contain this string by accident.
_BASE_PLACEHOLDER = "{{PUBLIC_BASE_URL}}"


def _public_base() -> str:
    """Origin for URLs handed to clients, without a trailing slash."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    if has_request_context():
        return request.host_url.rstrip("/")
    return _BASE_PLACEHOLDER

LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()
STARTUP_DELAY     = int(os.getenv("STARTUP_DELAY", "15"))
BROWSER_TIMEOUT   = int(os.getenv("BROWSER_TIMEOUT", "15"))
# How long (seconds) to reuse a cached extraction before launching a new browser
EXTRACT_CACHE_TTL  = int(os.getenv("EXTRACT_CACHE_TTL", "300"))  # 5 minutes
EXTRACT_CACHE_FILE = os.getenv("EXTRACT_CACHE_FILE", "/data/extract_cache.json")
TEAMS_FILE         = os.getenv("TEAMS_FILE", "/data/teams.json")
LINEUP_FILE        = os.getenv("LINEUP_FILE", "/data/lineup.json")
# Bundled roster copied into place when TEAMS_FILE does not exist yet.
SEED_TEAMS_FILE    = os.getenv("SEED_TEAMS_FILE", "/app/seed/teams.json")
# Bundled majors-only lineup, copied only on the same first-boot as the roster.
SEED_LINEUP_FILE   = os.getenv("SEED_LINEUP_FILE", "/app/seed/lineup.json")
# Cascade budget. Dispatcharr aborts a channel at channel_init_grace_period
# (currently 60s), so we stop starting new attempts before that ceiling.
# A wall-clock deadline beats an attempt count because a dead source costs
# anywhere from ~2s (fast 404) to the full browser hard timeout (~45s).
#
# Raised 40 -> 55 alongside SEGMENT_PROBE_TIMEOUT going 10s -> 20s. The probe
# is the slowest part of an attempt, so a longer probe with the old 40s budget
# would have left room for only one attempt and silently killed failover to
# the other sources. 55 keeps two attempts viable while staying under the 60s
# grace ceiling - do not raise it past 60 without raising that setting too.
CASCADE_BUDGET       = int(os.getenv("CASCADE_BUDGET", "55"))
CASCADE_MAX_ATTEMPTS = int(os.getenv("CASCADE_MAX_ATTEMPTS", "4"))
# Headroom kept in reserve before starting another attempt. Without it the
# cascade can start an attempt just under the deadline and then overrun it
# badly (observed: a 3rd attempt began at 44.2s of a 45s budget and pushed
# the total to 59.1s, past the 60s grace ceiling). Roughly the cost of a
# median attempt; a hung extraction costs BROWSER_TIMEOUT + ~5s.
CASCADE_RESERVE      = int(os.getenv("CASCADE_RESERVE", "20"))

# Favourite teams kept hot in the background so a click is instant. These
# are matched as home OR away: the API's home/away fields are not reliable
# (observed "Cincinnati Bengals at Philadelphia Eagles" listing the Eagles
# as away), and an away fixture would otherwise never be pre-warmed.
#
# KEEP THIS LIST SMALL (~10). Warming is serial and costs one Chromium
# launch per team (~20-25s each). It is deliberately NOT the same list as
# MAJOR_LEAGUE_TEAMS below: that one only decides which names are
# addressable from the away side, which is a set lookup and costs nothing.
# Do not re-merge the two - it would try to warm every team in four leagues.
# A feed may be listed here too, but check what start time it reports first.
# Observed: RedZone carries a real one (the Sunday kickoff), so the window
# below applies to it normally and warming stays bounded to the broadcast.
# Rally TV and Tennis Channel report 0, which is falsy, so they skip the
# window check entirely and stay eligible the whole time they are listed -
# a genuinely 24/7 feed there would re-warm every couple of minutes forever.
#
# Empty by default, so pre-warming is opt-in. The list used to ship with one
# operator's four teams, which spent a serial browser launch each on channels
# a new deployment has no interest in.
PREWARM_TEAMS = [t.strip() for t in os.getenv(
    "PREWARM_TEAMS", "").split(",") if t.strip()]
# How often to look for work. Cheap when nothing is playing.
PREWARM_INTERVAL      = int(os.getenv("PREWARM_INTERVAL", "60"))
# Re-warm once a cached entry has less than this many seconds of TTL left.
PREWARM_MARGIN        = int(os.getenv("PREWARM_MARGIN", "120"))
# Only warm inside a match's plausible live window (minutes around start).
PREWARM_WINDOW_BEFORE = int(os.getenv("PREWARM_WINDOW_BEFORE", "20"))
PREWARM_WINDOW_AFTER  = int(os.getenv("PREWARM_WINDOW_AFTER", "300"))

# ── Major-league away-side aliasing ──────────────────────────────────────
# A team channel normally resolves only the fixtures where the upstream API
# lists that team as HOME; anything listed away shows "No game scheduled"
# while the game is actually on. Favourites have always been exempt (see
# PREWARM_TEAMS) - these four leagues now are too. This buys addressability
# only: nothing here is ever pre-warmed.
#
# Why an explicit name list rather than a sport rule: the API exposes no
# league field. "american-football" mixes the NFL with ~265 NCAA teams and
# 9 CFL teams, and "basketball" is currently WNBA plus FIBA national sides,
# so a category rule would drag all of those in as well.
#
# Matching is exact slug equality, never substring - mascots collide freely
# across leagues and levels (college "Charlotte 49ers" vs "San Francisco
# 49ers", "St. Louis Cardinals" vs "Arizona Cardinals", a bare rugby
# "Broncos"). NFL and MLB names are verified verbatim against the live
# roster, including the two oddities "Athletics" (no city) and the period
# in "St. Louis Cardinals". NBA and NHL could not be verified - both were
# off-season when this was written - so a few alternate spellings are
# included as cheap insurance, since an unused name costs nothing. Confirm
# them with /teams?alias=1 once those seasons start: a major-league name
# still listed as unseen mid-season is a misspelling here.
NFL_TEAMS = [
    "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens",
    "Buffalo Bills", "Carolina Panthers", "Chicago Bears",
    "Cincinnati Bengals", "Cleveland Browns", "Dallas Cowboys",
    "Denver Broncos", "Detroit Lions", "Green Bay Packers",
    "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars",
    "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers",
    "Los Angeles Rams", "Miami Dolphins", "Minnesota Vikings",
    "New England Patriots", "New Orleans Saints", "New York Giants",
    "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers",
    "San Francisco 49ers", "Seattle Seahawks", "Tampa Bay Buccaneers",
    "Tennessee Titans", "Washington Commanders",
]
MLB_TEAMS = [
    "Arizona Diamondbacks", "Athletics", "Atlanta Braves",
    "Baltimore Orioles", "Boston Red Sox", "Chicago Cubs",
    "Chicago White Sox", "Cincinnati Reds", "Cleveland Guardians",
    "Colorado Rockies", "Detroit Tigers", "Houston Astros",
    "Kansas City Royals", "Los Angeles Angels", "Los Angeles Dodgers",
    "Miami Marlins", "Milwaukee Brewers", "Minnesota Twins",
    "New York Mets", "New York Yankees", "Philadelphia Phillies",
    "Pittsburgh Pirates", "San Diego Padres", "San Francisco Giants",
    "Seattle Mariners", "St. Louis Cardinals", "Tampa Bay Rays",
    "Texas Rangers", "Toronto Blue Jays", "Washington Nationals",
]
# "LA Clippers" is the team's own branding, "Los Angeles Clippers" the
# formal name; both are listed since either may appear.
NBA_TEAMS = [
    "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets",
    "Charlotte Hornets", "Chicago Bulls", "Cleveland Cavaliers",
    "Dallas Mavericks", "Denver Nuggets", "Detroit Pistons",
    "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
    "LA Clippers", "Los Angeles Clippers", "Los Angeles Lakers",
    "Memphis Grizzlies", "Miami Heat", "Milwaukee Bucks",
    "Minnesota Timberwolves", "New Orleans Pelicans", "New York Knicks",
    "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers",
    "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings",
    "San Antonio Spurs", "Toronto Raptors", "Utah Jazz",
    "Washington Wizards",
]
# The Utah franchise was "Utah Hockey Club" before being renamed "Utah
# Mammoth", and Vegas is sometimes written "Las Vegas"; both forms of
# each are listed. Accents fold in _slugify, so "Montreal" also matches
# "Montreal" spelled with an accent.
NHL_TEAMS = [
    "Anaheim Ducks", "Boston Bruins", "Buffalo Sabres", "Calgary Flames",
    "Carolina Hurricanes", "Chicago Blackhawks", "Colorado Avalanche",
    "Columbus Blue Jackets", "Dallas Stars", "Detroit Red Wings",
    "Edmonton Oilers", "Florida Panthers", "Los Angeles Kings",
    "Minnesota Wild", "Montreal Canadiens", "Nashville Predators",
    "New Jersey Devils", "New York Islanders", "New York Rangers",
    "Ottawa Senators", "Philadelphia Flyers", "Pittsburgh Penguins",
    "San Jose Sharks", "Seattle Kraken", "St. Louis Blues",
    "Tampa Bay Lightning", "Toronto Maple Leafs", "Utah Mammoth",
    "Utah Hockey Club", "Vancouver Canucks", "Vegas Golden Knights",
    "Las Vegas Golden Knights", "Washington Capitals", "Winnipeg Jets",
]
# Concat kept so away-side aliasing and the settings overlay do not change.
MAJOR_LEAGUE_TEAMS = NFL_TEAMS + MLB_TEAMS + NBA_TEAMS + NHL_TEAMS
# Extends the list above rather than replacing it, so a typo in the
# environment can never blank out all four leagues.
EXTRA_ALIAS_TEAMS = [t.strip() for t in os.getenv(
    "EXTRA_ALIAS_TEAMS", "").split(",") if t.strip()]
# Sport ids the major-league list may match, so a mascot shared with an
# unrelated sport cannot hijack a channel. The upstream id is "hockey",
# not "ice-hockey".
MAJOR_LEAGUE_SPORTS = frozenset({
    "american-football", "baseball", "basketball", "hockey",
})

# ── Non-team channels ────────────────────────────────────────────────────
# Motorsports titles look like "IndyCar 2026 - Milwaukee Mile 250": the
# series prefix recurs forever while the event rotates, which is the same
# shape as a team. Seeded so the channel exists through the off-season.
SERIES_CHANNELS = [t.strip() for t in os.getenv(
    "SERIES_CHANNELS",
    "Formula 1,IndyCar,Nascar Cup Series,Nascar Truck Series,"
    "MotoGP,Moto2,Moto3,World Rally Championship").split(",") if t.strip()]
# Alternate spellings folded onto the canonical slug above.
SERIES_ALIASES = {
    "f1": "formula-1", "formula1": "formula-1", "formula-one": "formula-1",
    "nascar-cup": "nascar-cup-series", "wrc": "world-rally-championship",
    "motogp-2": "moto2", "motogp-3": "moto3",
}
# Always-on feeds. These are DISPLAY names - what the channel is called.
FEED_CHANNELS = [t.strip() for t in os.getenv(
    "FEED_CHANNELS",
    "Rally TV,Tennis Channel,Willow Cricket,Fox League,"
    "NFL RedZone,NFL Network").split(",") if t.strip()]
# Upstream title (lowercased) -> display name above. The site forces an
# "X vs Y" shape onto everything it publishes, so RedZone arrives titled
# "NFL vs RedZone" - a matchup, not a channel. Matching the title and
# naming the channel therefore have to be two separate things. The extra
# spellings are retitle insurance: feeds match on exact title equality, so
# without them an upstream rename silently blanks the channel with no error
# anywhere - it just goes to "No game scheduled" forever.
FEED_TITLE_ALIASES = {
    "nfl vs redzone": "NFL RedZone",
    "nfl redzone":    "NFL RedZone",
    "nfl red zone":   "NFL RedZone",
    "nfl vs red zone": "NFL RedZone",
}
# Display name slug -> the slug to actually use. A slug is a channel's
# permanent address: Dispatcharr binds its channel to the tvg-id built from
# it, and the sync script never deletes. So renaming a feed whose slug is
# already live would mint a NEW channel and leave the old one orphaned,
# showing "No game scheduled" until someone deletes it by hand. Pinning the
# slug here lets the display name change while the channel keeps its
# identity, guide link and number. Do not edit casually.
FEED_SLUG_OVERRIDES = {
    "nfl-redzone": "nfl-vs-redzone",
}
# Shared slots for true one-offs. Fixed count, so nothing accumulates.
POOL_SLOTS = int(os.getenv("POOL_SLOTS", "4"))
POOL_NAME  = os.getenv("POOL_NAME", "Live Event")

# EPG window. Backfill covers matches already in progress so they still
# appear in the guide rather than vanishing at first pitch.
EPG_WINDOW_HOURS      = int(os.getenv("EPG_WINDOW_HOURS", "24"))
EPG_BACKFILL_HOURS    = int(os.getenv("EPG_BACKFILL_HOURS", "6"))
# The API gives a start time but no duration or end time, so length has to
# be assumed per sport. Over-estimating is safer than under: a programme
# that ends early just shows stale, while one that ends too soon leaves a
# live game looking off-air in the guide.
EPG_DEFAULT_MINUTES   = int(os.getenv("EPG_DEFAULT_MINUTES", "180"))
# Group titles for the team playlist. Anything not listed is title-cased.
SPORT_DISPLAY = {
    "american-football": "American Football",
    "motor-sports": "Motor Sports",
    "afl": "AFL",
    "football": "Soccer",
}
FAVOURITES_GROUP = os.getenv("FAVOURITES_GROUP", "Favorites")
SPORT_DURATION_MIN = {
    "baseball": 210, "american-football": 210, "basketball": 150,
    "hockey": 150, "football": 120, "rugby": 120, "afl": 150,
    "fight": 240, "cricket": 300, "golf": 300, "tennis": 180,
    "motor-sports": 180, "darts": 180, "billiards": 180,
}
# Max in-memory extract cache entries; oldest evicted when over (0 = no cap)
EXTRACT_CACHE_MAX_ENTRIES = int(os.getenv("EXTRACT_CACHE_MAX_ENTRIES", "100"))
# Max segment URLs to keep per active stream (avoids unbounded RAM for long streams)
STREAM_SEEN_MAX = int(os.getenv("STREAM_SEEN_MAX", "300"))
# How long (seconds) with no new segments before a stream is considered dead
# and self-terminates. Guards against Dispatcharr holding connections open
# after the client has disconnected.
STREAM_IDLE_TIMEOUT = int(os.getenv("STREAM_IDLE_TIMEOUT", "45"))
# The proxy forwards each chunk as a burst and then has nothing to send until
# either the next chunk finishes downloading or the source publishes one - and
# it used to send exactly that: nothing. Dispatcharr's health monitor reads
# ~10s of silence as a stalled stream and tears the connection down, which
# costs a cold cascade and a black screen on a source that was perfectly
# healthy. MPEG-TS null packets (PID 0x1FFF) are spec-defined padding that
# every decoder discards, so emitting one per interval during those gaps keeps
# bytes on the wire without touching the picture. 0 disables.
STREAM_KEEPALIVE_INTERVAL = float(os.getenv("STREAM_KEEPALIVE_INTERVAL", "1"))
# After an operator disconnect, refuse new /stream requests for the same
# channel so Dispatcharr's automatic reconnect does not resume playback.
# Must outlast that reconnect window (observed ~6s; established-channel
# retry/switch runs about 21s). HEAD stays 200 so playlist validation is
# not broken. 0 disables the hold.
STREAM_DISCONNECT_HOLD = int(os.getenv("STREAM_DISCONNECT_HOLD", "90"))
# sync 0x47, PID 0x1FFF, adaptation_field_control=01 (payload only), cc=0.
# The continuity counter is undefined for null packets, so a constant is fine.
TS_NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184
# Segment fetch budget. Sources here ship unusually large chunks - a measured
# median of 6.3MB, up to 7.7MB - and the VPN delivers on the order of 4 Mbit/s,
# so a single healthy chunk needs ~13s. The old 10s ceiling was therefore
# *below* the time a good chunk takes, and fired constantly on healthy streams.
# When it fired the partially-downloaded chunk had already been forwarded, so
# the player received a truncated segment spliced onto the next one - which is
# what produced the glitching and apparent jumps backwards.
SEGMENT_TIMEOUT = int(os.getenv("SEGMENT_TIMEOUT", "30"))
# The startup probe downloads one whole chunk to prove a source is alive. Same
# problem, and worse consequences: a timeout here rejects a perfectly good
# stream as "dead" and burns a cascade attempt. Kept below SEGMENT_TIMEOUT so a
# slow source is abandoned at startup rather than mid-watch.
SEGMENT_PROBE_TIMEOUT = int(os.getenv("SEGMENT_PROBE_TIMEOUT", "20"))
# Extra attempts for a chunk that fails or arrives incomplete. Deliberately
# small: this is live video, and a chunk stuck retrying drags the stream
# further behind the live edge with every attempt.
SEGMENT_RETRIES = int(os.getenv("SEGMENT_RETRIES", "1"))
# Sanity ceiling on a buffered chunk. Chunks are now held in memory so they can
# be verified complete before any of it is forwarded; this stops a mislabelled
# or runaway response from eating the box's RAM.
SEGMENT_MAX_MB = int(os.getenv("SEGMENT_MAX_MB", "64"))
# Some sources publish a video elementary stream and no audio one at all -
# observed on `admin`, which carries a raw MLB.TV feed. Playback looks fine and
# is silent, and _rank_streams puts `admin` above `delta` (both HD, `admin`
# earlier in ALL_SOURCES), so channels land on the silent source by default.
# With this on, the startup probe rejects a source that carries no audio and the
# cascade falls through to one that does. Set to 0 to disable without a rebuild.
REQUIRE_AUDIO = os.getenv("REQUIRE_AUDIO", "1") == "1"
# How long a confirmed no-audio verdict is remembered, per embed URL. Without
# it every cold click re-downloads a whole chunk from the silent source and
# burns a browser extraction before moving on. A feed does not sprout an audio
# track mid-match, so this can be generous.
NO_AUDIO_TTL = int(os.getenv("NO_AUDIO_TTL", "1800"))

ALL_SOURCES = [
    "admin", "bravo", "charlie", "delta",
    "echo", "foxtrot", "golf", "hotel", "intel",
]

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

_cache_lock   = threading.Lock()
_cached_m3u   = ""
_last_refresh = None
_last_count   = 0

API_HEADERS = {
    "User-Agent": "streamed-m3u/2.0",
    "Accept":     "application/json",
}
IMAGE_HEADERS = {
    "User-Agent": "streamed-m3u/2.0",
}

# ─── Logo cache ───────────────────────────────────────────────────────────────
# streamed.pk's image host is only reachable through this container's VPN
# tunnel, not from Dispatcharr's network — Dispatcharr's own logo-cache view
# hits dead direct URLs with a 3s connect-timeout each, starving its worker
# pool. Instead we proxy+cache images here so Dispatcharr always hits a fast,
# local endpoint. Keyed by resolved image URL -> {content, content_type, ts, ttl, ok}
LOGO_CACHE_TTL          = int(os.getenv("LOGO_CACHE_TTL", "86400"))  # 24h for successful fetches
LOGO_CACHE_FAIL_TTL     = int(os.getenv("LOGO_CACHE_FAIL_TTL", "60"))  # short retry window for failures
LOGO_CACHE_MAX_ENTRIES  = int(os.getenv("LOGO_CACHE_MAX_ENTRIES", "500"))

# Console-written settings overlay the environment and defaults above. This
# runs before any lookup table is derived from these constants, so a value
# in /data/settings.json is what the rest of the module sees. LOG_LEVEL is
# re-applied because logging was configured before this point.
_settings.apply(globals(), _settings.load())
logging.getLogger().setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
_logo_cache: dict         = {}
_logo_cache_lock          = threading.Lock()

# ─── Extraction cache ─────────────────────────────────────────────────────────
# Prevents launching a new browser for every Dispatcharr retry of the same URL.
# Keyed by embed_url -> {"data": dict, "ts": float}
_extract_cache: dict       = {}
_extract_cache_lock        = threading.Lock()

# Per-URL lock so concurrent requests for the same URL queue behind
# one browser launch instead of each spawning their own.
_extract_url_locks: dict   = {}
_extract_url_locks_lock    = threading.Lock()

# Active stream tracking for /stream/status
_active_streams: dict      = {}   # stream_id -> metadata dict
_active_streams_lock       = threading.Lock()

# Operator disconnect holds: hold_key -> expiry (monotonic seconds).
# A console stop closes one generator; Dispatcharr then retries /stream
# on the same team URL and playback resumes unless we refuse that retry.
_disconnect_holds: dict    = {}
_disconnect_holds_lock     = threading.Lock()

# Team roster (permanent) and the current team -> match resolution.
_team_lock                 = threading.Lock()
_team_roster: dict         = {}   # slug -> {name, first_seen, last_seen}
_team_map:    dict         = {}   # slug -> current match + ranked streams

# Pre-warm bookkeeping: slug -> what we warmed and when.
_prewarm_lock              = threading.Lock()
_prewarm_state: dict       = {}

# slug -> every known fixture for that team (drives the EPG, whereas
# _team_map holds only the single fixture that is on right now).
_team_schedule: dict       = {}


def _get_url_lock(url: str) -> threading.Lock:
    with _extract_url_locks_lock:
        if url not in _extract_url_locks:
            _extract_url_locks[url] = threading.Lock()
        return _extract_url_locks[url]


def _evict_extract_cache():
    """Periodically remove expired entries from the extraction cache, and cap size."""
    while True:
        time.sleep(60)
        now = time.time()
        evicted = []
        with _extract_cache_lock:
            expired = [url for url, entry in _extract_cache.items()
                       if (now - entry["ts"]) >= EXTRACT_CACHE_TTL]
            for url in expired:
                del _extract_cache[url]
                evicted.append(url)
                log.info("Evicted extract cache entry (TTL): %s", url)
            # Cap in-memory entries: remove oldest by timestamp when over limit
            if EXTRACT_CACHE_MAX_ENTRIES > 0 and len(_extract_cache) > EXTRACT_CACHE_MAX_ENTRIES:
                by_ts = sorted(_extract_cache.items(), key=lambda x: x[1]["ts"])
                for url, _ in by_ts[: len(_extract_cache) - EXTRACT_CACHE_MAX_ENTRIES]:
                    del _extract_cache[url]
                    evicted.append(url)
                    log.info("Evicted extract cache entry (max entries): %s", url)
        if evicted:
            with _extract_url_locks_lock:
                for url in evicted:
                    _extract_url_locks.pop(url, None)
            _save_extract_cache()


def _load_extract_cache():
    """Load persisted extraction cache from disk on startup."""
    try:
        with open(EXTRACT_CACHE_FILE, "r") as f:
            data = json.load(f)
        now = time.time()
        valid = {url: entry for url, entry in data.items()
                 if (now - entry["ts"]) < EXTRACT_CACHE_TTL}
        if EXTRACT_CACHE_MAX_ENTRIES > 0 and len(valid) > EXTRACT_CACHE_MAX_ENTRIES:
            by_ts = sorted(valid.items(), key=lambda x: x[1]["ts"])
            valid = dict(by_ts[-EXTRACT_CACHE_MAX_ENTRIES:])
        with _extract_cache_lock:
            _extract_cache.update(valid)
        log.info("Loaded %d valid entries from persistent extract cache (%d expired)",
                 len(valid), len(data) - len(valid))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Could not load extract cache: %s", e)


def _save_extract_cache():
    """Persist current extraction cache to disk."""
    try:
        with _extract_cache_lock:
            snapshot = dict(_extract_cache)
        _settings.atomic_write_json(EXTRACT_CACHE_FILE, snapshot)
    except Exception as e:
        log.warning("Could not save extract cache: %s", e)


def _refresh_extract_entry(embed_url: str) -> str:
    """Force a new Chromium extract for one cached embed URL.

    Only operates on keys that are already in the cache, so the console
    cannot be used to point Chromium at an arbitrary address. A failed
    extract leaves the previous entry in place. Returns 'ok', 'missing'
    or 'failed'.
    """
    with _extract_cache_lock:
        if embed_url not in _extract_cache:
            return "missing"
    log.info("Console extract-cache refresh: %s", embed_url)
    data = extract_m3u8_via_browser(embed_url, force=True)
    if not data:
        return "failed"
    return "ok"


def _clear_extract_entry(embed_url: str) -> str:
    """Drop one extract-cache entry. Returns 'ok' or 'missing'."""
    with _extract_cache_lock:
        if embed_url not in _extract_cache:
            return "missing"
        _extract_cache.pop(embed_url, None)
    _save_extract_cache()
    log.info("Console extract-cache clear: %s", embed_url)
    return "ok"


# ─── Zombie / orphan cleanup ──────────────────────────────────────────────────

def kill_orphan_chromium():
    """Kill any leftover chromium processes from a previous container run."""
    try:
        result = subprocess.run(
            ["pkill", "-9", "-f", "chrome-headless-shell"],
            capture_output=True,
        )
        if result.returncode == 0:
            log.info("Killed orphaned chromium processes on startup")
    except Exception as e:
        log.warning("Could not kill orphan chromium: %s", e)
    # Reap any zombies we are already the parent of
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except ChildProcessError:
        pass


# ─── Playwright m3u8 extraction ───────────────────────────────────────────────

def extract_m3u8_via_browser(embed_url: str, force: bool = False) -> dict | None:
    """
    Returns a cached extraction result if still fresh, otherwise launches
    extract_stream.py in its own process group so the entire Chromium tree
    (parent + all children) can be killed cleanly on timeout.

    Concurrent requests for the same URL wait for the first browser to finish
    rather than each spawning their own instance. force=True skips the cache
    read so a console refresh actually launches Chromium again; a failed
    force leaves the previous entry in place.
    """
    now = time.time()

    # 1. Fast path: check cache without blocking
    if not force:
        with _extract_cache_lock:
            entry = _extract_cache.get(embed_url)
            if entry and (now - entry["ts"]) < EXTRACT_CACHE_TTL:
                log.info("Cache hit for: %s (age %.0fs)", embed_url, now - entry["ts"])
                return entry["data"]

    # 2. Acquire per-URL lock — only one browser per URL at a time
    url_lock = _get_url_lock(embed_url)
    with url_lock:
        # Re-check cache: another thread may have populated it while we waited
        if not force:
            now = time.time()
            with _extract_cache_lock:
                entry = _extract_cache.get(embed_url)
                if entry and (now - entry["ts"]) < EXTRACT_CACHE_TTL:
                    log.info("Cache hit (post-lock) for: %s", embed_url)
                    return entry["data"]

        # 3. Launch subprocess in its own process group so SIGKILL reaches
        #    Chromium and every child process it spawned
        log.info("Browser navigating to: %s%s",
                 embed_url, " (forced)" if force else "")
        proc = None
        try:
            proc = subprocess.Popen(
                ["python", "/app/extract_stream.py", embed_url, str(BROWSER_TIMEOUT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # new process group = clean kill
            )
            hard_timeout = BROWSER_TIMEOUT + 15
            try:
                stdout, stderr = proc.communicate(timeout=hard_timeout)
            except subprocess.TimeoutExpired:
                log.error("Browser timed out after %ds — killing process group", hard_timeout)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return None

            if proc.returncode == 0 and stdout.strip():
                data = json.loads(stdout.strip())
                log.info("Intercepted m3u8: %s", data.get("url"))
                with _extract_cache_lock:
                    _extract_cache[embed_url] = {"data": data, "ts": time.time()}
                _save_extract_cache()
                return data
            else:
                log.warning("No m3u8 for: %s (stderr: %s)",
                            embed_url, stderr.decode(errors="replace")[:200])
                return None

        except Exception as exc:
            log.error("Browser extraction failed for %s: %s", embed_url, exc)
            return None

        finally:
            # Always reap the process so it never becomes a zombie
            if proc is not None:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass


from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

# CDNs like modifiles.fans use open CORS (access-control-allow-origin: *)
# and token-in-URL auth — sending browser session cookies causes 403.
# Only send cookies to DDoS Guard protected CDNs (strmd.top etc).
COOKIELESS_CDN_PATTERNS = ["modifiles.fans"]

def _cookies_for_url(url, cookies):
    for pattern in COOKIELESS_CDN_PATTERNS:
        if pattern in url:
            return {}
    return cookies

def _headers_for_url(url, headers):
    """modifiles.fans needs Origin/Referer from pooembed.eu, not embedsports."""
    for pattern in COOKIELESS_CDN_PATTERNS:
        if pattern in url:
            return {
                "User-Agent": headers.get("User-Agent", ""),
                "Origin": "https://pooembed.eu",
                "Referer": "https://pooembed.eu/",
            }
    return headers

def resolve_url(base_url, line):
    """Resolve a relative or absolute URL against a base m3u8 URL."""
    parsed = urlparse(base_url)
    base_origin = f"{parsed.scheme}://{parsed.netloc}"
    if line.startswith("http"):
        return line
    elif line.startswith("/"):
        return base_origin + line
    else:
        return base_url.rsplit("/", 1)[0] + "/" + line

# Query params that authorise a segment rather than identify it. The CDN
# re-signs a segment between manifest polls - same .ts object, fresh
# X-Amz-Date/X-Amz-Signature - so a full-URL comparison sees the repeat as a
# new chunk and forwards the same 5 seconds twice. Measured at ~49% of
# segments on a delta source; the decoder shows it as a jump backwards, and
# the surplus content inflates the buffer until audio trails video badly.
# Deliberately a narrow list: stripping a param that *does* identify a
# segment would dedup away real content, which is worse than the duplicate.
SIGNING_PARAMS = {"token", "signature", "sig", "expires", "hmac", "md5", "hash"}

def _segment_key(url):
    """Identity of a segment, ignoring the credentials that authorise it."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not (k.lower().startswith("x-amz-")
                    or k.lower() in SIGNING_PARAMS)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), ""))

# Status codes that mean the URL is permanently dead - no point retrying
# with the same cached m3u8. Invalidate cache and surface a 502.
FATAL_STATUS_CODES = {400, 401, 403, 404, 410, 451}

def get_segments(m3u8_url, headers, cookies, _depth=0):
    """
    Fetch m3u8 manifest and return (segments, status_code).
    Handles master playlists (variant streams) and media playlists that
    reference sub-playlists (e.g. tracks-v1a1/mono.ts.m3u8) by recursing
    into them until actual .ts segments are found.
    """
    if _depth > 3:
        return None, 0  # Guard against infinite recursion
    try:
        r = cf_requests.get(m3u8_url,
                            headers=_headers_for_url(m3u8_url, headers),
                            cookies=_cookies_for_url(m3u8_url, cookies),
                            timeout=15, impersonate="chrome")
    except Exception as exc:
        # Treat connection-level errors (DNS failure, refused, TLS) as
        # transient - the CDN host itself is unreachable, retrying won't help.
        log.warning("Connection error fetching m3u8 (%s): %s", exc, m3u8_url)
        return None, 503
    if r.status_code != 200:
        log.warning("m3u8 fetch returned HTTP %d: %s", r.status_code, m3u8_url)
        return None, r.status_code

    lines = r.text.splitlines()
    segments = []
    sub_playlists = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url = resolve_url(m3u8_url, line)
        if ".m3u8" in url.split("?")[0]:
            sub_playlists.append(url)
        else:
            segments.append(url)

    if sub_playlists and not segments:
        return get_segments(sub_playlists[0], headers, cookies, _depth + 1)

    return segments, 200


# MPEG-TS stream_type values that denote an audio elementary stream.
AUDIO_STREAM_TYPES = {
    0x03,  # MPEG-1 audio
    0x04,  # MPEG-2 audio
    0x0f,  # AAC (ADTS)
    0x11,  # AAC (LATM)
    0x1c,  # LPCM
    0x81,  # AC-3
    0x87,  # E-AC-3
}

# embed_url -> timestamp of a confirmed "carries no audio" probe.
_no_audio_cache = {}
_no_audio_lock = threading.Lock()


def _ts_sync_offset(body, packets=10, limit=4096):
    """Byte offset where 188-byte TS packet alignment begins, or None.

    Not assumed to be 0: some sources prepend a fake 42-byte WebP header
    (gotcha #17), and the wrapper length is detected rather than assumed so a
    source that is already clean is not mangled.
    """
    for i in range(min(limit, len(body))):
        if all(i + n * 188 < len(body) and body[i + n * 188] == 0x47
               for n in range(packets)):
            return i
    return None


def _strip_ts_prefix(body):
    """Return (clean_body, bytes_removed) with any leading non-TS wrapper gone.

    Some sources prepend a fake 42-byte WebP header to every chunk (gotcha
    #17). Forwarded as-is, each chunk boundary hands the decoder 42 bytes of
    garbage and it has to resync - visible as slice-header errors, dropped
    frames and stutter in anything that decodes the stream (Jellyfin's
    transcoder most of all). The wrapper length is *detected* with
    _ts_sync_offset, never assumed, so a source that is already clean passes
    through untouched. Fails open: if no alignment can be found at all, the
    body is returned unchanged rather than risk dropping real data.
    """
    off = _ts_sync_offset(body)
    if not off:            # 0 (already aligned) or None (undecidable)
        return body, 0
    return body[off:], off


def _has_audio_track(body):
    """True / False / None for 'this segment declares an audio stream'.

    None means undecidable - no TS alignment, or no PMT in this chunk. Callers
    must treat None as a pass: rejecting a source we merely failed to parse
    would burn a cascade attempt on a stream that plays perfectly well.
    """
    off = _ts_sync_offset(body)
    if off is None:
        return None

    pmt_pids = set()
    saw_pmt = False
    i = off
    while i + 188 <= len(body):
        if body[i] != 0x47:
            i += 1
            continue
        pid = ((body[i + 1] & 0x1f) << 8) | body[i + 2]
        pusi = body[i + 1] & 0x40
        afc = (body[i + 3] >> 4) & 3
        p = i + 4
        if afc in (2, 3):
            p += 1 + body[i + 4]
        if afc not in (1, 3) or p >= i + 188:
            i += 188
            continue
        if pusi:
            p += 1 + body[p]

        if pid == 0 and p + 12 <= i + 188:
            slen = ((body[p + 1] & 0x0f) << 8) | body[p + 2]
            for q in range(p + 8, min(p + 5 + slen - 4, i + 188) - 3, 4):
                pmt_pids.add(((body[q + 2] & 0x1f) << 8) | body[q + 3])

        elif pid in pmt_pids and p + 12 <= i + 188:
            saw_pmt = True
            slen = ((body[p + 1] & 0x0f) << 8) | body[p + 2]
            ilen = ((body[p + 10] & 0x0f) << 8) | body[p + 11]
            q = p + 12 + ilen
            end = min(p + 3 + slen - 4, i + 188)
            while q + 4 <= end:
                if body[q] in AUDIO_STREAM_TYPES:
                    return True
                q += 5 + (((body[q + 3] & 0x0f) << 8) | body[q + 4])
        i += 188

    return False if saw_pmt else None


def _known_silent(embed_url):
    """True if this embed was recently confirmed to carry no audio."""
    with _no_audio_lock:
        ts = _no_audio_cache.get(embed_url)
        if ts is None:
            return False
        if time.time() - ts >= NO_AUDIO_TTL:
            _no_audio_cache.pop(embed_url, None)
            return False
        return True


def _mark_silent(embed_url):
    with _no_audio_lock:
        _no_audio_cache[embed_url] = time.time()


def _validate_candidate(embed_url: str, label: str = ""):
    """Extract, then verify BOTH the manifest and one segment.

    A manifest can return 200 while its segments are already 404 - the state
    right after a match ends - so checking only the manifest lets a dead stream
    through, and the caller then commits a response that never yields a byte.

    Returns (True, payload, detail) or (False, None, detail), where payload is
    (data, headers, cookies, segments, primed). Shared by the click path and
    the pre-warm loop so a warmed stream is validated exactly as a real
    request would validate it.
    """
    tag = label or embed_url

    d = extract_m3u8_via_browser(embed_url)
    if not d:
        log.warning("No m3u8 extracted for %s", embed_url)
        return False, None, "%s:no-extract" % tag

    log.info("Extraction OK for %s (browser_m3u8_status=%s)",
             embed_url, d.get("browser_m3u8_status"))

    h = dict(d.get("headers", {}))
    h["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )
    c = d.get("cookies", {})

    segs, status = get_segments(d["url"], h, c)
    if segs is None:
        if status in FATAL_STATUS_CODES:
            # Dead/expired token (the player often hands us a stale LB URL).
            # Evict so a later request re-extracts a fresh live manifest.
            log.warning("Probe failed with fatal HTTP %d for %s "
                        "(browser saw %s) - evicting cache",
                        status, embed_url, d.get("browser_m3u8_status"))
            with _extract_cache_lock:
                _extract_cache.pop(embed_url, None)
            _save_extract_cache()
        return False, None, "%s:probe-%s" % (tag, status)

    if not segs:
        log.warning("Manifest listed no segments: %s", embed_url)
        return False, None, "%s:no-segments" % tag

    first_url = segs[0]
    try:
        fs = cf_requests.get(first_url,
                             headers=_headers_for_url(first_url, h),
                             cookies=_cookies_for_url(first_url, c),
                             timeout=SEGMENT_PROBE_TIMEOUT, impersonate="chrome")
        first_status, first_body = fs.status_code, fs.content
    except Exception as exc:
        # Note this is usually a timeout on a *working* source that is merely
        # slow, not a dead one - the message below deliberately says so.
        log.warning("Segment probe error for %s (timeout=%ds): %s",
                    first_url, SEGMENT_PROBE_TIMEOUT, exc)
        first_status, first_body = 0, b""

    if first_status != 200 or not first_body:
        log.warning("Segment probe failed HTTP %s (%d bytes) for %s - manifest "
                    "was OK but the stream is dead",
                    first_status, len(first_body), embed_url)
        if first_status in FATAL_STATUS_CODES:
            with _extract_cache_lock:
                _extract_cache.pop(embed_url, None)
            _save_extract_cache()
        return False, None, "%s:segment-%s" % (tag, first_status)

    # The chunk is already in hand, so proving it carries audio costs nothing
    # extra. The payload is returned even on rejection so the cascade can hold
    # this candidate aside - silent video is a poor outcome, but a far better
    # one than a channel that will not start at all.
    audio = _has_audio_track(first_body)
    payload = (d, h, c, segs, {first_url: first_body})
    if audio is False:
        _mark_silent(embed_url)
        if REQUIRE_AUDIO:
            log.warning("Segment probe OK (%d bytes) but NO AUDIO TRACK for %s "
                        "- deprioritising", len(first_body), embed_url)
            return False, payload, "%s:no-audio" % tag

    log.info("Segment probe OK (%d bytes, audio=%s) for %s",
             len(first_body), audio, embed_url)
    return True, payload, "%s:ok" % tag


# ─── API helpers ──────────────────────────────────────────────────────────────

def _get(path: str):
    url = f"{BASE_URL}{path}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=API_HEADERS)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("API error [%s]: %s", url, exc)
        return None


# ─── Sports & Matches ─────────────────────────────────────────────────────────

def fetch_sports() -> list[dict]:
    sports = _get("/api/sports") or []
    log.info("Fetched %d sport categories", len(sports))
    return sports


def fetch_all_matches(sports: list[dict]) -> list[dict]:
    seen:    set[str]   = set()
    matches: list[dict] = []

    def _add(m: dict, category: str):
        mid = m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            m["_category"] = category
            matches.append(m)

    for m in (_get("/api/matches/live") or []):
        _add(m, "Live")

    for sport in sports:
        sport_id   = sport.get("id", "")
        sport_name = sport.get("name", sport_id.title())
        if not sport_id:
            continue
        for m in (_get(f"/api/matches/{sport_id}") or []):
            _add(m, sport_name)

    log.info("Collected %d unique matches", len(matches))
    return matches


def fetch_streams_for_match(match: dict) -> list[dict]:
    source_map: dict[str, str] = {}
    for entry in (match.get("sources") or []):
        src = entry.get("source", "").lower()
        sid = entry.get("id", "")
        if src and sid and src not in source_map:
            source_map[src] = sid

    all_streams = []
    for src_name in ALL_SOURCES:
        src_id = source_map.get(src_name)
        if not src_id:
            continue
        streams = _get(f"/api/stream/{src_name}/{src_id}")
        if not streams or not isinstance(streams, list):
            continue
        for s in streams:
            s["_match_title"]    = match.get("title", "Unknown Match")
            s["_match_id"]       = match.get("id", "")
            s["_match_category"] = match.get("_category", "Sports")
            s["_match_poster"]   = match.get("poster", "")
        all_streams.extend(streams)

    return all_streams


# ─── M3U builder ──────────────────────────────────────────────────────────────

SOURCE_ABBREV = {
    "admin":   "A",
    "alpha":   "AL",
    "bravo":   "B",
    "charlie": "C",
    "delta":   "D",
    "echo":    "E",
    "foxtrot": "F",
    "golf":    "G",
    "hotel":   "H",
    "intel":   "I",
}


def format_channel_name(title: str, source: str, stream_no: int, hd: bool) -> str:
    abbrev  = SOURCE_ABBREV.get(source.lower(), source[:1].upper())
    quality = "HD" if hd else "SD"
    return f"[{abbrev}{stream_no}-{quality}] {title}"


# --- Team index --------------------------------------------------------------
#
# Groundwork for stable per-team channels. The roster is permanent and never
# shrinks: a slug is meant to become the fixed address for that team's channel,
# so it has to stay valid even when the team is not playing. The map is
# transient and rebuilt each refresh cycle from data already fetched for the
# playlist, so maintaining it costs no extra API calls.

# Characters ASCII-folding would silently drop rather than transliterate.
_SLUG_CHARS = {
    "\u00f8": "o", "\u00e6": "ae", "\u00e5": "a", "\u00df": "ss",
    "\u0111": "d", "\u00f0": "d", "\u0142": "l", "\u00fe": "th",
}


def _slugify(name: str) -> str:
    """Stable, URL-safe key for a team name.

    This becomes the permanent address for a team's channel, so the output
    must not drift between releases.
    """
    s = (name or "").strip().lower()
    for ch, repl in _SLUG_CHARS.items():
        s = s.replace(ch, repl)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _feed_slug(display: str) -> str:
    """Permanent slug for a feed's display name.

    Every place that derives a feed slug must go through here, or they will
    disagree and the channel will silently split in two. Callers: _feed_of,
    _seed_nonteam, _seed_favourites, _PREWARM_SLUGS and prewarm_loop's favs
    list. Defined here rather than beside _feed_of because _PREWARM_SLUGS
    below needs it at import time.
    """
    s = _slugify(display)
    return FEED_SLUG_OVERRIDES.get(s, s)


# Slug forms of the two team lists, resolved once at import. update_team_index
# consults these on every match of every refresh, so recomputing them per
# cycle would be pure waste. Kept separate on purpose - see PREWARM_TEAMS.
#
# Pre-warm slugs go through _feed_slug because the list may name a feed, and
# a feed's slug can be pinned to something other than its display name. With
# a plain _slugify, "NFL RedZone" would resolve to nfl-redzone, never match
# the pinned nfl-vs-redzone in _team_map, and report "not playing" forever.
# It is a no-op for team names - no team slug is in FEED_SLUG_OVERRIDES.
_PREWARM_SLUGS = frozenset(
    s for s in (_feed_slug(t) for t in PREWARM_TEAMS) if s)
_ALIAS_SLUGS = frozenset(
    s for s in (_slugify(t) for t in MAJOR_LEAGUE_TEAMS + EXTRA_ALIAS_TEAMS)
    if s)
# Per-league slug sets for console grouping. Membership is slug-in-list
# AND sport in MAJOR_LEAGUE_SPORTS (gotcha #10), so leftover
# american-football is NCAA/CFL, not NFL.
_LEAGUE_SLUGS = {
    "mlb": frozenset(s for s in (_slugify(t) for t in MLB_TEAMS) if s),
    "nfl": frozenset(s for s in (_slugify(t) for t in NFL_TEAMS) if s),
    "nba": frozenset(s for s in (_slugify(t) for t in NBA_TEAMS) if s),
    "nhl": frozenset(s for s in (_slugify(t) for t in NHL_TEAMS) if s),
}
_LEAGUE_ORDER = ("mlb", "nfl", "nhl", "nba")
_KIND_GROUPS = (
    ("kind:feed", "feed", "Stations"),
    ("kind:series", "series", "Racing"),
    ("kind:pool", "pool", "Events"),
)


_SERIES_RE = re.compile(r"^(.+?)\s+(?:19|20)\d{2}\s*[-\u2013]\s*(.+)$")


def _series_of(title: str):
    """(slug, display, event) for a motorsports-style title, else (None,)*3.

    Splits "IndyCar 2026 - Milwaukee Mile 250" into the recurring series and
    this week's event. Only the year-bearing form is matched: looser splitting
    on any dash would wrongly carve up one-off titles.
    """
    m = _SERIES_RE.match((title or "").strip())
    if not m:
        return None, None, None
    display = m.group(1).strip()
    slug = _slugify(display)
    slug = SERIES_ALIASES.get(slug, slug)
    return (slug or None), display, m.group(2).strip()


def _tokens(s: str) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def _has_token_run(haystack: str, slug: str) -> bool:
    """True if slug's tokens appear as a contiguous run in haystack.

    Token-wise, never substring: the alias "f1" must not match a hex blob
    that happens to contain "f1", and "formula-1" has to match the tokens
    [formula, 1] inside race-circuit-de-espana-3085-kms-formula-1-2586.
    """
    hay, need = _tokens(haystack), [t for t in slug.split("-") if t]
    if not need or len(need) > len(hay):
        return False
    return any(hay[i:i + len(need)] == need
               for i in range(len(hay) - len(need) + 1))


# Canonical series slug -> display name, plus every token run that maps to it.
_SERIES_BY_SLUG = {
    SERIES_ALIASES.get(_slugify(n), _slugify(n)): n for n in SERIES_CHANNELS
}
_SERIES_PATTERNS = (
    [(SERIES_ALIASES.get(_slugify(n), _slugify(n)), _slugify(n))
     for n in SERIES_CHANNELS]
    + [(canon, alias) for alias, canon in SERIES_ALIASES.items()]
)


def _series_from_match(match: dict):
    """(slug, display) if a match's id or title names a configured series.

    The site publishes the same race under several shapes, and only one of
    them has the "Series YYYY - Event" form _series_of can read:

        Formula 1 2026 - Spain GP              <- _series_of handles this
        Race | Circuit De Espana | 308.5 Kms   <- series is only in the id
        San Marino Grand Prix Moto2            <- series is a title suffix
        2026 NASCAR Cup Series Playoff at ...  <- year first, regex misses it

    The last three all carry the series as a token run in the id or title,
    which is what this reads. Restricted to motor-sports: every configured
    series is a motorsport, and the gate removes any chance of a stray token
    in some other sport's id hijacking a series channel.
    """
    if (match.get("category") or "") != "motor-sports":
        return None, None
    mid, title = str(match.get("id") or ""), str(match.get("title") or "")
    for canon, pattern in _SERIES_PATTERNS:
        if _has_token_run(mid, pattern) or _has_token_run(title, pattern):
            return canon, _SERIES_BY_SLUG.get(canon,
                                              canon.replace("-", " ").title())
    return None, None


def _merge_streams(stream_lists) -> list:
    """One best-first stream list from several listings of the same event.

    The site splits a race across separate entries with different sources
    (the Spanish GP arrived as three listings carrying 3, 1 and 20 streams).
    Pooling them gives the cascade every source to fall through instead of
    whichever single listing happened to win, so a dead source costs one
    retry rather than the channel.
    """
    merged, seen = [], set()
    for lst in stream_lists:
        for s in lst or []:
            url = s.get("embed_url")
            if url and url not in seen:
                seen.add(url)
                merged.append(s)
    merged.sort(key=lambda s: (
        0 if s.get("hd") else 1,
        ALL_SOURCES.index(s.get("source"))
        if s.get("source") in ALL_SOURCES else len(ALL_SOURCES),
        s.get("stream_no", 1),
    ))
    return merged


def _feed_of(title: str):
    """(slug, display) if this title is a configured always-on feed.

    Aliases are consulted first so an upstream title that does not look like
    the channel's name ("NFL vs RedZone" -> "NFL RedZone") still resolves.
    """
    t = (title or "").strip().lower()
    display = FEED_TITLE_ALIASES.get(t)
    if display is None:
        for f in FEED_CHANNELS:
            if t == f.strip().lower():
                display = f.strip()
                break
    if display is None:
        return None, None
    return _feed_slug(display), display


def _channel_id(slug: str, entry: dict) -> str:
    """Stable channel id. Teams keep their existing prefix untouched."""
    kind = (entry or {}).get("kind", "team")
    return ("streamed.team.%s" if kind == "team" else "streamed.feed.%s") % slug


def _rank_streams(streams: list[dict]) -> list[dict]:
    """Best-first: HD before SD, then source reliability, then stream number."""
    def key(s):
        src = (s.get("source") or "").lower()
        return (
            0 if s.get("hd") else 1,
            ALL_SOURCES.index(src) if src in ALL_SOURCES else len(ALL_SOURCES),
            s.get("streamNo", 1),
        )
    return sorted(streams, key=key)


def _install_seed_roster():
    """Copy the bundled roster (and lineup) into place on a fresh /data.

    The seed is a scrubbed snapshot: team entries only, no favourites, no
    feed/series/pool channels (those are recreated from config). Without it a
    new deployment starts at roughly twenty channels and fills in over days as
    fixtures are listed. An existing roster is never touched.

    The majors-only seed lineup is copied only in this same first-boot path.
    An existing /data with no lineup.json stays implicit-all: every slug is
    visible until someone presses −.
    """
    teams_existed = os.path.exists(TEAMS_FILE)
    if teams_existed or not os.path.exists(SEED_TEAMS_FILE):
        return
    try:
        os.makedirs(os.path.dirname(TEAMS_FILE), exist_ok=True)
        shutil.copyfile(SEED_TEAMS_FILE, TEAMS_FILE)
        with open(TEAMS_FILE, encoding="utf-8") as f:
            count = len(json.load(f))
        log.info("Installed seed roster: %d teams -> %s", count, TEAMS_FILE)
    except Exception as e:
        log.warning("Could not install seed roster: %s", e)
        return
    lineup.install_seed(LINEUP_FILE, SEED_LINEUP_FILE, teams_existed=False)


def _league_of(slug: str, sport: str, kind: str = "team"):
    """mlb/nfl/nba/nhl or None. Sport-gated so leftover football is not NFL."""
    if (kind or "team") != "team":
        return None
    if (sport or "") not in MAJOR_LEAGUE_SPORTS:
        return None
    for league in _LEAGUE_ORDER:
        if slug in _LEAGUE_SLUGS[league]:
            return league
    return None


def _entry_view(slug: str, entry: dict, doc=None) -> dict:
    """kind, sport, league, in_lineup for a roster row. Does not mutate entry."""
    if doc is None:
        doc, _ = lineup.current()
    kind = entry.get("kind", "team")
    sport = entry.get("sport") or ""
    return {
        "kind": kind,
        "sport": sport,
        "league": _league_of(slug, sport, kind),
        "in_lineup": lineup.in_lineup(doc, slug),
    }


def _group_id_of(kind: str, sport: str, league):
    if kind in ("feed", "series", "pool"):
        return "kind:" + kind
    if league:
        return league
    return "sport:" + (sport or "unknown")


def _slugs_in_group(roster: dict, group: str, doc=None) -> list:
    """Roster slugs that belong to a console group id."""
    if doc is None:
        doc, _ = lineup.current()
    out = []
    for slug, entry in roster.items():
        view = _entry_view(slug, entry, doc)
        if _group_id_of(view["kind"], view["sport"], view["league"]) == group:
            out.append(slug)
    return out


def _lineup_counts(roster=None):
    doc, implicit = lineup.current()
    if roster is None:
        with _team_lock:
            roster = dict(_team_roster)
    n_in = sum(1 for s in roster if lineup.in_lineup(doc, s))
    return {
        "policy": doc["policy"],
        "implicit": implicit,
        "in_lineup": n_in,
        "out_of_lineup": len(roster) - n_in,
        "roster": len(roster),
    }


def _lineup_snapshot():
    """GET /api/lineup payload: policy, counts, group sizes."""
    doc, implicit = lineup.current()
    with _team_lock:
        roster = dict(_team_roster)
    counts = _lineup_counts(roster)
    groups = []
    labels = {"mlb": "MLB", "nfl": "NFL", "nhl": "NHL", "nba": "NBA"}
    for league in _LEAGUE_ORDER:
        slugs = _slugs_in_group(roster, league, doc)
        groups.append({
            "id": league,
            "label": labels[league],
            "size": len(slugs),
            "in_lineup": lineup.count_in(doc, slugs),
        })
    for gid, _kind, label in _KIND_GROUPS:
        slugs = _slugs_in_group(roster, gid, doc)
        groups.append({
            "id": gid,
            "label": label,
            "size": len(slugs),
            "in_lineup": lineup.count_in(doc, slugs),
        })
    leftover = {}
    for slug, entry in roster.items():
        view = _entry_view(slug, entry, doc)
        gid = _group_id_of(view["kind"], view["sport"], view["league"])
        if gid.startswith("sport:"):
            leftover.setdefault(gid, []).append(slug)
    for gid in sorted(leftover, key=lambda g: _sport_display(g.split(":", 1)[1])):
        slugs = leftover[gid]
        sport = gid.split(":", 1)[1]
        groups.append({
            "id": gid,
            "label": _sport_display(sport),
            "size": len(slugs),
            "in_lineup": lineup.count_in(doc, slugs),
        })
    return {
        "policy": counts["policy"],
        "implicit": implicit,
        "in_lineup": counts["in_lineup"],
        "out_of_lineup": counts["out_of_lineup"],
        "roster": counts["roster"],
        "groups": groups,
    }


def _lineup_apply(payload: dict):
    """Apply a console add/remove. Returns (snapshot, error, status)."""
    if not isinstance(payload, dict):
        return None, "expected_json", 400
    op = payload.get("op")
    if op not in ("add", "remove"):
        return None, "op must be add or remove", 400
    slugs = payload.get("slugs")
    group = payload.get("group")
    if slugs is not None and group is not None:
        return None, "pass slugs or group, not both", 400
    if group is not None:
        if not isinstance(group, str) or not group.strip():
            return None, "invalid group", 400
        group = group.strip()
        known = set(_LEAGUE_ORDER)
        known.update(g[0] for g in _KIND_GROUPS)
        if group not in known and not group.startswith("sport:"):
            return None, "unknown group", 400
        with _team_lock:
            roster = dict(_team_roster)
        slugs = _slugs_in_group(roster, group, lineup.current()[0])
        if not slugs:
            return None, "empty group", 400
    elif isinstance(slugs, str):
        slugs = [slugs]
    elif not isinstance(slugs, (list, tuple)):
        return None, "expected slugs or group", 400
    try:
        doc, _implicit = lineup.current()
        new = lineup.apply_op(doc, op, slugs)
        lineup.persist(new, LINEUP_FILE)
    except ValueError as e:
        return None, str(e), 400
    except OSError as e:
        return None, "save_failed: %s" % e, 500
    return _lineup_snapshot(), None, 200


def _load_team_roster():
    """Load the permanent team roster from disk on startup.

    A corrupt file is moved aside and the previous save (.bak) is used; if
    that is unusable too the bundled seed is installed. Starting empty is the
    last resort: the roster only ever grows, and every channel that was ever
    created would come back under a new number.
    """
    data, err = _settings.read_json_with_fallback(TEAMS_FILE, "team roster")
    if data is None:
        if not os.path.exists(TEAMS_FILE):
            _install_seed_roster()
            data, _ = _settings.read_json_with_fallback(TEAMS_FILE, "seed roster")
        if data is None:
            log.info("No team roster on disk yet - starting empty")
            return
    if not isinstance(data, dict):
        log.error("Team roster is not a JSON object - starting empty")
        return
    with _team_lock:
        _team_roster.update(data)
    log.info("Loaded %d teams from persistent roster%s",
             len(data), " (recovered from backup)" if err else "")


def _seed_favourites():
    """Give favourite teams a roster entry before they are ever observed.

    Without this a team has no channel until their season starts - the 76ers
    and Flyers simply would not exist in the guide until October, which
    defeats the point of a permanent per-team channel. The stored index also
    fixes their display order, so favourites lead the channel list.

    Non-team entries in PREWARM_TEAMS are skipped entirely. The favourite
    flag wins the group-title race in build_team_m3u, so flagging a feed
    would drag it out of "Live Feeds" and into "Favorites" - and nothing in
    this file ever clears the flag, so that is a one-way door short of
    hand-editing teams.json. _seed_nonteam creates those entries properly a
    moment later, and pre-warming reads _team_map, not the roster, so
    skipping here costs nothing.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Membership, not entry["kind"]: this runs BEFORE _seed_nonteam, so on a
    # fresh install the feed's entry does not exist yet and a kind check
    # would miss it.
    nonteam = {_feed_slug(n) for n in FEED_CHANNELS}
    nonteam |= {SERIES_ALIASES.get(_slugify(n), _slugify(n))
                for n in SERIES_CHANNELS}
    nonteam |= {_slugify("%s %d" % (POOL_NAME, i + 1))
                for i in range(POOL_SLOTS)}
    seeded = 0
    skipped = 0
    with _team_lock:
        for name in PREWARM_TEAMS:
            slug = _feed_slug(name)
            if not slug:
                continue
            if slug in nonteam:
                skipped += 1
                continue
            entry = _team_roster.get(slug)
            if entry is None:
                _team_roster[slug] = {"name": name, "first_seen": now_iso,
                                      "last_seen": now_iso, "favourite": True}
                seeded += 1
            else:
                entry["favourite"] = True       # marker only, not ordering
    _save_team_roster()
    log.info("Favourites seeded: %d new, %d total tracked (%d non-team skipped)",
             seeded, len(PREWARM_TEAMS) - skipped, skipped)


def _seed_nonteam():
    """Give series, feeds and pool slots a roster entry up front.

    Same reasoning as seeding favourite teams: Formula 1 has no race listed
    during the summer break, but the channel should still exist so it keeps
    its place and picks races up automatically when the season resumes.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seeded = 0
    for name in SERIES_CHANNELS:
        slug = SERIES_ALIASES.get(_slugify(name), _slugify(name))
        if slug and _roster_touch(slug, name, "", "motor-sports", now_iso,
                                  kind="series"):
            seeded += 1
    for name in FEED_CHANNELS:
        slug = _feed_slug(name)
        if slug and _roster_touch(slug, name, "", "", now_iso, kind="feed"):
            seeded += 1
    for i in range(POOL_SLOTS):
        name = "%s %d" % (POOL_NAME, i + 1)
        if _roster_touch(_slugify(name), name, "", "", now_iso, kind="pool"):
            seeded += 1
    _save_team_roster()
    log.info("Non-team channels seeded: %d new (%d series, %d feeds, %d pool)",
             seeded, len(SERIES_CHANNELS), len(FEED_CHANNELS), POOL_SLOTS)


def _save_team_roster():
    try:
        with _team_lock:
            snapshot = dict(_team_roster)
        _settings.atomic_write_json(TEAMS_FILE, snapshot, indent=1, sort_keys=True)
    except Exception as e:
        log.warning("Could not save team roster: %s", e)


def _match_score(c: dict):
    """Rank competing entries for the same team slug. Higher is better.

    Needed because a team can be listed more than once - the same fixture may
    appear under both "Live" and its sport, and yesterday's game can still be
    listed alongside tonight's. Preferring the one actually on now avoids
    warming or serving a stale fixture.
    """
    started = c.get("starts")
    if started:
        age_min = (time.time() * 1000 - started) / 60000.0
        in_window = -PREWARM_WINDOW_BEFORE <= age_min <= PREWARM_WINDOW_AFTER
        closeness = -abs(age_min)
    else:
        in_window, closeness = False, -99999.0
    return (in_window, bool(c.get("streams")), c.get("category") == "Live",
            closeness)


def _roster_touch(slug: str, name: str, badge: str, sport: str,
                  now_iso: str, kind: str = "team") -> bool:
    """Record or refresh a team. Returns True if this team is new.

    Badge and sport are stored on the roster rather than derived per request,
    so a team keeps its logo and group even when it has no fixture listed -
    which is the whole point of a channel that never goes away.
    """
    with _team_lock:
        entry = _team_roster.get(slug)
        if entry is None:
            _team_roster[slug] = {
                "name": name, "first_seen": now_iso, "last_seen": now_iso,
                "badge": badge, "sport": sport, "kind": kind,
            }
            return True
        entry["last_seen"] = now_iso
        entry["name"] = name              # keep the freshest spelling
        entry.setdefault("kind", kind)
        if badge:
            entry["badge"] = badge
        if sport:
            entry["sport"] = sport
        return False


def _nonteam_candidate(match: dict, streams, kind: str, name: str,
                       event_title: str) -> dict:
    """A map entry for content that has no home/away teams."""
    ranked = _rank_streams(streams or [])
    return {
        "team":        name,
        "match_id":    match.get("id", ""),
        "match_title": event_title or match.get("title", ""),
        "away":        "",
        "category":    match.get("_category", "Sports"),
        "sport":       match.get("category", ""),
        "poster":      match.get("poster", ""),
        "starts":      match.get("date"),
        "kind":        kind,
        "side":        "",
        "streams": [
            {"embed_url": (x.get("embedUrl") or "").strip(),
             "source":    x.get("source", ""),
             "stream_no": x.get("streamNo", 1),
             "hd":        bool(x.get("hd"))}
            for x in ranked if (x.get("embedUrl") or "").strip()
        ],
    }


def _sched_add(sched: dict, slug: str, candidate: dict):
    """Record a fixture against a team for the guide.

    Unlike _offer this keeps every fixture, not just the current one - the
    guide needs tomorrow's game as much as tonight's. Duplicate listings of
    the same match (the API lists some under both "Live" and their sport) are
    collapsed by match id.
    """
    if not candidate.get("starts"):
        return
    rows = sched.setdefault(slug, [])
    for r in rows:
        if r["match_id"] == candidate.get("match_id"):
            return
    rows.append({
        "match_id": candidate.get("match_id", ""),
        "title":    candidate.get("match_title", ""),
        "starts":   candidate.get("starts"),
        "sport":    candidate.get("sport", ""),
        "category": candidate.get("category", "Sports"),
        "side":     candidate.get("side", ""),
    })


def _offer(target: dict, slug: str, candidate: dict):
    """Keep the better of the existing and proposed entry for this slug."""
    prev = target.get(slug)
    if prev is None or _match_score(candidate) > _match_score(prev):
        target[slug] = candidate


def update_team_index(matches: list[dict], streams_per_match: list[list[dict]]):
    """Refresh the team roster and the team -> current match map.

    Called from build_m3u with data already in hand. Has no effect on
    playlist output.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_map: dict = {}
    new_sched: dict = {}
    added = 0

    pool_raw: list = []
    # Series entries are collected rather than offered one at a time: the
    # site lists a single session several times over, so they are grouped by
    # series and merged once every match has been seen (see below).
    series_raw: dict = {}
    # Entries that look like an event but name no series. Held back for the
    # start-time correlation pass, which is the only non-guessing way to tie
    # a bare "Spanish Grand Prix" to F1 - MotoGP and F2 run one too.
    deferred: list = []
    # (sport, start) -> set of series slugs starting at that instant.
    starts_index: dict = {}

    for match, streams in zip(matches, streams_per_match):
        teams_obj = match.get("teams") or {}
        home = ((teams_obj.get("home") or {}).get("name") or "")
        away = ((teams_obj.get("away") or {}).get("name") or "")
        if not home:
            title = match.get("title", "")
            s_slug, s_name, s_event = _series_of(title)
            if not s_slug:
                # Regex found nothing - try the id/title token forms.
                s_slug, s_name = _series_from_match(match)
                s_event = title
            f_slug, f_name = _feed_of(title)
            sport_id = match.get("category", "")
            if s_slug:
                grp = series_raw.setdefault(s_slug, {"name": s_name,
                                                     "items": []})
                grp["items"].append((match, streams, s_event))
                if match.get("date"):
                    starts_index.setdefault(
                        (sport_id, match["date"]), set()).add(s_slug)
            elif f_slug:
                cand = _nonteam_candidate(match, streams, "feed",
                                          f_name, f_name)
                if _roster_touch(f_slug, f_name, "", sport_id, now_iso,
                                 kind="feed"):
                    added += 1
                _offer(new_map, f_slug, cand)
                _sched_add(new_sched, f_slug, cand)
            elif streams:
                deferred.append((match, streams))
            continue
        slug = _slugify(home)
        if not slug:
            continue

        home_badge = ((teams_obj.get("home") or {}).get("badge") or "")
        away_badge = ((teams_obj.get("away") or {}).get("badge") or "")
        sport      = match.get("category", "")

        if _roster_touch(slug, home, home_badge, sport, now_iso):
            added += 1

        ranked = _rank_streams(streams or [])
        candidate = {
            "team":        home,
            "match_id":    match.get("id", ""),
            "match_title": match.get("title", ""),
            "away":        ((match.get("teams") or {}).get("away") or {}).get("name", ""),
            "category":    match.get("_category", "Sports"),
            "sport":       match.get("category", ""),
            "poster":      match.get("poster", ""),
            "starts":      match.get("date"),
            "streams": [
                {"embed_url": (x.get("embedUrl") or "").strip(),
                 "source":    x.get("source", ""),
                 "stream_no": x.get("streamNo", 1),
                 "hd":        bool(x.get("hd"))}
                for x in ranked if (x.get("embedUrl") or "").strip()
            ],
        }

        candidate["side"] = "home"
        _offer(new_map, slug, candidate)
        _sched_add(new_sched, slug, candidate)

        # A favourite is addressable from either side, so an away fixture
        # still resolves (and pre-warms) under the team's own name. The same
        # now holds for the four major leagues, which buys addressability
        # only - pre-warming stays limited to PREWARM_TEAMS.
        #
        # Favourites are matched on any sport, preserving their long-standing
        # behaviour whatever they are. Major-league names are additionally
        # gated on sport, so a mascot shared with another sport or level
        # (college "Charlotte 49ers", rugby "Broncos") cannot hijack a channel.
        away_slug = _slugify(away)
        if away_slug and (away_slug in _PREWARM_SLUGS
                          or (away_slug in _ALIAS_SLUGS
                              and sport in MAJOR_LEAGUE_SPORTS)):
            alias = dict(candidate)
            alias["side"] = "away"
            alias["team"] = away
            _offer(new_map, away_slug, alias)
            _sched_add(new_sched, away_slug, alias)
            if _roster_touch(away_slug, away, away_badge, sport, now_iso):
                added += 1

    # An entry naming no series still belongs to one if it starts at the very
    # same instant as a session that does - that is how the bare "Spanish
    # Grand Prix" (20 streams, no series anywhere in it) is tied to the F1
    # race it duplicates. Only when exactly one series claims the instant: if
    # two do, the pairing is ambiguous and it goes to the pool instead.
    for match, streams in deferred:
        key = ((match.get("category") or ""), match.get("date"))
        owners = starts_index.get(key) if match.get("date") else None
        if owners and len(owners) == 1:
            slug = next(iter(owners))
            series_raw[slug]["items"].append(
                (match, streams, match.get("title", "")))
        else:
            pool_raw.append((match, streams))

    # One merged candidate per series channel. Metadata comes from the
    # best-scoring listing, streams from all of them pooled.
    for slug, grp in series_raw.items():
        cands = [_nonteam_candidate(m, st, "series", grp["name"], ev)
                 for m, st, ev in grp["items"]]
        merged = dict(max(cands, key=_match_score))
        merged["streams"] = _merge_streams(c["streams"] for c in cands)
        if _roster_touch(slug, grp["name"], "", merged.get("sport", ""),
                         now_iso, kind="series"):
            added += 1
        _offer(new_map, slug, merged)
        # The guide wants one programme per session, so schedule by distinct
        # start time: practice, qualifying and the race each get their own
        # row, while the duplicate listings of one session collapse into it.
        by_start: dict = {}
        for c in cands:
            if c.get("starts"):
                prev = by_start.get(c["starts"])
                if prev is None or _match_score(c) > _match_score(prev):
                    by_start[c["starts"]] = c
        for c in by_start.values():
            _sched_add(new_sched, slug, c)

    # One-offs share a fixed set of slots, best/soonest first, so the guide
    # shows what is actually on rather than growing a channel per event.
    pool_cands = [_nonteam_candidate(m, st, "pool", "", m.get("title", ""))
                  for m, st in pool_raw]
    pool_cands.sort(key=_match_score, reverse=True)
    for i in range(POOL_SLOTS):
        name = "%s %d" % (POOL_NAME, i + 1)
        slug = _slugify(name)
        if _roster_touch(slug, name, "", "", now_iso, kind="pool"):
            added += 1
        if i < len(pool_cands):
            cand = dict(pool_cands[i])
            cand["team"] = name
            new_map[slug] = cand
            _sched_add(new_sched, slug, cand)

    with _team_lock:
        _team_map.clear()
        _team_map.update(new_map)
        _team_schedule.clear()
        _team_schedule.update(new_sched)
        total = len(_team_roster)

    # Save every cycle, not only when a team is new: badge, sport and
    # last_seen change on existing entries too, and those updates were
    # otherwise lost on restart. The file is small and this runs once per
    # refresh cycle.
    _save_team_roster()

    log.info("Team index: %d resolvable now, %d listed, %d in roster (+%d new)",
             sum(1 for v in new_map.values() if v["streams"]), len(new_map),
             total, added)


def _team_candidates(team: str, slot: str):
    """Candidate streams for ?team=..., best first.

    An explicit slot pins the request to that single stream; without a slot the
    caller gets every slot and cascades through them. Returns
    (candidates, None) on success or (None, Response) on failure.
    """
    slug = _slugify(team)

    with _team_lock:
        known = slug in _team_roster
        entry = _team_map.get(slug)
        entry = dict(entry) if entry else None

    if entry is None:
        if not known:
            log.info("Unknown team requested: %s", slug)
            return None, Response("Unknown team '%s'\n" % slug, status=404)
        log.info("Team off air (nothing listed): %s", slug)
        return None, Response("'%s' is not playing right now\n" % slug,
                              status=503)

    streams = entry.get("streams") or []
    if not streams:
        log.info("Team off air (listed, no streams): %s", slug)
        return None, Response(
            "'%s' is listed for %s but has no streams available\n"
            % (slug, entry.get("match_title", "?")), status=503)

    def label(s):
        return "%s%s-%s" % (s["source"], s["stream_no"],
                            "HD" if s["hd"] else "SD")

    if slot:
        try:
            idx = int(slot) - 1
        except (TypeError, ValueError):
            idx = 0
        if idx < 0:
            idx = 0
        if idx >= len(streams):
            return None, Response(
                "'%s' has %d slot(s); slot %d does not exist\n"
                % (slug, len(streams), idx + 1), status=503)
        s = streams[idx]
        log.info("Resolved team=%s slot=%d -> %s for %s",
                 slug, idx + 1, label(s), entry.get("match_title", "?"))
        return [{"embed_url": s["embed_url"], "label": label(s)}], None

    cands = [{"embed_url": s["embed_url"], "label": label(s)} for s in streams]

    # If the pre-warm loop validated one of these, try it first: it is both
    # cached and known-good, so the click lands warm instead of cascading.
    with _prewarm_lock:
        warm = (_prewarm_state.get(slug) or {}).get("embed_url")
    if warm and any(c["embed_url"] == warm for c in cands):
        cands.sort(key=lambda c: c["embed_url"] != warm)
        log.info("Resolved team=%s -> %d candidate(s), pre-warmed first, for %s",
                 slug, len(cands), entry.get("match_title", "?"))
    else:
        log.info("Resolved team=%s -> %d candidate(s) for %s",
                 slug, len(cands), entry.get("match_title", "?"))
    return cands, None


def build_m3u(matches: list[dict]) -> tuple[str, int]:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines   = [
        "#EXTM3U",
        f"# Generated by streamed-m3u — {now_iso}",
        f"# Source: {BASE_URL}",
        "",
    ]
    count = 0

    # Fetch every match's streams concurrently. pool.map preserves input
    # order, so playlist ordering is identical to the serial version.
    with ThreadPoolExecutor(max_workers=BUILD_WORKERS) as pool:
        streams_per_match = list(pool.map(fetch_streams_for_match, matches))

    # Refresh the team index from the same data. Playlist output below is
    # unaffected by this call.
    try:
        update_team_index(matches, streams_per_match)
    except Exception:
        log.exception("Team index update failed (playlist unaffected)")

    for streams in streams_per_match:
        if not streams:
            continue

        for stream in streams:
            embed_url = (stream.get("embedUrl") or "").strip()
            if not embed_url:
                continue

            title     = stream["_match_title"]
            category  = stream["_match_category"]
            match_id  = stream["_match_id"]
            language  = stream.get("language", "")
            hd        = stream.get("hd", False)
            stream_no = stream.get("streamNo", 1)
            source    = stream.get("source", "")

            ch_name  = format_channel_name(title, source, stream_no, hd)
            tvg_id   = f"streamed.{match_id}.{source}.{stream_no}"

            proxy_url = f"{_BASE_PLACEHOLDER}/stream?url={quote(embed_url, safe='')}"

            # Route the logo through our own /logo proxy rather than linking
            # streamed.pk directly — see the comment on LOGO_CACHE_TTL above.
            poster = stream.get("_match_poster", "")
            logo_attr = ""
            if poster:
                poster_url = poster if poster.startswith("http") else f"{BASE_URL}{poster}"
                proxied_logo = f"{_BASE_PLACEHOLDER}/logo?url={quote(poster_url, safe='')}"
                logo_attr = f'tvg-logo="{proxied_logo}" '

            lines.append(
                f'#EXTINF:-1 tvg-id="{tvg_id}" '
                f'tvg-name="{ch_name}" '
                f'{logo_attr}'
                f'group-title="{category}",'
                f'{ch_name}'
            )
            lines.append(proxy_url)
            count += 1

    log.info("Built playlist: %d entries", count)
    return "\n".join(lines), count


# ─── Pre-warm ─────────────────────────────────────────────────────────────────

def _set_prewarm(slug, name, status, entry, embed_url=None, label=None):
    with _prewarm_lock:
        st = _prewarm_state.setdefault(slug, {})
        st.update({
            "team":        name,
            "status":      status,
            "match":       (entry or {}).get("match_title", ""),
            "side":        (entry or {}).get("side", ""),
            "checked_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        if embed_url:
            st["embed_url"] = embed_url
            st["label"] = label
            st["warmed_at"] = time.time()
        elif embed_url is None and not status.startswith("warm"):
            st.pop("embed_url", None)
            st.pop("label", None)


def _prewarm_pass(favs):
    now = time.time()
    for slug, name in favs:
        with _team_lock:
            entry = _team_map.get(slug)
            entry = dict(entry) if entry else None

        if not entry or not entry.get("streams"):
            _set_prewarm(slug, name, "not playing", entry)
            continue

        started = entry.get("starts")
        if started:
            age_min = (now * 1000 - started) / 60000.0
            if not (-PREWARM_WINDOW_BEFORE <= age_min <= PREWARM_WINDOW_AFTER):
                _set_prewarm(slug, name,
                             "outside window (%+.0f min)" % age_min, entry)
                continue

        # Skip if what we warmed last time still has plenty of TTL left.
        with _prewarm_lock:
            current = (_prewarm_state.get(slug) or {}).get("embed_url")
        if current:
            with _extract_cache_lock:
                cached = _extract_cache.get(current)
            if cached and (now - cached["ts"]) < (EXTRACT_CACHE_TTL - PREWARM_MARGIN):
                continue

        # Warm the first candidate that fully validates, so slot 1 at click
        # time is known-good rather than merely cached.
        details = []
        warm_order = entry["streams"]
        if REQUIRE_AUDIO:
            warm_order = sorted(warm_order,
                                key=lambda c: _known_silent(c["embed_url"]))
        for cand in warm_order[:CASCADE_MAX_ATTEMPTS]:
            label = "%s%s-%s" % (cand["source"], cand["stream_no"],
                                 "HD" if cand["hd"] else "SD")
            ok, _payload, detail = _validate_candidate(cand["embed_url"], label)
            details.append(detail)
            if ok:
                log.info("Pre-warmed %s (%s) -> %s for %s",
                         slug, entry.get("side", "?"), label,
                         entry.get("match_title", "?"))
                _set_prewarm(slug, name, "warm:%s" % label, entry,
                             embed_url=cand["embed_url"], label=label)
                break
        else:
            log.warning("Pre-warm found no working stream for %s: %s",
                        slug, ", ".join(details))
            _set_prewarm(slug, name, "no working stream", entry)


def prewarm_loop():
    """Keep favourite teams hot.

    Deliberately independent of the playlist refresh: warming several teams
    takes far longer than a refresh cycle should, and the two must not block
    each other. Serial by design - one Chromium at a time keeps peak memory
    predictable on a box that has little headroom.
    """
    if not PREWARM_TEAMS:
        log.info("Pre-warm disabled (no PREWARM_TEAMS set)")
        return
    # _feed_slug, not _slugify: the list may name a feed whose slug is pinned
    # (see FEED_SLUG_OVERRIDES). Slugging it naively yields a key that is not
    # in _team_map, and the pass reports "not playing" forever with no error.
    favs = [(_feed_slug(t), t) for t in PREWARM_TEAMS]
    log.info("Pre-warm active for %d team(s): %s",
             len(favs), ", ".join(s for s, _ in favs))
    while True:
        try:
            _prewarm_pass(favs)
        except Exception:
            log.exception("Pre-warm pass failed")
        time.sleep(PREWARM_INTERVAL)


# ─── Background refresh ───────────────────────────────────────────────────────

def refresh_loop():
    global _cached_m3u, _last_refresh, _last_count
    while True:
        log.info("Refreshing playlist …")
        try:
            sports   = fetch_sports()
            matches  = fetch_all_matches(sports)
            m3u, cnt = build_m3u(matches)
            with _cache_lock:
                _cached_m3u   = m3u
                _last_refresh = datetime.now(timezone.utc)
                _last_count   = cnt
            log.info("Playlist ready: %d stream entries across %d matches", cnt, len(matches))
        except Exception:
            log.exception("Refresh cycle failed")
        time.sleep(REFRESH_SECONDS)


# ─── Flask routes ─────────────────────────────────────────────────────────────

@app.route("/playlist.m3u")
def playlist():
    with _cache_lock:
        body = _cached_m3u
    if not body:
        return Response("# Playlist not yet ready — try again in a few seconds\n",
                        mimetype="text/plain", status=503)
    # The cached body is host-agnostic; the origin is resolved per request so
    # PUBLIC_BASE_URL takes effect immediately and the Host header (which the
    # client controls) is never baked into the cache.
    body = body.replace(_BASE_PLACEHOLDER, _public_base())
    return Response(body, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="streamed.m3u"'})


@app.route("/stream", methods=["GET", "HEAD"])
def stream_proxy():
    """
    Navigate to an embed page with a headless browser, intercept the .m3u8
    URL, then proxy the HLS stream as a continuous TS byte stream so
    Dispatcharr's ts_proxy receives the data it expects.
    HEAD requests are answered immediately so Dispatcharr validation passes.
    """
    embed_url = request.args.get("url", "").strip()
    team_arg  = request.args.get("team", "").strip()
    slot_arg  = request.args.get("slot", "").strip()
    if not embed_url and not team_arg:
        return Response("Missing 'url' or 'team' parameter", status=400)

    # Dispatcharr sends HEAD to validate — respond immediately, no browser needed
    if request.method == "HEAD":
        return Response(status=200, headers={"Content-Type": "video/mp2t"})

    hold_key = _stream_hold_key(team_arg, embed_url)
    if _disconnect_held(hold_key):
        log.info("Refusing stream, disconnect hold active (%s)", hold_key)
        return Response("Stream disconnected by operator\n", status=410)

    # Candidate list:
    #   ?url=...        -> exactly that stream (unchanged behaviour)
    #   ?team=X&slot=N  -> pinned to one slot, no cascade
    #   ?team=X         -> every slot, best first, tried until one works
    if embed_url:
        candidates = [{"embed_url": embed_url, "label": "url"}]
    else:
        candidates, err = _team_candidates(team_arg, slot_arg)
        if err is not None:
            return err


    # Cascade best-first until one candidate probes clean. The stop condition
    # is a wall-clock deadline, not an attempt count: a fast 404 costs ~2s and
    # leaves room for more tries, while a hung source costs the full browser
    # hard timeout and leaves none.
    # Sources already known to carry no audio go to the back rather than being
    # dropped: still usable as a last resort, but no longer worth a full chunk
    # download and a browser extraction ahead of a source that has sound.
    if REQUIRE_AUDIO:
        candidates.sort(key=lambda c: _known_silent(c["embed_url"]))

    started  = time.time()
    deadline = started + CASCADE_BUDGET
    tried    = min(len(candidates), CASCADE_MAX_ATTEMPTS)
    data = m3u8_url = headers = cookies = probe_segments = None
    primed   = None
    attempts = []
    silent_fallback = None   # (embed_url, label, payload) - used only if
                             # nothing with audio can be found at all

    for n, cand in enumerate(candidates[:CASCADE_MAX_ATTEMPTS], 1):
        # Only start another attempt if there is room to finish one.
        if n > 1 and time.time() + CASCADE_RESERVE > deadline:
            log.info("Cascade stopping after %d attempt(s), %.1fs elapsed — not enough budget left for another", n - 1, time.time() - started)
            attempts.append("budget-exhausted")
            break

        cand_url = cand["embed_url"]
        log.info("Resolving stream for: %s (attempt %d/%d, %s)",
                 cand_url, n, tried, cand["label"])

        ok, payload, detail = _validate_candidate(cand_url, cand["label"])
        if not ok:
            attempts.append(detail)
            if detail.endswith(":no-audio") and silent_fallback is None:
                silent_fallback = (cand_url, cand["label"], payload)
            continue

        d, h, c, segs, first = payload
        embed_url, data, m3u8_url = cand_url, d, d["url"]
        headers, cookies, probe_segments = h, c, segs
        primed = first
        if n > 1:
            log.info("Cascade succeeded on attempt %d (%s) after %.1fs",
                     n, cand["label"], time.time() - started)
        break

    # Nothing with audio. Rather than fail the channel, fall back to a source
    # that probed clean but is silent - the picture is still worth having.
    if probe_segments is None and silent_fallback is not None:
        cand_url, cand_label, payload = silent_fallback
        d, h, c, segs, first = payload
        embed_url, data, m3u8_url = cand_url, d, d["url"]
        headers, cookies, probe_segments = h, c, segs
        primed = first
        log.warning("No source with audio for this match - falling back to "
                    "silent %s (%s)", cand_label, cand_url)

    if probe_segments is None:
        detail = ", ".join(attempts) or "none tried"
        log.warning("No playable stream after %d attempt(s) in %.1fs: %s",
                    len(attempts), time.time() - started, detail)
        return Response(
            "No playable stream found (tried: %s)\n" % detail, status=502)

    def stream_ts(m3u8_url, headers, cookies, initial_segments, stream_id,
                  primed=None, stop=None):
        """Continuously fetch and yield TS segments as a raw byte stream.
        Uses a stop event so the loop exits cleanly when the client
        disconnects or the console asks this session to stop. On fatal CDN
        errors, invalidates the extract cache so the next request triggers
        a fresh browser extraction rather than re-using a dead URL."""
        stop = stop or threading.Event()

        def _bump(field, n=1):
            with _active_streams_lock:
                if stream_id in _active_streams:
                    _active_streams[stream_id][field] = \
                        _active_streams[stream_id].get(field, 0) + n

        def fetch_segment(seg_url):
            """Download one chunk whole, or return None.

            Buffered rather than streamed straight through on purpose. The old
            code forwarded bytes as they arrived, so a timeout partway meant a
            truncated chunk had *already* reached the player and was spliced
            onto the next one - a torn boundary mid-GOP, which is what the
            decoder showed as glitching and jumping backwards. Nothing leaves
            here until the whole chunk is in hand and its length checks out.

            Failures are logged, never swallowed: their invisibility is the
            reason this went undiagnosed for so long.
            """
            for attempt in range(1, SEGMENT_RETRIES + 2):
                if stop.is_set():
                    return None
                try:
                    seg = cf_requests.get(
                        seg_url,
                        headers=_headers_for_url(seg_url, headers),
                        cookies=_cookies_for_url(seg_url, cookies),
                        timeout=SEGMENT_TIMEOUT, stream=True,
                        impersonate="chrome",
                    )
                    if seg.status_code != 200:
                        log.warning("Segment HTTP %d (attempt %d/%d): %s",
                                    seg.status_code, attempt,
                                    SEGMENT_RETRIES + 1, seg_url)
                        continue

                    expected = seg.headers.get("Content-Length")
                    expected = int(expected) if expected and expected.isdigit() \
                        else None
                    if expected and expected > SEGMENT_MAX_MB * 1024 * 1024:
                        log.warning("Segment oversized (%d bytes > %dMB cap), "
                                    "skipping: %s",
                                    expected, SEGMENT_MAX_MB, seg_url)
                        return None

                    buf = bytearray()
                    cap = SEGMENT_MAX_MB * 1024 * 1024
                    for chunk in seg.iter_content(chunk_size=65536):
                        if stop.is_set():
                            return None
                        if chunk:
                            buf.extend(chunk)
                            if len(buf) > cap:
                                log.warning("Segment exceeded %dMB while "
                                            "downloading, abandoning: %s",
                                            SEGMENT_MAX_MB, seg_url)
                                return None

                    # A short read is the exact failure mode that used to be
                    # forwarded as a torn chunk. Treat it as a failure.
                    if expected is not None and len(buf) != expected:
                        log.warning("Segment incomplete %d/%d bytes "
                                    "(attempt %d/%d): %s",
                                    len(buf), expected, attempt,
                                    SEGMENT_RETRIES + 1, seg_url)
                        continue
                    if not buf:
                        log.warning("Segment empty (attempt %d/%d): %s",
                                    attempt, SEGMENT_RETRIES + 1, seg_url)
                        continue

                    if attempt > 1:
                        log.info("Segment recovered on attempt %d: %s",
                                 attempt, seg_url)
                        _bump("segments_retried")
                    return bytes(buf)

                except Exception as exc:
                    log.warning("Segment fetch failed (attempt %d/%d, "
                                "timeout=%ds): %s - %s",
                                attempt, SEGMENT_RETRIES + 1, SEGMENT_TIMEOUT,
                                seg_url, exc)

            log.warning("Segment dropped after %d attempt(s), leaving a gap: %s",
                        SEGMENT_RETRIES + 1, seg_url)
            _bump("segments_failed")
            return None

        def _generate():
            # Bounded set + deque so long-lived streams don't grow RAM unbounded
            seen_set = set()
            seen_deque = deque()

            def add_seen(url):
                key = _segment_key(url)
                while seen_deque and len(seen_deque) >= STREAM_SEEN_MAX:
                    seen_set.discard(seen_deque.popleft())
                seen_deque.append(key)
                seen_set.add(key)

            def _idle(seconds):
                """Wait without going silent on the wire.

                Yields one null packet per STREAM_KEEPALIVE_INTERVAL so the
                downstream health monitor sees bytes, not a stall. Deliberately
                does NOT touch last_data_at: keepalives are padding, not
                upstream data, and the idle self-termination must still fire
                on a source that has genuinely stopped publishing.
                """
                if STREAM_KEEPALIVE_INTERVAL <= 0:
                    stop.wait(seconds)
                    return
                end = time.time() + seconds
                while not stop.is_set():
                    remaining = end - time.time()
                    if remaining <= 0:
                        return
                    stop.wait(min(remaining, STREAM_KEEPALIVE_INTERVAL))
                    if stop.is_set():
                        return
                    _bump("keepalives_sent")
                    yield TS_NULL_PACKET

            def _note_prefix(cut):
                """Count a stripped wrapper; log the first one per stream."""
                _bump("prefix_stripped")
                _bump("prefix_bytes", cut)
                with _active_streams_lock:
                    first = _active_streams.get(stream_id, {}).get("prefix_stripped") == 1
                if first:
                    log.info("Stripping %d-byte non-TS wrapper from chunks "
                             "(id=%s)", cut, stream_id)

            def _fetch_with_keepalive(seg_url):
                """fetch_segment, yielding keepalives while the download runs.

                A whole chunk is buffered before any of it is forwarded (see
                fetch_segment), so at low bandwidth the download itself is the
                longest silence in the loop - measured at ~13s for a 6 MB chunk
                on a 4 Mbit/s night. The fetch runs on a worker thread and this
                generator pads the wait; the chunk comes back as the
                generator's return value, so callers use `yield from`.
                """
                if STREAM_KEEPALIVE_INTERVAL <= 0:
                    return fetch_segment(seg_url)
                result = {}

                def _run():
                    result["body"] = fetch_segment(seg_url)

                t = threading.Thread(target=_run, daemon=True)
                t.start()
                while True:
                    t.join(STREAM_KEEPALIVE_INTERVAL)
                    if not t.is_alive():
                        break
                    if stop.is_set():
                        # fetch_segment checks stop itself and will unwind.
                        return None
                    _bump("keepalives_sent")
                    yield TS_NULL_PACKET
                return result.get("body")

            consecutive_failures = 0
            last_data_at = time.time()  # tracks when we last yielded actual data
            delivered = 0               # segments that actually reached the player

            # Yield segments already fetched during the probe - no wasted
            # round-trip and the stream starts immediately.
            for seg_url in initial_segments:
                add_seen(seg_url)
                # Already fetched during the segment probe - reuse it so the
                # validation costs no extra round-trip.
                if primed and seg_url in primed:
                    body, cut = _strip_ts_prefix(primed.pop(seg_url))
                    if cut:
                        _note_prefix(cut)
                    delivered += 1
                    for off in range(0, len(body), 8192):
                        if stop.is_set():
                            return
                        chunk = body[off:off + 8192]
                        last_data_at = time.time()
                        with _active_streams_lock:
                            if stream_id in _active_streams:
                                _active_streams[stream_id]["bytes_sent"] += len(chunk)
                        yield chunk
                    continue
                body = yield from _fetch_with_keepalive(seg_url)
                if body is None:
                    continue
                body, cut = _strip_ts_prefix(body)
                if cut:
                    _note_prefix(cut)
                delivered += 1
                for off in range(0, len(body), 8192):
                    if stop.is_set():
                        return
                    chunk = body[off:off + 8192]
                    last_data_at = time.time()
                    with _active_streams_lock:
                        if stream_id in _active_streams:
                            _active_streams[stream_id]["bytes_sent"] += len(chunk)
                    yield chunk

            if initial_segments:
                last_data_at = time.time()
            # Count what actually reached the player, not what was attempted -
            # segments_failed is only meaningful against a truthful total.
            with _active_streams_lock:
                if stream_id in _active_streams:
                    _active_streams[stream_id]["segments_sent"] += delivered
                    _active_streams[stream_id]["last_segment_at"] = time.time()

            while not stop.is_set():
                # Idle timeout: if no data has been yielded for STREAM_IDLE_TIMEOUT
                # seconds, self-terminate. This handles Dispatcharr holding the
                # connection open after the client has disconnected.
                if time.time() - last_data_at > STREAM_IDLE_TIMEOUT:
                    log.info(
                        "Stream idle for >%ds, self-terminating (id=%s)",
                        STREAM_IDLE_TIMEOUT, stream_id,
                    )
                    break
                try:
                    segments, status = get_segments(m3u8_url, headers, cookies)
                    if not segments:
                        consecutive_failures += 1
                        if status in FATAL_STATUS_CODES:
                            log.warning(
                                "Fatal CDN error HTTP %d for %s - evicting cache",
                                status, embed_url,
                            )
                            with _extract_cache_lock:
                                _extract_cache.pop(embed_url, None)
                            _save_extract_cache()
                            break
                        if consecutive_failures > 5:
                            log.warning("Too many consecutive m3u8 failures, stopping stream")
                            break
                        yield from _idle(2)
                        continue
                    consecutive_failures = 0
                    new_segments = [s for s in segments
                                    if _segment_key(s) not in seen_set]
                    loop_delivered = 0
                    for seg_url in new_segments:
                        if stop.is_set():
                            return
                        body = yield from _fetch_with_keepalive(seg_url)
                        # Marked seen only after the attempts conclude, but
                        # marked either way: a chunk the CDN has genuinely
                        # dropped must not be retried forever while the live
                        # edge moves away from us.
                        add_seen(seg_url)
                        if body is None:
                            continue
                        body, cut = _strip_ts_prefix(body)
                        if cut:
                            _note_prefix(cut)
                        loop_delivered += 1
                        for off in range(0, len(body), 8192):
                            if stop.is_set():
                                return
                            chunk = body[off:off + 8192]
                            last_data_at = time.time()
                            with _active_streams_lock:
                                if stream_id in _active_streams:
                                    _active_streams[stream_id]["bytes_sent"] += len(chunk)
                            yield chunk
                    if loop_delivered:
                        with _active_streams_lock:
                            if stream_id in _active_streams:
                                _active_streams[stream_id]["segments_sent"] += loop_delivered
                                _active_streams[stream_id]["last_segment_at"] = time.time()
                    if not new_segments:
                        yield from _idle(2)
                except GeneratorExit:
                    return
                except Exception:
                    yield from _idle(2)

        try:
            yield from _generate()
        finally:
            stop.set()
            with _active_streams_lock:
                _active_streams.pop(stream_id, None)
            log.info("Stream closed for: %s", m3u8_url)

    # Register stream in active tracking before starting response.
    # The stop event lives on the record so the console can ask this
    # session to exit without a second shutdown path.
    stream_id = str(uuid.uuid4())[:8]
    stop = threading.Event()
    with _active_streams_lock:
        _active_streams[stream_id] = {
            "stream_id":       stream_id,
            "embed_url":       embed_url,
            "m3u8_url":        m3u8_url,
            "started_at":        time.time(),
            "segments_sent":     0,
            "bytes_sent":        0,
            "last_segment_at":   None,
            # Gaps in the video and near-misses. If segments_failed climbs
            # during a watch, that is picture glitching, not a cosmetic stat.
            "segments_failed":   0,
            "segments_retried":  0,
            # Null packets sent to hold the connection open while nothing
            # real was available. Not counted in bytes_sent, so mbit_per_s
            # stays a measure of the source, not of the padding.
            "keepalives_sent":   0,
            # Chunks that arrived wrapped in a non-TS prefix (gotcha #17)
            # and had it removed before forwarding.
            "prefix_stripped":   0,
            "prefix_bytes":      0,
            "hold_key":          hold_key,
            "stop":              stop,
        }

    log.info("Streaming TS from: %s (id=%s)", m3u8_url, stream_id)
    return Response(
        stream_ts(m3u8_url, headers, cookies, probe_segments, stream_id,
                  primed, stop=stop),
        content_type="video/mp2t",
        direct_passthrough=True,
    )


# Stream ids are 8 hex chars from uuid4. Anything else is a bad request,
# not a miss, so the console can tell a typo from a session that already ended.
_STREAM_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _stream_hold_key(team_arg: str, embed_url: str) -> str:
    """Stable identity for a /stream request, matching how candidates are
    chosen: an explicit url= wins, otherwise the team slug."""
    if embed_url:
        return "url:" + embed_url
    if team_arg:
        return "team:" + _slugify(team_arg)
    return ""


def _hold_disconnect(key: str) -> None:
    if not key:
        return
    now = time.monotonic()
    with _disconnect_holds_lock:
        _disconnect_holds[key] = now + STREAM_DISCONNECT_HOLD
        expired = [k for k, exp in _disconnect_holds.items()
                   if exp <= now and k != key]
        for k in expired:
            _disconnect_holds.pop(k, None)


def _disconnect_held(key: str) -> bool:
    if not key or STREAM_DISCONNECT_HOLD <= 0:
        return False
    now = time.monotonic()
    with _disconnect_holds_lock:
        exp = _disconnect_holds.get(key)
        if exp is None:
            return False
        if exp <= now:
            _disconnect_holds.pop(key, None)
            return False
        return True


def _stop_active_stream(stream_id: str) -> str:
    """Set the stop event for one proxy session and refuse reconnects for
    that channel for STREAM_DISCONNECT_HOLD seconds. Returns 'ok',
    'invalid', or 'missing'. Does not pop the record: stream_ts's finally
    block owns that, so a console stop and a client hangup stay on the
    same path."""
    if not _STREAM_ID_RE.match(stream_id or ""):
        return "invalid"
    with _active_streams_lock:
        rec = _active_streams.get(stream_id)
        ev = rec.get("stop") if rec else None
        hold_key = rec.get("hold_key") if rec else None
    if ev is None:
        return "missing"
    ev.set()
    if hold_key:
        _hold_disconnect(hold_key)
    log.info("Stream disconnect requested (id=%s hold=%s)", stream_id, hold_key)
    return "ok"


@app.route("/stream/status")
def stream_status():
    if auth.enabled() and not auth.logged_in():
        return auth.deny()
    now = time.time()
    with _active_streams_lock:
        streams = list(_active_streams.values())

    output = []
    for s in streams:
        elapsed = now - s["started_at"]
        last_seg = s["last_segment_at"]
        output.append({
            "stream_id":        s["stream_id"],
            "embed_url":        s["embed_url"],
            "m3u8_url":         s["m3u8_url"],
            "elapsed_seconds":  round(elapsed, 1),
            "segments_sent":    s["segments_sent"],
            "segments_failed":  s.get("segments_failed", 0),
            "segments_retried": s.get("segments_retried", 0),
            "keepalives_sent":  s.get("keepalives_sent", 0),
            "prefix_stripped":  s.get("prefix_stripped", 0),
            "prefix_bytes":     s.get("prefix_bytes", 0),
            "bytes_sent":       s["bytes_sent"],
            "mb_sent":          round(s["bytes_sent"] / 1_048_576, 2),
            "mbit_per_s":       round(s["bytes_sent"] * 8 / 1e6 / elapsed, 2)
                                if elapsed > 0 else None,
            "last_segment_ago": round(now - last_seg, 1) if last_seg else None,
        })

    return jsonify({
        "active_streams":   len(output),
        "segment_timeout":  SEGMENT_TIMEOUT,
        "segment_retries":  SEGMENT_RETRIES,
        "keepalive_interval": STREAM_KEEPALIVE_INTERVAL,
        "streams":          output,
    })


@app.route("/logo")
def logo():
    """Proxy+cache a streamed.pk image so Dispatcharr never has to reach
    streamed.pk directly (see LOGO_CACHE_TTL comment)."""
    img_url = request.args.get("url", "")
    if not img_url:
        return "", 400
    if not img_url.startswith("http"):
        img_url = f"{BASE_URL}{img_url}"

    now = time.time()
    with _logo_cache_lock:
        cached = _logo_cache.get(img_url)
    if cached and (now - cached["ts"]) < cached["ttl"]:
        if cached["ok"]:
            return Response(cached["content"], mimetype=cached["content_type"])
        return "", 502

    ok = False
    content = b""
    content_type = "image/webp"
    try:
        r = requests.get(img_url, timeout=REQUEST_TIMEOUT, headers=IMAGE_HEADERS)
        if r.status_code == 200:
            ok = True
            content = r.content
            content_type = r.headers.get("Content-Type", "image/webp")
        else:
            log.warning("Logo fetch got HTTP %d for %s", r.status_code, img_url)
    except requests.RequestException as e:
        log.warning("Logo fetch failed for %s: %s", img_url, e)

    with _logo_cache_lock:
        _logo_cache[img_url] = {
            "content": content,
            "content_type": content_type,
            "ts": now,
            "ttl": LOGO_CACHE_TTL if ok else LOGO_CACHE_FAIL_TTL,
            "ok": ok,
        }
        if LOGO_CACHE_MAX_ENTRIES and len(_logo_cache) > LOGO_CACHE_MAX_ENTRIES:
            oldest_key = min(_logo_cache, key=lambda k: _logo_cache[k]["ts"])
            del _logo_cache[oldest_key]

    if ok:
        return Response(content, mimetype=content_type)
    return "", 502


def _enrich_roster_entry(slug, entry, doc=None):
    """Copy a roster row and add kind/sport/league/in_lineup."""
    row = dict(entry or {})
    row.update(_entry_view(slug, row, doc))
    return row


@app.route("/teams")
def teams():
    """Diagnostic view of the team roster and current match resolution."""
    with _team_lock:
        roster = dict(_team_roster)
        tmap   = {k: dict(v) for k, v in _team_map.items()}
    doc, _ = lineup.current()

    want = request.args.get("team", "").strip()
    if want:
        # _feed_slug so a feed is findable by the name it displays under:
        # RedZone's channel is pinned to nfl-vs-redzone, but nobody looking
        # it up knows that - they type "NFL RedZone".
        slug = _feed_slug(want)
        entry = roster.get(slug)
        view = _entry_view(slug, entry or {}, doc) if entry is not None else {
            "kind": None, "sport": "", "league": None,
            "in_lineup": lineup.in_lineup(doc, slug),
        }
        return jsonify({"slug": slug, "known": slug in roster,
                        "roster": _enrich_roster_entry(slug, entry, doc) if entry else None,
                        "playing": tmap.get(slug), **view})

    if request.args.get("all"):
        enriched = {s: _enrich_roster_entry(s, e, doc) for s, e in roster.items()}
        return jsonify({"roster_size": len(roster), "roster": enriched})

    # Which away-side alias names the upstream API has actually produced.
    # A major-league name still listed as unseen once its season is under
    # way is a misspelling in MAJOR_LEAGUE_TEAMS, which otherwise fails
    # silently - the team simply never aliases.
    if request.args.get("alias"):
        seen   = sorted(s for s in _ALIAS_SLUGS if s in roster)
        unseen = sorted(s for s in _ALIAS_SLUGS if s not in roster)
        return jsonify({
            "count":        len(_ALIAS_SLUGS),
            "seen_count":   len(seen),
            "unseen_count": len(unseen),
            "sports":       sorted(MAJOR_LEAGUE_SPORTS),
            "seen":         seen,
            "unseen":       unseen,
        })

    playing = {k: v for k, v in tmap.items() if v["streams"]}

    def best(v):
        s = v["streams"][0]
        return "%s%s-%s" % (s["source"], s["stream_no"], "HD" if s["hd"] else "SD")

    return jsonify({
        "roster_size":       len(roster),
        "resolvable_now":    len(playing),
        "listed_no_streams": len(tmap) - len(playing),
        "teams": sorted(
            [{"slug": k, "team": v["team"], "match": v["match_title"],
              "category": v["category"], "streams": len(v["streams"]),
              "best": best(v), **_entry_view(k, roster.get(k, {}), doc)}
             for k, v in playing.items()],
            key=lambda x: x["team"],
        ),
    })


def _xmltv_time(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, timezone.utc).strftime(
        "%Y%m%d%H%M%S +0000")


def _xml_escape(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _match_duration_s(sport: str) -> int:
    return SPORT_DURATION_MIN.get((sport or "").lower(),
                                  EPG_DEFAULT_MINUTES) * 60


def build_epg() -> str:
    """XMLTV guide, one channel per team.

    Channel ids are streamed.team.<slug> and never change, which is the whole
    point of the per-team layout: Dispatcharr matches on these, so the guide
    keeps working even as fixtures come and go.
    """
    now = time.time()
    win_start = now - EPG_BACKFILL_HOURS * 3600
    win_end   = now + EPG_WINDOW_HOURS * 3600

    with _team_lock:
        roster = dict(_team_roster)
        sched  = {k: list(v) for k, v in _team_schedule.items()}

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="streamed-m3u">']

    for slug in sorted(roster):
        name = _xml_escape(roster[slug].get("name", slug))
        out.append('  <channel id="%s">' % _channel_id(slug, roster[slug]))
        out.append("    <display-name>%s</display-name>" % name)
        out.append("  </channel>")

    programmes = 0
    filler = 0

    for slug in sorted(roster):
        chan = _channel_id(slug, roster[slug])
        rows = []
        for r in sched.get(slug, []):
            start = r["starts"] / 1000.0
            stop = start + _match_duration_s(r.get("sport"))
            if stop <= win_start or start >= win_end:
                continue
            rows.append((start, stop, r))
        rows.sort(key=lambda x: x[0])

        cursor = win_start
        for start, stop, r in rows:
            # Overlapping fixtures would make an invalid guide; skip anything
            # already covered by the programme before it.
            if stop <= cursor:
                continue
            start = max(start, cursor)
            if start > cursor:
                out.append(
                    '  <programme start="%s" stop="%s" channel="%s">'
                    % (_xmltv_time(cursor), _xmltv_time(start), chan))
                out.append("    <title>No game scheduled</title>")
                out.append("  </programme>")
                filler += 1
            stop = min(stop, win_end)
            side = r.get("side") or ""
            desc = r.get("category", "Sports")
            if side:
                desc += " \u2014 %s" % ("home" if side == "home" else "away")
            out.append('  <programme start="%s" stop="%s" channel="%s">'
                       % (_xmltv_time(start), _xmltv_time(stop), chan))
            out.append("    <title>%s</title>" % _xml_escape(r["title"]))
            out.append("    <desc>%s</desc>" % _xml_escape(desc))
            out.append("    <category>%s</category>"
                       % _xml_escape(r.get("category", "Sports")))
            out.append("  </programme>")
            programmes += 1
            cursor = stop

        if cursor < win_end:
            out.append('  <programme start="%s" stop="%s" channel="%s">'
                       % (_xmltv_time(cursor), _xmltv_time(win_end), chan))
            out.append("    <title>No game scheduled</title>")
            out.append("  </programme>")
            filler += 1

    out.append("</tv>")
    log.info("EPG built: %d channels, %d fixtures, %d filler blocks",
             len(roster), programmes, filler)
    return "\n".join(out)


def _sport_display(sport: str) -> str:
    s = (sport or "").strip().lower()
    if not s:
        return "Sports"
    return SPORT_DISPLAY.get(s, s.replace("-", " ").title())


def build_team_m3u() -> tuple[str, int]:
    """One channel per team, addressed by slug.

    The URL carries no match id, so the entry never changes as fixtures come
    and go - that stability is what stops Dispatcharr churning channel numbers.
    Resolution to an actual stream happens at click time.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _team_lock:
        roster = dict(_team_roster)

    lines = [
        "#EXTM3U",
        "# streamed-m3u team channels - %s" % now_iso,
        "# One permanent channel per team; guide at /epg.xml",
        "",
    ]
    count = 0
    base = _public_base()

    for slug in sorted(roster, key=lambda s: (roster[s].get("name") or s).lower()):
        entry = roster[slug]
        name = entry.get("name") or slug
        kind = entry.get("kind", "team")
        if entry.get("favourite"):
            group = FAVOURITES_GROUP
        elif kind == "feed":
            group = "Live Feeds"
        elif kind == "pool":
            group = "Live Events"
        else:
            group = _sport_display(entry.get("sport"))

        logo_attr = ""
        badge = entry.get("badge")
        if badge:
            badge_url = "%s/api/images/badge/%s.webp" % (BASE_URL, badge)
            proxied = "%s/logo?url=%s" % (base, quote(badge_url, safe=""))
            logo_attr = 'tvg-logo="%s" ' % proxied

        lines.append(
            '#EXTINF:-1 tvg-id="%s" tvg-name="%s" %sgroup-title="%s",%s'
            % (_channel_id(slug, entry), name, logo_attr, group, name))
        lines.append("%s/stream?team=%s" % (base, quote(slug, safe="")))
        count += 1

    log.info("Team playlist built: %d channels", count)
    return "\n".join(lines), count


@app.route("/playlist-teams.m3u")
def playlist_teams():
    body, count = build_team_m3u()
    if not count:
        return Response("# No teams known yet - try again shortly\n",
                        mimetype="text/plain", status=503)
    return Response(body, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition":
                             'inline; filename="streamed-teams.m3u"'})


@app.route("/epg.xml")
def epg():
    try:
        body = build_epg()
    except Exception:
        log.exception("EPG build failed")
        return Response("EPG unavailable", status=503)
    return Response(body, mimetype="application/xml; charset=utf-8",
                    headers={"Content-Disposition":
                             'inline; filename="streamed-epg.xml"'})


@app.route("/prewarm")
def prewarm_status():
    """What the pre-warm loop is doing for each favourite team."""
    now = time.time()
    with _prewarm_lock:
        state = {k: dict(v) for k, v in _prewarm_state.items()}
    out = []
    for slug, st in sorted(state.items()):
        age = None
        if st.get("embed_url"):
            with _extract_cache_lock:
                ce = _extract_cache.get(st["embed_url"])
            if ce:
                age = round(now - ce["ts"], 1)
        out.append({
            "slug":        slug,
            "team":        st.get("team"),
            "status":      st.get("status"),
            "match":       st.get("match"),
            "side":        st.get("side"),
            "slot":        st.get("label"),
            "cache_age_s": age,
            "ttl_left_s":  (round(EXTRACT_CACHE_TTL - age, 1)
                            if age is not None else None),
            "checked_at":  st.get("checked_at"),
        })
    return jsonify({
        "teams":            PREWARM_TEAMS,
        "interval_s":       PREWARM_INTERVAL,
        "window_min":       [-PREWARM_WINDOW_BEFORE, PREWARM_WINDOW_AFTER],
        "warm":             sum(1 for o in out
                                if (o["status"] or "").startswith("warm")),
        "state":            out,
    })


@app.route("/health")
def health():
    with _cache_lock:
        refresh_iso = _last_refresh.isoformat() if _last_refresh else None
        count       = _last_count
    with _extract_cache_lock:
        cache_entries = len(_extract_cache)
    with _team_lock:
        roster_size = len(_team_roster)
        resolvable  = sum(1 for v in _team_map.values() if v["streams"])
        scheduled   = sum(len(v) for v in _team_schedule.values())
    return jsonify({
        "status":         "ok" if refresh_iso else "initialising",
        "last_refresh":   refresh_iso,
        "stream_count":   count,
        "refresh_every":  f"{REFRESH_SECONDS}s",
        "sources":        ALL_SOURCES,
        "extract_cached": cache_entries,
        "extract_ttl":    f"{EXTRACT_CACHE_TTL}s",
        "extract_cache_max_entries": EXTRACT_CACHE_MAX_ENTRIES,
        "stream_seen_max": STREAM_SEEN_MAX,
        "teams_roster":     roster_size,
        "teams_resolvable": resolvable,
        "epg_fixtures":     scheduled,
        "alias_teams":      len(_ALIAS_SLUGS),
        "prewarm_teams":    len(_PREWARM_SLUGS),
    })


# ─── Operations console ───────────────────────────────────────────────────────
# `/` used to be a plain-text info page that hardcoded this host's LAN IP.
# It is now a read-only dashboard served by dashboard.py. The providers below
# are the only coupling: dashboard.py owns no service state and takes no locks
# of its own, so every snapshot is taken here, where the locks live.

def _dash_runtime() -> dict:
    """Aggregate snapshot for /api/overview."""
    with _cache_lock:
        refresh = _last_refresh
        count   = _last_count
    with _team_lock:
        roster_size = len(_team_roster)
        resolvable  = sum(1 for v in _team_map.values() if v["streams"])
        listed      = len(_team_map) - resolvable
        fixtures    = sum(len(v) for v in _team_schedule.values())
    lineup_n = _lineup_counts()["in_lineup"]
    with _extract_cache_lock:
        extract_entries = len(_extract_cache)
    with _logo_cache_lock:
        logo_entries = len(_logo_cache)
    with _no_audio_lock:
        no_audio_entries = len(_no_audio_cache)
    with _active_streams_lock:
        active = len(_active_streams)
    with _prewarm_lock:
        warm = sum(1 for v in _prewarm_state.values()
                   if str(v.get("status") or "").startswith("warm"))

    age = next_in = None
    if refresh:
        age = (datetime.now(timezone.utc) - refresh).total_seconds()
        next_in = max(0.0, REFRESH_SECONDS - age)

    return {
        "status": "ok" if refresh else "initialising",
        "playlist": {
            "last_refresh":      refresh.isoformat() if refresh else None,
            "age_s":             round(age, 1) if age is not None else None,
            "next_refresh_in_s": round(next_in, 1) if next_in is not None else None,
            "stream_count":      count,
            "refresh_seconds":   REFRESH_SECONDS,
        },
        "roster": {
            "size":              roster_size,
            "resolvable":        resolvable,
            "listed_no_streams": listed,
            "epg_fixtures":      fixtures,
            "alias_names":       len(_ALIAS_SLUGS),
            "lineup":            lineup_n,
        },
        "prewarm": {
            "configured": len(_PREWARM_SLUGS),
            "warm":       warm,
            "interval_s": PREWARM_INTERVAL,
        },
        "streams": {
            "active":          active,
            "idle_timeout_s":  STREAM_IDLE_TIMEOUT,
            "segment_timeout": SEGMENT_TIMEOUT,
            "keepalive_interval_s": STREAM_KEEPALIVE_INTERVAL,
        },
        "caches": {
            "extract":  {"entries": extract_entries,
                         "max": EXTRACT_CACHE_MAX_ENTRIES,
                         "ttl_s": EXTRACT_CACHE_TTL},
            "logo":     {"entries": logo_entries,
                         "max": LOGO_CACHE_MAX_ENTRIES,
                         "ttl_s": LOGO_CACHE_TTL},
            "no_audio": {"entries": no_audio_entries,
                         "ttl_s": NO_AUDIO_TTL,
                         "enabled": REQUIRE_AUDIO},
        },
        "sources": ALL_SOURCES,
        "base_url": BASE_URL,
        "public_base": {
            "configured": bool(PUBLIC_BASE_URL),
            "value":      PUBLIC_BASE_URL or None,
        },
    }


def _dash_caches() -> dict:
    """Cache contents for /api/cache."""
    now = time.time()

    with _extract_cache_lock:
        raw = [(u, e.get("ts", 0), (e.get("data") or {}))
               for u, e in _extract_cache.items()]
    extract = []
    for url, ts, data in raw:
        age = now - ts
        extract.append({
            "embed_url":  url,
            "m3u8_url":   data.get("url"),
            "age_s":      round(age, 1),
            "ttl_left_s": round(EXTRACT_CACHE_TTL - age, 1),
            "expired":    age >= EXTRACT_CACHE_TTL,
        })
    extract.sort(key=lambda e: e["age_s"])

    with _no_audio_lock:
        silent = [{"embed_url": u,
                   "age_s": round(now - ts, 1),
                   "ttl_left_s": round(NO_AUDIO_TTL - (now - ts), 1)}
                  for u, ts in _no_audio_cache.items()]
    silent.sort(key=lambda e: e["age_s"])

    with _logo_cache_lock:
        logo_ok    = sum(1 for v in _logo_cache.values() if v.get("ok"))
        logo_total = len(_logo_cache)
        logo_bytes = sum(len(v.get("content") or b"")
                         for v in _logo_cache.values())

    return {
        "extract": {
            "entries": extract,
            "count":   len(extract),
            "max":     EXTRACT_CACHE_MAX_ENTRIES,
            "ttl_s":   EXTRACT_CACHE_TTL,
        },
        "no_audio": {
            "entries": silent,
            "count":   len(silent),
            "ttl_s":   NO_AUDIO_TTL,
            "enabled": REQUIRE_AUDIO,
        },
        "logo": {
            "count":  logo_total,
            "ok":     logo_ok,
            "failed": logo_total - logo_ok,
            "bytes":  logo_bytes,
            "mb":     round(logo_bytes / 1_048_576, 2),
            "max":    LOGO_CACHE_MAX_ENTRIES,
        },
    }


auth.install(app)
dashboard.register_dashboard(
    app,
    runtime=_dash_runtime,
    caches=_dash_caches,
    config_values=lambda: globals(),
    apply_settings=lambda s, o: _settings.apply_live(globals(), s, o),
    stop_stream=_stop_active_stream,
    refresh_extract=_refresh_extract_entry,
    clear_extract=_clear_extract_entry,
    restart_services=dockerctl.restart_services,
    lineup_view=_lineup_snapshot,
    lineup_update=_lineup_apply,
)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("streamed-m3u starting on 0.0.0.0:%d …", PORT)
    kill_orphan_chromium()
    _load_extract_cache()
    _install_seed_roster()
    _load_team_roster()
    lineup.reload(LINEUP_FILE)
    _seed_favourites()
    _seed_nonteam()
    time.sleep(STARTUP_DELAY)
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=_evict_extract_cache, daemon=True).start()
    threading.Thread(target=prewarm_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
