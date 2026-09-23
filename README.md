# streamed-m3u

A self-hosted live sports IPTV source. It turns a sports-streaming catalog into
a stable set of permanent channels, one per team, racing series or 24/7 feed,
and serves them as an M3U playlist with an XMLTV guide for
[Dispatcharr](https://github.com/Dispatcharr/Dispatcharr), Jellyfin, or any
IPTV client.

The key idea: a channel's address contains no match id. `Philadelphia Eagles`
is always `/stream?team=philadelphia-eagles`. Whichever fixture that team has
today is resolved when you press play, so channel numbers never shuffle and the
guide keeps working as fixtures come and go.

**What happens on a click:** your player asks Dispatcharr for the channel,
Dispatcharr asks this service, a headless Chromium session extracts the real
stream URL from the embed page, the segments are fetched with a browser TLS
fingerprint, and the result is re-served as a plain MPEG-TS stream. Cold start
is 13 to 45 seconds; favourites kept warm in the background start in about 5.

**Optional: multi-view.** With `MULTIVIEW_ENABLE=1` you also get two
**Multi-Player** channels, each carrying two fixtures at once: one filling the
frame, the other in a miniplayer you place and size from the console, with a
mixer for which one you hear. The picture is composited on the server, so it is
an ordinary channel to Dispatcharr and to every player, and the arrangement is
the channel's own rather than per viewer. It needs an Intel iGPU to be cheap;
see [Multi-player](#multi-player).

## Requirements

- Docker with Compose v2, on `linux/amd64`. The image bundles a Playwright
  Chromium build and is only published for amd64.
- A WireGuard VPN subscription for the `gluetun` sidecar (AirVPN by default;
  any provider gluetun supports works). Optional: see the no-VPN variant.
- Dispatcharr, run from the same compose file or already on your network.
- About 1.7 GB of disk for the image and a little RAM headroom for Chromium.
  The image bundles ffmpeg and the Intel VAAPI runtime.
- For multi-view only: an Intel iGPU, with its render node passed to the
  container (`/dev/dri/renderD128`; uncomment `devices:` in the compose file).
  One 1080p60 composite costs about 1.4 CPU cores even with the GPU encoding,
  because the scaling and overlay run in software. Without a GPU,
  `MULTIVIEW_ENCODER=cpu` works, at a much higher CPU cost.

## Quick start

```sh
git clone <this repository> streamed-m3u && cd streamed-m3u
cp .env.example .env
# Fill in WIREGUARD_PRIVATE_KEY, WIREGUARD_ADDRESSES,
# DISPATCHARR_USER and DISPATCHARR_PASS. Everything else is optional.
docker compose up -d
```

Then:

1. Open the console at `http://<host>:8787`. It shows the roster filling in,
   the guide, caches, live streams and every setting.
2. In Dispatcharr (`http://<host>:9191`), add an **M3U account** with the URL
   `http://gluetun:8787/playlist-teams.m3u` and an **EPG source** with
   `http://gluetun:8787/epg.xml`. The names you give them must match
   `M3U_ACCOUNT_NAME` and `EPG_SOURCE_NAME` in `.env` (the defaults are
   `streamed.pk teams` and `streamed.pk teams EPG`).
3. In Dispatcharr's stream settings, set the channel init grace period to
   `60` seconds. The service's `CASCADE_BUDGET` is tuned to finish under it.
4. The `streamed-m3u-sync` container creates a Dispatcharr channel for every
   playlist entry every `SYNC_INTERVAL` seconds (default 8 minutes) and links
   the guide. It never deletes a channel; that is deliberate.
5. Optionally number the channels so your leagues come first:
   ```sh
   docker exec streamed-m3u-sync python tools/reorder_channels.py --dry-run
   docker exec streamed-m3u-sync python tools/reorder_channels.py
   ```
   The built-in order is MLB, NFL, NFL RedZone, NFL Network, the two
   Multi-Player channels when multi-view is on, then everything else. Print it
   with `--dump-config`, edit the JSON, and pass it back with `--config` for
   your own order. Every run also closes gaps and pulls in channels created
   since the last one, so check the dry-run's ranges before applying.
6. Point Jellyfin (or your player) at Dispatcharr's M3U and XMLTV outputs.

### Without a VPN

If your VPN runs on the router or host, or you do not need one:

```sh
docker compose -f docker-compose.novpn.yml up -d
```

The service publishes port 8787 itself. Dispatcharr URLs become
`http://streamed-m3u:8787/playlist-teams.m3u` and `http://streamed-m3u:8787/epg.xml`.

## Configuration

Three layers, highest priority first:

1. **`/data/settings.json`**, written by the console when editing is enabled.
2. **The environment**: `.env`, compose, or your platform's app config.
3. **Built-in defaults.**

The settings file wins so that a change made in the console has an effect. The
console shows which layer each value came from, and resetting a setting in the
console removes it from the file and restores the environment or default value.
An empty environment value counts as unset.

A few settings are environment-only and never written to the file: `PORT`,
`TEAMS_FILE`, `EXTRACT_CACHE_FILE`, `LINEUP_FILE`, `DATA_DIR`,
`CONSOLE_PASSWORD`, `PUID` and `PGID`.

### Editing from the console

Set `CONSOLE_PASSWORD` to enable editing. Without it the console is read-only
and has no login. With it, the page, its API and `/stream/status` (which
exposes resolved CDN URLs) require signing in; the playlists, guide, `/health`
and the stream proxy stay open because Dispatcharr and the Docker healthcheck
consume them.

Each setting is marked with when a change takes effect: **live** (immediately),
**next rebuild** (on the next playlist refresh), or **on restart**. Changes that
need a restart are saved and shown in a banner until the service restarts.

The **Overrides** section lets you extend the built-in tables: extra league
names for away-side resolution, and alias maps for feeds and series. These are
additions only. A built-in entry cannot be removed from the console.

### Multi-player

With `MULTIVIEW_ENABLE=1` the console gains a **Multi-player** section between
Active streams and Pre-warm, one tab per slot. Choose a channel for the main
picture and, optionally, a second one for the miniplayer, then set the corner
it sits in, how large it is, and how the two are mixed. A stage diagram shows
the arrangement as the encoder will build it.

Three things are worth knowing before using it:

- **The layout and the mix belong to the channel, not to the viewer.** The
  picture is composited server-side and sent as one stream, so everyone
  watching sees the same arrangement and hears the same mix.
- **Nothing plays until a main picture is chosen.** An unconfigured slot
  refuses to tune, exactly like a team with no fixture on, and the miniplayer
  cannot be chosen first — it is defined relative to a main picture.
- **Picking a channel starts resolving it immediately**, so tuning shortly
  afterwards is quick: about eight seconds, against half a minute cold.

The corner, the size and the mixer are applied to a picture that is already
playing. Changing either channel is not — see Limitations.

Without `CONSOLE_PASSWORD` the section is shown but every control is inert,
the same as the rest of the console.

To turn it on: pass the render node to the container (see Requirements), set
`MULTIVIEW_ENABLE=1` in the console or the environment, and restart. The
channels appear on Dispatcharr's next sync. To number them, run the reorder
(Quick start, step 5): they take **65-66**, after NFL Network, and every
channel from 65 on moves down two, once. `MULTIVIEW_ENABLE=0` and a restart
turns it off again: the channels leave the playlist, and Dispatcharr keeps
its copies (the sync never deletes a channel) but tuning one fails.

### Addresses

Playlist URLs are built from the address each request arrives on, so on a
compose network they resolve to `http://gluetun:8787/...` (or
`http://streamed-m3u:8787/...`) with no configuration. Set `PUBLIC_BASE_URL`
when clients reach the service through a reverse proxy or a different host.

### User and group

The container starts as root only long enough to take ownership of `/data`,
then drops to `PUID:PGID` (default `1000:1000`). TrueNAS SCALE apps
conventionally use `568`. Set `PUID=0` to run as root.

### Reference

Generated from the schema in `settings.py` (`python settings.py --markdown`).

| Setting | Default | Applies | Description |
|---|---|---|---|
| **Service** | | | |
| `PORT` | `8787` | env only | Port the service binds. Must match the published container port. |
| `PUBLIC_BASE_URL` | `(empty)` | live | Address clients use to reach this service, used in every playlist URL. Empty means derive it from each request, which is right on a shared network. |
| `LOG_LEVEL` | `INFO` | live | Logging verbosity. Also sets what reaches the events panel. |
| `STARTUP_DELAY` | `15` | restart | Pause before the first scrape, giving the VPN tunnel time to come up. |
| `TEAMS_FILE` | `/data/teams.json` | env only | Roster file. The only irreplaceable state the service holds. |
| `EXTRACT_CACHE_FILE` | `/data/extract_cache.json` | env only | Where resolved stream URLs are persisted across restarts. |
| `LINEUP_FILE` | `/data/lineup.json` | env only | Jellyfin lineup file. Missing means every roster slug is visible. |
| **Upstream** | | | |
| `STREAMED_BASE_URL` | `https://streamed.pk` | live | Catalog the service scrapes. Change this when the site moves domain. |
| `REQUEST_TIMEOUT` | `10` | live | Seconds before an upstream API call is abandoned. |
| `BUILD_WORKERS` | `8` | live | Parallel workers for a playlist build. Modest values avoid rate limits. |
| `REFRESH_SECONDS` | `480` | live | Interval between playlist rebuilds. A change waits out the current interval. |
| **Channels** | | | |
| `PREWARM_TEAMS` | `(empty)` | restart | Favourites kept hot. Each costs one serial browser launch, so keep it short. |
| `EXTRA_ALIAS_TEAMS` | `(empty)` | restart | Extra names granted away-side resolution. Adds to the built-in league list. |
| `SERIES_CHANNELS` | `Formula 1, IndyCar, Nascar Cup Series, Nascar Truck Series, MotoGP, Moto2, Moto3, World Rally Championship` | restart | Racing series that keep a permanent channel through the off-season. |
| `FEED_CHANNELS` | `Rally TV, Tennis Channel, Willow Cricket, Fox League, NFL RedZone, NFL Network` | next-refresh | Always-on feeds, named by the title they display under. |
| `POOL_SLOTS` | `4` | next-refresh | Shared channels for one-off events. Lowering it never removes existing channels. |
| `POOL_NAME` | `Live Event` | restart | Display name for the shared event slots. Renaming mints new channels; the old ones stay. |
| `FAVOURITES_GROUP` | `Favorites` | live | Group title favourites are filed under in the playlist. |
| **Multi-view** | | | |
| `MULTIVIEW_ENABLE` | `off` | restart | Composite channels carrying two fixtures at once. Off means no slots are created and nothing can start an encoder. |
| `MULTIVIEW_SLOTS` | `2` | restart | Composite channels to create. Each is a permanent shelf; only MULTIVIEW_MAX_ACTIVE of them may run at once. |
| `MULTIVIEW_NAME` | `Multi-Player` | restart | Display name for the composite slots. Renaming mints new channels; the old ones stay. |
| `MULTIVIEW_MAX_ACTIVE` | `1` | live | Composites allowed to run at once. One encode costs roughly a third of a four-core box, so raising this needs headroom to spare. |
| `MULTIVIEW_ENCODER` | `vaapi` | restart | Encoder for the composite. vaapi uses the Intel iGPU and needs the render node passed in; cpu is the fallback and is much more expensive. |
| `MULTIVIEW_QP` | `23` | live | Composite quality, lower is better. A quantiser rather than a bitrate because the Intel driver here offers no other rate control. |
| `MULTIVIEW_IDLE_TIMEOUT` | `60` | live | Seconds with no viewer before a composite shuts down. An encoder must never outlive its audience. |
| `MULTIVIEW_START_TIMEOUT` | `90` | live | Seconds a cold composite may pad the wire before it is given up on. Both sources resolve one after the other, so this is several times one channel's cascade budget rather than comparable to it. |
| `MULTIVIEW_REATTACHES` | `8` | live | How many times one viewing may rebuild its encoder before the stream ends. Changing either channel costs one, and so does a source that dies; needing more than a few means something is wrong. |
| `MULTIVIEW_FILE` | `/data/multiview.json` | env only | Which fixtures each composite slot points at. |
| `MULTIVIEW_RENDER_NODE` | `/dev/dri/renderD128` | env only | Render node used by the vaapi encoder. The container also needs the host's render group, or opening this fails as 'no VA display found'. |
| `MULTIVIEW_FIFO_DIR` | `/dev/shm/streamed-m3u` | env only | Where the pipes carrying each composite's two inputs are created. Holds no state; a tmpfs is the right home for it. |
| **Pre-warm** | | | |
| `PREWARM_INTERVAL` | `60` | live | How often the pre-warm loop looks for work. |
| `PREWARM_MAX_ENTRIES` | `12` | live | Ceiling on the warm list, favourites and multi-view sources together. Warming is serial at 20-25s per entry, so twelve is already a five-minute cycle; multi-view sources are kept when it has to cut. |
| `PREWARM_MARGIN` | `120` | live | Re-warm once a cached entry drops below this much remaining TTL. |
| `PREWARM_WINDOW_BEFORE` | `20` | live | Minutes before kickoff that a fixture becomes eligible for warming. |
| `PREWARM_WINDOW_AFTER` | `300` | live | Minutes after kickoff that a fixture stays eligible for warming. |
| **Resolution** | | | |
| `BROWSER_TIMEOUT` | `15` | live | Ceiling on one headless extraction attempt. |
| `CASCADE_BUDGET` | `55` | live | Wall-clock ceiling for trying sources. Must stay below Dispatcharr's 60 second grace period. |
| `CASCADE_MAX_ATTEMPTS` | `4` | live | Hard cap on attempts, independent of the time budget. |
| `CASCADE_RESERVE` | `20` | live | Headroom required before a further attempt is allowed to start. |
| `EXTRACT_CACHE_TTL` | `300` | live | How long a resolved stream URL stays reusable. |
| `EXTRACT_CACHE_MAX_ENTRIES` | `100` | live | Cache size cap. Oldest entries evict first. Zero means no cap. |
| `REQUIRE_AUDIO` | `on` | live | Reject a source carrying no audio track and fall through to the next. |
| `NO_AUDIO_TTL` | `1800` | live | How long a confirmed silent source is remembered, so it is not retried. |
| **Playback** | | | |
| `SEGMENT_TIMEOUT` | `30` | live | Per-chunk download ceiling. Must stay above the time a healthy chunk takes. |
| `SEGMENT_PROBE_TIMEOUT` | `20` | live | Same ceiling for the startup liveness probe. Raising it costs cascade budget. |
| `SEGMENT_RETRIES` | `1` | live | Extra attempts for a failed chunk. Small on purpose: retries drag the live edge. |
| `SEGMENT_MAX_MB` | `64` | live | Sanity cap on a buffered chunk, since chunks are held in RAM before forwarding. |
| `STREAM_IDLE_TIMEOUT` | `45` | live | Seconds without a new segment before a stream is treated as dead. |
| `STREAM_KEEPALIVE_INTERVAL` | `1.0` | live | Seconds between null packets sent while waiting on a slow chunk, so the client does not give up. |
| `STREAM_SEEN_MAX` | `300` | live | Segment URLs remembered per stream, bounding memory on long sessions. |
| **Guide** | | | |
| `EPG_WINDOW_HOURS` | `24` | live | How far ahead the guide publishes. |
| `EPG_BACKFILL_HOURS` | `6` | live | How far back the guide reaches, so in-progress matches still appear. |
| `EPG_DEFAULT_MINUTES` | `180` | live | Assumed programme length for sports with no per-sport duration. |
| **Logos** | | | |
| `LOGO_CACHE_TTL` | `86400` | live | How long a successfully fetched logo is served from cache. |
| `LOGO_CACHE_FAIL_TTL` | `60` | live | Short retry window after a logo fetch fails. |
| `LOGO_CACHE_MAX_ENTRIES` | `500` | live | Logo cache size cap. |

## Data

Everything persistent lives in `/data` (mounted from `${CONFIG_DIR}/streamed-m3u`):

| File | What it is |
|---|---|
| `teams.json` | The roster: every channel that exists. It only ever grows. This is the one irreplaceable file; back it up. |
| `teams.json.bak` | The previous save. Used automatically if `teams.json` is unreadable, which is then kept as `teams.json.corrupt-<time>`. |
| `lineup.json` | Which roster slugs Jellyfin can see. Missing means every slug is visible. This box has the file (`policy: all`) after the first −. A new install seeds MLB / NFL / NHL / NBA only. |
| `lineup.json.bak` | The previous lineup save. Same recovery as the roster. |
| `settings.json` | Settings written by the console. Same `.bak` protection. |
| `multiview.json` | Which fixtures each composite slot points at. Only written when `MULTIVIEW_ENABLE` is on and a slot has been configured. Same `.bak` protection; losing it costs a re-pick, nothing more. |
| `extract_cache.json` | Resolved stream URLs, so a restart does not need to re-resolve everything. |
| `.secret_key` | Session signing key for console logins. |

On a fresh `/data` the image installs a **seed roster** of about 1,390 team
channels, so the major leagues exist from the first boot instead of appearing
one by one as fixtures are listed. The seed is a snapshot: team entries only,
no favourites, and no feed or series channels (those are created from
`FEED_CHANNELS` and `SERIES_CHANNELS`). An existing roster is never touched.

The same first-boot copies `seed/lineup.json`: an allowlist of MLB / NFL /
NHL / NBA. Stations, racing series, pool slots, and everything else stay
Dispatcharr streams until you add them in the console. **This box is
different.** It already had a roster, so the first boot did not install the
seed allowlist. The first − wrote `lineup.json` with `policy: all`; every
slug stays on the Jellyfin lineup until excluded. The playlist is never
filtered; visibility is `hidden_from_output` in Dispatcharr.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `/` | The console |
| `/playlist-teams.m3u` | The playlist Dispatcharr subscribes to. Always the full roster. |
| `/epg.xml` | XMLTV guide |
| `/stream?team=<slug>` | Resolve and proxy a team's current fixture |
| `/stream?multi=<n>` | Composite slot `n`, when `MULTIVIEW_ENABLE` is on |
| `/health` | Machine-readable status |
| `/teams`, `/teams?all=1`, `/teams?alias=1`, `/teams?team=<name>` | Roster diagnostics, including `in_lineup` |
| `GET /api/lineup` | Jellyfin lineup policy, counts and groups |
| `PUT /api/lineup` | Add or remove slugs or a group (session and CSRF) |
| `GET /api/multiview` | Multi-view slots, their layout, and whether each side is warm. Carries no CDN URLs. |
| `PUT /api/multiview/<n>` | Change a slot: any of primary, secondary, corner, size, audio (session and CSRF). Picking a channel starts resolving it immediately. |
| `POST /api/multiview/<n>/stop` | Stop one slot's encoder (session and CSRF) |
| `/prewarm` | Pre-warm state per favourite |
| `/stream/status` | Active streams with throughput |
| `/api/overview`, `/api/config`, `/api/cache`, `/api/events` | Console data |
| `PUT /api/settings` | Change settings (session and CSRF token required) |
| `/playlist.m3u` | Legacy per-match playlist, kept as a fallback |

## Limitations

- No mid-stream failover. If a source dies during playback the stream ends and
  the player reconnects, which resolves again from scratch.
- Changing a multi-view slot's channel rebuilds its encoder, so the picture
  re-buffers for a few seconds. The corner, the size and the mixer change
  instantly; the channels cannot, because ffmpeg cannot be handed a different
  stream on an input it has already started decoding.
- Programme durations are estimated per sport; the upstream catalog publishes
  no end times.
- The roster only grows. A team seen once keeps its channel.
- Single process, Flask's built-in server. Fine for a household, not a CDN.
- Depends on a third-party catalog that changes without notice. When it moves
  domain, change `STREAMED_BASE_URL`.
- `linux/amd64` only.

## Security notes

This is a LAN service with no TLS. Do not expose it to the internet directly.
If you must reach it remotely, put it behind a reverse proxy with TLS, set
`PUBLIC_BASE_URL`, set `CONSOLE_PASSWORD`, and set `CONSOLE_COOKIE_SECURE=1`.
Credentials only ever live in the environment; nothing under `/data` contains
a password or VPN key.

## Building and publishing

```sh
docker build --platform linux/amd64 \
  --build-arg STREAMED_M3U_VERSION=1.0.0 \
  --build-arg IMAGE_SOURCE=https://github.com/OWNER/streamed-m3u \
  -t OWNER/streamed-m3u:1.0.0 -t OWNER/streamed-m3u:latest .
docker push OWNER/streamed-m3u:1.0.0
docker push OWNER/streamed-m3u:latest
```

Then set `STREAMED_M3U_IMAGE=OWNER/streamed-m3u:1.0.0` in `.env`. The version
shows in the console footer.

## Maintainer notes

`instructions.md` is the maintainer handover: how the system works internally,
TrueNAS SCALE deployment specifics, and a numbered list of gotchas that each
cost real hours. Read it before changing `app.py`.

## License

MIT. See `LICENSE`.

This project does not host, provide or endorse any stream. It resolves
addresses published by third-party sites for your own player. Whether that is
lawful where you live is your responsibility.
