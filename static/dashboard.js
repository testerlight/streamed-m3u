/* streamed-m3u operations console
 *
 * No framework and no build step: the container already carries Chromium and
 * a Python runtime, and adding a Node toolchain to ship one page would be the
 * most expensive line in the image. Everything here is plain DOM work.
 *
 * Two polling tiers. Fast covers anything that moves second to second, slow
 * covers the roster, caches and configuration. Configuration is re-fetched
 * on the slow tier because another tab, or the settings file, can change it;
 * the re-render is skipped while the user has unsaved edits or a field
 * focused, so a poll never wipes what they are typing.
 */

(function () {
  "use strict";

  var FAST_MS = 5000;
  var SLOW_MS = 20000;
  var ROSTER_WINDOW = 200;

  var state = {
    polling: true,
    eventLevel: "",
    rosterQuery: "",
    rosterScope: "playing",
    rosterLimit: ROSTER_WINDOW,
    configQuery: "",
    configCustomOnly: false,
    teams: null,
    rosterAll: null,
    config: null,
    lastValues: {},
    pending: {},            // env -> new value, or null to revert to env/default
    pendingOverrides: {},   // override key -> table, or null to clear
    errors: {},             // env or "overrides.key" -> message; "_cross" -> [messages]
    flash: {},              // env or "overrides.key" -> "applied" | "restart"
    saving: false
  };

  var timers = { fast: null, slow: null };

  var OVERRIDE_META = {
    major_league_teams: {
      title: "Extra league names",
      hint: "Comma-separated team names. Each gains away-side resolution, like the built-in major-league list.",
      kind: "list"
    },
    extra_feed_title_aliases: {
      title: "Feed title aliases",
      hint: "One per line: upstream title = display name. Titles match case-insensitively.",
      kind: "dict"
    },
    feed_slug_overrides: {
      title: "Feed slug overrides",
      hint: "One per line: display slug = channel slug. Pins a feed's address when its display name changes.",
      kind: "dict"
    },
    series_aliases: {
      title: "Series aliases",
      hint: "One per line: alternate slug = canonical slug.",
      kind: "dict"
    }
  };

  /* ── Helpers ──────────────────────────────────────────────────────────── */

  function $(id) { return document.getElementById(id); }

  function esc(v) {
    if (v === null || v === undefined) { return ""; }
    return String(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function num(v) {
    if (v === null || v === undefined || isNaN(v)) { return "0"; }
    return Number(v).toLocaleString("en-US");
  }

  function dur(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) { return "n/a"; }
    var s = Math.max(0, Math.floor(seconds));
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    if (d > 0) { return d + "d " + h + "h"; }
    if (h > 0) { return h + "h " + m + "m"; }
    if (m > 0) { return m + "m " + sec + "s"; }
    return sec + "s";
  }

  /* Head-truncating a URL hides the only part that differs between rows, so
   * every cache row rendered as the same ellipsised string. Keep the host and
   * the last two segments, and hang the full value off the title attribute. */
  function shortUrl(url) {
    if (!url) { return ""; }
    try {
      var u = new URL(url);
      var parts = u.pathname.split("/").filter(Boolean);
      if (parts.length <= 2) { return u.host + u.pathname; }
      return u.host + "/…/" + parts.slice(-2).join("/");
    } catch (e) {
      return url;
    }
  }

  function plural(n, word) {
    return num(n) + " " + word + (Number(n) === 1 ? "" : "s");
  }

  function clock(ts) {
    if (!ts) { return ""; }
    var d = new Date(ts * 1000);
    return d.toLocaleTimeString("en-GB", { hour12: false });
  }

  function get(path) {
    return fetch(path, { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.status === 401) {
          window.location = "/login?next=/";
          throw new Error("Signed out");
        }
        if (!r.ok) { throw new Error(path + " returned " + r.status); }
        return r.json();
      });
  }

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  /* Every request that changes state goes through here, so the CSRF token
   * and the JSON content type are never forgotten at a call site. Resolves
   * with {status, ok, body} rather than throwing, because a 400 carries
   * field errors the caller wants to render. */
  function send(method, path, body) {
    return fetch(path, {
      method: method,
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": csrf()
      },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      if (r.status === 401) { window.location = "/login?next=/"; }
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { status: r.status, ok: r.ok, body: j };
      });
    });
  }

  function errorState(message, retryId) {
    return '<div class="error-state">' +
      '<p class="error-title">Could not load this panel</p>' +
      '<p>' + esc(message) + '</p>' +
      (retryId ? '<button type="button" class="btn" data-retry="' + esc(retryId) + '">Try again</button>' : '') +
      '</div>';
  }

  function emptyState(title, body) {
    return '<div class="empty">' +
      '<p class="empty-title">' + esc(title) + '</p>' +
      '<p>' + esc(body) + '</p>' +
      '</div>';
  }

  function table(headers, rows) {
    if (!rows.length) { return null; }
    var head = headers.map(function (h) {
      var cls = h.right ? ' class="right nowrap"' : ' class="nowrap"';
      return "<th" + cls + ">" + esc(h.label) + "</th>";
    }).join("");
    return '<div class="table-scroll"><table><thead><tr>' + head +
      "</tr></thead><tbody>" + rows.join("") + "</tbody></table></div>";
  }

  /* Flash a figure that changed since the last poll, so a stale panel and a
   * steady one are distinguishable at a glance. */
  function stat(key, label, value, sub) {
    var changed = state.lastValues[key] !== undefined &&
                  state.lastValues[key] !== value;
    state.lastValues[key] = value;
    return '<div class="stat">' +
      '<div class="stat-label">' + esc(label) + "</div>" +
      '<div class="stat-value' + (changed ? " changed" : "") + '">' + esc(value) + "</div>" +
      (sub ? '<div class="stat-sub">' + esc(sub) + "</div>" : "") +
      "</div>";
  }

  /* ── Overview ─────────────────────────────────────────────────────────── */

  function renderRestartBanner(list) {
    var el = $("restart-banner");
    if (!el) { return; }
    if (!list || !list.length) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    el.innerHTML = "<b>Restart required</b><span>Saved changes to " +
      list.map(function (k) { return '<span class="mono">' + esc(k) + "</span>"; }).join(", ") +
      " take effect on the next start.</span>";
  }

  function renderOverview(d) {
    var ok = d.status === "ok";
    var dot = $("health-dot");
    dot.className = "dot " + (ok ? "ok" : "warn");
    $("health-text").textContent = ok
      ? "Serving playlist and guide"
      : "Starting up, first build not finished";

    $("subnav-dot").className = "dot " + (ok ? "ok" : "warn");
    $("subnav-status-text").textContent = ok ? "Healthy" : "Initialising";

    var p = d.playlist || {};
    $("banner-facts").innerHTML =
      "<span>Upstream <b>" + esc(d.base_url || "unknown") + "</b></span>" +
      "<span>Sources <b>" + num((d.sources || []).length) + "</b></span>" +
      "<span>Playlist entries <b>" + num(p.stream_count) + "</b></span>" +
      "<span>Last rebuild <b>" +
        (p.age_s === null || p.age_s === undefined ? "never" : dur(p.age_s) + " ago") +
      "</b></span>";

    var pb = d.public_base || {};
    $("base-note").textContent = pb.configured
      ? "Playlist addresses use PUBLIC_BASE_URL: " + pb.value
      : "Playlist addresses are auto-detected from each request. Set " +
        "PUBLIC_BASE_URL if clients reach this service through a proxy or a different host.";

    renderRestartBanner(d.restart_pending || []);

    var r = d.roster || {};
    var pw = d.prewarm || {};
    var st = d.streams || {};
    var ca = d.caches || {};
    var sv = d.service || {};

    $("stat-grid").innerHTML = [
      stat("roster", "Roster", num(r.size), "channels that exist"),
      stat("resolvable", "Resolvable now", num(r.resolvable),
           num(r.listed_no_streams) + " listed without a stream"),
      stat("fixtures", "Guide fixtures", num(r.epg_fixtures), "programmes published"),
      stat("active", "Active streams", num(st.active),
           "idle cutoff " + num(st.idle_timeout_s) + "s"),
      stat("warm", "Favourites warm", num(pw.warm) + " of " + num(pw.configured),
           "checked every " + num(pw.interval_s) + "s"),
      stat("extract", "Extraction cache",
           num((ca.extract || {}).entries) + " of " + num((ca.extract || {}).max),
           "TTL " + num((ca.extract || {}).ttl_s) + "s"),
      stat("next", "Next rebuild",
           p.next_refresh_in_s === null || p.next_refresh_in_s === undefined
             ? "pending" : dur(p.next_refresh_in_s),
           "every " + num(p.refresh_seconds) + "s"),
      stat("uptime", "Uptime", dur(sv.uptime_s), "process " + num(sv.pid))
    ].join("");

    $("footer-uptime").textContent = "Up " + dur(sv.uptime_s);
    $("footer-source").textContent = "Upstream " + (d.base_url || "unknown");
    $("footer-updated").textContent = "Updated " + clock(d.now);
  }

  /* ── Streams ──────────────────────────────────────────────────────────── */

  function renderStreams(d) {
    var streams = d.streams || [];
    if (!streams.length) {
      $("streams-panel").innerHTML = emptyState(
        "Nothing playing",
        "No client is pulling a stream. Start a channel and it will appear here " +
        "with live throughput and segment counts."
      );
      return;
    }
    var rows = streams.map(function (s) {
      var stalled = s.last_segment_ago !== null &&
                    s.last_segment_ago > (d.segment_timeout || 30);
      var failed = (s.segments_failed || 0) > 0;
      return "<tr>" +
        '<td class="nowrap"><span class="dot ' + (stalled ? "error" : "live") +
          '" style="display:inline-block;margin-right:7px;"></span>' +
          '<span class="mono">' + esc(s.stream_id) + "</span></td>" +
        '<td><span class="cell-truncate mono" title="' + esc(s.m3u8_url || s.embed_url) +
          '">' + esc(shortUrl(s.m3u8_url || s.embed_url)) + "</span></td>" +
        '<td class="right nowrap">' + dur(s.elapsed_seconds) + "</td>" +
        '<td class="right nowrap cell-strong">' +
          (s.mbit_per_s === null ? "n/a" : s.mbit_per_s + " Mbit/s") + "</td>" +
        '<td class="right nowrap">' + num(s.mb_sent) + " MB</td>" +
        '<td class="right nowrap">' + num(s.segments_sent) + "</td>" +
        '<td class="right nowrap">' +
          (failed ? '<span class="badge error">' + num(s.segments_failed) + "</span>"
                  : '<span class="cell-dim">0</span>') + "</td>" +
        '<td class="right nowrap">' + num(s.segments_retried) + "</td>" +
        '<td class="right nowrap">' +
          (s.last_segment_ago === null ? "n/a" : s.last_segment_ago + "s") + "</td>" +
        "</tr>";
    });
    $("streams-panel").innerHTML = table([
      { label: "Stream" }, { label: "Source" },
      { label: "Elapsed", right: true }, { label: "Rate", right: true },
      { label: "Sent", right: true }, { label: "Segments", right: true },
      { label: "Failed", right: true }, { label: "Retried", right: true },
      { label: "Last segment", right: true }
    ], rows);
  }

  /* ── Pre-warm ─────────────────────────────────────────────────────────── */

  function renderPrewarm(d) {
    var rows = (d.state || []).map(function (t) {
      var status = String(t.status || "unknown");
      var cls = status.indexOf("warm") === 0 ? "ok"
              : (status.indexOf("error") === 0 || status.indexOf("fail") === 0) ? "error"
              : "default";
      var ttl = t.ttl_left_s;
      var bar = "";
      if (ttl !== null && ttl !== undefined) {
        var barCls = ttl <= 0 ? "out" : (ttl < 60 ? "low" : "");
        var width = Math.max(4, Math.min(56, Math.round((ttl / 300) * 56)));
        bar = '<span class="ttl"><span class="ttl-rule ' + barCls +
              '" style="width:' + width + 'px"></span><span>' +
              Math.max(0, Math.round(ttl)) + "s</span></span>";
      } else {
        bar = '<span class="cell-dim">not cached</span>';
      }
      return "<tr>" +
        '<td class="cell-strong">' + esc(t.team || t.slug) + "</td>" +
        '<td><span class="badge ' + cls + '">' + esc(status) + "</span></td>" +
        "<td>" + (t.match ? esc(t.match) : '<span class="cell-dim">no fixture listed</span>') + "</td>" +
        '<td class="nowrap">' + esc(t.side || "") + "</td>" +
        '<td class="nowrap">' + esc(t.slot || "") + "</td>" +
        '<td class="right nowrap">' + bar + "</td>" +
        '<td class="nowrap cell-dim mono">' + esc(t.checked_at || "") + "</td>" +
        "</tr>";
    });
    if (!rows.length) {
      $("prewarm-panel").innerHTML = emptyState(
        "No favourites configured",
        "Set PREWARM_TEAMS to keep a few teams resolved ahead of time. Each one " +
        "costs a serial browser launch, so keep the list short."
      );
      return;
    }
    $("prewarm-panel").innerHTML = table([
      { label: "Team" }, { label: "Status" }, { label: "Fixture" },
      { label: "Side" }, { label: "Slot" },
      { label: "Cache left", right: true }, { label: "Checked" }
    ], rows);
  }

  /* ── Roster ───────────────────────────────────────────────────────────── */

  function renderRoster() {
    var panel = $("roster-panel");
    var q = state.rosterQuery.toLowerCase();
    var rows, total, headers;

    if (state.rosterScope === "all") {
      if (!state.rosterAll) { panel.innerHTML = emptyState("Loading full roster", "Fetching every known channel."); return; }
      var entries = Object.keys(state.rosterAll.roster || {}).map(function (slug) {
        var v = state.rosterAll.roster[slug] || {};
        return { slug: slug, name: v.name || slug, first: v.first_seen, last: v.last_seen };
      });
      if (q) {
        entries = entries.filter(function (e) {
          return e.name.toLowerCase().indexOf(q) >= 0 || e.slug.indexOf(q) >= 0;
        });
      }
      entries.sort(function (a, b) { return a.name.localeCompare(b.name); });
      total = entries.length;
      headers = [{ label: "Channel" }, { label: "Slug" }, { label: "First seen" }, { label: "Last seen" }];
      rows = entries.slice(0, state.rosterLimit).map(function (e) {
        return "<tr>" +
          '<td class="cell-strong">' + esc(e.name) + "</td>" +
          '<td class="mono">' + esc(e.slug) + "</td>" +
          '<td class="nowrap cell-dim">' + esc(e.first || "") + "</td>" +
          '<td class="nowrap cell-dim">' + esc(e.last || "") + "</td>" +
          "</tr>";
      });
    } else {
      if (!state.teams) { panel.innerHTML = emptyState("Loading roster", "Fetching resolvable channels."); return; }
      var list = (state.teams.teams || []).slice();
      if (q) {
        list = list.filter(function (t) {
          return String(t.team).toLowerCase().indexOf(q) >= 0 ||
                 String(t.match).toLowerCase().indexOf(q) >= 0 ||
                 String(t.slug).indexOf(q) >= 0;
        });
      }
      total = list.length;
      headers = [{ label: "Channel" }, { label: "Resolves to" }, { label: "Category" },
                 { label: "Sources", right: true }, { label: "Best" }, { label: "Slug" }];
      rows = list.slice(0, state.rosterLimit).map(function (t) {
        return "<tr>" +
          '<td class="cell-strong">' + esc(t.team) + "</td>" +
          "<td>" + esc(t.match) + "</td>" +
          '<td class="nowrap">' + esc(t.category) + "</td>" +
          '<td class="right">' + num(t.streams) + "</td>" +
          '<td class="nowrap mono">' + esc(t.best) + "</td>" +
          '<td class="mono cell-dim">' + esc(t.slug) + "</td>" +
          "</tr>";
      });
    }

    if (!rows.length) {
      panel.innerHTML = emptyState(
        q ? "Nothing matches that search" : "No channels resolvable",
        q ? "Try a team name, a fixture title, or a slug."
          : "The roster fills in as the upstream catalog lists fixtures."
      );
    } else {
      var html = table(headers, rows);
      if (total > state.rosterLimit) {
        html += '<div class="empty" style="padding:17px;">' +
          '<button type="button" class="btn" id="roster-more">Show ' +
          num(Math.min(ROSTER_WINDOW, total - state.rosterLimit)) + " more</button></div>";
      }
      panel.innerHTML = html;
      var more = $("roster-more");
      if (more) {
        more.addEventListener("click", function () {
          state.rosterLimit += ROSTER_WINDOW;
          renderRoster();
        });
      }
    }

    $("roster-count").textContent =
      num(Math.min(state.rosterLimit, total)) + " shown of " + num(total);
  }

  /* ── Aliases ──────────────────────────────────────────────────────────── */

  function renderAliases(d) {
    var unseen = d.unseen || [];
    var pills = unseen.slice(0, 60).map(function (s) {
      return '<code class="inline">' + esc(s) + "</code>";
    }).join(" ");
    $("alias-panel").innerHTML =
      '<div class="stat-grid" style="margin-bottom:12px;">' +
        stat("alias_total", "Alias names", num(d.count), "granted away-side resolution") +
        stat("alias_seen", "Seen upstream", num(d.seen_count), "confirmed to match a fixture") +
        stat("alias_unseen", "Never seen", num(d.unseen_count), "check these for spelling") +
        stat("alias_sports", "Sports matched", num((d.sports || []).length),
             (d.sports || []).join(", ")) +
      "</div>" +
      '<div class="panel">' +
        (unseen.length
          ? "<p class=\"section-note\" style=\"margin-bottom:12px;\">Names that have not yet appeared in any fixture" +
            (unseen.length > 60 ? ", first 60 of " + num(unseen.length) : "") + ".</p>" + pills
          : "<p class=\"section-note\">Every alias name has been seen upstream at least once.</p>") +
      "</div>";
  }

  /* ── Caches ───────────────────────────────────────────────────────────── */

  function renderCaches(d) {
    var ex = d.extract || {}, lg = d.logo || {}, na = d.no_audio || {};

    $("cache-stats").innerHTML = [
      stat("c_extract", "Extraction entries", num(ex.count) + " of " + num(ex.max),
           "TTL " + num(ex.ttl_s) + "s"),
      stat("c_logo", "Logo cache", num(lg.count) + " of " + num(lg.max),
           num(lg.mb) + " MB held"),
      stat("c_logofail", "Logo failures", num(lg.failed), "retried on a short window"),
      stat("c_silent", "Silent sources", num(na.count),
           na.enabled ? "audio check on" : "audio check off")
    ].join("");

    var rows = (ex.entries || []).map(function (e) {
      var cls = e.expired ? "out" : (e.ttl_left_s < 60 ? "low" : "");
      var width = Math.max(4, Math.min(56, Math.round((e.ttl_left_s / (ex.ttl_s || 300)) * 56)));
      return "<tr>" +
        '<td><span class="cell-truncate mono" title="' + esc(e.embed_url) + '">' +
          esc(shortUrl(e.embed_url)) + "</span></td>" +
        '<td><span class="cell-truncate mono"' +
          (e.m3u8_url ? ' title="' + esc(e.m3u8_url) + '">' + esc(shortUrl(e.m3u8_url))
                      : '><span class="cell-dim">not resolved</span>') + "</span></td>" +
        '<td class="right nowrap">' + dur(e.age_s) + "</td>" +
        '<td class="right nowrap"><span class="ttl">' +
          (e.expired ? "" : '<span class="ttl-rule ' + cls + '" style="width:' + width + 'px"></span>') +
          "<span>" + (e.expired ? "expired" : Math.round(e.ttl_left_s) + "s") + "</span></span></td>" +
        "</tr>";
    });

    if (!rows.length) {
      $("cache-panel").innerHTML = emptyState(
        "Extraction cache is empty",
        "Entries appear once a channel is opened and its real stream URL is resolved."
      );
      return;
    }
    $("cache-panel").innerHTML = table([
      { label: "Embed page" }, { label: "Resolved stream" },
      { label: "Age", right: true }, { label: "TTL left", right: true }
    ], rows);
  }

  /* ── Events ───────────────────────────────────────────────────────────── */

  function renderEvents(d) {
    var events = d.events || [];
    if (!events.length) {
      $("events-panel").innerHTML = emptyState(
        "No events at this level",
        "Nothing has been logged at the selected level since the service started."
      );
    } else {
      $("events-panel").innerHTML = events.map(function (e) {
        return '<div class="event">' +
          "<time>" + esc(clock(e.ts)) + "</time>" +
          '<span class="lvl ' + esc(e.level) + '">' + esc(e.level) + "</span>" +
          '<span class="msg">' + esc(e.message) + "</span>" +
          "</div>";
      }).join("");
    }
    var c = d.counts || {};
    var errors = (c.ERROR || 0) + (c.CRITICAL || 0);
    $("events-count").textContent =
      num(events.length) + " shown. " + plural(c.WARNING || 0, "warning") + ", " +
      plural(errors, "error") + " in the last " + num(d.capacity) + " records";
  }

  /* ── Config: values and editing ───────────────────────────────────────── */

  function editing() {
    return !!(state.config && state.config.editable);
  }

  function pendingCount() {
    return Object.keys(state.pending).length + Object.keys(state.pendingOverrides).length;
  }

  function configFocused() {
    var a = document.activeElement;
    return !!(a && (a.closest("#config-panel") || a.closest("#overrides-panel")));
  }

  /* Value as it should appear in an input: the pending edit if there is one,
   * else the effective value. */
  function shownValue(s) {
    if (Object.prototype.hasOwnProperty.call(state.pending, s.env)) {
      var p = state.pending[s.env];
      return p === null ? s.baseline : p;
    }
    return s.value;
  }

  function sameValue(spec, a, b) {
    if (spec.type === "list") {
      var na = Array.isArray(a) ? a : String(a || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean);
      var nb = Array.isArray(b) ? b : String(b || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean);
      return na.join(" ") === nb.join(" ");
    }
    if (spec.type === "bool") { return !!a === !!b; }
    if (spec.type === "int" || spec.type === "float") { return Number(a) === Number(b); }
    return String(a === null || a === undefined ? "" : a) === String(b === null || b === undefined ? "" : b);
  }

  function readInput(spec, el) {
    if (spec.type === "bool") { return el.checked; }
    if (spec.type === "int" || spec.type === "float") {
      if (el.value.trim() === "") { return undefined; }
      return Number(el.value);
    }
    return el.value;
  }

  function fieldFor(s) {
    var v = shownValue(s);
    var t = s.type;
    var attrs = ' class="field" data-env="' + esc(s.env) + '"';
    if (t === "bool") {
      return '<input type="checkbox" class="field-check" data-env="' + esc(s.env) + '"' + (v ? " checked" : "") + ">";
    }
    if (t === "choice") {
      return '<select class="field-select" data-env="' + esc(s.env) + '">' +
        (s.choices || []).map(function (c) {
          return '<option value="' + esc(c) + '"' + (String(v) === c ? " selected" : "") + ">" + esc(c) + "</option>";
        }).join("") + "</select>";
    }
    if (t === "int" || t === "float") {
      return '<input type="number"' + attrs +
        (s.min !== null && s.min !== undefined ? ' min="' + s.min + '"' : "") +
        (s.max !== null && s.max !== undefined ? ' max="' + s.max + '"' : "") +
        ' step="' + (t === "float" ? "any" : "1") + '" value="' + esc(v === null || v === undefined ? "" : v) + '">';
    }
    var text = Array.isArray(v) ? v.join(", ") : (v === null || v === undefined ? "" : v);
    return '<input type="text"' + attrs + ' value="' + esc(text) + '"' +
      (t === "list" ? ' placeholder="Comma-separated"' : "") + ">";
  }

  function appliesLabel(a) {
    if (a === "live") { return "Live"; }
    if (a === "next-refresh") { return "Next rebuild"; }
    return "On restart";
  }

  function sourceBadge(s) {
    if (s.source === "file") { return '<span class="badge env">Console</span>'; }
    if (s.source === "environment") { return '<span class="badge env">Set</span>'; }
    return '<span class="badge default">Default</span>';
  }

  function renderConfig() {
    if (!state.config) { return; }
    var d = state.config;
    var q = state.configQuery.toLowerCase();
    var edit = editing();
    var shown = 0;

    var html = (d.groups || []).map(function (g) {
      var settings = g.settings.filter(function (s) {
        if (state.configCustomOnly && s.source === "default") { return false; }
        if (!q) { return true; }
        return s.env.toLowerCase().indexOf(q) >= 0 ||
               String(s.display).toLowerCase().indexOf(q) >= 0 ||
               s.description.toLowerCase().indexOf(q) >= 0;
      });
      if (!settings.length) { return ""; }
      shown += settings.length;
      var rows = settings.map(function (s) {
        var dirty = Object.prototype.hasOwnProperty.call(state.pending, s.env);
        var err = state.errors[s.env];
        var flash = state.flash[s.env];
        var valueCell;
        if (edit && s.editable) {
          valueCell = fieldFor(s);
          if (s.source === "file" && s.env_value !== null && s.env_value !== undefined) {
            valueCell += '<div class="field-hint">Environment has <code class="inline">' + esc(s.env_value) +
              '</code>. <a data-reset="' + esc(s.env) + '">Reset to it</a></div>';
          } else if (s.source === "file") {
            valueCell += '<div class="field-hint"><a data-reset="' + esc(s.env) + '">Reset to default</a></div>';
          }
        } else {
          valueCell = s.display === "" || s.display === null || s.display === undefined
            ? '<span class="cell-dim">empty</span>'
            : '<code class="inline">' + esc(s.display) + "</code>";
          if (edit && !s.editable) {
            valueCell += '<div class="field-hint">Environment only</div>';
          }
        }
        if (err) { valueCell += '<div class="field-error">' + esc(err) + "</div>"; }
        if (flash === "applied") { valueCell += ' <span class="badge ok">Applied</span>'; }
        if (flash === "restart") { valueCell += ' <span class="badge warn">Restart required</span>'; }

        var applies = '<span class="cell-dim">' + appliesLabel(s.applies) + "</span>";
        if (s.restart_pending) { applies += ' <span class="badge warn">Pending</span>'; }

        return '<tr' + (dirty ? ' class="dirty"' : "") + ">" +
          '<td class="nowrap"><span class="cell-strong mono">' + esc(s.env) + "</span>" +
            '<div class="config-desc">' + esc(s.description) + "</div></td>" +
          "<td>" + valueCell + "</td>" +
          '<td class="nowrap">' + sourceBadge(s) + "</td>" +
          '<td class="nowrap">' + applies + "</td>" +
          "</tr>";
      });
      return '<div class="config-group"><h3>' + esc(g.group) + "</h3>" +
        '<div class="table-wrap">' + table([
          { label: "Setting" }, { label: edit ? "Value" : "Effective value" },
          { label: "Source" }, { label: "Applies" }
        ], rows) + "</div></div>";
    }).join("");

    $("config-panel").innerHTML = html || emptyState(
      "Nothing matches",
      state.configCustomOnly
        ? "No settings are supplied through the environment or the console. Everything is at its built-in default."
        : "Try part of a setting name or value."
    );

    var note = num(shown) + " of " + num(d.total) + " settings. " + num(d.customised) + " set";
    if (!edit) {
      note += d.auth && d.auth.enabled ? ". Sign in to edit" : ". Editing is off: set CONSOLE_PASSWORD to enable it";
    }
    $("config-count").textContent = note;
    if (d.load_error) {
      $("config-count").textContent += ". Settings file problem: " + d.load_error;
    }
    renderSaveBar();
  }

  function renderOverrides() {
    if (!state.config) { return; }
    var ov = state.config.overrides || {};
    var edit = editing();
    var html = Object.keys(OVERRIDE_META).map(function (key) {
      var meta = OVERRIDE_META[key];
      var o = ov[key] || {};
      var dirty = Object.prototype.hasOwnProperty.call(state.pendingOverrides, key);
      var fileVal = dirty ? state.pendingOverrides[key] : o.file_value;
      var added = fileVal ? (Array.isArray(fileVal) ? fileVal.length : Object.keys(fileVal).length) : 0;
      var text;
      if (!fileVal) { text = ""; }
      else if (meta.kind === "list") { text = fileVal.join(", "); }
      else { text = Object.keys(fileVal).map(function (k) { return k + " = " + fileVal[k]; }).join("\n"); }
      var err = state.errors["overrides." + key];
      var flash = state.flash["overrides." + key];
      var body;
      if (edit) {
        body = meta.kind === "list"
          ? '<textarea class="field" data-override="' + esc(key) + '" rows="2">' + esc(text) + "</textarea>"
          : '<textarea class="field" data-override="' + esc(key) + '" rows="4">' + esc(text) + "</textarea>";
        body += '<div class="field-hint">' + esc(meta.hint) +
          (added ? ' <a data-clear="' + esc(key) + '">Clear additions</a>' : "") + "</div>";
      } else if (!added) {
        body = '<p class="section-note">No additions. ' + esc(meta.hint) + "</p>";
      } else if (meta.kind === "list") {
        body = fileVal.map(function (v) { return '<code class="inline">' + esc(v) + "</code>"; }).join(" ");
      } else {
        body = Object.keys(fileVal).map(function (k) {
          return '<div class="mono">' + esc(k) + " = " + esc(fileVal[k]) + "</div>";
        }).join("");
      }
      if (err) { body += '<div class="field-error">' + esc(err) + "</div>"; }
      return '<div class="panel override-block' + (dirty ? " dirty" : "") + '">' +
        '<div class="override-head"><h3>' + esc(meta.title) + "</h3>" +
        '<span class="section-note">' + num(o.builtin_count) + " built in, " + plural(added, "addition") + "</span>" +
        (o.restart_pending ? ' <span class="badge warn">Restart pending</span>' : "") +
        (flash === "restart" ? ' <span class="badge warn">Restart required</span>' : "") +
        "</div>" + body + "</div>";
    }).join("");
    $("overrides-panel").innerHTML = html;
  }

  function renderSaveBar() {
    var bar = $("save-bar");
    if (!bar) { return; }
    var n = pendingCount();
    bar.hidden = !(editing() && n > 0);
    var cross = state.errors._cross;
    $("save-count").innerHTML = (n ? plural(n, "change") : "") +
      (cross && cross.length ? ' <span class="field-error" style="display:inline; margin-left:8px;">' + esc(cross.join(". ")) + "</span>" : "");
    $("save-settings").disabled = state.saving;
    $("save-settings").textContent = state.saving ? "Saving" : "Save";
  }

  function parseOverrideText(key, text) {
    var meta = OVERRIDE_META[key];
    var t = text.trim();
    if (!t) { return null; }
    if (meta.kind === "list") {
      return t.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    }
    var out = {};
    t.split("\n").forEach(function (line) {
      line = line.trim();
      if (!line) { return; }
      var i = line.indexOf("=");
      if (i < 0) { throw new Error('Each line needs the form "key = value": ' + line); }
      var k = line.slice(0, i).trim(), v = line.slice(i + 1).trim();
      if (!k || !v) { throw new Error('Each line needs the form "key = value": ' + line); }
      out[k] = v;
    });
    return out;
  }

  function onFieldChange(el) {
    var env = el.getAttribute("data-env");
    var spec = findSpec(env);
    if (!spec) { return; }
    var v = readInput(spec, el);
    if (v === undefined) { delete state.pending[env]; }
    else if (sameValue(spec, v, spec.value) && !Object.prototype.hasOwnProperty.call(state.pending, env)) { /* unchanged */ }
    else if (sameValue(spec, v, spec.value)) { delete state.pending[env]; }
    else { state.pending[env] = v; }
    delete state.errors[env];
    var row = el.closest("tr");
    if (row) { row.classList.toggle("dirty", Object.prototype.hasOwnProperty.call(state.pending, env)); }
    renderSaveBar();
  }

  function onOverrideChange(el) {
    var key = el.getAttribute("data-override");
    try {
      var parsed = parseOverrideText(key, el.value);
      var current = (state.config.overrides[key] || {}).file_value || null;
      if (JSON.stringify(parsed) === JSON.stringify(current)) { delete state.pendingOverrides[key]; }
      else { state.pendingOverrides[key] = parsed; }
      delete state.errors["overrides." + key];
    } catch (e) {
      state.errors["overrides." + key] = e.message;
      state.pendingOverrides[key] = undefined;
      delete state.pendingOverrides[key];
    }
    var block = el.closest(".override-block");
    if (block) { block.classList.toggle("dirty", Object.prototype.hasOwnProperty.call(state.pendingOverrides, key)); }
    var errEl = block ? block.querySelector(".field-error") : null;
    if (state.errors["overrides." + key]) {
      if (!errEl) { errEl = document.createElement("div"); errEl.className = "field-error"; block.appendChild(errEl); }
      errEl.textContent = state.errors["overrides." + key];
    } else if (errEl) {
      errEl.remove();
    }
    renderSaveBar();
  }

  function findSpec(env) {
    var groups = (state.config && state.config.groups) || [];
    for (var i = 0; i < groups.length; i++) {
      for (var j = 0; j < groups[i].settings.length; j++) {
        if (groups[i].settings[j].env === env) { return groups[i].settings[j]; }
      }
    }
    return null;
  }

  function saveSettings() {
    if (state.saving || !pendingCount()) { return; }
    state.saving = true;
    state.errors = {};
    renderSaveBar();
    var body = { settings: state.pending, overrides: state.pendingOverrides };
    send("PUT", "/api/settings", body).then(function (res) {
      state.saving = false;
      if (res.status === 400) {
        state.errors = res.body.errors || {};
        renderConfig();
        renderOverrides();
        return;
      }
      if (!res.ok) {
        state.errors = { _cross: [(res.body && res.body.message) || ("Save failed with status " + res.status)] };
        renderSaveBar();
        return;
      }
      var b = res.body;
      state.flash = {};
      (b.applied || []).forEach(function (k) { state.flash[k] = "applied"; });
      (b.restart_required || []).forEach(function (k) { state.flash[k] = "restart"; });
      state.pending = {};
      state.pendingOverrides = {};
      if (b.config) { state.config = b.config; }
      renderConfig();
      renderOverrides();
      renderRestartBanner(b.restart_pending || []);
      setTimeout(function () { state.flash = {}; if (!configFocused()) { renderConfig(); renderOverrides(); } }, 6000);
    }).catch(function (err) {
      state.saving = false;
      state.errors = { _cross: [err.message] };
      renderSaveBar();
    });
  }

  function discardSettings() {
    state.pending = {};
    state.pendingOverrides = {};
    state.errors = {};
    renderConfig();
    renderOverrides();
  }

  function applyConfig(d) {
    state.config = d;
    if (pendingCount() || configFocused()) { renderSaveBar(); return; }
    renderConfig();
    renderOverrides();
  }

  /* ── Endpoints ────────────────────────────────────────────────────────── */

  function renderEndpoints() {
    var base = window.location.origin;
    var items = [
      ["/playlist-teams.m3u", "The playlist Dispatcharr subscribes to. One channel per team, series or feed."],
      ["/epg.xml", "XMLTV guide. Channel ids are permanent, so bindings survive fixture changes."],
      ["/health", "Machine-readable status. Roster size, guide count, cache state."],
      ["/teams", "Every channel resolvable right now, with its ranked sources."],
      ["/prewarm", "Per-favourite warm state and remaining cache TTL."],
      ["/stream/status", "Currently active streams with byte and segment counters."],
      ["/api/overview", "Aggregate figures behind this page."],
      ["/api/config", "Effective configuration as JSON, with sources and bounds."],
      ["/api/settings", "PUT a change here. Needs a session and the CSRF token."],
      ["/api/cache", "Extraction, logo and no-audio cache contents."],
      ["/api/events", "Recent log records as JSON."],
      ["/playlist.m3u", "Legacy per-match playlist. Kept as a fallback, consumed by nothing."]
    ];
    $("endpoints-panel").innerHTML = items.map(function (it) {
      return '<div class="endpoint">' +
        '<a class="path mono" href="' + esc(it[0]) + '">' + esc(base + it[0]) + "</a>" +
        "<p>" + esc(it[1]) + "</p></div>";
    }).join("");
  }

  /* ── Polling ──────────────────────────────────────────────────────────── */

  function guard(panelId, retryKey) {
    return function (err) {
      var el = $(panelId);
      if (el) { el.innerHTML = errorState(err.message, retryKey); }
    };
  }

  function pollFast() {
    get("/api/overview").then(renderOverview).catch(function (err) {
      $("health-dot").className = "dot error";
      $("health-text").textContent = "Cannot reach the service";
      $("subnav-dot").className = "dot error";
      $("subnav-status-text").textContent = "Offline";
      $("banner-facts").innerHTML = "<span>" + esc(err.message) + "</span>";
    });
    get("/stream/status").then(renderStreams).catch(guard("streams-panel", "streams"));
    get("/prewarm").then(renderPrewarm).catch(guard("prewarm-panel", "prewarm"));
    get("/api/events?limit=200&level=" + encodeURIComponent(state.eventLevel))
      .then(renderEvents).catch(guard("events-panel", "events"));
  }

  function pollSlow() {
    get("/teams").then(function (d) { state.teams = d; renderRoster(); })
      .catch(guard("roster-panel", "roster"));
    if (state.rosterScope === "all" && !state.rosterAll) {
      get("/teams?all=1").then(function (d) { state.rosterAll = d; renderRoster(); })
        .catch(guard("roster-panel", "roster"));
    }
    get("/teams?alias=1").then(renderAliases).catch(guard("alias-panel", "aliases"));
    get("/api/cache").then(renderCaches).catch(guard("cache-panel", "caches"));
    get("/api/config").then(applyConfig).catch(guard("config-panel", "config"));
  }

  function pollAll() { pollFast(); pollSlow(); }

  function startPolling() {
    stopPolling();
    timers.fast = setInterval(pollFast, FAST_MS);
    timers.slow = setInterval(pollSlow, SLOW_MS);
  }

  function stopPolling() {
    if (timers.fast) { clearInterval(timers.fast); timers.fast = null; }
    if (timers.slow) { clearInterval(timers.slow); timers.slow = null; }
  }

  /* ── Wiring ───────────────────────────────────────────────────────────── */

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    $("theme-toggle").textContent = theme === "dark" ? "Light" : "Dark";
    try { localStorage.setItem("streamed-m3u-theme", theme); } catch (e) { /* private mode */ }
  }

  function pressGroup(buttons, active) {
    buttons.forEach(function (b) {
      b.setAttribute("aria-pressed", b === active ? "true" : "false");
    });
  }

  function init() {
    var saved = null;
    try { saved = localStorage.getItem("streamed-m3u-theme"); } catch (e) { /* private mode */ }
    setTheme(saved || "dark");

    var signOut = $("sign-out");
    if (signOut) {
      signOut.addEventListener("click", function () {
        send("POST", "/logout").then(function () { window.location = "/login"; });
      });
    }

    $("theme-toggle").addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      setTheme(current === "dark" ? "light" : "dark");
    });

    $("poll-toggle").addEventListener("click", function () {
      state.polling = !state.polling;
      this.setAttribute("aria-pressed", state.polling ? "true" : "false");
      this.textContent = state.polling ? "Auto refresh on" : "Auto refresh off";
      if (state.polling) { pollAll(); startPolling(); } else { stopPolling(); }
    });

    $("refresh-now").addEventListener("click", function () {
      state.rosterAll = null;
      pollAll();
    });

    var rosterSearch = $("roster-search");
    rosterSearch.addEventListener("input", function () {
      state.rosterQuery = this.value.trim();
      state.rosterLimit = ROSTER_WINDOW;
      renderRoster();
    });

    var scopePlaying = $("roster-scope-playing");
    var scopeAll = $("roster-scope-all");
    scopePlaying.addEventListener("click", function () {
      state.rosterScope = "playing";
      state.rosterLimit = ROSTER_WINDOW;
      pressGroup([scopePlaying, scopeAll], scopePlaying);
      renderRoster();
    });
    scopeAll.addEventListener("click", function () {
      state.rosterScope = "all";
      state.rosterLimit = ROSTER_WINDOW;
      pressGroup([scopePlaying, scopeAll], scopeAll);
      if (!state.rosterAll) {
        $("roster-panel").innerHTML = emptyState("Loading full roster", "Fetching every known channel.");
        get("/teams?all=1").then(function (d) { state.rosterAll = d; renderRoster(); })
          .catch(guard("roster-panel", "roster"));
      } else {
        renderRoster();
      }
    });

    var levelChips = Array.prototype.slice.call(
      document.querySelectorAll("#events .chip[data-level]"));
    levelChips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        state.eventLevel = chip.getAttribute("data-level");
        pressGroup(levelChips, chip);
        get("/api/events?limit=200&level=" + encodeURIComponent(state.eventLevel))
          .then(renderEvents).catch(guard("events-panel", "events"));
      });
    });

    $("config-search").addEventListener("input", function () {
      state.configQuery = this.value.trim();
      renderConfig();
    });
    var cfgAll = $("config-all"), cfgCustom = $("config-custom");
    cfgAll.addEventListener("click", function () {
      state.configCustomOnly = false;
      pressGroup([cfgAll, cfgCustom], cfgAll);
      renderConfig();
    });
    cfgCustom.addEventListener("click", function () {
      state.configCustomOnly = true;
      pressGroup([cfgAll, cfgCustom], cfgCustom);
      renderConfig();
    });

    $("save-settings").addEventListener("click", saveSettings);
    $("discard-settings").addEventListener("click", discardSettings);

    /* Edits, resets and clears are delegated: the tables re-render often. */
    document.addEventListener("input", function (ev) {
      var t = ev.target;
      if (!t || typeof t.getAttribute !== "function") { return; }
      if (t.hasAttribute("data-env")) { onFieldChange(t); }
      else if (t.hasAttribute("data-override")) { onOverrideChange(t); }
    });
    document.addEventListener("change", function (ev) {
      var t = ev.target;
      if (t && typeof t.getAttribute === "function" && t.hasAttribute("data-env") &&
          (t.type === "checkbox" || t.tagName === "SELECT")) { onFieldChange(t); }
    });
    document.addEventListener("click", function (ev) {
      var target = ev.target;
      if (!target || typeof target.closest !== "function") { return; }
      if (target.closest("[data-retry]")) { pollAll(); return; }
      var reset = target.closest("[data-reset]");
      if (reset) {
        var env = reset.getAttribute("data-reset");
        state.pending[env] = null;
        delete state.errors[env];
        renderConfig();
        return;
      }
      var clear = target.closest("[data-clear]");
      if (clear) {
        var key = clear.getAttribute("data-clear");
        state.pendingOverrides[key] = null;
        delete state.errors["overrides." + key];
        renderOverrides();
        renderSaveBar();
      }
    });

    /* A backgrounded tab does not need five requests every five seconds. */
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        stopPolling();
      } else if (state.polling) {
        pollAll();
        startPolling();
      }
    });

    renderEndpoints();
    pollAll();
    startPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
