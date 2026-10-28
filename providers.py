"""
providers: the outside services that list games and offer Sources.

A Provider answers two questions in one common shape, so the rest of the app
never needs to know which service a game came from:

    list_games()          -> [game, ...]      what is on (and coming)
    list_sources(game)    -> [source, ...]    ways to watch one game

streamed.pk is the first Provider and keeps the exact shapes it always had;
ppv.st and streamfree map their one list response onto those same shapes.
Each game and source is stamped with `_provider` (the Provider's name) so a
later step can route a game back to the Provider that listed it.

A Provider also has a short `code` of capital letters for Source labels
("ST" -> `[ST1-720p]`),
and a Source may carry `quality`: the picture height in pixels, when the
Provider says so. A Source without it is of unknown quality.

Every Provider reaches its service through `Mirrors`: an ordered list of
configured base URLs. Nothing outside that list is ever contacted. This
module imports nothing from app.py.
"""

import logging
import re
import threading
from urllib.parse import urlsplit

import requests

log = logging.getLogger("providers")


class ProviderError(Exception):
    """A Provider's list call failed: no answer, 403, "Violation", or a body
    that is not a game list. Distinct from an empty list, which is an answer."""


def clean_bases(*groups) -> list[str]:
    """Configured base URLs in order, de-duplicated, http(s) only.

    Anything else (a bare host, another scheme, empty text) is dropped: a
    domain is only ever used because someone wrote it into the settings as a
    full web address.
    """
    out: list[str] = []
    for group in groups:
        for raw in ([group] if isinstance(group, str) else (group or [])):
            base = str(raw).strip().rstrip("/")
            if base.lower().startswith(("http://", "https://")) and base not in out:
                out.append(base)
    return out


class Mirrors:
    """Ordered base URLs for one Provider.

    A request tries the base that answered last first, then the rest in
    order, so a dead primary costs one failed attempt and is not retried on
    every request that follows. `prefer_first()` starts over from the first
    base; a Provider calls it at the top of each refresh, so a recovered
    primary is back in use within one cycle.
    """

    def __init__(self, bases: list[str]):
        self.bases = list(bases)
        self._good = 0
        self._lock = threading.Lock()
        # Why the latest request found no usable answer ("" after a good one).
        self.last_error = ""

    def prefer_first(self):
        with self._lock:
            self._good = 0

    def get_json(self, path: str, timeout: float, headers: dict):
        """JSON from the first base that answers, or None if none does.

        `last_error` says why, for a caller that must tell a failure from an
        empty answer.
        """
        with self._lock:
            start = self._good
        order = [start] + [i for i in range(len(self.bases)) if i != start]
        self.last_error = "" if self.bases else "no address configured"
        for i in order:
            url = f"{self.bases[i]}{path}"
            try:
                r = requests.get(url, timeout=timeout, headers=headers)
                r.raise_for_status()
                try:
                    payload = r.json()
                except ValueError:
                    # A block page or "Violation" answers 200 with text.
                    snippet = " ".join(str(getattr(r, "text", "") or "").split())[:80]
                    raise ValueError("answer is not JSON: %r" % snippet) from None
            except requests.HTTPError as exc:
                log.warning("API error [%s]: %s", url, exc)
                code = getattr(exc.response, "status_code", 0)
                self.last_error = "HTTP %s from %s" % (code or "error", self.bases[i])
                if code in (404, 410):
                    # "No such thing" is an answer, not an outage: another
                    # mirror would say the same, and one missing path must
                    # not make the app move over to it.
                    return None
                continue
            except Exception as exc:
                log.warning("API error [%s]: %s", url, exc)
                self.last_error = "%s: %s" % (self.bases[i], exc)
                continue
            if i != start:
                with self._lock:
                    self._good = i
                log.info("Using %s from now on", self.bases[i])
            self.last_error = ""
            return payload
        return None


class StreamedPk:
    """streamed.pk: sports, then matches per sport, then streams per source."""

    name = "streamed"
    code = "ST"

    # streamed.pk only says HD or not; these are the heights that stand for it.
    HD_QUALITY, SD_QUALITY = 720, 540

    def __init__(self, bases: list[str], timeout: float, headers: dict,
                 source_names: list[str]):
        self.mirrors = Mirrors(bases)
        self.timeout = timeout
        self.headers = headers
        self.source_names = source_names

    def _get(self, path: str):
        return self.mirrors.get_json(path, self.timeout, self.headers)

    def list_games(self) -> list[dict]:
        self.mirrors.prefer_first()
        sports = self._get("/api/sports")
        live = self._get("/api/matches/live")
        if sports is None and live is None:
            raise ProviderError(self.mirrors.last_error or "no answer")
        sports = sports or []
        log.info("Fetched %d sport categories", len(sports))

        seen: set[str] = set()
        games: list[dict] = []

        def _add(m: dict, category: str):
            if not isinstance(m, dict):
                return
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                m["_category"] = category
                m["_provider"] = self.name
                games.append(m)

        for m in (live or []):
            _add(m, "Live")

        for sport in sports:
            if not isinstance(sport, dict):
                continue
            sport_id = sport.get("id", "")
            sport_name = sport.get("name", sport_id.title())
            if not sport_id:
                continue
            for m in (self._get(f"/api/matches/{sport_id}") or []):
                _add(m, sport_name)

        log.info("Collected %d unique matches", len(games))
        return games

    def list_sources(self, game: dict) -> list[dict]:
        source_map: dict[str, str] = {}
        for entry in (game.get("sources") or []):
            src = entry.get("source", "").lower()
            sid = entry.get("id", "")
            if src and sid and src not in source_map:
                source_map[src] = sid

        found = []
        for src_name in self.source_names:
            src_id = source_map.get(src_name)
            if not src_id:
                continue
            streams = self._get(f"/api/stream/{src_name}/{src_id}")
            if not streams or not isinstance(streams, list):
                continue
            streams = [s for s in streams if isinstance(s, dict)]
            for s in streams:
                s["_match_title"] = game.get("title", "Unknown Match")
                s["_match_id"] = game.get("id", "")
                s["_match_category"] = game.get("_category", "Sports")
                s["_match_poster"] = game.get("poster", "")
                s["_provider"] = self.name
                s["quality"] = self.HD_QUALITY if s.get("hd") else self.SD_QUALITY
            found.extend(streams)
        return found


# ppv.st's sport names, as the app's sport ids (streamed.pk's, which the
# major-league gate reads: "hockey", not "ice-hockey").
_PPV_SPORT_IDS = {"ice-hockey": "hockey"}

_PPV_AT = re.compile(r"\s+at\s+", re.I)
_PPV_VS = re.compile(r"\s+(?:vs\.?|v)\s+", re.I)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def _ppv_teams(title: str) -> dict:
    """{"home": {"name"}, "away": {"name"}} from a ppv.st title, or {}.

    "Away at Home" is the American shape; "X vs. Y" names the home side
    first, as streamed.pk's titles did. Anything else (a feed, a show) names
    no teams.
    """
    for pat, home_first in ((_PPV_AT, False), (_PPV_VS, True)):
        parts = pat.split(title or "", maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            first, second = parts[0].strip(), parts[1].strip()
            home, away = (first, second) if home_first else (second, first)
            return {"home": {"name": home}, "away": {"name": away}}
    return {}


class Ppv:
    """ppv.st: one call lists every game, with its embed link and substreams.

    Embed hosts come out of the response (`iframe`), never from here. A game's
    substreams are extra Sources for the same game. ppv.st says nothing about
    picture quality, so its Sources carry no `quality`. Sources are built from
    the list call alone: `list_sources` makes no request.
    """

    name = "ppv"
    code = "PPV"

    def __init__(self, bases: list[str], timeout: float, headers: dict):
        self.mirrors = Mirrors(bases)
        self.timeout = timeout
        self.headers = headers

    def list_games(self) -> list[dict]:
        self.mirrors.prefer_first()
        payload = self.mirrors.get_json("/api/streams", self.timeout, self.headers)
        categories = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(categories, list):
            # No answer, or one that is not a game list (a refusal such as
            # {"error": "Violation"}). Not the same as an empty list.
            why = self.mirrors.last_error
            if not why and isinstance(payload, dict):
                why = "unexpected answer: %s" % (
                    payload.get("error") or payload.get("message") or sorted(payload)[:4])
            raise ProviderError(why or "unexpected answer")
        games: list[dict] = []
        seen: set = set()
        for cat in (categories or []):
            if not isinstance(cat, dict):
                continue
            label = str(cat.get("category") or "Sports")
            sport = _PPV_SPORT_IDS.get(_slug(label), _slug(label))
            for g in (cat.get("streams") or []):
                game = self._game(g, sport, label)
                if game and game["id"] not in seen:
                    seen.add(game["id"])
                    games.append(game)
        log.info("ppv: collected %d games", len(games))
        return games

    def _game(self, g, sport: str, label: str):
        if not isinstance(g, dict) or not g.get("id") or not g.get("name"):
            return None
        title = str(g["name"]).strip()
        always = bool(g.get("always_live"))
        links = [g] + [s for s in (g.get("substreams") or []) if isinstance(s, dict)]
        game = {
            "id": "ppv-%s" % g["id"],
            "title": title,
            "category": sport,
            "poster": g.get("poster") or "",
            "teams": {} if always else _ppv_teams(title),
            "always_live": always,
            "_category": label,
            "_provider": self.name,
            "_links": [(str(s["iframe"]).strip(), str(s.get("source_tag") or ""))
                       for s in links if str(s.get("iframe") or "").strip()],
        }
        # A feed has no start; ppv.st gives it 0 or a small offset.
        if g.get("starts_at") and not always:
            game["date"] = int(g["starts_at"]) * 1000
        return game

    def list_sources(self, game: dict) -> list[dict]:
        return [{
            "id": "%s-%d" % (game["id"], n),
            "streamNo": n,
            "language": "",
            "embedUrl": url,
            "source": _slug(tag) or self.name,
            "_match_title": game.get("title", "Unknown Match"),
            "_match_id": game.get("id", ""),
            "_match_category": game.get("_category", "Sports"),
            "_match_poster": game.get("poster", ""),
            "_provider": self.name,
        } for n, (url, tag) in enumerate(game.get("_links") or [], 1)]


# streamfree's categories, as the app's sport ids (streamed.pk's: soccer is
# "football", and the NFL's sport is "american-football").
_SF_SPORT_IDS = {
    "soccer": "football", "football": "american-football",
    "racing": "motor-sports", "combat": "fight",
}

# A streamfree embed link ends `<stream_key><height>p<variant>`: ".../redzone1080p2".
_SF_VARIANT = re.compile(r"(\d{3,4})p(\d*)$")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return "%s://%s" % (parts.scheme.lower(), parts.netloc.lower()) if parts.netloc else ""


class Streamfree:
    """streamfree: one call lists every live and upcoming game with its Sources.

    The API and the embed pages live on different hosts, so each has its own
    configured, ordered base list. A Source's picture quality is read off its
    link (`...1080p`, `...1080p2`). Until a game is about to start the
    Provider has no live Sources for it; the game is still returned (it
    belongs in the schedule) with none, and gains them on a later refresh.

    Embed hosts come out of the response, but only a host the operator has
    configured is ever used: a link on any other host is moved onto the first
    configured embed base, so a response cannot send the app to a domain nobody
    wrote into the settings. `list_sources` makes no request.
    """

    name = "streamfree"
    code = "SF"
    LIST_PATH = "/api/v1/streams"

    def __init__(self, bases: list[str], embed_bases: list[str], timeout: float,
                 headers: dict):
        self.mirrors = Mirrors(bases)
        self.embed_bases = list(embed_bases)
        self.timeout = timeout
        self.headers = headers

    def list_games(self) -> list[dict]:
        self.mirrors.prefer_first()
        payload = self.mirrors.get_json(self.LIST_PATH, self.timeout, self.headers)
        items = payload.get("streams") if isinstance(payload, dict) else None
        if isinstance(items, dict):         # by category, as the site's own list is
            items = [g for gs in items.values() if isinstance(gs, list) for g in gs]
        if not isinstance(items, list):
            # No answer, or a refusal ("Violation"), which is not a game list.
            why = self.mirrors.last_error
            if not why and isinstance(payload, dict):
                why = "unexpected answer: %s" % (
                    payload.get("error") or payload.get("message") or sorted(payload)[:4])
            raise ProviderError(why or "unexpected answer")
        games: list[dict] = []
        seen: set = set()
        for g in items:
            game = self._game(g)
            if game and game["id"] not in seen:
                seen.add(game["id"])
                games.append(game)
        log.info("streamfree: collected %d games, %d with Sources", len(games),
                 sum(1 for g in games if g["_links"]))
        return games

    def _embed(self, link) -> str:
        """The link on a configured embed host, or "" if none is configured."""
        link = str(link or "").strip()
        parts = urlsplit(link)
        if not (parts.scheme.lower() in ("http", "https") and parts.netloc):
            return ""
        if _origin(link) in [b.lower() for b in self.embed_bases]:
            return link
        if not self.embed_bases:
            return ""
        log.warning("streamfree: embed link on %s, which is not a configured embed address; "
                    "using %s", parts.netloc, self.embed_bases[0])
        return self.embed_bases[0] + link[len("%s://%s" % (parts.scheme, parts.netloc)):]

    def _poster(self, g: dict) -> str:
        thumb = str(g.get("thumbnail_url") or "").strip()
        if thumb.startswith("/") and self.mirrors.bases:
            return self.mirrors.bases[0] + thumb
        return thumb

    def _game(self, g):
        if not isinstance(g, dict) or not g.get("stream_key") or not g.get("name"):
            return None
        category = str(g.get("category") or "").strip().lower()
        home = str((g.get("team1") or {}).get("name") or "").strip()
        away = str((g.get("team2") or {}).get("name") or "").strip()
        # A channel such as RedZone or Sky Sports F1 names no teams and runs
        # all day: a feed, not an event.
        always = not (home and away)
        listed = [str(u) for u in (g.get("sources") or [])]
        # Only what is live now. The list also names the variants that "start
        # on their own" later; those would be dead Sources until then.
        live = g["live_sources"] if isinstance(g.get("live_sources"), list) else listed
        links = []
        for url in live:
            embed = self._embed(url)
            if not embed:
                continue
            m = _SF_VARIANT.search(embed.rstrip("/").rsplit("/", 1)[-1])
            # The variant's place in the full list is its stable number.
            n = listed.index(url) + 1 if url in listed else len(links) + 1
            links.append((embed, int(m.group(1)) if m else 0, n))
        game = {
            "id": "sf-%s" % g["stream_key"],
            "title": str(g["name"]).strip(),
            "category": _SF_SPORT_IDS.get(category, category),
            "poster": self._poster(g),
            "teams": {} if always else {"home": {"name": home}, "away": {"name": away}},
            "always_live": always,
            "_category": str(g.get("league") or category.title() or "Sports"),
            "_provider": self.name,
            "_links": links,
        }
        if g.get("match_timestamp") and not always:
            try:
                game["date"] = int(g["match_timestamp"]) * 1000
            except (TypeError, ValueError):
                pass
        return game

    def list_sources(self, game: dict) -> list[dict]:
        return [{
            "id": "%s-%d" % (game["id"], n),
            "streamNo": n,
            "language": "",
            "embedUrl": url,
            "source": self.name,
            "quality": quality or None,
            "_match_title": game.get("title", "Unknown Match"),
            "_match_id": game.get("id", ""),
            "_match_category": game.get("_category", "Sports"),
            "_match_poster": game.get("poster", ""),
            "_provider": self.name,
        } for url, quality, n in (game.get("_links") or [])]
