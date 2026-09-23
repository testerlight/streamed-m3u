# streamed-m3u: maintainer handover

The user-facing guide is `README.md` (quick start, configuration reference,
endpoints, limitations). This document is the other half: how the service
works internally, how this TrueNAS SCALE deployment is wired, the settings
and login model, and a numbered list of gotchas that each cost real hours.
Nothing here duplicates the README; where the README covers something, this
file points at it.

**Channel numbering:** MLB 1-30, NFL 31-62, NFL RedZone 63, NFL Network 64,
everything else 65+. This is **not automatic**; it is applied by
`tools/reorder_channels.py`. See §5i.

---

## 1. How it works (read this first)

Three containers, plus one cron job:

| Component | Role |
|---|---|
| **gluetun** | WireGuard VPN. All streamed-m3u traffic egresses through it. |
| **streamed-m3u** | The service. Builds the playlist + EPG, resolves and proxies streams. Shares gluetun's network namespace. |
| **dispatcharr** | IPTV middleware. Consumes the playlist/EPG, exposes them to Jellyfin. |
| **dispatcharr_sync.py** | Cron job, every 8 min. Creates channels for in-lineup streams; never deletes. Hides the rest from Jellyfin. |

**The key design idea:** a channel's URL contains no match ID — it's just
`/stream?team=<slug>`. The channel is a permanent shelf; whichever fixture that
team has today gets resolved at click time. This is why channel numbers never
shuffle. Do not "optimise" this back into per-match channels.

**The Jellyfin lineup is a second, smaller set.** Every roster entry stays a
Dispatcharr stream (`playlist-teams.m3u` is never filtered). Only slugs in
`/data/lineup.json` become, or stay, visible channels. A missing file is
implicit-all. This box now has the file (`policy: all`) after the first −;
membership is still every slug until someone excludes one. New installs
seed MLB / NFL / NHL / NBA only. Do not copy `seed/lineup.json` onto an
existing `/data`.

**Two team lists, deliberately separate.** A channel normally resolves only the
fixtures where the upstream API lists that team as *home*; an away fixture would
show "No game scheduled" while the game is on. Two lists grant exemptions, and
they exist apart because they cost wildly different amounts:

| List | Grants | Cost |
|---|---|---|
| `PREWARM_TEAMS` (~4) | Away-side resolution **and** background pre-warming | **Expensive** — one Chromium launch per team, serial, ~20-25s each |
| `MAJOR_LEAGUE_TEAMS` (127) | Away-side resolution only | Free — a set lookup per match |

Do not merge them. Adding all four leagues to `PREWARM_TEAMS` would try to warm
127 teams serially and never finish a pass.

**Flow on a click:** Jellyfin → Dispatcharr → `streamed-m3u/stream?team=x` →
headless Chromium extracts the real `.m3u8` → segments fetched with a Chrome TLS
fingerprint → re-served as a raw TS byte stream.

---

## 2. Repository layout

| Path | Role |
|---|---|
| `app.py` | The service: scrape, playlist, guide, resolution, proxy. |
| `settings.py` | The settings schema, the `/data/settings.json` overlay, atomic writes. §3. |
| `auth.py` | Optional console login and CSRF. §4. |
| `dashboard.py`, `templates/`, `static/` | The console. |
| `extract_stream.py` | Playwright worker that sniffs out the real stream URL. |
| `dispatcharr_sync.py` | Channel sync. One cycle by default; `--loop` in the sync container. |
| `tools/reorder_channels.py` | Channel numbering. `--dump-config` prints the built-in order as editable JSON. |
| `tools/channel_order.example.json` | Generated: exactly `reorder_channels.py --dump-config`. Regenerate it, do not edit it; `check_reorder.py` fails if it drifts. |
| `tools/check_pacing.py` | Does a composite play at real speed when its inputs arrive in production-shaped bursts? Frame-numbered picture, read back per output frame. `MODE=smooth`/`filler`, `SECONDARY=none`. About a minute; run it after any change to the feeders or the ffmpeg argv. #34-#38. |
| `tools/check_reorder.py` | Offline proof of the numbering. Pure planner, synthetic channel list, no Dispatcharr. §12b. |
| `tools/soak_multiview.py` | Run on the **host**. Plays a Multi-Player channel through Dispatcharr's proxy (as Jellyfin does), optionally with an ordinary channel alongside and every control stepped, and reports gaps, Dispatcharr's unhealthy/switch counts, encoder exits, CPU and GPU. No Dispatcharr login. |
| `tools/multiview_api.py` | Console-API helper piped into the running container (`docker exec -i streamed-m3u python - get` / `put '<json>'` / `stop`), so the console password never leaves it. Used by the soak. |
| `tools/check_console.py`, `tools/check_render.py` | Verification harnesses. Bind-mounted into a scratch image, never shipped. |
| `tools/check_ffmpeg.py` | Verifies the image's composite capability: filters, VAAPI encode, and that live filter commands actually take effect. Same bind-mount pattern. §5b. |
| `tools/check_multiview.py` | Multi-view checks with the feature **on**. A separate script because the slot map is derived once at import, so enabled and disabled cannot share a process. §12. |
| `tools/live_multiview_ui.py` | The console's multi-player section driven against a **real** encoder and real fixtures. Needs the network and the render node; costs two extractions and a minute of encoding. A gate check, not a routine one. §5b. |
| `lineup.py` | Jellyfin lineup load/save/membership. Implicit-all when the file is missing. |
| `multiview.py` | Which fixtures each composite slot points at, and the rules that keep a slot playable. No encoder logic; safe to import with the feature off. §12. |
| `composite.py` | Multi-view plumbing: the feeders that keep each composite's two input FIFOs fed, the ffmpeg supervisor that drains them into one stream, and the ZMQ commands that move the layout and the mix while it plays. §12. |
| `seed/teams.json` | Seed roster installed on an empty `/data`. Team entries only, no favourites. |
| `seed/lineup.json` | Majors-only allowlist. Installed only with a fresh roster. Never copy onto existing `/data`. |
| `entrypoint.sh`, `Dockerfile` | The image. The entrypoint chowns `/data` then drops to `PUID:PGID`. |
| `docker-compose.yml`, `docker-compose.novpn.yml`, `.env.example` | Generic deployment. `.env.example` is generated: `python settings.py --env-example`. |
| `docs/internal/` | Scoped-out plans and working notes. Not user documentation. |
| `app_pre_*.py`, `app_post_*.py` | Rollback snapshots, a host convention. Ignored by git and the image. |

`teams.json` in `/data` is the roster and the only irreplaceable state. How an
empty roster fills in is §11. `lineup.json` is visibility, not identity.

---

## 3. Settings model

Three layers, highest first: `/data/settings.json` (console), the environment,
the built-in defaults in `app.py`. The file wins so a console edit has an
effect; the console shows the environment value beside it and a reset removes
the key from the file.

How it is wired, in `app.py` order:

1. `settings.scrub_empty_env()` runs before the config block, because compose
   interpolation hands over `VAR=""` for unset values and `int("")` would fail.
2. Every `X = os.getenv(...)` runs as before, so the constants hold
   environment-or-default values.
3. `settings.apply(globals(), settings.load())` runs immediately after the last
   constant and **before** any derived lookup table (`_PREWARM_SLUGS`,
   `_ALIAS_SLUGS`, `_SERIES_BY_SLUG`, `_SERIES_PATTERNS`). It first snapshots
   every constant into `settings.BASELINE`, which is what a reset restores, so
   defaults are never duplicated anywhere. Then it overlays the file. Bad
   values are logged and skipped, never fatal. `LOG_LEVEL` is re-applied to the
   root logger because logging was configured earlier in the file.
4. A console write goes `validate` (types, bounds, choices, the cross rules
   `CASCADE_RESERVE < CASCADE_BUDGET`, `SEGMENT_PROBE_TIMEOUT <= SEGMENT_TIMEOUT`,
   `PREWARM_MARGIN <= EXTRACT_CACHE_TTL`,
   `MULTIVIEW_MAX_ACTIVE <= MULTIVIEW_SLOTS`) then `save` (atomic) then
   `apply_live`. Nothing is saved on a validation error and nothing is applied
   on a save error.

Each setting's `applies` says when a change lands: `live` (the constant is read
at call time, so rebinding works at once), `next-refresh` (read inside the
playlist rebuild), or `restart` (consumed once at startup). Restart-only
changes are saved and listed in `settings.PENDING_RESTART`, which the console
shows as a banner. Two that look live but are not:

- `PREWARM_TEAMS` is frozen three times over: the global, `_PREWARM_SLUGS`, and
  the `favs` local captured before `prewarm_loop`'s `while`. Worse, the loop
  returns immediately if the list is empty at startup, so it can never be
  switched on live. Restart only.
- `PORT` is read at call time for URL generation but the socket is bound once.
  Rebinding it live would silently change every playlist URL while the socket
  stayed put. It is `editable=False`.

Overrides (`major_league_teams`, `extra_feed_title_aliases`,
`feed_slug_overrides`, `series_aliases`) merge onto the code tables at import:
lists union, dicts update. They are additive on purpose, so a typo in the file
cannot blank a league. All overrides are restart-only because each feeds an
import-time structure.

`LINEUP_FILE` is env-only, sibling of `TEAMS_FILE`, default `/data/lineup.json`.
A missing file is implicit `policy: "all"` with an empty exclude. The first −
materializes it. Do not install the seed lineup onto an existing `/data`.

Persistence: `settings.atomic_write_json` writes a temp file in the same
directory, fsyncs, keeps the previous file as `.bak`, then renames. It is also
what `_save_team_roster`, the lineup save and `_save_extract_cache` use now; the cache save is
called from four threads and used to truncate-write the final path directly.
On load, a corrupt file is moved to `.corrupt-<time>`, `.bak` is tried, then
(for the roster) the seed, then empty.

---

## 4. Console login

Off unless `CONSOLE_PASSWORD` is set; then the console is read-only with no
login page at all. With a password:

- Gated: `/`, `/api/*`, and `/stream/status` (it exposes resolved CDN URLs with
  their signing tokens). A browser navigation is redirected to `/login`; a
  JSON request gets 401.
- Open: `/health` (Docker healthcheck), the playlists, `/epg.xml`, `/stream`,
  `/logo`, `/teams`, `/prewarm`, `/static/*`, `/login`, `/api/session`.
- Writes need a signed-in session, an `X-CSRF-Token` header matching the
  session token, and a JSON body. The cookie is `HttpOnly`, `SameSite=Lax`,
  and `Secure` only if `CONSOLE_COOKIE_SECURE=1`.
- Failed logins sleep 1, 2, 4, then 8 seconds per source address. Entries age
  out after ten minutes.
- The session key lives at `/data/.secret_key` (0600). If `/data` is not
  writable the key is ephemeral and a warning is logged.

`docker exec` into the container runs as root because the image has no `USER`
directive; the privilege drop happens in the entrypoint for the main process
only. Use `-u 568` (or whatever `PUID` is) when a command should see the
files as the service does.

---

## 5. This deployment (TrueNAS SCALE)

The generic compose files are the reference. Since **2026-09-23** this box
runs them as **four separate Custom Apps** rather than one `dispatcharr11`
app: `gluetun`, `dispatcharr`, `streamed-m3u` (streamed-m3u and
streamed-m3u-sync) and `atwill` (its sibling project). Compose projects follow
the app names: `ix-gluetun`, `ix-dispatcharr`, `ix-streamed-m3u`, `ix-atwill`.
The rendered compose is regenerated by the middleware, so edits go through the
app config, not the rendered file:

| App | Rendered compose |
|---|---|
| streamed-m3u | `/mnt/.ix-apps/app_configs/streamed-m3u/versions/1.0.0/templates/rendered/docker-compose.yaml` |
| gluetun, dispatcharr, atwill | the same path with that app's name |

Everything below that says "the app" means `streamed-m3u`. History before
the split, including the Phase 11 deploy, refers to `dispatcharr11`.

### 5a. Custom App YAML

Deltas from the generic `docker-compose.yml`:

- `streamed-m3u` uses `build: context: /mnt/citizen/appdata/streamed-m3u`
  rather than `image:`, so an app update rebuilds from this directory.
- Its `environment:` carries `PUID=568`, `PGID=568` (the TrueNAS `apps`
  user), `PUBLIC_BASE_URL=http://<lan-ip>:8787` (Dispatcharr reaches the
  service through gluetun's published port, not the compose network),
  `PREWARM_TEAMS`, and optionally `CONSOLE_PASSWORD`.
- The volume is `/mnt/citizen/configs/streamed-m3u/data:/data`. The entrypoint
  chowns it to 568 on first start.
- `network_mode: container:gluetun` - gluetun is another app now, so the
  `service:` form cannot reach it. See gotcha #28 for what that costs.
- `devices: ["/dev/dri:/dev/dri"]` and `MULTIVIEW_ENABLE=1` (since
  2026-09-23, §12). The entrypoint grants the render node's group itself;
  there is no `group_add`.
- A `streamed-m3u-sync` service with the same `build:`, `command: ["python",
  "dispatcharr_sync.py", "--loop"]`,   `DISPATCHARR_URL=http://dispatcharr:9191`,
  the credentials, `M3U_ACCOUNT_NAME`, `EPG_SOURCE_NAME`,
  `STREAMED_M3U_URL=http://<lan-ip>:8787`, `SYNC_INTERVAL=480`,
  and `healthcheck: {disable: true}` (the image's healthcheck probes the web
  service). It joins Dispatcharr's network, `networks: [ix-dispatcharr_default]`
  declared `external`, and must **not** share gluetun's namespace; the tunnel
  has no route to other containers (gotcha #7). `STREAMED_M3U_URL` is how the
  sync reads `/teams?all=1` for lineup membership - by LAN address now,
  because gluetun is no longer on a network the sync shares.
- In the stored app config, `environment:` is a **list** of `KEY=value`
  strings, not a mapping. Anything that edits it programmatically must append
  to the list.

Read the current config with `midclt call app.config streamed-m3u`; apply a
change with `midclt call -j app.update streamed-m3u '{"custom_compose_config": {...}}'`
or through the UI. Two things the middleware does that are easy to get wrong:

- It recreates **every** service in the app, not only the changed ones.
  Since the split that is streamed-m3u and its sync only - gluetun,
  Dispatcharr and atwill are untouched - but a playing channel still dies.
  Check `active_streams` first.
- It only **builds** an image whose tag does not exist yet. An existing
  `ix-streamed-m3u-streamed-m3u` tag is reused as is, code changes and all.
  After editing code, run the explicit build in §5b before the app update,
  or the container keeps running the previous image.

### 5b. Rebuilding after a code change

Editing the app config triggers a rebuild. For a code-only change without
touching the config:

```bash
R=/mnt/.ix-apps/app_configs/streamed-m3u/versions/1.0.0/templates/rendered/docker-compose.yaml
docker compose -p ix-streamed-m3u -f $R build streamed-m3u streamed-m3u-sync
docker compose -p ix-streamed-m3u -f $R up -d --no-deps streamed-m3u streamed-m3u-sync
```

When the **app config** changes as well as the code (a device, a volume,
new environment), build first and update second, so there is one outage
rather than two: `docker compose ... build streamed-m3u streamed-m3u-sync`
against the current rendered file, then `midclt call -j app.update`. The
update recreates both services, and because the tag now exists it uses the
image just built instead of building. A change to `dispatcharr_sync.py`
alone needs no update at all: build, then `up -d --no-deps
streamed-m3u-sync`, and nothing that plays is touched. Before running it, check nothing in
the project's container names was started by hand (gotcha #27).

Check `active_streams` in `/stream/status` first; a restart kills every
playing channel (gotcha #21). Verify with the harness before any of this:

```bash
docker build -t streamed-m3u:verify .
docker run --rm -e STARTUP_DELAY=0 -e CONSOLE_PASSWORD=x \
  -v $PWD/tools/check_console.py:/tmp/check.py:ro streamed-m3u:verify python /tmp/check.py
```

That covers the feature switched **off**. The multi-view half needs its own
run, because the slot map is derived once at import and so enabled and
disabled cannot share a process:

```bash
docker run --rm -e STARTUP_DELAY=0 -e MULTIVIEW_ENABLE=1 \
  -v $PWD/tools/check_multiview.py:/tmp/check_mv.py:ro streamed-m3u:verify python /tmp/check_mv.py
```

It needs neither the network nor a render node — it drives the software
encoder on filler — and takes about three minutes, most of it spent waiting
out real timeouts.

The image also carries ffmpeg and the VAAPI runtime. To verify that half,
attach the render node and run as the service's own uid — root can open the
device when `568` cannot, so checking as root proves nothing:

```bash
docker run --rm --device /dev/dri --group-add "$(stat -c '%g' /dev/dri/renderD128)" \
  --user 568:568 -v $PWD/tools/check_ffmpeg.py:/tmp/check_ffmpeg.py:ro \
  streamed-m3u:verify python /tmp/check_ffmpeg.py
```

Note the `--user` there bypasses `entrypoint.sh` entirely. That is fine for
`check_ffmpeg.py` and misleading for anything else: pitfall #11 hid behind it
for a whole phase.

The console's own half - the part no Python check can see, because a corner
that does not move and a slider that does not travel both look like a passing
API - is `tools/check_render.py`, which drives Chromium against the real page.
It turns multi-view on unless told otherwise, and it takes a second, shorter
path when there is **no** password, where the point is that every control
that writes is inert:

```bash
mkdir -p _shots && chmod 777 _shots
docker run --rm -e STARTUP_DELAY=0 -e CONSOLE_PASSWORD=render-pw \
  -v $PWD/tools/check_render.py:/tmp/render.py:ro -v $PWD/_shots:/out \
  streamed-m3u:verify python /tmp/render.py
docker run --rm -e STARTUP_DELAY=0 \
  -v $PWD/tools/check_render.py:/tmp/render.py:ro -v $PWD/_shots:/out \
  streamed-m3u:verify python /tmp/render.py
```

Screenshots land in `_shots/`; both themes are captured, because the console
has two and only one of them is ever looked at by accident.

Neither of those runs an encoder. The one that does is
`tools/live_multiview_ui.py`, which picks the first two fixtures that actually
resolve, drives the section in Chromium, pulls the channel as an ordinary
viewer and moves the layout on the picture it is watching:

```bash
docker run --rm --device /dev/dri -e STARTUP_DELAY=0 -e CONSOLE_PASSWORD=live-pw \
  -e MV_CANDIDATES=baltimore-orioles,boston-red-sox,nfl-network,tennis-channel \
  -v $PWD/tools/live_multiview_ui.py:/tmp/live.py:ro -v $PWD/_shots:/out \
  streamed-m3u:verify python /tmp/live.py
```

Give it candidates in preference order and it takes the first two that warm -
a fixture listed is not a fixture on, and the 24/7 feeds at the end of the
list are the fallback when nothing is.

### 5c. The cron-to-container cutover (done 2026-09-18)

Until the containerization, a TrueNAS cron job (id 1, `*/8 * * * *`, root, no
environment) ran `/mnt/citizen/appdata/dispatcharr_sync.py`, which meant the
credential defaults hardcoded in that file were what was actually in use. The
sync container replaced it:

1. Add the `streamed-m3u-sync` service and credentials to the app config.
2. Deploy; confirm `docker logs streamed-m3u-sync` shows `Cycle complete`.
3. `midclt call cronjob.update 1 '{"enabled": false}'`, then delete it.
4. Delete the host copies: `/mnt/citizen/appdata/{dispatcharr_sync.py,
   reorder_channels.py, revert_channel_reorder_2026-09-11.py}`.
5. Rotate the Dispatcharr password and the WireGuard keys, and put the new
   values in the app config. The old ones were in plaintext in files for months.

### 5d. Add the M3U account in Dispatcharr

Open `http://<new-ip>:9191`, create an account, then add an M3U account:

- **Name:** must match `M3U_ACCOUNT_NAME` in the app config
- **URL:** `http://<new-ip>:8787/playlist-teams.m3u`
- **Active:** yes

Refresh it. With `teams.json` brought over it reports ~1,344 streams; starting
fresh it reports only ~22 and grows from there.

### 5e. Add the EPG source

- **Name:** must match `EPG_SOURCE_NAME` in the app config
- **Type:** XMLTV
- **URL:** `http://<new-ip>:8787/epg.xml`
- **Active:** yes

### 5f. Set the proxy settings

In Dispatcharr's settings, set:

| Setting | Value | Why |
|---|---|---|
| `channel_init_grace_period` | **60** | A cold stream takes 13–44s to resolve. Lower values abort every cold click. |
| `buffering_timeout` | **10** | |

> These are the single most common cause of "the channel never starts". The
> grace period is a **ceiling, not a delay** — raising it does not slow warm
> streams, it only gives cold ones room to finish.

### 5g. Create the channels

The sync container does this every `SYNC_INTERVAL` seconds. To run one cycle
now instead of waiting:

```bash
docker exec streamed-m3u-sync python dispatcharr_sync.py
```

Expect: streams found, channels created, EPG imported, guide linked.

### 5h. Point Jellyfin at it

**Dashboard → Live TV**

- **Tuner Device → M3U:** `http://<new-ip>:9191/output/m3u`
- **Guide Data Provider → XMLTV:** `http://<new-ip>:9191/output/epg`

Then **Refresh Guide Data**.

### 5i. Set favorite teams (optional but recommended)

Favorites are pre-warmed in the background, cutting click time from ~25s to ~5s.
Add to the `streamed-m3u` service's `environment:` block:

```yaml
    - PREWARM_TEAMS=Philadelphia Phillies,Philadelphia Eagles,Philadelphia 76ers,Philadelphia Flyers,NFL RedZone
```

Comma-separated, no quotes. The code default is empty, so nothing is pre-warmed
until this is set. It can also be set in the console (restart required). Verify a name resolves before committing to it:

```bash
curl "http://<new-ip>:8787/teams?team=Boston%20Red%20Sox"
```

Keep this list to roughly 10. Warming is serial and takes ~20–25s per team.
A feed may be listed here (RedZone is), but read gotcha #14 first.

### 5j. Number the channels (MLB / NFL first)

Dispatcharr assigns a channel number **once, at creation**, in whatever order
streams happened to arrive — so out of the box the numbering is arbitrary.
This puts the leagues at the front:

```bash
docker exec streamed-m3u-sync python tools/reorder_channels.py --dry-run   # preview
docker exec streamed-m3u-sync python tools/reorder_channels.py             # apply
```

The sync container has the credentials and can reach Dispatcharr by name.
Backups go to `REORDER_BACKUP_DIR`, which defaults to the script's own
directory inside the container; set it to `/data` to keep them.

Result: **MLB 1-30, NFL 31-62, NFL RedZone 63, NFL Network 64**, then
everything else from 65 on, keeping its existing relative order. With
multi-view enabled, **Multi-Player 1 and 2 take 65-66** and everything else
starts at 67 instead (§12b). Until those channels exist the block is inert.

Every run numbers **every** channel, hidden ones included. The channels the
lineup hides still exist in Dispatcharr and still hold numbers (1,337 on this
box) - Dispatcharr's list simply omits them unless asked. The reorder fetches
them with `visibility_filter=all` and puts them after everything visible, in
their existing order, so no visible channel ever shares a number with a
hidden one. Until 2026-09-23 it did not, and a run would have created 51 such
duplicates. Channels created since the last run (numbered at the end by
Dispatcharr) are pulled in too, so a run can move more than the block you
touched - check the dry-run's ranges.

- Idempotent and re-runnable. Every apply first writes a timestamped
  `channel_number_backup_*.json`; undo with `--revert <that-file>`.
- The order is data: `--dump-config` prints it as JSON, `--config` reads it back.
- Matches channels by `tvg_id` (their permanent address), never by name.
- `--all-leagues` additionally promotes NHL and NBA. **Held back on purpose** —
  promoting a half-populated league numbers only the teams that exist today and
  then reshuffles them every time another appears. Enable once
  `/teams?alias=1` shows most of those rosters present.
- **Run this after the roster is populated.** If you started without
  `teams.json`, wait until the MLB/NFL blocks are actually full, or accept that
  you will re-run it (and renumber) later. See §11.

---

## 6. Verification checklist

```bash
IP=<new-ip>
curl -s http://$IP:8787/health | jq          # status ok, teams_roster > 0,
                                             # prewarm_teams matches PREWARM_TEAMS, alias_teams 127
curl -s http://$IP:8787/prewarm | jq .teams  # the pre-warm entries
curl -s "http://$IP:8787/teams?alias=1" | jq '.seen_count, .unseen_count'
curl -s http://$IP:8787/playlist-teams.m3u | grep -c '#EXTINF'
curl -s http://$IP:8787/epg.xml | head -5    # valid XML
curl -s http://$IP:8787/teams?all=1 | jq '[.roster | to_entries[] | select(.value.in_lineup==false)] | length'
# This box: 1336 (lineup.json, policy "all" with an exclude list, since
# 2026-09-20). New install: the non-majors. No lineup.json at all: 0.

# The two NFL feeds resolve, and RedZone kept its pinned slug:
curl -s "http://$IP:8787/teams?team=NFL%20RedZone" | jq '.slug, .roster.kind'
#   -> "nfl-vs-redzone", "feed"   (NOT "nfl-redzone" - see gotcha #12)
curl -s "http://$IP:8787/teams?team=NFL%20Network" | jq '.slug, .roster.kind'
```

Then confirm, in order:

1. Dispatcharr's visible streamed channels match the **lineup** members
   (`in_lineup: true`), not the playlist's `#EXTINF` count - the playlist
   carries every roster entry and the sync creates only what the lineup
   allows. This box, 2026-09-23: 1459 entries, 123 lineup channels, plus 4
   AtWill channels from the sibling project in the same Dispatcharr
2. **Every channel has EPG data attached** (see gotcha #4 if not)
3. The playlist shows exactly as many `group-title="Favorites"` lines as you
   have pre-warm *teams* (feeds must not appear there — gotcha #11)
4. Jellyfin's guide shows fixtures, not blank rows
5. A team with a live game actually plays
6. Channels 1-64 are MLB / NFL / RedZone / NFL Network after §5j

---

## 7. Gotchas that will cost hours

**1. The hardcoded IP (historical).** The LAN IP used to be baked into four
files. It is gone: playlist URLs come from `PUBLIC_BASE_URL`, or from each
request's Host header when that is unset (§3). The legacy per-match playlist is
built on a background thread with no request to read, so it carries a
placeholder that `playlist()` substitutes per request. Never cache a
substituted body: the Host header is client-controlled. If URLs ever point at
the wrong host again, check `PUBLIC_BASE_URL` before anything else.

**2. Two copies of the sync script (historical).** There is one copy now, in
the build directory, shipped in the image and run by the `streamed-m3u-sync`
container with `--loop`. The host cron job that ran a second copy with no
environment (so its hardcoded credential defaults were live) was retired at the
cutover in §5c. If a host copy ever reappears, delete it.

**3. Code changes need a rebuild.** The build context is this directory; the
commands are in §5b. Settings do not: anything in §3 marked live or
next-refresh changes without a rebuild or a restart.

**4. Dispatcharr binds EPG data to a channel only at creation time.** A channel
created *before* its guide entry exists stays blank forever — the data is there,
the channel is there, nothing joins them. `link_epg_data()` in the sync script
repairs this on every run. **Do not remove it.**

**5. `POST /api/epg/import/` needs `{"id": <source_id>}` in the body.** Called
without it, the worker dispatches with source `None`, parses nothing, and still
returns HTTP 202. The failure is invisible from the response.

**6. The sync script deletes nothing, deliberately.** Deleting channels when a
game ends recycles channel numbers and reshuffles the guide — the exact problem
this design solves. A team with no game shows "No game scheduled".

**7. streamed-m3u cannot reach the LAN.** It shares gluetun's network namespace,
so all traffic goes out the VPN. Debug LAN connectivity from a different
container.

**8. Roughly 50% of teams have no logo.** That is the upstream API's limit, not
a misconfiguration.

**9. A misspelled name in `MAJOR_LEAGUE_TEAMS` fails silently.** Matching is
exact slug equality, so a wrong name simply never aliases — no error anywhere.
NFL and MLB names are verified against the live roster; NBA and NHL were added
while both were off-season and are unverified. Once those seasons start, run
`curl -s http://<host>:8787/teams?alias=1 | jq .unseen` — any major-league name
still listed there mid-season is a typo in the list.

**10. `MAJOR_LEAGUE_TEAMS` is sport-gated, `PREWARM_TEAMS` is not.** A
major-league name only aliases when the fixture's sport is one of
`american-football`, `baseball`, `basketball`, `hockey` (note: `hockey`, not
`ice-hockey`). This is what stops college "Charlotte 49ers" or a bare rugby
"Broncos" from hijacking an NFL channel. Favourites are matched on any sport so
their long-standing behaviour is unchanged.

**11. A feed has THREE separate names, and they are not interchangeable.**
The site forces an `"X vs Y"` title on everything, so RedZone arrives titled
"NFL vs RedZone". So there is now:

| Thing | Where it lives | Example |
|---|---|---|
| upstream title (what we match) | `FEED_TITLE_ALIASES` keys | `nfl vs redzone` |
| display name (what you see) | `FEED_CHANNELS` | `NFL RedZone` |
| slug (the permanent channel id) | `FEED_SLUG_OVERRIDES` | `nfl-vs-redzone` |

`FEED_SLUG_OVERRIDES` pins the slug so the rename did not mint a new channel
and orphan the old one. **Do not "tidy" it** — the slug is bound to a live
Dispatcharr channel and its guide link.

**12. Every feed slug must be derived via `_feed_slug()`, never `_slugify()`.**
There are five call sites (`_feed_of`, `_seed_nonteam`, `_seed_favourites`,
`_PREWARM_SLUGS`, `prewarm_loop`'s `favs`, plus the `/teams?team=` lookup).
Miss one and it silently disagrees — a bare `_slugify("NFL RedZone")` yields
`nfl-redzone`, which is not in `_team_map`, so pre-warm reports "not playing"
forever with no error anywhere. This bit during implementation.

**13. `_feed_slug` is defined next to `_slugify`, not next to `_feed_of`.**
`_PREWARM_SLUGS` is a module-level constant that calls it at import time, so
defining it lower in the file is an instant `NameError` crash-loop on start.

**14. Feeds do NOT all report `starts == 0`.** Observed live: Rally TV and
Tennis Channel report `0`; Willow Cricket and Fox League report negative
values; **RedZone and NFL Network report real start times.** This decides
whether the pre-warm window applies — a `0` makes a feed eligible whenever
it is listed (re-warming every ~2 min forever), while a real start time keeps
warming bounded to the broadcast. Check before adding a feed to
`PREWARM_TEAMS`: `curl -s .../teams?team=<name> | jq .playing.starts`.

**15. A series channel merges several upstream listings, by design.** The site
publishes one race under up to three shapes, and only the first is readable by
the `Series YYYY - Event` regex:

| Shape | Example | Caught by |
|---|---|---|
| `Series YYYY - Event` | `Formula 1 2026 - Spain GP` | `_series_of` regex |
| `Session \| Venue \| Detail` | `Race \| Circuit De Espana \| 308.5 Kms` | series slug in the **id** |
| series as suffix | `San Marino Grand Prix Moto2` | slug in id/title |
| year first | `2026 NASCAR Cup Series Playoff at Bristol` | slug in id/title |
| no series marker at all | `Spanish Grand Prix` | **start-time correlation** |

`_series_from_match` reads the id/title forms; it is **gated to
`motor-sports`** so a stray token elsewhere cannot hijack a series channel, and
it matches whole hyphen tokens, never substrings (so the alias `f1` cannot
match a hex blob like `deadbeef-f1a9`). The last row is the interesting one: a
bare `Spanish Grand Prix` names no series anywhere, and MotoGP and F2 run one
too, so it is attributed **only** when it shares an exact start time with a
session that does name the series - and only when exactly one series claims
that instant. No curated round-name list, no guessing.

All listings for a series are then pooled into **one** candidate with their
streams merged and deduped, so the cascade can fall through every source.
Measured effect: the F1 channel went from 3 streams (one `delta` source) to 23
across `admin`/`delta`/`golf`, and playback verified as using an `admin`
stream it previously had no access to.

For the guide, `_sched_add` is called once per **distinct start time**, so
practice / qualifying / sprint / race each get their own programme while the
duplicate listings of one session collapse into a single row.

> **Not yet verified:** practice, qualifying and sprint sessions were not in
> the catalog when this was built (it was race day). The race session came
> through the `Session | Venue | Detail` id form, and the others are published
> the same way, so they should follow - confirm on a Friday or Saturday with
> `curl -s http://<host>:8787/teams?team=Formula%201 | jq '.playing.match_title'`
> and check `/epg.xml` lists them as separate programmes.

---

**16. Chunks are buffered whole before any of it is forwarded.** `fetch_segment`
downloads a chunk into memory, checks the byte count against `Content-Length`,
and only then emits it. This is not an optimisation — the old code streamed
bytes through as they arrived, so a timeout partway meant a **truncated chunk
had already reached the player** and was spliced onto the next one. That torn
boundary is what the decoder showed as glitching and jumping backwards. Do not
"optimise" this back into a pass-through.

Related: the old 10s timeout was *below* the ~13s a healthy 6 MB chunk needed
at the time, so it fired constantly on good streams. If you ever see
`segments_failed` climbing in `/stream/status`, check `mbit_per_s` in the same
output before touching anything — it is usually bandwidth, not the source.

**17. Some sources wrap each chunk in a fake 42-byte WebP header — and the
proxy now strips it.** Observed on `admin` sources: every chunk begins
`RIFF....WEBPVP8L...EXIF` with the real MPEG-TS starting at byte 42. It is a
CDN anti-hotlinking trick — video disguised as an image. **It is not
consistent**: on the same match, `delta2` and `admin2` delivered clean TS
starting at byte 0, and every `admin` chunk captured since has been wrapped.

Until 2026-09-18 the wrapper was forwarded as-is, so a decoder hit 42 bytes of
garbage at every chunk boundary and had to resync. On a direct-playing client
that cost a glitch; through Jellyfin's transcoder it was far worse — measured
on a Twins/Angels stream: 64 `decode_slice_header error` + 16 `no frame` in
4½ minutes, ffmpeg inventing **47% of its output frames** (`dup=7466` of
15825) to cover the losses, and playback that stuttered and periodically
reloaded.

`_strip_ts_prefix()` (next to `_ts_sync_offset`) now removes the wrapper
before a chunk is forwarded. Three things that are load-bearing:

- **The length is detected, never assumed.** It uses `_ts_sync_offset` — the
  first byte where `0x47` holds for 10 consecutive 188-byte strides — so a
  7-byte wrapper, a 42-byte one, or none at all are all handled, and a clean
  source is returned untouched (same object, no copy).
- **It fails open.** If no alignment can be proven — an unparseable body, or
  one too short to show 10 packets — the chunk is passed through unchanged.
  Dropping real bytes on a guess would be worse than the wrapper.
- **It is applied at every point a chunk enters the generator** — the primed
  probe chunk, the initial-manifest fetches and the main loop — so the first
  chunk a player sees is as clean as the last.

Verified after deploy on the same `admin` source: a 25s pull produced
19.8 MB / 105,281 packets with **zero** sync breaks and zero `RIFF` headers
(the pre-fix capture of the same source could not hold alignment at all).

Diagnose from `/stream/status`: `prefix_stripped` / `prefix_bytes` count what
was removed, and the first strip on each stream logs
`Stripping 42-byte non-TS wrapper from chunks`. A stream showing
`prefix_stripped == segments_sent` is simply an `admin` source behaving
normally. Note the startup probe (`_has_audio_track`, gotcha #19) still sees
the raw chunk and does its own offset detection — that is intentional, the
probe must not depend on the streaming path.

**18. Segment dedup must ignore the signing params, not compare whole URLs.**
The CDN re-signs a segment between manifest polls — same `.ts` object, fresh
`X-Amz-Date` and `X-Amz-Signature` — so the same chunk reappears under a URL
that differs only in its credentials. Comparing full URLs treats that as a new
segment and forwards the same 5 seconds to the player **twice**. `_segment_key()`
(next to `resolve_url`) strips `X-Amz-*` plus a short list of common token
names, and both `add_seen()` and the `new_segments` filter key on it.

Measured on a `delta` source before the fix: **49% of forwarded segments were
duplicates**, delivery ran at 1.15x real time, and the surplus grew without
bound — +123s, +143s, +164s across three samples of one session. Symptoms were
video jitter (each duplicate steps PTS backwards 5s) and audio trailing video
by ~30s and widening, because the inflated buffer makes the video path skip to
track the live edge while the audio path plays the duplicates through. After
the fix the same stream ran at 0.984x with surplus oscillating around zero.

> Keep the strip list **narrow**. Stripping a param that genuinely identifies a
> segment would dedup away real content — a silent gap in the video, which is
> worse than the duplicate it prevents. Verified re-signed URLs return
> byte-identical content (matching SHA1s), so path-level identity is sound here.

> This is a different bug from #16 in the same code path: #16 was a *torn*
> chunk, this is a *repeated* one. Buffering chunks whole does not prevent it.
> Diagnose it from `/stream/status` — if `segments_sent × segment_duration`
> drifts above `elapsed_seconds` and keeps climbing, duplicates are getting
> through. A live source cannot outrun real time.

**19. Some sources carry no audio track at all, and they rank first.** The
`admin` source publishes a raw MLB.TV feed as a video-only transport stream —
one H.264 elementary stream, no audio PID, ~99.8% of packets on the video PID
and nothing else but PAT/SDT/padding. Playback looks perfect and is completely
silent. `_rank_streams` sorts HD first then by `ALL_SOURCES` order, where
`admin` sits ahead of `delta`, so with both HD the silent source wins by
default. Measured when this was found: **59 teams simultaneously ranked to an
`admin` source.** The `delta` copy of the same match carries AAC 48 kHz stereo.

`_validate_candidate` already downloads a whole chunk to prove a source is
alive, so `_has_audio_track()` reads the PAT/PMT out of that same chunk for
free and the cascade falls through to a source with sound.

> Three things that are load-bearing, not incidental:
> - **It fails open.** `_has_audio_track` returns `None` when it cannot find TS
>   alignment or a PMT, and `None` must be treated as a pass. Rejecting a
>   source we merely failed to parse burns a cascade attempt on a stream that
>   plays fine.
> - **Silent video still beats no video.** The cascade keeps the first
>   `:no-audio` candidate's payload and falls back to it if nothing with audio
>   can be found, rather than failing the channel.
> - **The sync offset is detected, not assumed.** `admin` chunks are WebP-
>   wrapped (gotcha #17), so the PMT scan must start at the real TS offset or
>   it finds nothing and wrongly reports "no audio". Confirmed working against
>   both wrapped (offset 42) and clean (offset 0) captures.

`NO_AUDIO_TTL` caching matters more than it looks: without it every cold click
re-downloads a ~3.4 MB chunk from the silent source and burns a browser
extraction before moving on. Measured on the same channel — first click 35.2s
(probe `admin`, reject, extract `delta`), second click ~6s with `delta` tried
first.

> Note the tradeoff: `delta` is 720p where `admin` is 1080p. Sound is the right
> trade, but this does cost resolution. Diagnose with
> `docker logs streamed-m3u | grep -i "NO AUDIO TRACK"`, and note that
> every successful probe now logs `audio=True/False`.

**20. The proxy used to go silent between chunks, and Dispatcharr hangs up on
silence.** A chunk is buffered whole (gotcha #16) and then forwarded as a
burst; after that the loop has nothing to send until the next chunk is either
downloaded or published upstream, and it sent exactly that: nothing.
Dispatcharr's health monitor treats ~10s without bytes as a stalled stream,
and after a second grace period it tears the connection down and reconnects.
Observed on a healthy Lions/Bills stream at 9.4 Mbit/s: `no data for 10.8-13.9s`
warnings on **every** chunk, then `Setting reconnect flag for stable stream
(stable for 557.4s)` and a forced reconnect - a 14s black screen on a source
that had done nothing wrong. Because a reconnect is a cold `/stream?team=`
request, it re-ran the cascade and, with the extract cache already expired,
launched a browser and came back on a different CDN URL. That is the
"stream cut out and jumped to a new extract" symptom.

The fix is to pad the gaps with MPEG-TS null packets (PID `0x1FFF`) - 188
bytes of spec-defined filler every decoder discards on sight. `_idle()` emits
one per `STREAM_KEEPALIVE_INTERVAL` while waiting at the live edge, and
`_fetch_with_keepalive()` runs the chunk download on a worker thread and pads
that wait too, since at 4 Mbit/s the download itself is the longest silence
(~13s for a 6 MB chunk). Three things that are load-bearing:

- **Keepalives do not touch `last_data_at`.** `STREAM_IDLE_TIMEOUT` must still
  fire on a source that has genuinely stopped publishing. Verified in
  isolation: the idle self-termination fires on schedule with keepalives
  flowing.
- **Keepalives are not counted in `bytes_sent`**, so `mbit_per_s` in
  `/stream/status` stays a measure of the source. They have their own
  `keepalives_sent` counter. A stream showing a steadily climbing
  `keepalives_sent` and a healthy `mbit_per_s` is the *normal* picture of a
  proxy sitting at the live edge - not a problem.
- **Null packets are only ever inserted at chunk boundaries**, so the stream
  stays 188-aligned — including on WebP-wrapped sources, now that gotcha #17
  strips the wrapper before the chunk reaches this point.

Verified after deploy, same game, same `admin` source: a 100s pull through
Dispatcharr's own proxy produced **zero** `Stream unhealthy` lines (previously
one every 20-40s), 24 segments, 0 failed, 58 keepalives. An 80s raw capture
showed every null-packet run sitting exactly on a chunk boundary.

> Do not "fix" this by raising Dispatcharr's stall threshold instead. That
> threshold exists to catch genuinely dead sources; the proxy's healthy rhythm
> should not look like one. Set `STREAM_KEEPALIVE_INTERVAL=0` to disable
> without a rebuild if a player ever objects to the padding.

**21. Restarting the container kills every channel that is playing, and
viewers have to re-select it.** Dispatcharr's reconnect back-off is 0.25s,
then 0.5s, then give up — all three attempts fit inside the first two seconds
of a ~15s container boot, so every active channel goes to `state: error`
(`Maximum retry attempts (3) reached`) and nobody reconnects automatically.
Observed on every deploy. Gotcha #20's keepalive does not help here: the
connection is refused, not silent. Before `up -d`, check
`curl -s http://<host>:8787/stream/status | jq .active_streams` and either
wait for zero or warn whoever is watching. There is no fix on this side;
the retry policy is Dispatcharr's.

**22. Do not copy `seed/lineup.json` onto an existing `/data`.** That seed is
an MLB/NFL/NHL/NBA allowlist for new installs. A missing file is
implicit-all, so Jellyfin stays unchanged until someone presses −. Copying
the seed would hide every college, soccer, feed and series channel
overnight. After the first − the file exists (`policy: all`); that is not
a licence to replace it with the seed.

**23. Never filter `build_team_m3u()`.** Removing an M3U line orphans the
Dispatcharr stream and later duplicates the channel when the team returns.
Visibility is `hidden_from_output` only. See `PENDING_channel_scaling.md` §6.

**24. Hide is not delete.** The sync script still creates nothing it would
later need to remove, and it still deletes nothing. `hidden_from_output`
drops a channel out of `/output/m3u` and `/output/epg` while keeping the
number, `tvg_id`, EPG link and stream ids.

**25. The first − materializes `/data/lineup.json`.** Until that write the
policy is implicit-all and every slug reports `in_lineup: true`. + / − on
`all` only edit `exclude`; + / − on `allowlist` only edit `include`.

**26. Dispatcharr's channel list omits hidden rows by default.**
`GET /api/channels/channels/` uses `visibility_filter=active` unless you
pass `all`. Sync must list with `visibility_filter=all` or a − then +
creates a second channel (new number) and leaves the original hidden.
Retrieve-by-id still reaches a hidden row; the list does not. A hidden row
keeps its channel number, so anything that renumbers must list with `all`
too: `reorder_channels.py` does since 2026-09-23 (it plans the hidden rows
after everything visible). Before that it planned visible rows only, and the
"gaps" its dry-run showed at 65, 67 and 80-90 were hidden channels, not
drift.

**27. A hand-started container with a project service's name breaks
`app.update` half-way.** The middleware's `down` removes only containers
carrying the project's compose labels. A container started with plain
`docker run --name atwill` (as happened on 2026-09-21 during atwill work)
survives it, and the `up` then fails on `Conflict. The container name
"/atwill" is already in use` - **after** everything else has been removed and
recreated but before it is started. gluetun, Dispatcharr and the sync
containers sit in `Created`, streamed-m3u is gone, and TrueNAS shows the app
`DEPLOYING` indefinitely. Before any `app.update`:

```bash
docker ps -a --filter name='^(gluetun|dispatcharr|streamed-m3u|streamed-m3u-sync|atwill|atwill-sync)$' \
  --format '{{.Names}} {{.Label "com.docker.compose.project"}}'
```

Every name must show a project - since the split, `ix-<app>` for the app it
belongs to (`streamed-m3u` and `streamed-m3u-sync` show `ix-streamed-m3u`).
If one shows nothing, move it aside first (`docker rename`, then stop). To
recover after the fact without touching the stray, start the app's own
services directly: `docker compose -p ix-<app> -f <rendered> up -d --no-deps
<services>`. The split also shrinks the damage: an update to one app can no
longer strand the others.

**28. `network_mode: container:gluetun` does not survive gluetun being
recreated.** Docker resolves the name to a container id when the dependent
container is created, and joins that container's network namespace. Update
or redeploy the gluetun app and its container is replaced; streamed-m3u and
atwill keep running, report healthy (their health checks are loopback), and
are unreachable, because the namespace they joined is gone. That is exactly
the state the hand-started `atwill` was found in during Phase 11. After any
change that recreates gluetun, recreate streamed-m3u and atwill too:
`docker compose -p ix-streamed-m3u -f <rendered> up -d --force-recreate
--no-deps streamed-m3u` (and the same for atwill). A plain restart of
gluetun has the same effect on the namespace; treat it the same way.

## 8. Endpoint reference

The endpoint table is in the README. Recipes that are only useful when
debugging:

```bash
curl -s "http://$IP:8787/teams?team=NFL%20RedZone" | jq   # accepts name or slug; shows roster + current fixture
curl -s "http://$IP:8787/teams?alias=1" | jq '.unseen'      # league names never seen upstream (misspellings)
curl -s "http://$IP:8787/stream?team=<slug>&slot=2" -o /dev/null   # pin one slot, no cascade
curl -s "http://$IP:8787/stream?url=<embed-url>" -o /dev/null      # legacy direct form
```

`/api/config` shows every setting with its source and bounds; `PUT
/api/settings` changes them (§3, §4). `GET /api/lineup` is the Jellyfin
lineup; `PUT /api/lineup` adds or removes slugs or a group. `/teams` and
`/teams?all=1` now carry `kind`, `sport`, `league` and `in_lineup`.

---

## 9. Key tunables

The full table with defaults is in the README, generated from `settings.py`.
What the table cannot say:

- `PREWARM_TEAMS` and `MAJOR_LEAGUE_TEAMS` are deliberately separate lists.
  The first costs a serial Chromium launch per entry; the second is a set
  lookup. Do not merge them (§1). Extra league names go in `EXTRA_ALIAS_TEAMS`
  or the console's Overrides.
- `CASCADE_BUDGET` must stay under Dispatcharr's `channel_init_grace_period`
  (60). The schema caps it at 59 for that reason.
- `SEGMENT_TIMEOUT` was tuned for ~6 MB chunks over a ~4 Mbit/s tunnel
  (gotcha #16). A faster link can lower it; a slower one must raise it.
- `FEED_TITLE_ALIASES`, `FEED_SLUG_OVERRIDES` and `SERIES_ALIASES` are code
  tables that the settings file can extend but not shrink. Read gotchas #9,
  #11 and #15 before touching them.

---

## 10. Known limitations

- **No mid-stream failover.** If a source dies during playback the stream ends;
  recovery needs a reconnect, which cascades fresh. (Reconnects caused by the
  proxy merely pausing between chunks are fixed - gotcha #20 - so this now only
  happens when the source actually dies.)
- **Programme durations are estimated** per sport. No upstream API supplies a
  scheduled end time for most sports.
- **Cold start is 13–44s**; pre-warmed favorites are ~5s.
- **Upstream home/away fields are sometimes reversed.** Favorites are matched on
  both sides to compensate.
- **The roster only grows.** Every team ever seen keeps its channel. The
  Jellyfin lineup is the visibility control; it does not delete streams or
  channels.

---

## 11. First-boot behaviour: how channels 1-64 fill in

The MLB/NFL/RedZone/NFL Network ordering is **applied**, not inherent. Nothing
in `app.py` knows or cares about channel numbers — the playlist is emitted
alphabetically, Dispatcharr numbers each channel once when it creates it, and
`reorder_channels.py` (§5j) is what rewrites those numbers into league blocks.
Three consequences on a fresh system:

**1. A channel must exist before it can be numbered.** `reorder_channels.py`
matches on `tvg_id`, so it can only place teams already in the roster. Its
dry-run prints what it found, e.g. `MLB 30 channels`, `NHL 13 channels (21 not
present yet)`.

**2. What you get depends entirely on `teams.json`:**

| | Brought `teams.json` | Started fresh |
|---|---|---|
| Channels at first sync | ~1,344 | ~22 (pre-warm teams + 9 series + 6 feeds + 4 pool) |
| MLB block | all 30 immediately | fills in as games are listed — a few days in season |
| NFL block | all 32 immediately | NFL plays weekly, so realistically **a full week** |
| NFL RedZone / NFL Network | present (feeds are seeded at startup) | present — feeds seed immediately |
| Run `reorder_channels.py` when | straight away | after the blocks are full |

Feeds and series are the exception: `_seed_nonteam` creates them at boot from
config, so RedZone and NFL Network get channels on the very first sync whether
or not they are airing. Teams do not — they appear only once observed.

A brand-new `/data` also gets `seed/lineup.json` (MLB / NFL / NHL / NBA
allowlist). Those majors become visible Jellyfin channels; stations, series,
pool slots and everything else stay streams until added. **This box already
had a roster**, so it must not receive that seed. Missing `lineup.json` here
means implicit-all.

**3. Running the reorder early is not harmful, just temporary.** With, say, 9 of
30 MLB teams known, you get MLB 1-9, NFL 10-…, and the feeds land right after
whatever exists. Every re-run renumbers from scratch, so numbers keep moving
until the rosters settle. That is the same reason `--all-leagues` holds NHL/NBA
back. If stable numbers matter to you from day one, bring `teams.json`.

> A fresh install will also show far fewer than 1,344 channels for a while, and
> many teams will read "No game scheduled" — both are expected. The roster is
> cumulative: it reflects every team ever *seen*, not every team that exists.

---

## 12. Multi-view (built, not yet deployed)

Two fixtures composited into the one MPEG-TS stream a channel can carry: a
primary filling the frame, an optional secondary in a miniplayer corner. The
full plan, the measurements behind it and the phase gates are in
`docs/internal/PENDING_multiview.md`. **Phases 0-11 are done, and since
2026-09-23 it is enabled on this box** - Multi-Player 1 and 2 are channels
65 and 66 - with the full-game soak of Phase 12 still to run: a configured
slot plays, survives being tuned, and can be changed while it plays. Two
feeders fill two FIFOs, ffmpeg composites them, and the result is served as
one MPEG-TS - with the response committed before anything resolves and the gap
padded with null packets. Corner, size and the mixer move on the running
encoder over ZMQ; a channel change rebuilds it without breaking the stream.
The console has a **Multi-player** section driving all of it over
`/api/multiview`, and the numbering block has been applied (§12b).

**It is off unless `MULTIVIEW_ENABLE=1`**, and off means nothing is seeded,
nothing is written under `/data`, and the only trace in the log is a
`0 multi-view` count on the existing seeding summary line. Verified by
diffing `/health`, the playlist, `/epg.xml` and the console HTML against a
build from before the feature existed: identical apart from the generation
timestamp each carries anyway.

What exists today:

- Nine `MULTIVIEW_*` settings (README table). Two are environment-only:
  `MULTIVIEW_FILE` and `MULTIVIEW_RENDER_NODE`.
- `/data/multiview.json`, written through `settings.atomic_write_json`, so it
  inherits the same `.bak` and `.corrupt-<time>` handling as the roster. It
  materialises on the first write, not at startup — a missing file is a valid
  empty document, exactly like `lineup.json`.
- `multiview.py`, which owns the document and nothing else.
- A **Multi-player** section in the console, between Active streams and
  Pre-warm: slot tabs, a channel picker per side, a stage diagram drawn from
  the encoder's own geometry, the corner grid, the size chips, the mixer and
  a status strip with the running encoder and a Stop. Present only when the
  feature is on (pitfall #28).
- Two channels when enabled, `Multi-Player 1` and `Multi-Player 2`:

  | | |
  |---|---|
  | Slug | `multi-player-N` |
  | Channel id | `streamed.feed.multi-player-N` — the existing non-team prefix |
  | Group | `Multi-view` |
  | URL | `/stream?multi=N` |
  | Guide | `<Primary> + <Secondary>`, or `Multi-view not configured` |

**When this first deploys, check the two Dispatcharr-side facts before
anything else**: that the sync container created the channels, and that each
has guide data attached. Gotcha #4 gives no second chance — a channel created
before its guide entry exists stays blank forever, and the repair is deleting
and recreating it.

### 12a. The composite path, end to end

Everything else in the service is a byte pipe. This is the one place that
decodes, composites and re-encodes, so it is worth knowing the whole route a
tune takes before touching any part of it:

```
 Dispatcharr ──GET /stream?multi=N──▶ _multiview_stream      404 unknown slot,
                                        │                     503 unconfigured
                                        ▼                     (in memory, instant)
                               _multiview_body ── response committed at once;
                                        │          null packets pad the wire
                                        │          until the first picture
                                        ▼
                         composite.Manager ── MULTIVIEW_MAX_ACTIVE, reaper,
                                        │     reconciler against multiview.json
                                        ▼
                     Composite (one ffmpeg) ◀── ZMQ over ipc:// in /dev/shm:
                        ▲             ▲         corner, size, mixer, live
          FIFO mvN-primary-<tok>.ts   FIFO mvN-secondary-<tok>.ts
                        ▲             ▲
                 Feeder (primary)   Feeder (secondary)
                        │             │
              GET /stream?team=…   GET /stream?team=…    loopback: the ordinary
                        │             │                  proxy path, cascade,
                        ▼             ▼                  extract cache, keepalives
                     upstream CDN   upstream CDN
```

In order, when a player tunes a configured slot:

1. **The response is committed before anything resolves.** `_multiview_body`
   writes a null packet straight away and keeps padding at under a second
   between packets, because Dispatcharr's budget after the first byte is ten
   seconds and a cold composite can take longer than that (pitfall #16). The
   bytes are handed over through a bounded queue by a worker thread, so a
   stuck encoder can never hold a request thread hostage.
2. **The Manager starts one composite**, or refuses a second beyond
   `MULTIVIEW_MAX_ACTIVE`. It owns the reaper - no viewer for
   `MULTIVIEW_IDLE_TIMEOUT`, or no output for `OUTPUT_STALL_TIMEOUT` (20s),
   and the encoder is stopped - and the reconciler, which compares every
   running composite against `multiview.json` so that a hand edit to the file
   takes effect exactly like a console change.
3. **Two feeders fill two FIFOs** in `MULTIVIEW_FIFO_DIR` (`/dev/shm`). Each
   reads `/stream?team=<slug>` over loopback as an ordinary viewer (pitfall
   #8), resolving while ffmpeg is still opening the other input (#18). Until
   an input has carried a real stream it is fed a looping black-and-silence
   clip; after that, never (#20). A per-feeder watchdog cuts a source that
   goes quiet for `SOURCE_STALL_TIMEOUT` (15s), because one silent input
   freezes the whole picture (#17).
   Each feeder **reads ahead on its own thread and writes through a
   `_Pacer`**, which releases the stream at the speed its own PCR says,
   rewrites every PCR, PTS and DTS onto one real-time timeline shared by
   both inputs, and renumbers continuity counters so segment joins, filler
   loops and reconnects look like one stream (#34-#37).
4. **ffmpeg composites.** VAAPI decode, software `scale` / `overlay` /
   `volume` / `amix` (the only filters that obey live commands, #2),
   `h264_vaapi` at CQP (#1), 1080p60, MPEG-TS on stdout. It reads the
   streams' own timestamps, which the feeders have made continuous (#14).
5. **Changes arrive through `PUT /api/multiview/<n>`**, are merged into the
   stored slot (#24), and then either go to the running encoder over ZMQ -
   corner, size and mix, with no break in the picture - or, for a channel
   change, stop it; the viewer's stream re-attaches and the rebuilt composite
   picks up the new channels (#20, #21). `MULTIVIEW_REATTACHES` bounds how
   many times one viewer may do that, and `MULTIVIEW_START_TIMEOUT` how long
   each rebuild may take to show a picture.
6. **Picking a channel warms it**, there and then (#25), and configured slots
   go to the front of the pre-warm list (#26). That is the difference between
   a tune reaching picture in about three seconds and in thirty to fifty.

Measured on this box: first picture **1.7-2.9s** with both sides warm, 13-27s
cold, ~50s worst case; about **1.4 cores** at live pace for one 1080p60
composite; output **0.5-1.3 Mbit/s** at `qp=23` depending on how much the
pictures move. After the pacing fix (2026-09-23, production, NFL Network +
Tennis Channel): exactly **1.0s of video per wall-clock second**, a 0.1s
swing against the real clock, **every frame of the main picture a new
one**, and the container's CPU a steady ~1.1 cores (±0.2) where it had
swung 0-1.8 in a five-second saw-tooth. Earlier, through Dispatcharr in
production (2026-09-23, 12 minutes,
every control exercised, an ordinary channel playing alongside): first byte
at the player **6-10s** with both sides warm, **~2.5 Mbit/s** out, the
container at 0.68 core mean and 1.86 peak (Chromium pre-warm and the other
channel included), the GPU's Video engine at 29% mean and 69% peak. A
full-game figure is Phase 12's to record.

### 12b. Numbering

With the feature on, the two channels are seeded at boot like the feeds, so
they exist from the first sync. `reorder_channels.py` then puts them at
**65-66**, straight after NFL Network, and moves every channel that was at
65 or later down by exactly two - once. Until the channels exist the block
matches nothing and changes nothing; the dry-run just reports
`Multi-view 0 channels (2 not present yet)`.

The block names the channels by id and assumes the defaults,
`MULTIVIEW_NAME=Multi-Player` and `MULTIVIEW_SLOTS=2`. Change either and the
ids change with it (`multi-player-N` is derived from the name), so the order
needs `--config` with the new ids.

`tools/check_reorder.py` proves the numbering offline in a second: the block
is inert without the channels, lands on 65-66 with them, shifts everything
else by two and nothing else, and puts hidden rows after everything visible
so no number is ever shared (gotcha #26).

### 12c. Pitfalls

Everything below cost real time to find. Most of it is invisible from reading
the code, which is why it is written down rather than left to comments.

**1. `MULTIVIEW_QP` is a quantiser, not a bitrate.** The Intel driver in this
image supports CQP and nothing else; `-b:v` fails outright with
`Driver does not support any RC mode compatible with selected options`.
Switching to a bitrate means adding `intel-media-va-driver-non-free`.

**2. The composite must use software `scale` and `overlay`.** `vpp_qsv` and
`overlay_qsv` are faster and are the obvious optimisation, and they **accept
live commands and silently ignore them** — proven by byte-identical output
with and without the command. Moving the graph onto them would break the
miniplayer position, size and the audio mixer with no error anywhere.
`MULTIVIEW_ENCODER` offers `vaapi` and `cpu` for this reason; there is no
`qsv` choice.

**3. A slot's rules are enforced in `multiview.normalize_slot`, not in the
console.** A secondary without a primary is dropped, a slug that is not in the
roster is dropped, a slug cannot be shown against itself, and gains are
clamped to 0-100. The console is not the only way into that file — it is
hand-editable — so the rules live where the document is parsed.

**4. `MULTIVIEW_MAX_ACTIVE` cannot exceed `MULTIVIEW_SLOTS`**, enforced as a
cross-rule in `settings.validate`. One 1080p60 composite costs roughly 1.4 of
this box's 4 cores, so the default of 1 is a measurement, not caution.

**5. `_multiview_slug()` is the only place a composite slug is derived**, and
everything goes through it: seeding, the playlist, the guide, the stream route
and the roster lookup. Gotcha #12's real lesson is not "be careful in five
places" but "have one place". It routes through `_feed_slug`, so a slot can be
pinned in `FEED_SLUG_OVERRIDES` if `MULTIVIEW_NAME` ever changes — the slug is
a live channel's address once the channel exists.

**6. `_MULTIVIEW_BY_SLUG` is empty when the feature is off.** That is what
keeps the playlist, the guide and the seeding inert without each one carrying
its own `if MULTIVIEW_ENABLE`. Like `_PREWARM_SLUGS` it is derived after
`settings.apply`, so defining it higher in the file is a crash on start.

**7. The multi-view reload runs after `_seed_nonteam`, not before.** A slot may
point at a feed or a series, and those roster entries are created there;
validating against the roster a moment earlier would silently drop a good
RedZone slot on first boot for being read too soon.

**8. The feeders read `/stream?team=` over loopback on purpose.** It looks
like an obvious inefficiency to remove — call the segment path directly and
skip the HTTP hop. Do not. The segment path is where gotchas #16, #17 and #18
live, it is defined inside the `stream_proxy` request closure, and reading it
back as an ordinary viewer is what keeps every one of those behaviours intact
without re-testing them. The hop costs nothing at these bitrates, and each leg
gets the cascade, the extract cache and the keepalive padding for free.

**9. Nothing in a feeder may block without a way out.** Two real ways it can,
both found live and both fixed: opening a FIFO for writing blocks until a
reader attaches (opened non-blocking and retried), and a cold channel sits
inside one blocking request for most of `CASCADE_BUDGET` (the connect runs on
its own thread, polled). Add a blocking call here and a switch will silently
take a minute to happen.

**10. A switch to a cold channel is a cold extraction of silence.** The FIFO
stays open and the encoder keeps running, but that input goes quiet for ~20s.
Warm channels switch in about 5. This is why the console selection has to
pre-warm rather than just record a choice.

**11. The privilege drop throws away supplementary groups.** `setpriv
--groups` *replaces* the whole list and `--init-groups` rebuilds it from
`/etc/group`, where a host gid does not appear. So a group granted from
outside — compose `group_add`, docker `--group-add` — is gone the moment
`entrypoint.sh` drops privileges unless the script names it. The render node's
gid is looked up there for exactly this reason. Miss it and ffmpeg reports
**"No VA display found"**, which reads like a missing device rather than a
missing group and will cost you an afternoon.

**12. An input FIFO must never go silent, even when there is nothing to
show.** ffmpeg blocks probing an input that has produced no data, so an
unconfigured or unresolvable source would stall the entire graph rather than
leaving a hole in the picture. A feeder with nothing to play writes a looping
black-and-silence clip instead. This is also what makes a dead primary behave
sensibly: that side goes black and the miniplayer keeps playing.

**13. ffmpeg opens its inputs sequentially.** It will not open the second FIFO
until the first is open and probed, so the second feeder routinely waits out a
whole cold extraction before its reader appears. Anything that gives up on a
timer while waiting for a reader will leave that input unfed for the life of
the composite.

**14. Do not stamp the inputs by arrival (`-use_wallclock_as_timestamps`).**
This pitfall used to say the opposite, and that advice made the composite
unwatchable (2026-09-23). The problem it solved is real - every source
switch, reconnect and filler loop restarts that stream's own clock, and
replaying those values gives `DTS out of order` - but arrival stamping
caused two worse ones: it turned burst delivery into a slideshow (#34), and
it starved the main input outright (#35). The feeders now restamp every
packet onto one continuous real-time timeline instead (`_Pacer`), which is
the continuity arrival time was standing in for, and ffmpeg reads the
streams' own clocks.

**15. One composite at a time, and never one without an audience.**
`MULTIVIEW_MAX_ACTIVE` is enforced by refusing the second, not by queueing it,
and a reaper stops any composite with no viewer for `MULTIVIEW_IDLE_TIMEOUT`.
An encoder is the most expensive thing this service does; a forgotten one
costs a third of the box indefinitely.

**16. A multi slot commits its response before it knows anything.** A cold
composite is tens of seconds from its first frame and no tuner waits that
long, so `/stream?multi=` returns 200 immediately and pads the wire with
`TS_NULL_PACKET` until the picture exists. The padding **must not leave a gap
over ~10s**: Dispatcharr grants its 60s init grace only while its buffer is
still empty, and the first padding byte ends that - from then on the budget is
`CONNECTION_TIMEOUT`, which is 10s. Raising `STREAM_KEEPALIVE_INTERVAL` above
about 8s would break multi-view in a way that looks like a source fault. The
price of committing early is that a failure can no longer be reported: a slot
that will not start pads for `MULTIVIEW_START_TIMEOUT` and then ends.

**17. One silent input freezes the whole composite, not half of it.** ffmpeg
will not produce a frame without both inputs, and a source that stops
producing *without closing* leaves its feeder blocked in a read that never
returns - so neither end notices. Measured live: a secondary that stalled
thirteen seconds in held the composite frozen for 55 minutes while the primary
poured 3.8 GB into a queue nobody could use. Each feeder watches its own
source and cuts one quiet for `SOURCE_STALL_TIMEOUT`. The cut works by closing
the *transport*, which is why every source must be a closable wrapper: a bare
generator cannot be closed while it is executing.

**18. Resolve while waiting for the reader, never after it.** Pitfall #13
says ffmpeg opens its inputs sequentially. The consequence is that a feeder
which waits for its reader before starting to resolve makes the composite's
two cold starts *stack*: 50s to picture, with the secondary not beginning its
own extraction until the 30s mark. Overlapping them halves it. The early
attempt has to retry on failure too - a transient upstream 503 on the first
try is common, and without a retry the overlap buys nothing.

**19. A reader that cannot be interrupted is a viewer that never leaves.**
`Composite.read()` takes its caller's stop event and polls, rather than
blocking in `stdout.read()`. Blocking there means a wedged encoder keeps its
readers counted as an audience for ever - four phantom viewers were once sat
on one slot - and pitfall #15 becomes quietly false, because the reaper can
never fire.

**20. An input carries one kind of thing for its whole life.** ffmpeg
configures each input's decoder from the first stream it sees, and anything
spliced on afterwards that does not match is not decodable *on that input* -
which freezes the entire composite, not just that side of the picture,
silently, with nothing logged anywhere. So filler is for an input that has no
channel at all and never will; once a real stream has gone in, nothing else
ever does (`Feeder.carried_source`). This is the rule behind three separate
bugs: filler played during a connect broke every cold start, filler played
after a source died would have frozen the picture, and **swapping a channel on
a running encoder is simply not possible**. Verified live: a healthy 13.8 MB
composite stopped dead the instant its secondary changed and never produced
another byte.

**21. A channel change therefore rebuilds the encoder, and that is fine.**
`Composite.apply` returns `restart` instead of retargeting a feeder; the
viewer's `_multiview_body` loops, re-attaching to the replacement and re-
reading the stored configuration, so the **HTTP response is never broken** -
downstream sees a re-buffer, not a channel that died, because the padding
covers the gap exactly as it covers a cold start. Corner, size and the mixer
are the things that are genuinely seamless. Do not "optimise" a channel change
back into a feeder switch.

**22. A live command only lands while frames are moving.** The `zmq` filter
services its socket between frames, so a composite whose output nobody is
draining - or whose input has gone quiet - answers nothing at all, and every
command costs a full `ZMQ_TIMEOUT`. Batches abandon on the first timeout and
set `needs_resend` so the reconciler retries; a partial application is the
dangerous state, because the stored value has moved on and the picture has
not.

**23. The control endpoint is a Unix socket, and its address needs two
backslashes before every colon.** A TCP port would be reachable by everything
else in the VPN's network namespace, which this container shares. The double
escaping is because the string is unescaped once by the filtergraph parser and
again by the filter's own option parser; one backslash fails as `No option
name near '//127.0.0.1'`, which reads like a malformed URL rather than an
escaping bug.

**24. `merge_slot`, never `set_slot`, for anything a person asked for.**
Callers send the one thing that changed. `set_slot` replaces the whole slot,
so a "move the miniplayer" that went through it would silently drop both
channels - which does not look like a bad request, it looks like the stream
dying for no reason.

**25. Picking a channel has to start resolving it, there and then.** This is
the feature, not a speed-up: a cold composite is two cold extractions end to
end, measured at up to 50s, and a selection that merely records a preference
leaves every one of those seconds on the tune. With both sides warm the same
tune reaches picture in **7.8s**. `_warm_now` runs it off the request thread
and behind `_warm_gate`, so it queues behind the pre-warm loop rather than
starting a second Chromium.

**26. The warm list is capped and multi-view goes first.** Warming is serial
at 20-25s per entry, so `PREWARM_MAX_ENTRIES` (12) is already a five-minute
cycle. Configured slots are warmed whether or not anyone is watching - the
slot nobody has tuned yet is exactly the one about to be tuned - and when the
list has to be cut it is the favourites that go, because a slot is a choice
somebody made a moment ago.

**27. `/api/multiview` never contains a resolved URL.** `/stream/status` is
gated *because* of the signing tokens in its URLs. Rather than depend on a
gate for the same reason, this payload simply has none, and the harness greps
the serialised JSON to keep it that way. It is still gated with the rest of
the console; that is belt and braces, not the plan.

**28. The console section is absent from the markup when the feature is off,
not hidden - and the whitespace markers are part of that.**
`{%- if multiview_enabled %}` in `templates/dashboard.html` is what keeps the
console HTML byte-identical to a build predating the feature, which is the
diff the rollout promise is actually checked against. Written without the
`-` markers it is *not* identical: Jinja leaves the blank line the tag sat on,
twice, and the promise quietly becomes "identical apart from some
whitespace". Hiding the section with CSS or script would be worse again -
copy describing a feature the build does not have. `dashboard.js` looks for
`#mv-panel` and no-ops when it is missing, so the script needs no flag.

**29. A poll must never re-render a control that is in use.** The section
re-renders every five seconds from `/api/multiview`. `mvHolding()` suppresses
that whenever an input inside `#multiview` has focus, and the picker's search
box re-renders only the list beneath it - otherwise a poll would eat what was
being typed, and a slider would jump back under the pointer mid-drag. The
mixer writes on `change`, not `input`, so one drag is one command to the
encoder rather than twenty. Same discipline as the configuration panel, for
the same reason.

The subtler half of the same problem: a poll that was **already in flight**
when a write landed carries the state from before it, and applying it puts
the old value back on screen for five seconds - a click that visibly undoes
itself. Each poll is tagged with the time it was sent and discarded if that
is older than the last write. It showed up as a one-in-several-runs flake in
the browser harness, which is the only reason it was found at all.

**30. A multi-player channel cannot be a source for a multi-player channel.**
With the feature on, the composite channels are themselves on the roster, so
the picker would list them and `_multiview_api_update` would have accepted
them: a composite whose input is a composite that only starts once something
tunes it. That deadlock looks like a hang rather than a mistake, so it is
refused by name in the API and filtered out of the picker. Found by building
the picker, not by reading the code.

**31. The mixer slider is the one component `DESIGN.md` does not describe.**
That document records under Known Gaps that no slider was ever surfaced, so
the control is an extrapolation and is commented as one in `dashboard.css`.
It is built only from parts the system does define - the pill radius, the
hairline, the single accent, a circular thumb - so that if the gap is ever
filled upstream this is the shape most likely to agree with it. Two rules the
section is the likeliest place in the console to break are asserted by the
harness rather than trusted: no shadow anywhere in its styles (the system's
one shadow belongs to product photography) and no colour of its own (the
secondary stream is told apart by label and position, never by hue).

**32. A cut source kills the whole graph when decoding is on the GPU.** When
the watchdog cuts a frozen source (#17), the feeder splices a fresh
connection into the same running input. That stream begins mid-packet
(`PES packet size mismatch`); a software decoder would conceal the damaged
frame, but VAAPI reports `Failed to sync surface ... internal decoding
error`, `hwdownload` returns EIO, and ffmpeg ends the filter graph. The
viewer's re-attach rebuilds the composite 5-6s later, so nothing needs a
person - but a frozen upstream costs the freeze plus about ten seconds, and
it is the "unexplained exit" first seen in Phase 9. Seen twice in twelve
minutes on a night of flaky feeds (2026-09-23). Whether to rebuild
deliberately at the moment of the cut instead is an open Phase 12 decision.

**33. The ordinary channel you test "alongside" must be a different game.**
A team channel and the composite side showing the same fixture read the
same upstream stream, so a hiccup there lands on both and looks exactly like
the composite starving its neighbour. The first production run made that
mistake, and its one gap on the ordinary channel cannot be attributed
either way.

**34. A team stream arrives a segment at a time, so the feeders pace it.**
The proxy hands over a whole HLS segment - about five seconds of video,
~4 MB - in 10-20 ms, then nothing but keepalive padding for 4.5-5s, every
time (measured). A composite fed that directly gets five seconds of video
as one instant: the first person to watch it called it unwatchable, and
the container's CPU swung 0-180% of a core in a five-second rhythm. Each
feeder now reads ahead on its own thread and releases on the stream's PCR.
Nothing in Phases 0-11 caught this, because the harnesses' synthetic
sources arrive smoothly and the live checks measured bytes, not motion.
`tools/check_pacing.py` reproduces the real delivery and reads a frame
number back out of every output frame: 2% of frames advanced normally
before, 100% after.

**35. Arrival stamping starves the main input, whatever the feeders do.**
With `-use_wallclock_as_timestamps` on both inputs, ffmpeg read 8 seconds
of the main input in 30 while reading the miniplayer in full, and its
`fps` stage duplicated 942 frames to fill the gap - reproduced with the
bare argv and plain FIFO writers, no feeder code at all. Remove the flag
and both inputs are read at full speed. This is the other half of what was
seen as jitter, and why #14 was reversed rather than worked around.

**36. Renumber continuity counters; nothing upstream is lossy.** Every HLS
segment, filler loop and reconnect restarts its counters. ffmpeg flags the
first packet after each jump as corrupt, and the h264 decoder re-initialises
and loses frames - production logged a "corrupt input packet" at every
segment boundary. The bytes come over TCP from our own proxy, so the
pacer renumbers per stream for the input's life and the fault disappears.

**37. A new timeline continues the last one, behind schedule or not.** When
a filler loop restarts or a source reconnects, the pacer starts the new
timeline straight after the previous one ended. Two earlier versions froze
the picture: starting from "now + cushion" left a hole at every two-second
filler loop, and starting from "now if behind" turned the startup backlog -
ffmpeg stops reading one input for ~5s while it opens the other - into a
four-second timestamp jump at every join. Only an outage longer than
`PACE_MAX_BEHIND` (10s) starts a fresh schedule.

**38. Judge motion on the main picture, never on whole frames.** The first
production measurement of the jitter counted 93% of frames as new pictures
and looked healthy - because the *miniplayer* was moving. The main picture,
in a region clear of the miniplayer, was 67% new with freezes of two
seconds. Measure a crop of the main picture, or better, a picture that
numbers its own frames.

**39. Stop holds the slot; a cleared slot ends its stream.** A playing
viewer's stream re-attaches when its composite ends (#21) - which, until
2026-09-23, included a composite ended by the console's Stop, so Stop was
undone within a second, and clearing a slot rebuilt an encoder with no
channels in it. Stop now sets the same hold Disconnect uses for a team
stream (`STREAM_DISCONNECT_HOLD`, 90s): viewers are dropped, reconnects are
refused, and any change to the slot from the console lifts the hold.

**40. Backpressure is not a stall.** When nobody reads a composite's output -
the viewer has left and the reaper has not yet stopped it - ffmpeg stops
reading its inputs, the feeders' queues fill, and every idle clock stops.
The watchdogs took that for dead sources and "cut" them every two seconds
for a minute. A reader held back by a full queue now counts as fed, and a
writer blocked on a full FIFO is not "quiet": in both cases the encoder is
the one not reading, and cutting an input cannot help. The encoder's output
watchdog and the idle reaper own that case. `check_pacing.py MODE=noreader`
- 6 false cuts before, 0 after.

**41. A channel change stops the composite; it does not start another.**
The viewer's stream re-attaches within a second and builds the new one
from the stored slot, or ends if the slot was cleared (#39). `Manager.apply`
used to start one too, which raced the viewer when there was one and, when
there was not, built an encoder nobody would read - clearing an idle slot
started one with no channels in it.
