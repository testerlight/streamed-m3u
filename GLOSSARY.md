# streamed-m3u

A proxy that turns a live-sports provider's changing sources into one stable channel per team, played through Dispatcharr and Jellyfin.

## Language

**Channel**:
A permanent per-team (or per-feed) entry the viewer tunes to, such as NFL Network. It outlives any single game or source.
_Avoid_: Station, feed (a feed is a kind of channel with no team)

**Provider**:
An outside service that lists games and offers sources for them (streamed.pk, ppv.st, streamfree). Several providers can be enabled at once, and a channel's sources may come from more than one.
_Avoid_: Site, API, backend

**Source**:
One provider-side origin of a channel's video for a given game (for example `admin1-HD`). A channel has several candidate sources, ranked, and one is active at a time. Every source belongs to exactly one provider.
_Avoid_: Stream (that is the session), server, link

**Stream**:
One viewing session of a channel: from the player's first byte until it leaves. A stream plays from exactly one source at a time.
_Avoid_: Connection, session

**Chunk**:
One few-second piece of video that a source publishes and the proxy forwards to the player.
_Avoid_: Segment, packet

**Hole**:
A chunk that cannot be delivered (refused, empty placeholder, or left the playlist before it was fetched), so its video is missing from what the player receives.
_Avoid_: Gap, drop

**Cushion**:
The seconds of video sent ahead of what a player playing in real time has used. Empty cushion means a freeze.
_Avoid_: Buffer

**Freeze**:
A period of at least one second when the cushion is empty and the picture stops, whether it later resumes on its own or not.
_Avoid_: Refresh, drop, stutter, pause

**Behind-live delay**:
How far the video the viewer sees trails the live edge of the source. A larger delay is paid for with a larger cushion.
_Avoid_: Latency, lag

**Recovery**:
An automatic action taken during a stream, without the viewer re-tuning, that keeps or restores smooth playback: switching source, re-requesting, or bridging a hole.
_Avoid_: Failover (only one kind of recovery), retry

**Pin**:
A source chosen for a channel and game that stays in use until the game changes or the pin is cleared.
_Avoid_: Lock, favourite
