/* Plex Cache web UI — plain JS, no build step. */
(function () {
  "use strict";

  var ST = null;            // last /api/state
  var S = null;             // settings (local copy, updated optimistically)
  var page = "health";
  var guides = { warmup: false, nowplaying: false, nextepisode: false };
  var openDetails = {};  // which <details> boxes are open, so re-renders keep them open
  var mapPending = {};   // library folder -> time its mapping was saved (shows "Checking…")
  var otherOpen = {};    // library folders where "Other path…" was chosen
  var main = document.getElementById("main");
  var MASK = "__unchanged__";
  var GB = 1e9;
  var KINDS = ["movie", "tv", "audio"];
  var KIND_NAME = { movie: "Movies", tv: "TV", audio: "Audio" };
  var START_HELP = {
    after_nowplaying: "Starts as soon as this episode’s Now-playing preload is done, so the two don’t compete for the disks. If Now-playing is off, starts right away.",
    "80": "Starts 80% of the way through. Late enough to skip episodes people abandon, with plenty of time to finish.",
    "90": "Starts 90% of the way through — about when Plex marks an episode watched (its “Video played threshold”). Read late, so it’s still in RAM when the next episode starts."
  };
  function startHelp(v) {
    if (v !== "default") return START_HELP[v] || "";
    var l2 = ST.l2arc === "present";
    return "Default picks for you. " + (l2
      ? "Your media pool has an L2ARC, so this uses “After Now-playing preload finishes” — the SSD cache keeps the next episode until it’s needed."
      : "No L2ARC was found, so this uses “At 90%” — reading late keeps the next episode in RAM until it starts.");
  }

  // ------------------------------------------------------------------ utils
  // ---- Info buttons: longer explanations sit behind a small "i" next to what they explain.
  // Hover or focus shows them; a click (or tap) pins them open until you click elsewhere.
  var INFO = {}, INLINE_MAX = 70;
  function info(html, about) {
    if (!html) return "";
    var h = 0;
    for (var i = 0; i < html.length; i++) h = (h * 31 + html.charCodeAt(i)) | 0;
    var id = "i" + (h >>> 0).toString(36);
    INFO[id] = html;
    return '<button type="button" class="info" data-info="' + id + '" aria-label="' + esc("About " + (about || "this")) +
      '" aria-expanded="false" aria-controls="infopop"><span aria-hidden="true">i</span></button>';
  }
  function plainLen(h) { return String(h).replace(/<[^>]*>/g, "").length; }
  // Field help is either a string (short stays under the field, long goes behind an info button)
  // or {s: "shown under the field", more: "behind the info button"}.
  function splitHelp(help) {
    if (!help) return { s: "", more: "" };
    if (typeof help === "object") return { s: help.s || "", more: help.more || "" };
    return plainLen(help) <= INLINE_MAX ? { s: help, more: "" } : { s: "", more: help };
  }
  function helpDiv(h) { return h.s ? '<div class="help">' + h.s + "</div>" : ""; }
  // A card heading with its explanation behind an info button (short ones stay as a line under it).
  function heading(title, hint, extra) {
    var long = hint && plainLen(hint) > 90;
    return "<h2>" + title + (long ? info(hint, title.replace(/<[^>]*>/g, "").replace(/&amp;/g, "&")) : "") + "</h2>" + (extra || "") + (hint && !long ? '<p class="hint">' + hint + "</p>" : "");
  }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtBytes(n) {
    if (n == null) return "unknown";
    var u = [["TB", 1e12], ["GB", 1e9], ["MB", 1e6], ["KB", 1e3]];
    for (var i = 0; i < u.length; i++) if (n >= u[i][1]) return (n / u[i][1]).toFixed(n < 10 * u[i][1] ? 1 : 0) + " " + u[i][0];
    return Math.round(n) + " B";
  }
  function fmtNum(n) { return (n || 0).toLocaleString(); }
  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000), now = new Date();
    var t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return "Today " + t;
    var y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return "Yesterday " + t;
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + ", " + t;
  }
  function fmtIso(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return fmtTime(d.getTime() / 1000);
  }
  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? h + " h " + m + " m" : (m ? m + " m " + s + " s" : s + " s");
  }
  function ago(ts) {
    if (!ts) return "never";
    var s = Date.now() / 1000 - ts;
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " minutes ago";
    if (s < 86400) return Math.round(s / 3600) + " hours ago";
    return Math.round(s / 86400) + " days ago";
  }
  function toast(msg) {
    var t = document.getElementById("toast");
    t.textContent = msg; t.classList.add("show");
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.classList.remove("show"); }, 2600);
  }
  function api(path, body) {
    var opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) { location.reload(); return new Promise(function () {}); }  // signed out: show the sign-in page
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }
  function light(status, title, detail, testing) {
    var st = testing ? "testing" : status;
    return '<div class="lightrow"><span class="light l-' + esc(st) + '" aria-hidden="true"></span><div class="grow"><div class="what">' +
      esc(testing ? "Checking…" : title) + '</div>' + (detail && !testing ? '<div class="det">' + esc(detail) + "</div>" : "") + "</div></div>";
  }
  function banner(sum, testing) {
    if (testing) return '<div class="banner bn-testing" role="status"><span class="light l-testing" aria-hidden="true"></span>Checking your settings…</div>';
    if (!sum) return "";
    return '<div class="banner bn-' + esc(sum.status) + '" role="status"><span class="light l-' + esc(sum.status) + '" aria-hidden="true"></span>' + esc(sum.text) + "</div>";
  }
  // openByDefault: shown open until you close it (e.g. the API-key steps while there's no working key).
  function details(key, summary, body, openByDefault) {
    var open = key in openDetails ? openDetails[key] : !!openByDefault;
    return '<details data-key="' + esc(key) + '" data-def="' + (openByDefault ? 1 : 0) + '"' + (open ? " open" : "") + "><summary>" + summary + "</summary>" + body + "</details>";
  }
  function badge(status, text) { return '<span class="badge b-' + esc(status) + '">' + esc(text) + "</span>"; }
  var STATUS_TEXT = { pass: "Pass", warn: "Warn", fail: "Fail", info: "Info" };
  var VERDICT_TEXT = { use: "Use", maybe: "Maybe", avoid: "Unavailable" };

  // --------------------------------------------------------------- saving
  var pending = {}, saveTimer = null;
  function setLocal(sec, key, val) { S[sec][key] = val; }
  function queueSave(sec, key, val, now) {
    setLocal(sec, key, val);
    pending[sec] = pending[sec] || {};
    pending[sec][key] = val;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, now ? 0 : 400);
  }
  function flush() {
    var body = pending; pending = {};
    if (!Object.keys(body).length) return Promise.resolve();
    return api("/api/settings", body).then(function (r) {
      S = r.settings; if (ST) ST.settings = S;
      toast("Saved");
      return load();
    }).catch(function (e) { toast("Not saved: " + e.message); return load(); });
  }

  // ------------------------------------------------------------- planning
  // Mirrors plexcache/warmup.py plan() so sliders update instantly.
  var DEFAULT_INDEX = { movie: 512 * 1024, tv: 256 * 1024, audio: 0 };  // per file, until a run measures yours
  function plan(stats, w, l2) {
    var pad = { movie: w.padding_video, tv: w.padding_video, audio: w.padding_audio };
    var mx = {}, mn = {}, idx = {}, needMax = 0, needMin = 0, measured = ST.index_avg || {};
    KINDS.forEach(function (k) {
      var perSec = (stats.avg_kbps[k] || 0) * 125 * (stats.count[k] || 0);
      mx[k] = perSec * w[k + "_sec"] * (100 + pad[k]) / 100;
      mn[k] = perSec * w[k + "_min_sec"] * (100 + pad[k]) / 100;
      idx[k] = (stats.count[k] || 0) * (measured[k] != null ? measured[k] : DEFAULT_INDEX[k]);  // always cached
      needMax += mx[k] + idx[k]; needMin += mn[k] + idx[k];
    });
    var reserve = w.reserve_gb * GB, seconds = {}, status = "unknown";
    KINDS.forEach(function (k) { seconds[k] = w[k + "_sec"]; });
    if (l2 != null) {
      var avail = l2 - reserve;
      if (l2 <= 0) status = "none";
      else if (needMax <= avail * 0.9) status = "fits";
      else if (needMax <= avail) status = "tight";
      else if (needMin <= avail) {
        var wig = needMax - needMin, f = wig > 0 ? Math.min(1, (needMax - avail) / wig) : 1;
        KINDS.forEach(function (k) { var hi = w[k + "_sec"], lo = w[k + "_min_sec"]; seconds[k] = Math.max(lo, Math.floor((hi - (hi - lo) * f) * 10) / 10); });
        status = "reduced";
      } else { KINDS.forEach(function (k) { seconds[k] = w[k + "_min_sec"]; }); status = "insufficient"; }
    }
    var used = {};
    // Each type's share includes its files' indexes (the end of each file players read first).
    KINDS.forEach(function (k) { used[k] = (w[k + "_sec"] ? mx[k] * seconds[k] / w[k + "_sec"] : 0) + idx[k]; });
    var index = idx.movie + idx.tv + idx.audio;
    return { needMax: needMax, need: mx, used: used, index: index, indexMeasured: !!ST.index_avg, reserve: reserve, l2: l2, seconds: seconds, status: status };
  }

  // ---------------------------------------------------------------- pages
  function pageHealth() {
    var h = ST.health;
    if (!h) return top("Health", "Running the first checks…", "") + '<p class="muted">Checking Plex, your media mounts and ZFS. This takes a few seconds.</p>';
    var c = h.counts;
    var out = top("Health",
      "Checks run on startup, every 6 hours, and whenever settings change. Last run " + esc(ago(h.at)) + ".",
      '<button class="btn pri" data-act="recheck"' + (ST.checking ? " disabled" : "") + ">" + (ST.checking ? "Checking…" : "Re-run all checks") + "</button>");
    out += banner(h.summary, false);
    out += '<div class="tiles">' +
      tile(c.pass, "checks passed", "var(--ok)") +
      tile(c.warn, c.warn === 1 ? "warning — features still run" : "warnings — features still run", "var(--warn)") +
      tile(c.fail, c.fail === 1 ? "failure" : "failures", c.fail ? "var(--bad)" : "var(--muted)") + "</div>";
    out += '<div class="stack">';
    h.groups.forEach(function (g) {
      out += '<section class="card">' + heading(esc(g.title), esc(g.hint));
      if (g.table && g.table.length) {
        out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Library</th><th scope="col">Folders (Plex → here)</th><th scope="col">Items</th><th scope="col">Readable here</th></tr></thead><tbody>';
        g.table.forEach(function (r) {
          var folders = r.folders.length ? r.folders.map(function (f) {
            return '<div class="mono">' + esc(f.plex_path) + (f.local_path !== f.plex_path ? ' <span class="muted">→ ' + esc(f.local_path) + "</span>" : "") + "</div>";
          }).join("") : '<span class="muted">' + esc(r.detail || "") + "</span>";
          out += "<tr><td>" + esc(r.library) + "</td><td>" + folders + '</td><td class="nw">' + (r.status === "info" ? "—" : fmtNum(r.count)) +
            "</td><td>" + badge(r.status, r.badge || STATUS_TEXT[r.status] || r.status) +
            (r.status !== "pass" && r.status !== "info" ? '<div class="det">' + esc(r.detail) + "</div>" : "") + "</td></tr>";
        });
        out += "</tbody></table></div>";
        g.table.forEach(function (r) {
          if (r.fix) out += '<div class="fix"><strong>' + esc(r.library) + ":</strong> " + esc(r.fix) + ' <a href="/settings#mappings" data-nav>Open Media mappings</a></div>';
        });
      }
      if (g.datasets && g.datasets.length) {
        out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Dataset</th><th scope="col">In use</th><th scope="col">Cache settings</th><th scope="col">Record size</th><th scope="col">L2ARC</th></tr></thead><tbody>';
        g.datasets.forEach(function (r) {
          var c = function (x) { return x ? badge(x.status, x.badge || STATUS_TEXT[x.status] || x.status) + (x.text ? '<div class="det">' + esc(x.text) + "</div>" : "") : '<span class="muted">—</span>'; };
          out += '<tr><td class="mono nw">' + esc(r.dataset) + (r.mountpoint !== r.dataset ? '<div class="det">at ' + esc(r.mountpoint) + "</div>" : "") + "</td><td>" + c(r.in_use) + "</td><td>" + c(r.cache) + "</td><td>" + c(r.recordsize) + (r.recordsize ? scanCtl(r.dataset) : "") + "</td><td>" + c(r.l2arc) + "</td></tr>";
        });
        out += "</tbody></table></div>";
        g.datasets.forEach(function (r) { r.fixes.forEach(function (it) { out += itemRow(it); }); });
      }
      g.items.forEach(function (it) { out += itemRow(it); });
      out += "</section>";
    });
    out += "</div>";
    return out;
  }

  // "Check every file" for a dataset: Health otherwise looks at a random sample on each check.
  function scanCtl(ds) {
    var sc = (ST.block_scans || {})[ds];
    if (sc && sc.running) return '<div class="det">Checking every file… ' + fmtNum(sc.checked) + " so far</div>";
    var when = sc && sc.at && sc.complete ? '<span class="muted">checked ' + esc(ago(sc.at)) + "</span>" : "";
    return '<div class="det scanctl"><button class="link" type="button" data-act="blockscan" data-ds="' + esc(ds) + '">' +
      (when ? "Check again" : "Check every file") + "</button>" + when + info(
        "Health looks at a random sample of about 300 files on each check, so a few older files can go unnoticed. " +
        "This reads the details (not the contents) of every file on the dataset, which takes a minute or two for a large library, and changes nothing.",
        "checking every file") + "</div>";
  }

  function itemRow(it, extra) {
    var out = '<div class="row">' + badge(it.status, STATUS_TEXT[it.status] || it.status) + '<div class="grow"><div class="what">' + esc(it.title) +
      '</div><div class="det">' + esc(it.detail) + "</div>";
    if (it.fix || it.fix_cmd) {
      // {APP} in a command is this app's own address (e.g. to download a script it serves).
      var cmd = it.fix_cmd ? it.fix_cmd.split("{APP}").join(location.origin) : "";
      out += '<div class="fix">' + esc(it.fix);
      if (cmd) out += '<div class="cmd"><code style="white-space:pre-wrap">' + esc(cmd) + '</code><button class="btn" data-copy="' + esc(cmd) + '">Copy</button></div>';
      out += "</div>";
    }
    return out + (extra || "") + "</div></div>";
  }

  function tile(n, label, color) {
    return '<div class="card tile"><div class="big" style="color:' + color + '">' + esc(n || 0) + '</div><div class="det" style="margin:0">' + esc(label) + "</div></div>";
  }
  function top(title, sub, actions) {
    return '<div class="top"><div><h1>' + esc(title) + '</h1><div class="sub">' + sub + "</div></div>" + (actions || "") + "</div>";
  }
  function rec(feature) {
    var r = ST.health && ST.health.recommendations.filter(function (x) { return x.feature === feature; })[0];
    if (!r) return "";
    var text = { use: "Recommended", maybe: "Check first", avoid: "Unavailable" }[r.verdict];
    // Warm-up blocked by a missing SSD cache says so; anything else needs a setting changed.
    if (r.verdict === "avoid") text = feature === "warmup" && ST.l2arc === "absent" ? "L2ARC Required" : "Configuration required";
    return badge(r.verdict, text);
  }
  function unavailable(feature) {
    var r = ST.health && ST.health.recommendations.filter(function (x) { return x.feature === feature; })[0];
    return r && r.verdict === "avoid" ? r.reason : "";
  }
  // Why a feature can't run on this NAS right now ("" if it can).
  function blocked(sec) {
    if (sec === "warmup" && ST.l2arc === "absent") return "Needs an L2ARC (SSD cache) on the media pool";
    return unavailable(sec);
  }
  // On means switched on AND able to run. An unavailable feature shows as off and can't be
  // switched on; its saved choice is kept, so it comes back by itself once the problem is fixed.
  function isOn(sec) { return !!S[sec].enabled && !blocked(sec); }
  function toggle(sec, label) {
    var why = blocked(sec);
    return '<button class="sw" type="button" aria-pressed="' + (isOn(sec) ? "true" : "false") + '" aria-label="' + esc(label) + '" data-toggle="' + sec + '"' +
      (why ? ' disabled title="' + esc(why) + '"' : "") + "><span></span></button>";
  }
  function unavailableNote(sec, html) {
    return '<div class="note"><strong>Not available:</strong> ' + html +
      (S[sec].enabled ? " It switches back on by itself once that’s fixed." : "") + "</div>";
  }
  function guideBtn(sec) {
    return '<button class="link" type="button" data-guide="' + sec + '" aria-expanded="' + guides[sec] + '">' + (guides[sec] ? "Hide how it works" : "How it works") + "</button>";
  }
  function guide(sec, cols) {
    if (!guides[sec]) return "";
    return '<div class="guide">' + cols.map(function (c) { return "<div><h3>" + c[0] + "</h3><p>" + c[1] + "</p></div>"; }).join("") + "</div>";
  }
  // A small "↺" that puts a setting back to its default; only shown once it's been changed.
  // Sliders always get one; other fields only when listed here.
  var REVERT = { "general.poll_seconds": 1, "general.ram_reserve_gb": 1, "warmup.pause_disk_busy": 1 };
  function revertInner(sec, key, unit) {
    var d = ST.defaults && ST.defaults[sec] ? ST.defaults[sec][key] : undefined;
    if (d === undefined || Number(S[sec][key]) === Number(d)) return "";
    var shown = d === 0 && sec + "." + key === "warmup.pause_disk_busy" ? "off" : d + (unit ? " " + unit : "");
    return '<button type="button" class="revert" data-revert="' + sec + "." + key + '" title="Back to the default (' + esc(shown) +
      ')" aria-label="Reset to the default, ' + esc(shown) + '">↺</button>';
  }
  // A slot that's always there, so the button can appear the moment a value changes (no redraw needed).
  function revertBtn(sec, key, unit) {
    return '<span class="rv-slot" data-rv="' + sec + "." + key + '" data-unit="' + esc(unit || "") + '">' + revertInner(sec, key, unit) + "</span>";
  }
  function refreshRevert(sec, key) {
    var slot = main.querySelector('.rv-slot[data-rv="' + sec + "." + key + '"]');
    if (slot) slot.innerHTML = revertInner(sec, key, slot.dataset.unit);
  }
  function range(sec, key, label, min, max, step, unit, help) {
    var id = "f-" + sec + "-" + key;
    var h = splitHelp(help);
    return '<div class="fld"><label for="' + id + '">' + esc(label) + info(h.more, label) + '<span class="val" data-val="' + id + '">' + esc(S[sec][key]) + " " + unit + "</span>" + revertBtn(sec, key, unit) + "</label>" +
      '<input id="' + id + '" type="range" min="' + min + '" max="' + max + '" step="' + step + '" value="' + esc(S[sec][key]) + '" data-set="' + sec + "." + key + '" data-unit="' + unit + '">' +
      helpDiv(h) + "</div>";
  }
  function select(sec, key, label, options, help) {
    var id = "f-" + sec + "-" + key;
    var cur = String(S[sec][key]), h = splitHelp(help);
    return '<div class="fld"><label for="' + id + '">' + esc(label) + info(h.more, label) + (REVERT[sec + "." + key] ? revertBtn(sec, key, "") : "") + '</label><select id="' + id + '" data-set="' + sec + "." + key + '">' +
      options.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (String(o[0]) === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join("") +
      "</select>" + helpDiv(h) + "</div>";
  }
  function number(sec, key, label, min, max, help) {
    var id = "f-" + sec + "-" + key, h = splitHelp(help);
    return '<div class="fld"><label for="' + id + '">' + esc(label) + info(h.more, label) + (REVERT[sec + "." + key] ? revertBtn(sec, key, "") : "") + '</label><input id="' + id + '" type="number" min="' + min + '" max="' + max + '" value="' + esc(S[sec][key]) + '" data-set="' + sec + "." + key + '">' +
      helpDiv(h) + "</div>";
  }

  function estimateHtml() {
    var st = ST.library.stats, w = S.warmup;
    if (!st.scanned_at) return '<div class="est"><div class="muted">' + (ST.library.scanning ? "Reading your libraries to estimate space…" : "Connect Plex to see a space estimate.") + "</div></div>";
    var p = plan(st, w, ST.zfs.l2_total);
    var total = Math.max(p.l2 || 0, p.needMax + p.reserve, 1);
    function pct(x) { return Math.max(0, Math.min(100, x / total * 100)).toFixed(2); }
    var head, cls;
    if (p.l2 == null) { head = "Needs about " + fmtBytes(p.needMax + p.reserve) + " of L2ARC (incl. " + w.reserve_gb + " GB kept free)"; cls = ["info", "L2ARC size unknown"]; }
    else {
      head = "Needs " + fmtBytes(p.needMax + p.reserve) + " of your " + fmtBytes(p.l2) + " L2ARC";
      cls = { fits: ["pass", "Fits"], tight: ["warn", "Tight fit"], reduced: ["warn", "Too big — seconds reduced automatically"],
        insufficient: ["fail", "Doesn't fit even at minimum seconds"], none: ["fail", "No L2ARC found"] }[p.status];
    }
    var out = '<div class="est"><div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;align-items:baseline"><div style="font-weight:600">' + esc(head) + "</div>" + badge(cls[0], cls[1]) + "</div>";
    out += '<div class="bar" role="img" aria-label="L2ARC space estimate">' +
      '<div style="width:' + pct(p.used.movie) + '%;background:var(--c-movie)"></div><div style="width:' + pct(p.used.tv) + '%;background:var(--c-tv)"></div>' +
      '<div style="width:' + pct(p.used.audio) + '%;background:var(--c-audio)"></div>' +
      '<div style="width:' + pct(p.reserve) + '%;background:var(--c-res)"></div></div>';
    out += '<div class="legend"><span><i style="background:var(--c-movie)"></i>Movies ' + fmtBytes(p.used.movie) + "</span><span><i style=\"background:var(--c-tv)\"></i>TV " + fmtBytes(p.used.tv) +
      '</span><span><i style="background:var(--c-audio)"></i>Audio ' + fmtBytes(p.used.audio) + "</span>" +
      '<span><i style="background:var(--c-res)"></i>Kept free ' + w.reserve_gb + " GB</span>" +
      (p.l2 != null ? "<span>Unused " + fmtBytes(Math.max(0, p.l2 - p.needMax - p.reserve)) + "</span>" : "") + "</div>";
    if (p.status === "reduced" || p.status === "insufficient") {
      out += '<div class="help muted" style="font-size:13px;margin-top:6px">Will use: ' + KINDS.map(function (k) { return KIND_NAME[k] + " " + p.seconds[k] + " s"; }).join(", ") + "</div>";
    }
    out += '<div class="muted" style="font-size:13px;margin-top:8px">Based on ' + fmtNum(st.count.movie) + " movies (avg " + (st.avg_kbps.movie / 1000).toFixed(1) + " Mbps), " +
      fmtNum(st.count.tv) + " episodes (" + (st.avg_kbps.tv / 1000).toFixed(1) + " Mbps) and " + fmtNum(st.count.audio) + " tracks (" + st.avg_kbps.audio +
      " kbps), plus a small safety margin. Each file is read using its own bitrate.</div></div>";
    return out;
  }

  // How big an L2ARC Library warm-up needs, worked out from your library like the original script:
  // titles × average bitrate × seconds of the start, plus a safety margin, file indexes and the
  // space kept free. Shown even without an L2ARC, since that's when it's most useful.
  var SSD_SIZES = [128, 256, 500, 1000, 2000, 4000, 8000];  // GB, as drives are sold
  var GROWTH = 0.25;  // room for the library to grow
  function sizingHtml(standalone) {
    var st = ST.library.stats, w = S.warmup;
    if (!st || !st.scanned_at) return standalone ? '<div class="sizing"><p class="muted" style="margin:14px 0 0">' +
      (ST.library.scanning ? "Reading your libraries to work out how big an L2ARC you’d need…" : "Connect Plex to see how big an L2ARC you’d need.") + "</p></div>" : "";
    var pad = { movie: w.padding_video, tv: w.padding_video, audio: w.padding_audio }, measured = ST.index_avg || {};
    var rows = [], tot = { min: 0, set: 0 };
    // One row per library (audiobooks and music have very different bitrates), using its type's seconds.
    var libs = st.libraries && st.libraries.length ? st.libraries :
      KINDS.map(function (k) { return { name: KIND_NAME[k], kind: k, count: st.count[k] || 0, avg_kbps: st.avg_kbps[k] || 0 }; });
    libs.forEach(function (l) {
      var k = l.kind, perSec = (l.avg_kbps || 0) * 125 * l.count * (100 + pad[k]) / 100;
      var idx = l.count * (measured[k] != null ? measured[k] : DEFAULT_INDEX[k]);  // each file's index, always cached
      var r = { name: l.name, n: l.count, kbps: l.avg_kbps || 0, min: perSec * w[k + "_min_sec"] + idx, set: perSec * w[k + "_sec"] + idx,
        minSec: w[k + "_min_sec"], setSec: w[k + "_sec"], kind: KIND_NAME[k] };
      tot.min += r.min; tot.set += r.set;
      rows.push(r);
    });
    var reserve = w.reserve_gb * GB;
    var need = { min: tot.min + reserve, set: tot.set + reserve };
    var advise = need.set * (1 + GROWTH);
    var ssd = SSD_SIZES.filter(function (g) { return g * GB >= advise; })[0];
    // L2ARC keeps a small record in RAM for every block it holds (about 100 bytes each).
    var rs = ((ST.zfs.datasets || []).map(function (d) { return d.props && d.props.recordsize; }).filter(Boolean)[0]) || 131072;
    var headerRam = advise / rs * 100;
    var kbps = function (v) { return v >= 1000 ? (v / 1000).toFixed(1) + " Mbps" : Math.round(v) + " kbps"; };
    var table = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col"></th><th scope="col">Titles</th><th scope="col">Avg bitrate</th>' +
      '<th scope="col">Minimum</th><th scope="col">Your settings</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      table += "<tr><td>" + esc(r.name) + (r.name.toLowerCase().indexOf(r.kind.toLowerCase()) < 0 ? ' <span class="muted">· ' + esc(r.kind) + "</span>" : "") + '</td><td class="nw">' + fmtNum(r.n) + '</td><td class="nw">' + (r.n ? kbps(r.kbps) : "—") +
        '</td><td class="nw">' + esc(fmtBytes(r.min)) + ' <span class="muted">· ' + r.minSec + ' s</span></td><td class="nw">' + esc(fmtBytes(r.set)) +
        ' <span class="muted">· ' + r.setSec + " s</span></td></tr>";
    });
    var both = function (v, note) { var c = '<td class="nw">' + esc(fmtBytes(v)) + (note ? ' <span class="muted">· ' + note + "</span>" : "") + "</td>"; return c + c; };
    table += "<tr><td>Kept free</td><td></td><td></td>" + both(reserve) + "</tr>" +
      '<tr class="sum"><th scope="row">Total</th><td></td><td></td><td class="nw">' + esc(fmtBytes(need.min)) + '</td><td class="nw">' + esc(fmtBytes(need.set)) + "</td></tr></tbody></table></div>";
    var l2 = ST.zfs.l2_total, head;
    if (ST.l2arc === "absent" || !l2) {
      head = "For Library warm-up, add an L2ARC of at least " + fmtBytes(need.set) + (ssd ? " — a " + (ssd >= 1000 ? ssd / 1000 + " TB" : ssd + " GB") + " SSD leaves room to grow." : ".");
    } else {
      head = "Your " + fmtBytes(l2) + " L2ARC " + (l2 >= advise ? "holds your settings with room for the library to grow." :
        l2 >= need.set ? "holds your settings, with a little room to grow." : l2 >= need.min ? "holds the minimum; seconds are reduced to fit." :
        "is smaller than the minimum (" + fmtBytes(need.min) + ").");
    }
    var why = "Each title’s first seconds × its library’s average bitrate (" + w.padding_video + "% extra for video, " + w.padding_audio + "% for audio, for bitrate spikes)" +
", plus each file’s seek index (" + (ST.index_avg ? "measured by warm-up" : "estimated until warm-up has measured yours") +
      "), plus the space kept free for everything else ZFS caches. " +
      "<strong>Minimum</strong> is the least warm-up will cache (" + w.movie_min_sec + " s / " + w.tv_min_sec + " s / " + w.audio_min_sec + " s); <strong>Your settings</strong> is what it aims for. " +
      "The suggested SSD adds " + Math.round(GROWTH * 100) + "% for the library to grow. " +
      "An L2ARC also uses a little RAM to keep track of what it holds — about " + fmtBytes(headerRam) + " for " + fmtBytes(advise) + " at this dataset’s " +
      Math.round(rs / 1024) + " KiB record size.";
    var body = '<p style="font-weight:600;margin:12px 0 8px">' + esc(head) + info(why, "L2ARC sizing") + "</p>" + table +
      '<p class="muted" style="font-size:13px;margin:8px 0 0">From ' + fmtNum(st.count.movie + st.count.tv + st.count.audio) + " titles in the libraries you’re caching.</p>";
    if (standalone) return '<div class="sizing">' + body + "</div>";
    return '<div class="sizing">' + details("l2-sizing", "L2ARC sizing", body) + "</div>";
  }

  // The settings that decide the size, for when warm-up's own controls aren't shown (no L2ARC yet).
  // They're the real settings: what you pick here is what warm-up uses once it can run.
  function sizingControls() {
    return '<div class="grid">' +
      range("warmup", "movie_sec", "Movies — seconds to cache", 3, 180, 1, "s", "") +
      range("warmup", "tv_sec", "TV episodes — seconds to cache", 3, 180, 1, "s", "") +
      range("warmup", "audio_sec", "Audio — seconds to cache", 1, 60, 1, "s", "") +
      range("warmup", "reserve_gb", "Keep free in L2ARC", 0, 500, 10, "GB", "Room left for everything else ZFS wants to cache.") + "</div>";
  }
  function sizingAdvanced() {
    return details("l2-advanced", "Minimums and safety margins",
      '<p class="help" style="margin:8px 0 0">When the L2ARC is too small for your seconds, warm-up cuts them back, but never below these.</p>' +
      '<div class="grid" style="margin-top:8px">' +
      number("warmup", "movie_min_sec", "Movies — minimum seconds", 1, 600, "") +
      number("warmup", "tv_min_sec", "TV episodes — minimum seconds", 1, 600, "") +
      number("warmup", "audio_min_sec", "Audio — minimum seconds", 1, 600, "") +
      number("warmup", "padding_video", "Video safety margin (%)", 0, 100, "Extra on top of the average bitrate, for scenes that run higher.") +
      number("warmup", "padding_audio", "Audio safety margin (%)", 0, 100, "") + "</div>");
  }

  function checkbox(sec, key, label, help) {
    var h = splitHelp(help);
    return '<div class="chkblk"><div class="chk"><label><input type="checkbox" data-check="' + sec + "." + key + '"' + (S[sec][key] ? " checked" : "") + ">" + esc(label) + "</label>" + info(h.more, label) + "</div>" +
      helpDiv(h) + "</div>";
  }

  // Library warm-up steps aside for these, then carries on from where it was.
  function pauseCard() {
    var w = S.warmup, timed = w.schedule === "daily" || w.schedule === "weekly";
    var state = ST.l2arc === "absent" ? "Library warm-up is unavailable on this NAS (it needs an L2ARC), so these only matter once one is added." :
      (isOn("warmup") ? "" : "Library warm-up is switched off in Features; these apply whenever it runs.");
    return '<section class="card" id="warmup-pauses">' + heading("Library warm-up pauses", "Warm-up steps aside for any of these, then carries on where it left off.") +
      (state ? '<div class="note" style="background:var(--guide-bg);border-color:var(--guide-line);color:var(--ink)">' + esc(state) + "</div>" : "") +
      checkbox("warmup", "pause_buffering", "A player is buffering", "Plex reports a stream waiting for data. Stays paused for 3 minutes after it clears.") +
      checkbox("warmup", "pause_preload", "Now-playing or Next-episode is reading", "Someone watching now comes first — usually a pause of a minute or two while a title loads.") +
      checkbox("warmup", "pause_scrub", "A scrub or resilver is running", "They already keep every disk in the pool busy; warm-up would only slow them down. Needs the TrueNAS API.") +
      '<div class="grid" style="margin-top:8px">' +
      select("warmup", "pause_disk_busy", "The pool’s disks are busy with other work", [[40, "40% busy or more"], [50, "50% busy or more"], [60, "60% busy or more"], [0, "Don’t pause for this"]],
        "File copies, backups or other apps. Measured while warm-up itself is idle, so its own reads don’t count. “Run now” ignores this.") +
      '<div class="fld"><span class="lbl">Finish by' + info((timed ? "If a run is still going then, it waits and carries on at the next start (" + esc(w.time) + ")." :
          "Applies to daily and weekly schedules: a run still going then waits for the next start.") + " “Run now” ignores this.", "Finish by") + "</span>" + checkbox("warmup", "finish_by_enabled", "Stop for the day at", "") +
        '<input type="time" value="' + esc(w.finish_by) + '" data-set="warmup.finish_by" aria-label="Finish by"' + (w.finish_by_enabled ? "" : " disabled") + "></div>" +
      "</div></section>";
  }

  function warmupStatus() {
    var wu = ST.warmup;
    if (wu.running) {
      var p = wu.progress || { done: 0, total: 0, bytes: 0 };
      var pct = p.total ? Math.round(p.done / p.total * 100) : 0;
      return '<div class="note" style="background:var(--guide-bg);border-color:var(--guide-line);color:var(--ink)"><div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between"><div>' +
        (p.paused ? badge("warn", "Paused") : badge("running", wu.phase === "scanning" ? "Preparing" : "Running")) + " " +
        (p.total ? fmtNum(p.done) + " of " + fmtNum(p.total) + " files · " + fmtBytes(p.bytes) + " read" : "Reading library list…") +
        (p.paused ? '<div class="help" style="margin:4px 0 0">Paused: ' + esc(p.paused) + ".</div>" : "") +
        '</div><button class="btn danger" data-act="warmup-stop">Stop</button></div><div class="meter"><div style="width:' + pct + '%"></div></div></div>';
    }
    return "";
  }

  function reserveHelp() {
    var r = ST.ram, why = "Now-playing and Next-episode never fill this much of the ZFS cache, so TrueNAS, shares and other apps keep their cached data.";
    if (!r || !r.cache_max) return { more: why };
    return { s: "Available now: " + fmtBytes(r.available) + " · left for preloads: " + fmtBytes(r.usable || 0),
      more: why + " “Available” is what the ZFS cache holds (" + fmtBytes(r.arc_size || 0) + ") plus free RAM; RAM used by apps and services doesn’t count. " +
        "Preloads share what’s left after this reserve. It changes as apps use more or less RAM." };
  }

  // "≈ 20.8 GB of the 52 GB usable RAM cache, shared by all streams"
  function ramHelp(feature, how) {
    var r = ST.ram;
    if (!r || !r.usable) return { s: r && r.cache_max ? "No ZFS cache is left after the reserve (Settings → Behaviour)." : "Share of the usable ZFS cache.", more: how };
    return { s: "About " + fmtBytes(r[feature]) + " right now, shared by all streams",
      more: how + " " + r_pct(feature) + "% of the " + fmtBytes(r.usable) + " usable: ZFS cache available " + fmtBytes(r.available) + " minus " +
        fmtBytes(r.reserve) + " left for the rest of the NAS (Settings → Behaviour)." };
  }
  function r_pct(feature) { return S[feature].ram_percent; }
  function speedGuess() {
    var sp = S.general.disk_speeds || {}, best = 0;
    Object.keys(sp).forEach(function (k) { best = Math.max(best, sp[k].mb_s); });
    return best || S.general.hdd_mb_per_sec;
  }

  function pageFeatures() {
    var out = top("Features", "Each feature can be switched on separately. Open “How it works” on any card for when to use it and what it needs. Changes save automatically.", "");
    // Datasets whose settings keep file data out of RAM: their libraries can't be cached.
    var badDs = (ST.zfs.datasets || []).filter(function (d) { return d.props && d.props.primarycache && d.props.primarycache !== "all"; });
    // ---- warm-up
    var w = S.warmup;
    var noL2 = ST.l2arc === "absent";
    out += '<div class="stack"><section class="card' + (isOn("warmup") ? "" : " off") + '"><div class="head">' +
      toggle("warmup", "Library warm-up") +
      "<h2>Library warm-up</h2>" + rec("warmup") + '<span style="flex:1"></span>' + guideBtn("warmup") + "</div>" +
      '<p class="lede">On a schedule, reads the first few seconds of every movie, episode and track so they land in the SSD cache (L2ARC). Playback then starts at SSD speed instead of waiting for the hard drives.</p>' +
      guide("warmup", [
        ["When it helps", "Pressing play on anything in the library, especially titles nobody has watched in a while. Large libraries on HDD pools see the biggest gain."],
        ["What it needs", "An L2ARC (SSD cache) on the media pool, and <span class=\"mono\">primarycache=all</span> plus <span class=\"mono\">secondarycache=all</span> on the media dataset — ZFS only fills L2ARC from data that passed through RAM. Health checks all three."],
        ["Cost", "Disk time while it runs (paced below) and L2ARC space (estimated below). Re-runs keep cached starts from being evicted."]]);
    if (noL2) {
      out += unavailableNote("warmup", "no L2ARC found. This feature stores the start of each title on an SSD cache (L2ARC), and the media pool doesn’t have one. " +
        "To add one in TrueNAS: Storage → your media pool → Manage Devices → Add VDEV → Cache, and pick an SSD.");
    } else if (unavailable("warmup")) {
      out += unavailableNote("warmup", esc(unavailable("warmup")));
    } else if (ST.l2arc === "unknown") {
      out += '<div class="note">Couldn’t confirm the media pool has an L2ARC. <a href="/settings" data-nav>Connect TrueNAS in Settings</a> to check.</div>';
    }
    out += warmupStatus();
    if (!isOn("warmup")) {
      out += '<h3 style="margin:18px 0 0;font-size:15px">How big an L2ARC you’d need</h3>' +
        '<p class="help" style="margin:4px 0 0">Try different settings to see what size of SSD to get. They’re saved, so warm-up uses them once it can run.</p>' +
        sizingControls() + sizingHtml(true) + sizingAdvanced();
    }
    if (isOn("warmup")) {
      var sched = select("warmup", "schedule", "Schedule", [["daily", "Daily"], ["weekly", "Weekly"], ["hours", "Every few hours"], ["manual", "Manual only"]]);
      var when = "";
      if (w.schedule === "daily" || w.schedule === "weekly") {
        when = '<div class="fld"><label for="f-warmup-time">At</label><div class="inline">' +
          (w.schedule === "weekly" ? '<select aria-label="Day" data-set="warmup.weekday">' + ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map(function (d, i) {
            return '<option value="' + i + '"' + (w.weekday === i ? " selected" : "") + ">" + d + "</option>"; }).join("") + "</select>" : "") +
          '<input id="f-warmup-time" type="time" value="' + esc(w.time) + '" data-set="warmup.time"></div><div class="help">' +
          (ST.warmup.next_run ? "Next run " + esc(fmtIso(ST.warmup.next_run)) : "") + "</div></div>";
      } else if (w.schedule === "hours") {
        when = number("warmup", "every_hours", "Every (hours)", 1, 168, ST.warmup.next_run ? "Next run " + esc(fmtIso(ST.warmup.next_run)) : "");
      } else {
        when = '<div class="fld"><span class="lbl">Runs only when you start it</span><div class="help">Use “Run now”.</div></div>';
      }
      out += '<div class="grid">' + sched + when +
        range("warmup", "pacing_percent", "Background pacing", 5, 100, 5, "% of disk time", "Lower is gentler on other workloads. “Run now” runs at full speed.") +
        '<div class="fld"><span class="lbl">Run it</span><div class="acts" style="margin-top:0"><button class="btn" data-act="warmup-run"' + (ST.warmup.running ? " disabled" : "") +
        '>Run now</button></div><div class="help">' + (ST.warmup.last_run ? "Last run " + esc(fmtIso(ST.warmup.last_run)) : "Hasn't run yet") + "</div></div>" +
        range("warmup", "movie_sec", "Movies — how much of the start to cache", 3, 180, 1, "s of playback", "Playback time from the start of each movie — not a wait before loading. Plex buffers 3–15 s locally, up to 60 s for remote or transcoded streams.") +
        range("warmup", "tv_sec", "TV episodes — how much of the start to cache", 3, 180, 1, "s of playback", "Playback time from the start of each episode. Episodes are usually lower bitrate, so more costs less.") +
        range("warmup", "audio_sec", "Audio — how much of the start to cache", 1, 60, 1, "s of playback", "Playback time from the start of each track or book. Tiny; rarely matters for space.") +
        range("warmup", "reserve_gb", "Keep free in L2ARC", 0, 500, 10, "GB", "Room left for everything else ZFS wants to cache.");
      var libsOn = (ST.locations || []).filter(function (l) { return !l.excluded; });
      var keys = {}; libsOn.forEach(function (l) { keys[l.key] = l.library; });
      var names = Object.keys(keys).map(function (k) { return keys[k]; });
      out += '<div class="fld" style="grid-column:1/-1"><span class="lbl">Libraries</span><div class="help" style="margin-top:0">' +
        (names.length ? "Caching " + esc(names.join(", ")) + ". " : "") + 'Choose libraries in <a href="/settings#mappings" data-nav>Settings → Media mappings</a>.</div></div>';
      out += "</div>" + estimateHtml() + sizingHtml(false) + sizingAdvanced() +
        '<p class="help" style="margin-top:14px">When it pauses (buffering, scrubs, busy disks, a finish-by time) is set in <a href="/settings#warmup-pauses" data-nav>Settings → Library warm-up pauses</a>.</p>';
    }
    out += "</section>";

    // ---- now playing
    var n = S.nowplaying;
    out += '<section class="card' + (isOn("nowplaying") ? "" : " off") + '"><div class="head">' + toggle("nowplaying", "Now-playing preload") +
      "<h2>Now-playing preload</h2>" + rec("nowplaying") + '<span style="flex:1"></span>' + guideBtn("nowplaying") + "</div>" +
      '<p class="lede">Reads ahead of whoever is watching, so skipping forward or back comes from cache instead of the hard drives.</p>' +
      guide("nowplaying", [
        ["When it helps", "Skipping intros, scrubbing, jumping back. Works even with no SSDs, as long as file data can stay in RAM."],
        ["What it needs", "<span class=\"mono\">primarycache=all</span> on the media dataset, so what’s read stays in RAM. With <span class=\"mono\">metadata</span> nothing is cached and this has no effect."],
        ["How it reads", "Once Plex reports the stream playing and its start-up buffer has had time to fill, it reads from the play position to the end, then the beginning — the whole file if it fits the RAM limit, otherwise as much as fits ahead of the viewer. If they jump past that, it starts again from there."],
        ["Cost", "RAM (the ZFS cache), up to the limit below for all streams together. Older one-off reads are what make room; data other apps use often stays."]]);
    if (unavailable("nowplaying")) out += unavailableNote("nowplaying", esc(unavailable("nowplaying")));
    if (isOn("nowplaying")) {
      if (badDs.length) {
        out += '<div class="note"><strong>From Health:</strong> ' + badDs.map(function (d) {
          return esc(d.libraries.join(", ")) + ' (on <span class="mono">' + esc(d.dataset) + "</span>, primarycache=" + esc(d.props.primarycache) + ")";
        }).join("; ") + " can’t be cached, so they’re skipped. <a href=\"/health\" data-nav>See the fix</a>.</div>";
      }
      out += '<div class="grid">' +
        range("nowplaying", "ram_percent", "RAM limit", 5, 90, 5, "%", ramHelp("nowplaying", "Reads the whole file if it fits, otherwise as much as fits ahead of the viewer.")) +
        select("nowplaying", "delay_sec", "When to start", [[0, "Automatic (after Plex’s buffer)"], [15, "15 s after playback starts"], [30, "30 s after playback starts"], [60, "60 s after playback starts"]],
          n.delay_sec ? "Counted from when Plex reports the stream playing." : "Waits until Plex reports the stream playing, then for the time the disks take to read Plex’s buffer, so it doesn’t compete with it.") +
        number("nowplaying", "max_streams", "Files to read at the same time", 1, 20,
          "If more people are watching, the others wait their turn — every stream still gets preloaded. " +
          "Hard drives slow right down when reading several files at once (the heads jump between them), so one or two at a time finishes each sooner. " +
          "Suggested: hard drives 2, SATA SSD 4, NVMe SSD 8.") +
        "</div>";
      if (!n.delay_sec) {
        out += details("np-timing", "Automatic start timing",
          '<p class="help" style="margin:8px 0 12px">The wait is Plex’s buffer read at the disks’ speed.' + info("Plex keeps filling its buffer after playback starts. The wait is that buffer — some seconds of playback at the file’s bitrate — read at the measured speed of its dataset (Settings → Disk speed). " +
          "For example, 60 s of an 80 Mbps remux is 600 MB: about " + Math.max(n.min_delay_sec, Math.round(600 / speedGuess())) + " s at " + speedGuess() + " MB/s.", "automatic timing") + "</p>" +
          '<div class="grid" style="margin-top:0">' +
          number("nowplaying", "buffer_direct_sec", "Plex’s buffer — direct play (seconds of playback)", 0, 600, "") +
          number("nowplaying", "buffer_transcode_sec", "Plex’s buffer — transcoding (seconds of playback)", 0, 600, "") +
          number("nowplaying", "min_delay_sec", "Never start sooner than (seconds)", 0, 600, "Gives the player time to open the file.") + "</div>");
      }
    }
    out += "</section>";

    // ---- next episode
    var x = S.nextepisode;
    out += '<section class="card' + (isOn("nextepisode") ? "" : " off") + '"><div class="head">' + toggle("nextepisode", "Next-episode prefetch") +
      "<h2>Next-episode prefetch</h2>" + rec("nextepisode") + '<span style="flex:1"></span>' + guideBtn("nextepisode") + "</div>" +
      '<p class="lede">While a TV episode plays, reads the start of the next one so it starts instantly. For music and audiobooks, reads ahead in the album or book — how far is chosen per library below. ' +
      "Uses Plex’s own order, so it crosses seasons and works with any folder layout.</p>" +
      guide("nextepisode", [
        ["When it helps", "Binge watching, and listening to an album or audiobook straight through. Removes the pause before the next episode or track, even without L2ARC."],
        ["What it needs", "<span class=\"mono\">primarycache=all</span> on the media dataset so the read stays cached. Plex tells the app what’s playing — no Tautulli, no scripts, no Plex Pass."],
        ["Cost", "One read of up to the limit below per episode watched, at a gentle pace."]]);
    if (unavailable("nextepisode")) out += unavailableNote("nextepisode", esc(unavailable("nextepisode")));
    if (isOn("nextepisode")) {
      out += '<div class="grid">' +
        select("nextepisode", "start_at", "Start prefetching the next episode", [
          ["default", "Default (" + (ST.l2arc === "present" ? "after Now-playing preload finishes" : "at 90%") + ")"],
          ["after_nowplaying", "After Now-playing preload finishes"],
          ["80", "At 80%"],
          ["90", "At 90% (about when Plex marks it watched)"]], startHelp(x.start_at)) +
        range("nextepisode", "ram_percent", "RAM limit", 5, 90, 5, "%", ramHelp("nextepisode", "Reads from the start of the episode, so the opening is cached first; the whole episode if it fits.")) +
        nextLibraries() + "</div>";
    }
    out += "</section></div>";
    return out;
  }

  // What Next-episode does for each TV, music and audiobook library.
  var TV_CHOICES = [["off", "Don’t preload"], ["next", "Next episode (default)"]];
  var AUDIO_CHOICES = [["off", "Don’t preload"], ["10", "10 minutes ahead"], ["20", "20 minutes ahead (default)"], ["60", "1 hour ahead"],
    ["rest", "Rest of the album / book"], ["album", "Whole album / book"], ["artist", "Everything by the artist / author"]];
  var PRELOAD_HELP =
    "<strong>Everything here stays within the RAM limit above</strong>, shared with every other stream — the bigger choices often won’t finish, and that’s fine: " +
    "they always read in order of what’s most likely to play next.<br><br>" +
    "<strong>Don’t preload</strong> — leave this library alone.<br>" +
    "<strong>10 / 20 minutes, 1 hour ahead</strong> — the next tracks in album order, whole files. A few tracks ahead is enough for the next one to start instantly; " +
    "10 minutes suits systems short on RAM, 20 is a good default.<br>" +
    "<strong>Rest of the album / book</strong> — everything after the current track. Good for audiobooks, which are rarely played out of order, and albums played start to finish.<br>" +
    "<strong>Whole album / book</strong> — the rest, then the tracks before this one too. For albums played on shuffle, where any track can come next.<br>" +
    "<strong>Everything by the artist / author</strong> — the rest of this album, then the first seconds of every other album (so any of them starts instantly), " +
    "then those albums in full. Only worth it if you hop between someone’s albums, and with plenty of RAM; with shuffle across a whole library it rarely pays off.<br><br>" +
    "TV libraries just read the start of the next episode. It tops up as each track starts, and skips anything read in the last few hours.";
  function nextLibraries() {
    var libs = ((ST.library && ST.library.libraries) || []).filter(function (l) { return !l.excluded && (l.kind === "tv" || l.kind === "audio"); });
    if (!libs.length) return "";
    var modes = S.nextepisode.library_modes || {};
    return '<div class="fld" style="grid-column:1/-1"><span class="lbl">What to preload, per library' + info(PRELOAD_HELP, "preload choices") + "</span>" +
      '<div class="libmodes">' + libs.map(function (l) {
        var opts = l.kind === "tv" ? TV_CHOICES : AUDIO_CHOICES, def = l.kind === "tv" ? "next" : "20", cur = modes[l.key] || def;
        return '<label class="libmode"><span>' + esc(l.title) + ' <span class="muted">· ' + (l.kind === "tv" ? "TV" : "music / audiobooks") + "</span>" +
          (cur !== def ? '<button type="button" class="revert" data-mode-revert="' + esc(l.key) + '" title="Back to the default" aria-label="Reset ' + esc(l.title) + ' to the default">↺</button>' : "") +
          '</span><select data-nemode="' + esc(l.key) + '" aria-label="' + esc("Preload for " + l.title) + '">' +
          opts.map(function (o) { return '<option value="' + o[0] + '"' + (o[0] === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join("") +
          "</select></label>";
      }).join("") + "</div></div>";
  }

  function pageActivity() {
    var z = ST.zfs, arc = z.arc, a = ST.activity;
    var out = top("Activity", "Whether caching is making playback better — and where it needs a hand.", "");
    out += periodSwitch() + reportHtml(ST.reports && ST.reports[actPeriod], z, arc, a);

    // sessions
    var ss = ST.playback.sessions;
    out += '<section class="card" style="margin-bottom:20px"><h2 style="margin-bottom:14px">Playing now</h2>';
    if (ST.playback.error) out += '<div class="note">' + esc(ST.playback.error) + "</div>";
    if (!ss.length) out += '<p class="muted" style="margin:0">Nothing is playing.</p>';
    ss.forEach(function (s) {
      out += '<div class="sess"><div><div class="what">' + esc(s.label) + '</div><div class="muted" style="font-size:13px">' + esc(s.player || "Unknown player") + " · " +
        (s.local === false ? "remote" : "local") + " · " + (s.transcoding ? "transcoding" : "direct play") + " · " + esc(s.progress) + "% watched</div></div><div>";
      if (!s.jobs.length) out += '<span class="muted" style="font-size:13px">No cache work for this stream yet</span>';
      var nextJobs = s.jobs.filter(function (j) { return j.feature === "nextepisode" && j.state !== "cancelled"; });
      var shown = s.jobs;
      if (nextJobs.length > 3) {
        // A long queue (music, audiobooks): one line for it, plus what's being read now.
        var doneN = nextJobs.filter(function (j) { return j.state === "done"; }).length;
        var bytes = nextJobs.reduce(function (t, j) { return t + (j.read || 0); }, 0);
        var now = nextJobs.filter(function (j) { return j.state === "reading"; })[0];
        out += '<div class="muted" style="font-size:13px">Next-episode prefetch</div><div style="display:flex;gap:10px;align-items:center;margin:4px 0 8px;flex-wrap:wrap">' +
          badge(doneN === nextJobs.length ? "done" : "running", doneN === nextJobs.length ? "Done" : "Reading") + "<span>" + fmtNum(doneN) + " of " + fmtNum(nextJobs.length) +
          " tracks ready · " + esc(fmtBytes(bytes)) + (now ? " · now: " + esc(now.target) : "") + "</span></div>";
        shown = s.jobs.filter(function (j) { return j.feature !== "nextepisode"; });
      }
      shown.forEach(function (j) {
        var label = { waiting: "Waiting", reading: "Reading", done: "Done", failed: "Failed", cancelled: "Stopped" }[j.state];
        var feat = j.feature === "nextepisode" ? "Next-episode prefetch" : "Now-playing preload";
        var of = j.whole ? " (whole file)" : (j.size ? " (" + Math.round(j.length / j.size * 100) + "% of the file)" : "");
        var amt = j.state === "reading" ? fmtBytes(j.read) + " of " + fmtBytes(j.length) + " loaded" + of :
          (j.state === "done" ? fmtBytes(j.read) + " loaded" + of + " " + ago(j.finished) :
          (j.state === "waiting" ? fmtBytes(j.length) + of + (j.delay ? " · starts " + j.delay + " s after playback began" : "") : fmtBytes(j.length)));
        out += '<div class="muted" style="font-size:13px">' + esc(feat) + '</div><div style="display:flex;gap:10px;align-items:center;margin:4px 0 8px;flex-wrap:wrap">' + badge(j.state, label) + "<span>" + esc(j.target) + " · " + esc(amt) + "</span></div>";
      });
      out += "</div></div>";
    });
    out += "</section>";

    // runs
    out += '<section class="card" style="margin-bottom:20px"><div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:baseline"><h2>Library warm-up runs' + info("“L2ARC grew” compares cache size before and after each run. ZFS fills L2ARC gradually, so it can lag a little. " +
      "If it stays near zero and “Already cached” stays low on repeat runs, reads aren’t landing in L2ARC — Health will flag it.", "warm-up runs") + '</h2><span class="muted" style="font-size:13px">' +
      (isOn("warmup") ? (ST.warmup.next_run ? "Next run " + esc(fmtIso(ST.warmup.next_run)) : "Manual only") : "Library warm-up is off") + "</span></div>" + warmupStatus();
    if (!a.runs.length) out += '<p class="muted">No runs yet.</p>';
    else {
      out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Started</th><th scope="col">Files</th><th scope="col">Read</th><th scope="col">Already cached</th><th scope="col">L2ARC grew</th><th scope="col">Took</th><th scope="col">Result</th></tr></thead><tbody>';
      a.runs.slice(0, 20).forEach(function (r) {
        out += '<tr><td class="nw">' + esc(fmtTime(r.started)) + (r.manual ? " " + badge("manual", "manual") : "") + '</td><td class="nw">' + fmtNum(r.files) + '</td><td class="nw">' + esc(fmtBytes(r.bytes)) +
          '</td><td class="nw">' + (r.hit_rate == null ? "—" : Math.round(r.hit_rate * 100) + "%") + '</td><td class="nw">' +
          (r.l2_growth == null ? "—" : (r.l2_growth >= 0 ? "+" : "−") + esc(fmtBytes(Math.abs(r.l2_growth)))) + '</td><td class="nw">' + esc(fmtDur(r.finished - r.started)) + "</td><td>" +
          badge(r.result, { ok: "OK", stopped: "Stopped", errors: "Errors" }[r.result] || r.result) + (r.errors ? ' <span class="muted" style="font-size:13px">' + fmtNum(r.errors) + " unreadable</span>" : "") + "</td></tr>";
      });
      out += "</tbody></table></div>";

    }
    out += "</section>";

    // events
    out += '<section class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center"><h2>Recent events</h2></div><div class="log" style="margin-top:12px">';
    if (!a.events.length) out += '<div class="muted">Nothing yet.</div>';
    a.events.forEach(function (e) {
      var t = new Date(e.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      out += '<div class="' + (e.level === "warn" ? "w" : "") + '">' + esc(t) + "  " + esc((e.kind + "            ").slice(0, 13)) + " " + esc(e.message) + "</div>";
    });
    out += "</div></section>";
    return out;
  }

  // ---- Activity: period switch and the "Over time" chart
  var actPeriod = "week", chartMetric = "skips";
  try { actPeriod = localStorage.getItem("pc-period") || "week"; chartMetric = localStorage.getItem("pc-chart") || "skips"; } catch (e) { /* storage unavailable */ }
  var PERIOD_NAMES = [["today", "Today"], ["week", "7 days"], ["month", "30 days"], ["year", "12 months"]];
  function periodSwitch() {
    return '<div class="seg" role="tablist" aria-label="Period">' + PERIOD_NAMES.map(function (p) {
      return '<button role="tab" aria-selected="' + (p[0] === actPeriod) + '" data-period="' + p[0] + '">' + p[1] + "</button>";
    }).join("") + "</div>";
  }
  var CHARTS = {
    skips: { name: "Skips", keys: [["skips_cached", "From cache"], ["skips_missed", "From disk"]], fmt: fmtNum,
      hint: "Each bar: skips that landed in preloaded data, and those that had to wait on the disks." },
    next: { name: "Next episodes", keys: [["next_ready", "Ready in time"], ["next_late", "Not ready"]], fmt: fmtNum,
      hint: "Each bar: next episodes that were fully loaded before they started, and those that weren’t." },
    preloaded: { name: "Data preloaded", keys: [["preloaded", "Read ahead"]], fmt: fmtBytes,
      hint: "How much Now-playing and Next-episode read ahead for viewers." },
    retention: { name: "Still cached", keys: [["retention", "Still in RAM 30 min later"]], fmt: function (v) { return v + "%"; }, max: 100,
      hint: "Average share of preloaded data still in RAM 30 minutes after loading." },
    busy: { name: "Disk load", keys: [["busy", "Disks busy during streams"]], fmt: function (v) { return v + "%"; }, max: 100,
      hint: "How busy the media pool’s disks were, on average, while people watched." }
  };
  function niceMax(v) {
    if (v <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }
  function chartCard(r) {
    var c = CHARTS[chartMetric] || CHARTS.skips, data = r.series || [];
    var tabs = Object.keys(CHARTS).map(function (k) {
      return '<button role="tab" aria-selected="' + (k === chartMetric) + '" data-chart="' + k + '">' + esc(CHARTS[k].name) + "</button>";
    }).join("");
    var totals = data.map(function (d) { return c.keys.reduce(function (t, k) { return t + (d[k[0]] || 0); }, 0); });
    var top = c.max || niceMax(Math.max.apply(null, totals.concat([0])));
    if (!c.max && c.fmt === fmtNum && top % 2) top += 1;  // counts: keep the middle gridline a whole number
    var narrow = window.innerWidth < 640, W = narrow ? 360 : 720, H = narrow ? 180 : 200, L = 44, R = 8, T = 10, B = 26, n = Math.max(1, data.length);
    var band = (W - L - R) / n, bw = Math.min(24, Math.max(4, band * 0.6));
    var svg = '<svg class="chart" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(c.name + ", " + r.span) + '">';
    [0, 0.5, 1].forEach(function (f) {
      var y = T + (H - T - B) * (1 - f);
      svg += '<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/><text class="axis" x="' + (L - 6) + '" y="' + (y + 4) + '" text-anchor="end">' +
        esc(c.fmt(Math.round(top * f * 10) / 10)) + "</text>";
    });
    var every = Math.ceil(n / (narrow ? 6 : 12)), any = false;
    data.forEach(function (d, i) {
      var x = L + band * i + (band - bw) / 2, y0 = H - B, tip = d.label + " — ";
      var parts = [];
      c.keys.forEach(function (k, j) {
        var v = d[k[0]];
        parts.push(k[1] + ": " + (v == null ? "—" : c.fmt(v)));
        if (!v) return;
        any = true;
        var h = Math.max(1, (H - T - B) * Math.min(v, top) / top), last = j === c.keys.length - 1 || !d[c.keys[j + 1] && c.keys[j + 1][0]];
        var gap = j > 0 ? 2 : 0;  // a 2px surface gap between stacked segments
        y0 -= gap;
        var yTop = y0 - h;
        svg += last ? '<path class="s' + j + '" d="M' + x + " " + y0 + "V" + (yTop + Math.min(4, h)) + "q0 -" + Math.min(4, h) + " " + Math.min(4, bw / 2) + " -" + Math.min(4, h) +
          "H" + (x + bw - Math.min(4, bw / 2)) + "q" + Math.min(4, bw / 2) + " 0 " + Math.min(4, bw / 2) + " " + Math.min(4, h) + "V" + y0 + 'Z"/>' :
          '<rect class="s' + j + '" x="' + x + '" y="' + yTop + '" width="' + bw + '" height="' + h + '"/>';
        y0 = yTop;
      });
      svg += '<rect class="hit" x="' + (L + band * i) + '" y="' + T + '" width="' + band + '" height="' + (H - T - B) + '" data-tip="' + esc(tip + parts.join(" · ")) + '"/>';
      if (i % every === 0 || i === n - 1) svg += '<text class="axis" x="' + (L + band * i + band / 2) + '" y="' + (H - 8) + '" text-anchor="middle">' + esc(d.label) + "</text>";
    });
    svg += "</svg>";
    var legend = c.keys.length > 1 ? '<div class="legend">' + c.keys.map(function (k, j) { return '<span><i class="k' + j + '"></i>' + esc(k[1]) + "</span>"; }).join("") + "</div>" : "";
    var table = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">' + esc(r.period === "today" ? "Hour" : r.period === "year" ? "Month" : "Day") + "</th>" +
      c.keys.map(function (k) { return '<th scope="col">' + esc(k[1]) + "</th>"; }).join("") + "</tr></thead><tbody>" +
      data.map(function (d) { return "<tr><td>" + esc(d.label) + "</td>" + c.keys.map(function (k) { return "<td>" + (d[k[0]] == null ? "—" : esc(c.fmt(d[k[0]]))) + "</td>"; }).join("") + "</tr>"; }).join("") +
      "</tbody></table></div>";
    return '<section class="card chartcard" style="margin-bottom:20px"><div class="head"><h2>Over time</h2><span style="flex:1"></span><div class="seg small" role="tablist" aria-label="Chart">' + tabs + "</div></div>" +
      '<p class="hint">' + esc(c.hint) + "</p>" + legend +
      (any ? '<div class="chartwrap">' + svg + '<div class="chart-tip" hidden></div></div>' : '<p class="muted" style="margin:18px 0">Nothing measured in this period yet.</p>') +
      details("chart-table", "Show as a table", table) + "</section>";
  }

  // The Activity report card: a verdict, four numbers, a chart, and what needs attention.
  function reportHtml(r, z, arc, a) {
    if (!r) return "";
    var t = r.tiles, out = banner(r.verdict, false);
    function rtile(title, big, sub, status, why) {
      return '<div class="card tile rtile r-' + status + '"><div class="muted" style="font-size:13px">' + esc(title) + info(esc(why), title) + '</div><div class="big">' + big +
        '</div><div class="muted" style="font-size:13px">' + sub + "</div></div>";
    }
    function frac(n, d) { return d ? fmtNum(n) + ' <small>of ' + fmtNum(d) + "</small>" : "—"; }
    out += '<div class="tiles rtiles">';
    out += rtile("Skips served from cache", frac(t.skips.cached, t.skips.total),
      t.skips.total ? Math.round(t.skips.cached / t.skips.total * 100) + "% landed in preloaded data" : "no skips yet",
      !t.skips.total ? "none" : (t.skips.cached / t.skips.total >= 0.9 ? "ok" : "warn"),
      "When someone jumps ahead or back, was that part already loaded? Those skips start straight away instead of waiting on the disks.");
    out += rtile("Next episodes ready", frac(t.next.ready, t.next.total),
      t.next.total ? (t.next.lead_min != null ? "on average " + t.next.lead_min + " min early" : "&nbsp;") : (isOn("nextepisode") ? "no episode changes yet" : "Next-episode is off"),
      !t.next.total ? "none" : (t.next.ready === t.next.total ? "ok" : "warn"),
      "Was the next episode fully loaded before it started? Ready means no pause between episodes.");
    out += rtile("Still cached 30 min later", t.retention.pct == null ? "—" : t.retention.pct + "%",
      t.retention.checks ? "average of " + fmtNum(t.retention.checks) + (t.retention.checks === 1 ? " check" : " checks") : "checked after each preload",
      t.retention.pct == null ? "none" : (t.retention.pct >= 70 ? "ok" : "warn"),
      "Checks that what was loaded is still in RAM later. The numbers beside it assume it is; if this is low, ZFS is pushing preloads out to make room — usually the RAM limits are bigger than what's really free.");
    out += rtile("Data preloaded", t.preloaded.bytes ? esc(fmtBytes(t.preloaded.bytes)) : "—",
      t.preloaded.cached_pct != null ? t.preloaded.cached_pct + "% was in cache already" : "by Now-playing and Next-episode", t.preloaded.bytes ? "ok" : "none",
      "How much Now-playing and Next-episode read ahead for viewers. The part already in cache cost the disks nothing.");
    out += "</div>" + chartCard(r);

    if (r.attention.length) {
      out += '<section class="card" style="margin-bottom:20px"><h2>Needs attention</h2><p class="hint">Only shows up when something could be better.</p>';
      r.attention.forEach(function (x) {
        out += itemRow({ status: x.status, title: x.title, detail: x.detail, fix: x.fix },
          x.link ? '<a href="' + esc(x.link) + '" data-nav style="font-size:13px">Go there</a>' : "");
      });
      out += "</section>";
    }

    // RAM limits right now, and how busy the disks were while people watched
    function meter(label, used, limit) {
      var pct = limit ? Math.min(100, used / limit * 100) : 0;
      return '<div style="margin:6px 0 12px"><div style="display:flex;justify-content:space-between;font-size:13px"><span>' + esc(label) + '</span><span class="muted">' +
        esc(fmtBytes(used)) + " of " + esc(fmtBytes(limit)) + '</span></div><div class="meter"><div style="width:' + pct.toFixed(1) + '%"></div></div></div>';
    }
    out += '<div class="cols2" style="margin-bottom:20px"><section class="card"><h2>RAM limits</h2><p class="hint">In use right now, by streams that are playing.</p>' +
      meter("Now-playing", r.ram.nowplaying.in_use, r.ram.nowplaying.limit) + meter("Next-episode", r.ram.nextepisode.in_use, r.ram.nextepisode.limit) +
      '<div class="muted" style="font-size:13px">' + esc(r.span) + ": " + fmtNum(r.ram.limited) + (r.ram.limited === 1 ? " file" : " files") +
      " only partly fitted · " + fmtNum(r.ram.no_room) + " times there was no room at all</div></section>";
    out += '<section class="card"><h2>Disks while people watched</h2><p class="hint">How busy the media pool’s disks were during streams.</p>';
    if (r.disk.avg == null) out += '<p class="muted" style="margin:0">Measured during streams; nothing yet.</p>';
    else out += '<div class="big" style="font-size:28px">' + r.disk.avg + '% <small class="muted" style="font-size:14px">average</small></div><div class="meter"><div style="width:' + r.disk.avg + '%"></div></div>' +
      '<div class="muted" style="font-size:13px;margin-top:6px">Peak ' + r.disk.peak + "% · over " + fmtNum(r.disk.streams) + (r.disk.streams === 1 ? " stream" : " streams") + "</div>";
    if (z.l2_total && arc) {
      out += '<div class="muted" style="font-size:13px;margin-top:12px">L2ARC (SSD cache): ' + esc(fmtBytes(arc.l2_asize)) + " of " + esc(fmtBytes(z.l2_total)) + " filled</div>";
    }
    out += "</section></div>";
    return out;
  }

  function secretInput(sec, key, label, id) {
    var set = S[sec][key + "_set"];
    // A plain text field, not a password field or one masked with CSS: browsers treat either as a
    // login and offer to save it. It's cleared the moment it's saved, so the key isn't left on screen.
    return '<div class="fld"><label for="' + id + '">' + esc(label) + '</label><input id="' + id + '" type="text" class="secret" autocomplete="off" autocapitalize="off" spellcheck="false" ' +
      'data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other" placeholder="' + (set ? "Saved — paste to replace" : "Paste it here") +
      '" value="" data-secret="' + sec + "." + key + '"></div>';
  }
  function textInput(sec, key, label, id, help, placeholder) {
    var h = splitHelp(help);
    return '<div class="fld"><label for="' + id + '">' + esc(label) + info(h.more, label) + '</label><input id="' + id + '" type="text" value="' + esc(S[sec][key]) + '" placeholder="' + esc(placeholder || "") +
      '" data-set="' + sec + "." + key + '">' + helpDiv(h) + "</div>";
  }

  function pageSettings() {
    var c = ST.connections || {}, testing = ST.testing;
    var out = top("Settings", "Connections and system-wide options. Stored in the app’s config folder, so they survive updates.",
      '<button class="btn" data-act="retest"' + (testing ? " disabled" : "") + ">" + (testing ? "Testing…" : "Re-test settings") + "</button>");
    out += banner(settingsSummary(c.summary), testing);
    out += '<div class="stack" style="max-width:980px">';

    // ---- Plex
    var guessPlex = ST.host ? "http://" + ST.host + ":32400" : "http://192.168.1.10:32400";
    var tokOk = c.plex_token && c.plex_token.status === "ok";
    out += '<section class="card"><h2>Plex</h2><p class="hint">Used to list your libraries and see what’s playing.</p>' +
      '<div class="lights">' + lightFor(c.plex_server, "Plex server", testing) + lightFor(c.plex_token, "Plex sign-in", testing) + "</div>" +
      '<div class="grid">' +
      textInput("plex", "url", "Server address", "s-purl", "Usually your NAS’s IP. Port 32400 is used if you leave it off.", guessPlex) +
      secretInput("plex", "token", "Token (filled in when you sign in)", "s-ptok") + "</div>" +
      '<div class="acts"><button class="btn' + (tokOk ? "" : " pri") + '" data-act="plex-signin">' + (tokOk ? "Sign in again" : "Sign in with Plex") + "</button>" +
      '<span class="muted" style="font-size:13px">Why sign in?' + info("Plex only shares your libraries and what’s playing with apps that hold a token for your account, so this step is required. " +
      "“Sign in with Plex” opens plex.tv; once you approve “Plex Cache”, Plex gives this app a token. It’s kept only in this app’s config folder and is used to read from your server — " +
      "it can’t see your password. If you already have a token, you can paste it instead.", "signing in") + "</span></div></section>";

    // ---- TrueNAS
    var guessTn = ST.host ? "https://" + ST.host : "https://192.168.1.10";
    out += '<section class="card"><h2>TrueNAS <span class="muted" style="font-weight:400;font-size:14px">· optional, recommended</span></h2>' +
      '<p class="hint">Read-only: checks cache devices and dataset settings.' + info("Lets the app check cache devices and dataset settings, and size Library warm-up. It only reads; it never changes anything.", "TrueNAS") + "</p>" +
      '<div class="lights">' + lightFor(c.truenas_server, "TrueNAS", testing) + lightFor(c.truenas_key, "API key", testing) + "</div>" +
      '<div class="grid">' +
      textInput("truenas", "url", "TrueNAS address", "s-turl", "https:// is used automatically — TrueNAS revokes API keys sent over plain http.", guessTn) +
      secretInput("truenas", "api_key", "API key", "s-tkey") + "</div>" +
      '<div class="chk" style="margin-top:12px"><label><input type="checkbox" data-check="truenas.verify_tls"' + (S.truenas.verify_tls ? " checked" : "") +
      ">Verify the TLS certificate (leave off for TrueNAS’s self-signed certificate)</label></div>" +
      details("apikey", "How to create an API key",
        "<ol style=\"margin:8px 0 0;padding-left:20px\"><li>In TrueNAS, open your user menu (top right) → <strong>API Keys</strong>.</li>" +
        "<li>Choose <strong>Add</strong>, name it “plex-cache”, and save.</li>" +
        "<li>Copy the key it shows (it’s only shown once) and paste it above.</li></ol>",
        !(c.truenas_key && c.truenas_key.status === "ok")) +
      "</section>";

    // ---- Media mappings
    out += mappingsCard();

    // ---- Behaviour
    var g = S.general;
    out += '<section class="card"><h2>Behaviour</h2><p class="hint">Applies to all features.</p><div class="grid" style="margin-top:0">' +
      select("general", "poll_seconds", "Check what’s playing every", [[3, "3 seconds"], [5, "5 seconds (default)"], [10, "10 seconds"], [30, "30 seconds"]],
        { s: "Drives Now-playing and Next-episode.", more: "How often the app asks Plex what’s playing. Each check is one small request on your own network, so the load is tiny at any of these. " +
          "What changes is how quickly it reacts: when a stream starts, when someone skips (Now-playing then reads from the new spot), and when a player buffers (warm-up pauses for it).<br><br>" +
          "<strong>3 seconds</strong> — a lot of music, or viewers who skip around: reacts a little sooner.<br>" +
          "<strong>5 seconds</strong> — the best fit for most people.<br>" +
          "<strong>10 seconds</strong> — fine for mostly movies and TV watched straight through.<br>" +
          "<strong>30 seconds</strong> — the fewest requests, but streams and skips can be noticed up to half a minute late; short songs may be half over, and brief buffering can be missed.<br><br>" +
          "Faster than 3 seconds gains little: Now-playing waits for Plex’s own start-up buffer anyway." }) +
      number("general", "ram_reserve_gb", "ZFS cache to leave for the rest of the NAS (GB)", 0, 4096, reserveHelp()) +
      "</div>" +
      '<div class="chk" style="margin-top:12px"><label><input type="checkbox" data-check="general.update_check"' + (g.update_check ? " checked" : "") +
      ">Check GitHub for new versions" + info("Every 6 hours the app downloads one small file from GitHub (the version number on the main branch) " +
        "and, when there's a newer version, says so under the app's name and in Home Assistant’s update entity. Nothing about your setup is sent. " +
        "Updating is still done in TrueNAS.", "update check") + "</label>" + updateLine() + "</div></section>";
    out += pauseCard();
    out += diskSpeedCard();
    out += mqttCard();
    out += "</div>";
    return out;
  }

  var MQTT_STEPS = [["broker", "Broker found"], ["tls", "Encrypted (TLS)"], ["login", "Login accepted"], ["publish", "Publishing"]];
  var MQTT_WAITING = { broker: "Looking for the broker…", tls: "Starting TLS…", login: "Signing in…", publish: "Sending…" };
  function updateLine() {
    var u = ST.updates || {};
    if (!S.general.update_check) return "";
    if (u.available) return '<span class="muted" style="font-size:13px">v' + esc(u.latest) + " is available — update the app in TrueNAS.</span>";
    if (u.checked) return '<span class="muted" style="font-size:13px">Up to date (checked ' + esc(ago(u.checked)) + ").</span>";
    if (u.error) return '<span class="muted" style="font-size:13px">Couldn’t check: ' + esc(u.error) + "</span>";
    return "";
  }
  function mqttLights() {
    var m = ST.mqtt || {}, M = S.mqtt, steps = m.steps || {};
    if (!M.enabled) return light("off", "Off", "Tick “Publish over MQTT” below to start.");
    return MQTT_STEPS.map(function (st) {
      var r = steps[st[0]] || { status: "off", text: "" }, detail = r.text;
      if (st[0] === "publish" && r.status === "ok") {
        detail = r.text + (m.last_publish ? " · last update " + ago(m.last_publish) : "") +
          (m.ha_seen ? " · Home Assistant last said hello " + ago(m.ha_seen) : "");
      }
      if (r.status === "testing") return light("testing", st[1], MQTT_WAITING[st[0]]);
      if (r.status === "off") return light("off", st[1], detail || (m.state === "error" ? "Waiting for the step before" : ""));
      return light(r.status, st[1], detail);
    }).join("");
  }
  function mqttRetryLine() {
    var m = ST.mqtt || {};
    if (!S.mqtt.enabled || !S.mqtt.host) return "";
    var txt = "";
    if (m.state === "error" && m.next_retry) {
      var secs = Math.max(0, Math.round(m.next_retry - Date.now() / 1000));
      txt = secs ? "Trying again in " + secs + " s." : "Trying again…";
    } else if (m.state === "connecting") txt = "Connecting…";
    return '<div class="acts"><button class="btn" data-act="mqtt-retry"' + (m.state === "connecting" ? " disabled" : "") + ">" +
      (m.state === "connected" ? "Reconnect" : "Reconnect now") + "</button>" +
      (txt ? '<span class="muted" style="font-size:13px">' + txt + "</span>" : "") + "</div>";
  }
  function mqttCard() {
    var M = S.mqtt, on = M.enabled;
    var out = '<section class="card" id="mqtt"><h2>MQTT <span class="muted" style="font-weight:400;font-size:14px">· Home Assistant, Node-RED, openHAB and more · optional</span></h2>' +
      '<p class="hint">Publishes status and lots of metrics, and takes commands to switch features and run jobs.' +
      info("MQTT is a common way for home automation and monitoring tools to share data, through a <strong>broker</strong> that everything connects to. " +
        "Plex Cache publishes everything as one JSON message on <code>" + esc(M.base_topic) + "/state</code> (every " + M.interval + " s, and straight away after a change), " +
        "says <code>online</code>/<code>offline</code> on <code>" + esc(M.base_topic) + "/availability</code>, and takes commands on <code>" + esc(M.base_topic) + "/set/…</code>. " +
        "Node-RED, openHAB, ioBroker, Telegraf and others can read those directly.<br><br>" +
        "<strong>Home Assistant:</strong> with discovery on, a “Plex Cache” device appears with sensors for health, streams, RAM, ARC/L2ARC, warm-up, " +
        "the Activity report and lifetime totals, plus switches, buttons and selects. Install the <strong>Mosquitto broker</strong> add-on and the " +
        "<strong>MQTT</strong> integration, add a Home Assistant user for this app (Settings → People → Users), and enter its name and password here.<br><br>" +
        "<strong>Security:</strong> the broker login is the lock. Use a separate login per device, use TLS if your broker offers it, and switch off " +
        "“Allow control” for a watch-only connection. Broker ACLs can limit this login to the <code>" + esc(M.base_topic) + "/#</code> and <code>" +
        esc(M.discovery_prefix) + "/#</code> topics.", "MQTT") + "</p>" +
      '<div class="lights">' + mqttLights() + "</div>" + mqttRetryLine() +
      '<div class="chk" style="margin-top:12px"><label><input type="checkbox" data-check="mqtt.enabled"' + (on ? " checked" : "") + ">Publish over MQTT</label></div>";
    if (!on) return out + "</section>";
    out += '<div class="grid">' +
      textInput("mqtt", "host", "Broker address", "s-mhost", "For Home Assistant’s Mosquitto add-on, Home Assistant’s IP.", "192.168.1.20") +
      number("mqtt", "port", "Port", 1, 65535, { s: "1883, or 8883 with TLS.", more: "1883 is the usual port without TLS and 8883 with it. " +
        "In Home Assistant’s Mosquitto add-on, 8883 only opens when the add-on has certificate files (its log says “SSL not enabled - No valid certs found!” otherwise). " +
        "Its websocket ports (1884, 8884) are for browsers and won’t work here." }) +
      textInput("mqtt", "username", "Username", "s-muser", "", "plexcache") +
      secretInput("mqtt", "password", "Password", "s-mpass") + "</div>" +
      '<div class="chk" style="margin-top:12px"><label><input type="checkbox" data-check="mqtt.tls"' + (M.tls ? " checked" : "") + ">Use TLS</label>" +
      (M.tls ? '<label><input type="checkbox" data-check="mqtt.verify_tls"' + (M.verify_tls ? " checked" : "") + ">Verify the broker’s certificate" +
        info("Leave off for a self-signed certificate, or when connecting by IP to a broker whose certificate names a domain (such as a Let’s Encrypt one). " +
          "The connection is still encrypted; on, it also proves it’s really your broker.", "Verify") + "</label>" : "") +
      '<label><input type="checkbox" data-check="mqtt.discovery"' + (M.discovery ? " checked" : "") + ">Home Assistant discovery" +
      info("Publishes Home Assistant’s discovery messages, so the “Plex Cache” device and its entities appear on their own. openHAB and Domoticz can read them too. " +
        "Off: only the state, availability and command topics, for tools that don’t use discovery. Turning it off removes the entities from Home Assistant.", "Home Assistant discovery") + "</label>" +
      '<label><input type="checkbox" data-check="mqtt.allow_control"' + (M.allow_control ? " checked" : "") + ">Allow control" +
      info("Accepts commands on <code>" + esc(M.base_topic) + "/set/…</code>: switch the three features on or off, run or stop warm-up, re-run health checks, " +
        "benchmark disks, change RAM limits and pacing, and pick each library’s preload. In Home Assistant these are switches, buttons, sliders and selects. " +
        "Off: status only.", "Allow control") + "</label></div>" +
      details("mqtt-more", "More options", '<div class="grid">' +
        number("mqtt", "interval", "Send an update every (seconds)", 5, 600, "Changes are sent straight away too.") +
        select("mqtt", "report_period", "Activity report figures cover", [["today", "Today"], ["week", "Last 7 days"], ["month", "Last 30 days"]],
          "The skips, next-episode and preload figures.") +
        textInput("mqtt", "base_topic", "Topic", "s-mbase", "Status goes to &lt;topic&gt;/state; commands come in on &lt;topic&gt;/set/….", "plexcache") +
        (M.discovery ? textInput("mqtt", "discovery_prefix", "Discovery prefix", "s-mdisc", "Leave as homeassistant unless you changed it in Home Assistant.", "homeassistant") : "") +
        "</div>", false) +
      "</section>";
    return out;
  }

  // While a benchmark runs: which dataset it's reading now, and which are still to come.
  function benchState(name) {
    var p = ST.benchmark.running && ST.benchmark.progress;
    if (!p || p.finished.indexOf(name) >= 0) return "";
    return p.dataset === name ? badge("running", "Measuring…") : badge("info", "Waiting");
  }
  function diskSpeedCard() {
    var g = S.general, b = ST.benchmark, speeds = g.disk_speeds || {};
    var ds = (ST.zfs.datasets || []).filter(function (d) { return d.keys.length; });
    var out = '<section class="card" id="disk-speed"><div class="head"><h2>Disk speed</h2><span style="flex:1"></span>' +
      '<button class="btn" data-act="benchmark"' + (b.running || !ds.length ? " disabled" : "") + ">" +
      (b.running ? (b.progress ? "Measuring " + (b.progress.done + 1) + " of " + b.progress.total + "…" : "Measuring…") : "Benchmark now") + "</button></div>" +
      '<p class="hint">How fast each dataset reads when nothing is cached.' + info("The app uses it to tell a cache hit from a disk read " +
      "(a read at least 4× faster came from cache) — that’s how warm-up reports what was already cached — to time Now-playing’s automatic start, and to work out how hard background " +
      "warm-up is working your disks. Datasets on different disks (HDD, SSD, mixed) get their own speed.<br><br>" +
      "<strong>Benchmark now</strong> reads up to 64 MB from five random media files on each dataset and times it. The middle result becomes that dataset’s speed, " +
      "so one file that happened to be cached already doesn’t skew it. It takes a few seconds per dataset, only reads, and is harmless. " +
      "Run it again after adding disks or SSDs, or type a speed in by hand.", "disk speed") + "</p>";
    if (!ds.length) {
      out += '<p class="muted" style="margin:0">Datasets appear here once a library’s files are readable (see Media mappings above).</p>';
    } else {
      out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Dataset</th><th scope="col">Libraries</th><th scope="col">Uncached speed</th><th scope="col">Measured</th></tr></thead><tbody>';
      ds.forEach(function (d) {
        var name = d.dataset || d.mountpoint, row = speeds[name];
        out += '<tr><td class="mono nw">' + esc(name) + "</td><td>" + esc(d.libraries.join(", ")) + '</td><td class="nw"><input type="number" min="10" max="20000" aria-label="Speed of ' + esc(name) +
          ' in MB/s" data-speed="' + esc(name) + '" value="' + esc(row ? row.mb_s : g.hdd_mb_per_sec) + '"> MB/s</td><td class="muted">' +
          (benchState(name) || (row && row.measured_at ? esc(row.measured_at.replace("T", " ")) : (row ? "Set by hand" : "Not yet — using the default"))) + "</td></tr>";
      });
      out += "</tbody></table></div>";
    }
    out += '<div class="grid">' + number("general", "hdd_mb_per_sec", "Default for datasets not measured yet (MB/s)", 10, 5000, "A typical hard drive pool reads 150–250 MB/s.") + "</div>";
    if (b.last && b.last.error) out += '<div class="note">' + esc(b.last.error) + "</div>";
    if (b.last && b.last.rows && b.last.rows.length) {
      var rowsHtml = "";
      b.last.rows.forEach(function (r) {
        rowsHtml += '<tr><td class="mono">' + esc(r.dataset) + "</td><td>" + esc(KIND_NAME[r.kind]) + "</td><td>" + esc(r.mb_s) + " MB/s</td><td>" + esc(r.mb) + ' MB</td><td class="mono">' + esc(r.file) + "</td></tr>";
      });
      out += details("bench", "Last benchmark details", '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Dataset</th><th>Type</th><th>Speed</th><th>Read</th><th>File</th></tr></thead><tbody>' + rowsHtml + "</tbody></table></div>");
    }
    return out + "</section>";
  }

  // Connections alone aren't "everything": ticked libraries whose folders can't be found count too.
  function settingsSummary(sum) {
    if (!sum) return sum;
    var bad = {}, n = 0;
    var off = S.general.excluded_libraries || [];  // unticked libraries never count against the summary
    (ST.locations || []).forEach(function (l) { if (off.indexOf(l.key) < 0 && l.status !== "ok" && !bad[l.key]) { bad[l.key] = l.library; n++; } });
    if (!n) return sum;
    var names = Object.keys(bad).map(function (k) { return bad[k]; }).join(", ");
    var text = (n === 1 ? "1 library isn’t" : n + " libraries aren’t") + " being cached: " + names + " — see Media mappings below.";
    if (sum.status === "ok") return { status: "warn", text: "Connected, but " + text.charAt(0).toLowerCase() + text.slice(1) };
    return { status: sum.status, text: sum.text + " Also, " + text.charAt(0).toLowerCase() + text.slice(1) };
  }

  function lightFor(r, title, testing) {
    if (!r) return light("off", title, "Not tested yet", testing);
    return light(r.status, r.text, r.detail, testing);
  }

  function currentMapping(plexPath) {
    var m = (S.general.path_map || []).filter(function (r) { return r.plex === plexPath; })[0];
    return m ? m.local : "";
  }

  // A folder already serving another library (green light), or anything inside it.
  function inUse(folder, exceptPlexPath) {
    return (ST.locations || []).some(function (l) {
      if (l.status !== "ok" || l.excluded || l.plex_path === exceptPlexPath) return false;
      return folder === l.local_path || folder.indexOf(l.local_path.replace(/\/$/, "") + "/") === 0;
    });
  }

  function mappingsCard() {
    var locs = ST.locations || [];
    var out = '<section class="card" id="mappings"><h2>Media mappings</h2>' +
      '<p class="hint">Tick the libraries to cache.' + info("All libraries are ticked by default; unticked ones are skipped by every feature and by the health checks. " +
      "Plex lists each library’s folders as it sees them inside its own app; if this app has a folder mounted somewhere else, choose where. " +
      "Folders marked ✓ contain that library’s files. A library is only cached once all its folders are found.", "media mappings") + "</p>";
    if (!locs.length) return out + '<p class="muted" style="margin:0">Your library folders appear here once Plex is connected.</p></section>';
    var healthAt = ST.health ? ST.health.at : 0;
    out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Cache</th><th scope="col">Library</th><th scope="col">Plex folder</th><th scope="col">In this app</th></tr></thead><tbody>';
    var firstOfLib = {};
    locs.forEach(function (l) {
      // A library is cached when it's ticked and all its folders can be read.
      var libRows = locs.filter(function (x) { return x.key === l.key; });
      var libOk = libRows.every(function (x) { return x.status === "ok"; });
      var excluded = (S.general.excluded_libraries || []).indexOf(l.key) >= 0;  // the saved setting, not the last check
      var tick = "";
      if (!firstOfLib[l.key]) {
        firstOfLib[l.key] = true;
        // The box is the setting: ticked = you want it cached. A ticked library whose
        // folder can't be found shows red until it's fixed or unticked.
        tick = '<label class="tick" title="' + (excluded ? "Not cached — tick to cache it" : (libOk ? "Cached" : "Wanted, but its folder isn’t found yet")) +
          '"><input type="checkbox" data-lib="' + esc(l.key) + '"' + (excluded ? "" : " checked") + '><span class="sr">Cache ' + esc(l.library) + "</span></label>";
      }
      var cur = currentMapping(l.plex_path);
      var pending = mapPending[l.plex_path] && mapPending[l.plex_path] >= healthAt;
      var st = { ok: "ok", missing: "fail", empty: "fail", unreadable: "fail" }[l.status];
      var found = l.found_at || [];
      var opts = '<option value=""' + (cur === "" ? " selected" : "") + ">Same path as Plex (" + esc(l.plex_path) + ")</option>";
      var seen = {};
      seen[l.plex_path] = 1;
      found.forEach(function (f) { seen[f] = 1; opts += '<option value="' + esc(f) + '"' + (cur === f ? " selected" : "") + ">✓ " + esc(f) + "</option>"; });
      if (cur && !seen[cur]) { seen[cur] = 1; opts += '<option value="' + esc(cur) + '" selected>' + esc(cur) + "</option>"; }
      (ST.mapping_choices || []).forEach(function (f) {
        var insideMatch = found.some(function (m) { return f.indexOf(m.replace(/\/$/, "") + "/") === 0; });
        if (!seen[f] && !insideMatch && !inUse(f, l.plex_path)) opts += '<option value="' + esc(f) + '">' + esc(f) + "</option>";
      });
      opts += '<option value="__other__"' + (otherOpen[l.plex_path] ? " selected" : "") + ">Other path…</option>";
      var lightText = pending ? "" : (excluded ? "Not cached" : { ok: "Files readable", missing: "Not found", empty: "Folder is empty", unreadable: "Files not found here" }[l.status]);
      if (excluded && !pending) st = "off";
      var detail = pending ? "" : (l.status === "ok" ? "" : (found.length ? "Found at " + found[0] + " — choose it below." : l.detail + ". Mount it, or untick it to stop caching it."));
      if (excluded && !pending) {
        detail = l.status === "ok" ? "Unticked, so it isn’t cached or checked." :
          "Unticked, so it isn’t cached or checked. (" + l.detail + " — tick it again once it’s mounted.)";
      }
      // Which dataset the folder is on, and whether that dataset can cache at all.
      var ds = (ST.zfs.datasets || []).filter(function (d) { return d.dataset === l.dataset || d.mountpoint === l.dataset; })[0];
      if (!pending && !excluded && l.status === "ok" && ds) {
        var pc = ds.props && ds.props.primarycache;
        detail = "On " + ds.dataset + (pc && pc !== "all" ? " — primarycache=" + pc + ", so it can’t be cached until that’s fixed (see Health)." : "");
        if (pc && pc !== "all") st = "warn";
      }
      // One tick box and name per library, spanning all of its folders.
      var span = libRows.length > 1 ? ' rowspan="' + libRows.length + '"' : "";
      var libCells = tick ? "<td" + span + ">" + tick + "</td><td" + span + ">" + esc(l.library) +
        (libRows.length > 1 ? '<div class="muted" style="font-size:12px">' + libRows.length + " folders</div>" : "") + "</td>" : "";
      out += '<tr class="' + (excluded ? "excl" : "") + (tick ? "" : " cont") + '">' + libCells + '<td class="mono">' + esc(l.plex_path) + "</td><td>" +
        light(st, lightText, detail, pending) +
        '<select aria-label="Where ' + esc(l.plex_path) + ' is mounted in this app" data-map="' + esc(l.plex_path) + '">' + opts + "</select>" +
        (otherOpen[l.plex_path] ? '<input type="text" class="mono" placeholder="/path/in/this/app" aria-label="Other path" data-map-other="' + esc(l.plex_path) + '" value="' + esc(cur) + '">' : "") +
        "</td></tr>";
    });
    out += "</tbody></table></div>";
    out += details("mounting", "Mounting media in this app",
      "<p style=\"margin:8px 0 0\">Each library folder needs to be mounted (read-only) into this app. You can mount one shared folder that holds everything, " +
      "or each library separately. If you use the same container paths as your Plex app, nothing needs choosing here. " +
      "Mounts are set in TrueNAS: Apps → this app → Edit.</p>");
    return out + "</section>";
  }

  // --------------------------------------------------------------- render
  var PAGES = { health: pageHealth, features: pageFeatures, activity: pageActivity, settings: pageSettings };

  function render() {
    if (!ST) return;
    document.querySelectorAll(".nav").forEach(function (a) {
      var on = a.dataset.page === page;
      a.classList.toggle("on", on);
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    var dot = document.getElementById("dot-health");
    var c = ST.health ? ST.health.counts : null;
    dot.className = "dot " + (!c ? "" : c.fail ? "fail" : c.warn ? "warn" : "ok");
    var ver = document.getElementById("version"), up = ST.updates || {};
    ver.innerHTML = "TrueNAS · v" + esc(ST.version) + (up.available ?
      '<a class="update" href="' + esc(up.repo) + '" target="_blank" rel="noopener">v' + esc(up.latest) + " available ↗</a>" : "");
    var bar = document.getElementById("userbar");
    if (ST.auth && ST.auth.enabled && !bar) {
      bar = document.createElement("div");
      bar.id = "userbar";
      bar.className = "userbar";
      bar.innerHTML = '<span class="who">' + esc(ST.auth.user) + "</span>" +
        '<button type="button" class="signout" title="Sign out" aria-label="Sign out ' + esc(ST.auth.user) + '">' +
        '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg></button>';
      bar.querySelector("button").addEventListener("click", function () { api("/logout", {}).then(function () { location.reload(); }); });
      document.querySelector(".shell").appendChild(bar);
    }
    main.innerHTML = PAGES[page]();
    reattachInfo();
  }

  function editing() {
    var a = document.activeElement;
    // Typing or picking in a field holds re-drawing off; a ticked box doesn't.
    return a && main.contains(a) && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.type !== "checkbox" && a.type !== "radio";
  }

  function load() {
    return api("/api/state").then(function (st) {
      ST = st;
      if (!Object.keys(pending).length) S = st.settings;
      if (!editing()) render();
    }).catch(function () {
      if (!ST) main.innerHTML = '<p class="muted">Can’t reach the Plex Cache service. Retrying…</p>';
    });
  }

  function go(p, push) {
    page = PAGES[p] ? p : "health";
    if (push) history.pushState({}, "", "/" + page);
    document.title = "Plex Cache – " + page.charAt(0).toUpperCase() + page.slice(1);
    render();
    window.scrollTo(0, 0);
    main.focus({ preventScroll: true });
  }

  // --------------------------------------------------------------- events
  document.addEventListener("mouseover", function (e) {
    var hit = e.target.closest && e.target.closest("[data-tip]");
    var wrap = hit && hit.closest(".chartwrap");
    document.querySelectorAll(".chart-tip").forEach(function (t) { if (!wrap || !wrap.contains(t)) t.hidden = true; });
    if (!wrap) return;
    var tip = wrap.querySelector(".chart-tip"), box = wrap.getBoundingClientRect(), r = hit.getBoundingClientRect();
    tip.textContent = hit.dataset.tip;
    tip.hidden = false;
    var left = r.left - box.left + r.width / 2 - tip.offsetWidth / 2;
    tip.style.left = Math.max(0, Math.min(box.width - tip.offsetWidth, left)) + "px";
    tip.style.top = "0px";
  });

  // ---- info popover: one shared bubble, placed under whichever "i" is active
  var pop = document.createElement("div"), popFor = null, popPinned = false;
  pop.id = "infopop"; pop.className = "infopop"; pop.setAttribute("role", "tooltip"); pop.hidden = true;
  document.body.appendChild(pop);
  function showInfo(btn, pinned) {
    if (popFor && popFor !== btn.dataset.info) document.querySelectorAll('.info[aria-expanded="true"]').forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    popFor = btn.dataset.info; popPinned = !!pinned;
    pop.innerHTML = INFO[popFor] || "";
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-describedby", "infopop");
    placeInfo(btn);
  }
  function placeInfo(btn) {
    var r = btn.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    var left = Math.max(8, Math.min(document.documentElement.clientWidth - w - 8, r.left + r.width / 2 - w / 2));
    var below = r.bottom + 8 + h < window.innerHeight || r.top < h + 16;
    pop.style.left = (left + window.scrollX) + "px";
    pop.style.top = ((below ? r.bottom + 8 : r.top - h - 8) + window.scrollY) + "px";
  }
  function hideInfo() {
    if (!popFor) return;
    document.querySelectorAll('.info[data-info="' + popFor + '"]').forEach(function (b) { b.setAttribute("aria-expanded", "false"); b.removeAttribute("aria-describedby"); });
    popFor = null; popPinned = false; pop.hidden = true;
  }
  // The page redraws every few seconds; keep an open bubble on its (new) button.
  function reattachInfo() {
    if (!popFor) return;
    var b = document.querySelector('.info[data-info="' + popFor + '"]');
    if (!b) { hideInfo(); return; }
    b.setAttribute("aria-expanded", "true");
    b.setAttribute("aria-describedby", "infopop");
    pop.innerHTML = INFO[popFor] || "";
    placeInfo(b);
  }
  var hoverable = window.matchMedia && window.matchMedia("(hover: hover)").matches;
  document.addEventListener("mouseover", function (e) {
    if (!hoverable || popPinned) return;
    var b = e.target.closest && e.target.closest(".info");
    if (b) showInfo(b, false);
  });
  document.addEventListener("mouseout", function (e) {
    if (!hoverable || popPinned) return;
    var b = e.target.closest && e.target.closest(".info");
    if (b && !(e.relatedTarget && b.contains(e.relatedTarget))) hideInfo();
  });
  document.addEventListener("focusin", function (e) {
    var b = e.target.closest && e.target.closest(".info");
    if (b && !popPinned) showInfo(b, false);
  });
  document.addEventListener("focusout", function (e) {
    var b = e.target.closest && e.target.closest(".info");
    if (b && !popPinned && !(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest(".info") === b)) hideInfo();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hideInfo(); });
  window.addEventListener("resize", function () { if (popFor) reattachInfo(); });

  document.addEventListener("click", function (e) {
    var mr = e.target.closest("[data-mode-revert]");
    if (mr) {
      e.preventDefault();
      var lm2 = JSON.parse(JSON.stringify(S.nextepisode.library_modes || {}));
      delete lm2[mr.dataset.modeRevert];
      queueSave("nextepisode", "library_modes", lm2, true);
      render();
      return;
    }
    var rv = e.target.closest("[data-revert]");
    if (rv) {
      e.preventDefault();  // it sits inside a <label>
      var rp = rv.dataset.revert.split(".");
      queueSave(rp[0], rp[1], ST.defaults[rp[0]][rp[1]], true);
      render();
      return;
    }
    var ib = e.target.closest(".info");
    if (ib) {
      e.preventDefault();  // inside a <label>, don't also toggle its checkbox
      if (popFor === ib.dataset.info && popPinned) hideInfo(); else showInfo(ib, true);
      return;
    }
    if (popPinned && !e.target.closest(".infopop")) hideInfo();
    var seg = e.target.closest("[data-period], [data-chart]");
    if (seg) {
      if (seg.dataset.period) actPeriod = seg.dataset.period; else chartMetric = seg.dataset.chart;
      try { localStorage.setItem("pc-period", actPeriod); localStorage.setItem("pc-chart", chartMetric); } catch (err) { /* storage unavailable */ }
      render();
      return;
    }
    var a = e.target.closest("a.nav, a[data-nav]");
    if (a && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      var parts = a.getAttribute("href").split("#");
      go(parts[0].replace("/", ""), true);
      if (parts[1]) {
        var target = document.getElementById(parts[1]);
        if (target) target.scrollIntoView({ block: "start" });
      }
      return;
    }
    var t = e.target.closest("[data-toggle],[data-guide],[data-act],[data-copy]");
    if (!t) return;
    if (t.dataset.toggle) {
      if (t.disabled) return;
      var sec = t.dataset.toggle;
      queueSave(sec, "enabled", !S[sec].enabled, true);
      render();
    } else if (t.dataset.guide) {
      guides[t.dataset.guide] = !guides[t.dataset.guide];
      render();
    } else if (t.dataset.copy) {
      copyText(t.dataset.copy);
    } else {
      action(t.dataset.act, t);
    }
  });

  function action(act, el) {
    if (act === "recheck") { api("/api/checks", { rescan: true }).then(function () { toast("Checking…"); ST.checking = true; render(); }); }
    else if (act === "blockscan") {
      api("/api/blockscan", { dataset: el.dataset.ds }).then(function (r) {
        toast(r.started ? "Checking every file…" : "Already checking");
        ST.block_scans = ST.block_scans || {};
        ST.block_scans[el.dataset.ds] = { running: true, checked: 0 };
        render();
      });
    }
    else if (act === "benchmark") { api("/api/benchmark", {}).then(function () { toast("Measuring disk speed…"); ST.benchmark.running = true; render(); }); }
    else if (act === "warmup-run") { api("/api/warmup/run", {}).then(function (r) { toast(r.started ? "Warm-up started" : "Already running"); load(); }); }
    else if (act === "warmup-stop") { api("/api/warmup/stop", {}).then(function () { toast("Stopping…"); }); }
    else if (act === "mqtt-retry") {
      flush().then(function () { return api("/api/mqtt/reconnect", {}); }).then(function () {
        ST.mqtt = Object.assign({}, ST.mqtt, { state: "connecting", next_retry: null }); render(); setTimeout(load, 1500);
      });
    }
    else if (act === "retest") { flush().then(function () { return api("/api/test", {}); }).then(function () { ST.testing = true; render(); load(); }); }
    else if (act === "plex-signin") { signIn(); }
  }

  // navigator.clipboard only works on https:// or localhost; this app is usually
  // opened as http://<nas-ip>, so fall back to the older copy command.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { toast("Copied"); }, function () { legacyCopy(text); });
    } else {
      legacyCopy(text);
    }
  }
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select(); ta.setSelectionRange(0, text.length);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    toast(ok ? "Copied" : "Couldn’t copy — select the command and copy it by hand");
  }

  function saveMapping(plexPath, local) {
    var pm = (S.general.path_map || []).filter(function (r) { return r.plex !== plexPath; });
    if (local) pm.push({ plex: plexPath, local: local });
    mapPending[plexPath] = Date.now() / 1000;
    queueSave("general", "path_map", pm, true);
    render();
  }

  // Keep <details> boxes open across the automatic refreshes ("toggle" doesn't bubble, so capture it).
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    // Only remember a choice: the box opening itself by default isn't one.
    if (d.tagName === "DETAILS" && d.dataset.key && (d.dataset.key in openDetails || d.open !== (d.dataset.def === "1"))) openDetails[d.dataset.key] = d.open;
  }, true);

  function signIn() {
    var win = window.open("about:blank", "plexauth", "width=800,height=720");
    api("/api/plex/pin", {}).then(function (pin) {
      if (win) win.location = pin.auth_url; else window.open(pin.auth_url, "_blank");
      var tries = 0;
      var timer = setInterval(function () {
        if (++tries > 90) { clearInterval(timer); toast("Sign-in timed out"); return; }
        api("/api/plex/pin/" + pin.id, {}).then(function (r) {
          if (r.done) {
            clearInterval(timer);
            if (win) try { win.close(); } catch (e) { /* ignore */ }
            toast("Signed in with Plex"); load();
          }
        }).catch(function () { /* keep polling */ });
      }, 2000);
    }).catch(function (e) { if (win) win.close(); toast("Couldn’t start sign-in: " + e.message); });
  }

  function parseVal(sec, key, el) {
    var cur = S[sec][key];
    if (typeof cur === "number") return Number(el.value);
    return el.value;
  }

  main.addEventListener("input", function (e) {
    var el = e.target;
    if (el.type === "number" && el.dataset.set && el.value !== "") {
      var np = el.dataset.set.split("."), n = Number(el.value), lo = Number(el.min), hi = Number(el.max);
      if (n >= lo && n <= hi) {
        setLocal(np[0], np[1], n);
        refreshRevert(np[0], np[1]);
        if (np[0] === "warmup") {  // the sizing table follows as you type
          var siz2 = main.querySelector(".sizing");
          if (siz2) siz2.outerHTML = sizingHtml(!isOn("warmup"));
          var est2 = main.querySelector(".est");
          if (est2) est2.outerHTML = estimateHtml();
        }
      }
      return;
    }
    if (el.type === "range" && el.dataset.set) {
      var parts = el.dataset.set.split(".");
      setLocal(parts[0], parts[1], Number(el.value));
      var lab = main.querySelector('[data-val="' + el.id + '"]');
      if (lab) lab.textContent = el.value + " " + el.dataset.unit;
      refreshRevert(parts[0], parts[1]);
      if (parts[0] === "warmup") {
        var est = main.querySelector(".est");
        if (est) est.outerHTML = estimateHtml();
        var siz = main.querySelector(".sizing");
        if (siz) siz.outerHTML = sizingHtml(!isOn("warmup"));
      }
    }
  });

  main.addEventListener("change", function (e) {
    var el = e.target;
    if (el.dataset.set) {
      var p = el.dataset.set.split(".");
      queueSave(p[0], p[1], parseVal(p[0], p[1], el), el.tagName === "SELECT");
      refreshRevert(p[0], p[1]);
      if (el.tagName === "SELECT") render();
    } else if (el.dataset.secret) {
      var q = el.dataset.secret.split(".");
      if (el.value) {
        queueSave(q[0], q[1], el.value.trim(), true);
        // Don't leave it in the page: browsers offer to save fields that still hold a key.
        el.value = "";
        el.placeholder = "Saved — paste to replace";
      }
    } else if (el.dataset.nemode !== undefined) {
      var lm = JSON.parse(JSON.stringify(S.nextepisode.library_modes || {}));
      lm[el.dataset.nemode] = el.value;
      queueSave("nextepisode", "library_modes", lm, true);
      render();
    } else if (el.dataset.check) {
      var c = el.dataset.check.split(".");
      queueSave(c[0], c[1], el.checked, true);
    } else if (el.dataset.lib) {
      var keys = [];
      // Greyed-out boxes (folder not found yet) stay wanted: they're cached once the folder is fixed.
      main.querySelectorAll("[data-lib]").forEach(function (cb) { if (!cb.checked && !cb.disabled) keys.push(cb.dataset.lib); });
      queueSave("general", "excluded_libraries", keys, true);
    } else if (el.dataset.map !== undefined) {
      var plexPath = el.dataset.map;
      if (el.value === "__other__") { otherOpen[plexPath] = true; render(); return; }
      otherOpen[plexPath] = false;
      saveMapping(plexPath, el.value);
    } else if (el.dataset.speed !== undefined) {
      var sp = JSON.parse(JSON.stringify(S.general.disk_speeds || {}));
      var v = parseInt(el.value, 10);
      if (!(v >= 10 && v <= 20000)) { toast("Speed must be between 10 and 20000 MB/s"); return; }
      sp[el.dataset.speed] = { mb_s: v, measured_at: null };
      queueSave("general", "disk_speeds", sp, true);
    } else if (el.dataset.mapOther !== undefined) {
      var v = el.value.trim();
      if (v && v.charAt(0) !== "/") { toast("Enter a full path starting with /"); return; }
      saveMapping(el.dataset.mapOther, v);
    }
  });

  window.addEventListener("popstate", function () { go(location.pathname.replace("/", "") || "health", false); });

  page = location.pathname.replace("/", "") || "health";
  if (!PAGES[page]) page = "health";
  load().then(function () { go(page, false); });
  setInterval(function () { if (!document.hidden) load(); }, 4000);
})();
