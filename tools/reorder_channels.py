#!/usr/bin/env python3
"""
reorder_channels.py
-------------------
Re-runnable Dispatcharr channel renumbering for the streamed-m3u setup.

Puts chosen leagues at the front of the channel list in a fixed order and
leaves everything else alone, in whatever relative order it already has.
Safe to run repeatedly - it is idempotent, and re-running after new channels
appear simply folds them into the right block.

The built-in order (MLB, NFL, NFL RedZone, NFL Network, then everything else)
is one operator's preference. Supply your own with --config, starting from
`--dump-config`, which prints the built-in order as editable JSON:

    {"blocks": [{"label": "MLB", "teams": ["Arizona Diamondbacks", ...]},
                {"label": "NFL RedZone", "tvg_ids": ["streamed.feed.nfl-vs-redzone"]}],
     "pending_blocks": [{"label": "NHL", "teams": [...]}]}

A block names channels either by team display name (`teams`, slugified to
streamed.team.<slug>) or by exact channel id (`tvg_ids`). `pending_blocks`
are held back until --all-leagues is passed: promoting a half-populated
league would number the teams that exist today and reshuffle them every time
another one appears.

Channels are matched by tvg_id, which is a channel's permanent address, never
by name, which can be edited.

Why one API call: Dispatcharr's /channels/assign/ endpoint takes an ordered
list of channel ids and assigns sequential numbers from `starting_number`.
The list must contain EVERY channel; a partial list renumbers only those and
leaves duplicates behind.

Usage:
    python3 reorder_channels.py --dry-run             # preview, change nothing
    python3 reorder_channels.py                       # apply
    python3 reorder_channels.py --config order.json   # your own order
    python3 reorder_channels.py --dump-config         # print the built-in order
    python3 reorder_channels.py --revert FILE         # restore a saved backup

Every apply writes a timestamped backup first, into REORDER_BACKUP_DIR
(default: the directory this script lives in). To undo, pass the newest one
back via --revert.

Environment:
    DISPATCHARR_URL      default http://dispatcharr:9191 (the compose service name)
    DISPATCHARR_USER     required
    DISPATCHARR_PASS     required
    REORDER_BACKUP_DIR   where backups are written
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

import requests

BASE = os.getenv("DISPATCHARR_URL", "http://dispatcharr:9191").rstrip("/")
USER = os.getenv("DISPATCHARR_USER", "")
PASS = os.getenv("DISPATCHARR_PASS", "")
BACKUP_DIR = os.getenv("REORDER_BACKUP_DIR",
                       os.path.dirname(os.path.abspath(__file__)))

# The built-in order. `--dump-config` prints this; `--config` replaces it.
DEFAULT_CONFIG = {
    "blocks": [
        {"label": "MLB", "teams": [
            "Arizona Diamondbacks", "Athletics", "Atlanta Braves", "Baltimore Orioles",
            "Boston Red Sox", "Chicago Cubs", "Chicago White Sox", "Cincinnati Reds",
            "Cleveland Guardians", "Colorado Rockies", "Detroit Tigers", "Houston Astros",
            "Kansas City Royals", "Los Angeles Angels", "Los Angeles Dodgers",
            "Miami Marlins", "Milwaukee Brewers", "Minnesota Twins", "New York Mets",
            "New York Yankees", "Philadelphia Phillies", "Pittsburgh Pirates",
            "San Diego Padres", "San Francisco Giants", "Seattle Mariners",
            "St. Louis Cardinals", "Tampa Bay Rays", "Texas Rangers",
            "Toronto Blue Jays", "Washington Nationals",
        ]},
        {"label": "NFL", "teams": [
            "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills",
            "Carolina Panthers", "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns",
            "Dallas Cowboys", "Denver Broncos", "Detroit Lions", "Green Bay Packers",
            "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars",
            "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers",
            "Los Angeles Rams", "Miami Dolphins", "Minnesota Vikings",
            "New England Patriots", "New Orleans Saints", "New York Giants",
            "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers",
            "San Francisco 49ers", "Seattle Seahawks", "Tampa Bay Buccaneers",
            "Tennessee Titans", "Washington Commanders",
        ]},
        # Feeds are listed by their pinned slug, not their display name.
        # RedZone's channel address is nfl-vs-redzone even though it displays
        # as "NFL RedZone" (see FEED_SLUG_OVERRIDES in app.py).
        {"label": "NFL RedZone", "tvg_ids": ["streamed.feed.nfl-vs-redzone"]},
        {"label": "NFL Network", "tvg_ids": ["streamed.feed.nfl-network"]},
    ],
    "pending_blocks": [
        {"label": "NHL", "teams": [
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
        ]},
        {"label": "NBA", "teams": [
            "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets",
            "Chicago Bulls", "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets",
            "Detroit Pistons", "Golden State Warriors", "Houston Rockets",
            "Indiana Pacers", "LA Clippers", "Los Angeles Clippers", "Los Angeles Lakers",
            "Memphis Grizzlies", "Miami Heat", "Milwaukee Bucks",
            "Minnesota Timberwolves", "New Orleans Pelicans", "New York Knicks",
            "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers",
            "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings",
            "San Antonio Spurs", "Toronto Raptors", "Utah Jazz", "Washington Wizards",
        ]},
    ],
}

_SLUG_CHARS = {"ø": "o", "æ": "ae", "å": "a", "ß": "ss",
               "đ": "d", "ð": "d", "ł": "l", "þ": "th"}


def slugify(name):
    """Mirrors _slugify in app.py. Must stay in step with it."""
    s = (name or "").strip().lower()
    for ch, repl in _SLUG_CHARS.items():
        s = s.replace(ch, repl)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def load_config(path):
    """Read and validate an order file. Exits with a clear message on error."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit("Could not read %s: %s" % (path, e))
    if not isinstance(cfg, dict):
        sys.exit("%s: expected a JSON object with a \"blocks\" list" % path)
    for key in ("blocks", "pending_blocks"):
        blocks = cfg.get(key, [])
        if not isinstance(blocks, list):
            sys.exit("%s: \"%s\" must be a list" % (path, key))
        for i, b in enumerate(blocks):
            if not isinstance(b, dict) or not b.get("label"):
                sys.exit("%s: %s[%d] needs a \"label\"" % (path, key, i))
            if not (b.get("teams") or b.get("tvg_ids")):
                sys.exit("%s: block \"%s\" needs \"teams\" or \"tvg_ids\""
                         % (path, b["label"]))
    cfg.setdefault("blocks", [])
    cfg.setdefault("pending_blocks", [])
    return cfg


def expand_blocks(blocks):
    """[{label, teams|tvg_ids}] -> [(label, [tvg_id, ...])]."""
    out = []
    for b in blocks:
        ids = ["streamed.team.%s" % slugify(t) for t in b.get("teams", [])]
        ids += list(b.get("tvg_ids", []))
        out.append((b["label"], ids))
    return out


def connect():
    missing = [n for n, v in (("DISPATCHARR_USER", USER),
                              ("DISPATCHARR_PASS", PASS)) if not v]
    if missing:
        sys.exit("Not configured: set %s in the environment"
                 % " and ".join(missing))
    s = requests.Session()
    r = s.post(f"{BASE}/api/accounts/token/",
               json={"username": USER, "password": PASS}, timeout=10)
    r.raise_for_status()
    s.headers.update({"Authorization": f"Bearer {r.json()['access']}"})
    return s


def paged(s, path, **params):
    page, out = 1, []
    while True:
        r = s.get(f"{BASE}{path}",
                  params={**params, "page": page, "page_size": 500}, timeout=30)
        r.raise_for_status()
        d = r.json()
        out.extend(d.get("results", d) if isinstance(d, dict) else d)
        if isinstance(d, dict) and d.get("next"):
            page += 1
        else:
            return out


def save_backup(channels):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(
        BACKUP_DIR,
        "channel_number_backup_%s.json"
        % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"saved_at": datetime.now(timezone.utc).isoformat(),
                   "dispatcharr_url": BASE,
                   "channels": [{"id": c["id"],
                                 "channel_number": c["channel_number"],
                                 "tvg_id": c.get("tvg_id"),
                                 "name": c.get("name")} for c in channels]},
                  f, indent=1)
    return path


def apply_order(s, ids):
    r = s.post(f"{BASE}/api/channels/channels/assign/",
               json={"channel_ids": ids, "starting_number": 1}, timeout=120)
    print("  assign -> %s %s" % (r.status_code, r.text[:120]))
    return r.status_code == 200


def do_revert(s, path):
    entries = json.load(open(path, encoding="utf-8"))["channels"]
    entries = [e for e in entries if e.get("channel_number") is not None]
    entries.sort(key=lambda e: e["channel_number"])
    print("Reverting %d channels to the order saved in %s" % (len(entries), path))
    return apply_order(s, [e["id"] for e in entries])


def main():
    ap = argparse.ArgumentParser(
        description="Renumber Dispatcharr channels so chosen leagues come first.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and change nothing")
    ap.add_argument("--revert", metavar="FILE",
                    help="restore channel numbers from a backup file")
    ap.add_argument("--all-leagues", action="store_true",
                    help="also promote the pending blocks (NHL and NBA by default)")
    ap.add_argument("--config", metavar="FILE",
                    help="JSON order file; see --dump-config for the shape")
    ap.add_argument("--dump-config", action="store_true",
                    help="print the built-in order as JSON and exit")
    args = ap.parse_args()

    if args.dump_config:
        print(json.dumps(DEFAULT_CONFIG, indent=1))
        return

    cfg = load_config(args.config) if args.config else DEFAULT_CONFIG
    blocks = expand_blocks(cfg["blocks"])
    pending = expand_blocks(cfg["pending_blocks"])
    if args.all_leagues:
        blocks += pending
    elif pending:
        print("%s held back (use --all-leagues to include)\n"
              % "/".join(label for label, _ in pending))

    s = connect()
    if args.revert:
        sys.exit(0 if do_revert(s, args.revert) else 1)

    chans = paged(s, "/api/channels/channels/")
    by_tvg = {}
    for c in chans:
        if c.get("tvg_id"):
            by_tvg.setdefault(c["tvg_id"], c)
    print("Fetched %d channels" % len(chans))

    ordered, claimed, ranges = [], set(), []
    for label, tvg_ids in blocks:
        block = [by_tvg[t] for t in tvg_ids
                 if t in by_tvg and by_tvg[t]["id"] not in claimed]
        block.sort(key=lambda c: (c.get("name") or "").lower())
        for c in block:
            claimed.add(c["id"])
        if block:
            ranges.append((label, len(ordered) + 1, len(ordered) + len(block)))
        ordered += block
        missing = len(tvg_ids) - len(block)
        print("  %-12s %3d channels%s" % (
            label, len(block),
            "  (%d not present yet)" % missing if missing else ""))

    rest = [c for c in chans if c["id"] not in claimed]
    rest.sort(key=lambda c: (c.get("channel_number")
                             if c.get("channel_number") is not None else 1e9))
    if rest:
        ranges.append(("other", len(ordered) + 1, len(ordered) + len(rest)))
    ordered += rest
    print("  %-12s %3d channels (relative order preserved)" % ("other", len(rest)))

    assert len(ordered) == len(chans), "plan size mismatch"
    assert len({c["id"] for c in ordered}) == len(chans), "duplicate in plan"

    print("\nResulting number ranges:")
    for label, lo, hi in ranges:
        first = ordered[lo - 1].get("name")
        print("  %-12s %4d-%-4d starting with %s" % (label, lo, hi, first))

    if args.dry_run:
        print("\nDry run - nothing changed.")
        return

    print("\nBackup: %s" % save_backup(chans))
    if not apply_order(s, [c["id"] for c in ordered]):
        sys.exit("assign failed - channel numbers may be unchanged; check above")
    print("Done.")


if __name__ == "__main__":
    main()
