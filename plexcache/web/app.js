/* Plex Cache web UI — plain JS, no build step. */
(function () {
  "use strict";

  var ST = null;            // last /api/state
  var S = null;             // settings (local copy, updated optimistically)
  var page = "health";
  var guides = { warmup: false, nowplaying: false, nextepisode: false };
  var importNote = "";   // result of the last config import, kept across re-renders
  var testNotes = {};    // results of connection tests
  var main = document.getElementById("main");
  var MASK = "__unchanged__";
  var GB = 1e9;
  var KINDS = ["movie", "tv", "audio"];
  var KIND_NAME = { movie: "Movies", tv: "TV", audio: "Audio" };

  // ------------------------------------------------------------------ utils
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtBytes(n) {
    if (n == null) return "unknown";
    var u = [["TB", 1e12], ["GB", 1e9], ["MB", 1e6]];
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
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }
  function badge(status, text) { return '<span class="badge b-' + esc(status) + '">' + esc(text) + "</span>"; }
  var STATUS_TEXT = { pass: "Pass", warn: "Warn", fail: "Fail", info: "Info" };
  var VERDICT_TEXT = { use: "Use", maybe: "Maybe", avoid: "Skip" };

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
    }).catch(function (e) { toast("Not saved: " + e.message); load(); });
  }

  // ------------------------------------------------------------- planning
  // Mirrors plexcache/warmup.py plan() so sliders update instantly.
  function plan(stats, w, l2) {
    var pad = { movie: w.padding_video, tv: w.padding_video, audio: w.padding_audio };
    var mx = {}, mn = {}, needMax = 0, needMin = 0;
    KINDS.forEach(function (k) {
      var perSec = (stats.avg_kbps[k] || 0) * 125 * (stats.count[k] || 0);
      mx[k] = perSec * w[k + "_sec"] * (100 + pad[k]) / 100;
      mn[k] = perSec * w[k + "_min_sec"] * (100 + pad[k]) / 100;
      needMax += mx[k]; needMin += mn[k];
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
    KINDS.forEach(function (k) { used[k] = w[k + "_sec"] ? mx[k] * seconds[k] / w[k + "_sec"] : 0; });
    return { needMax: needMax, need: mx, used: used, reserve: reserve, l2: l2, seconds: seconds, status: status };
  }

  // ---------------------------------------------------------------- pages
  function pageHealth() {
    var h = ST.health;
    if (!h) return top("Health", "Running the first checks…", "") + '<p class="muted">Checking Plex, your media mounts and ZFS. This takes a few seconds.</p>';
    var c = h.counts;
    var out = top("Health",
      "Checks run on startup, every 6 hours, and whenever settings change. Last run " + esc(ago(h.at)) + ".",
      '<button class="btn pri" data-act="recheck"' + (ST.checking ? " disabled" : "") + ">" + (ST.checking ? "Checking…" : "Re-run all checks") + "</button>");
    out += '<div class="tiles">' +
      tile(c.pass, "checks passed", "var(--ok)") +
      tile(c.warn, c.warn === 1 ? "warning — features still run" : "warnings — features still run", "var(--warn)") +
      tile(c.fail, c.fail === 1 ? "failure" : "failures", c.fail ? "var(--bad)" : "var(--muted)") + "</div>";
    out += '<div class="cols"><div class="wide stack">';
    h.groups.forEach(function (g) {
      out += '<section class="card"><h2>' + esc(g.title) + '</h2><p class="hint">' + esc(g.hint) + "</p>";
      if (g.table && g.table.length) {
        out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Library</th><th scope="col">Plex sees</th><th scope="col">Items</th><th scope="col">Readable here</th></tr></thead><tbody>';
        g.table.forEach(function (r) {
          out += "<tr><td>" + esc(r.library) + '</td><td class="mono">' + r.plex_paths.map(esc).join("<br>") +
            (r.local_paths.join() !== r.plex_paths.join() ? '<br><span class="muted">→ ' + r.local_paths.map(esc).join(", ") + "</span>" : "") +
            '</td><td class="nw">' + (r.excluded ? "—" : fmtNum(r.count)) + "</td><td>" + badge(r.status, STATUS_TEXT[r.status] || r.status) +
            (r.status !== "pass" ? '<div class="det">' + esc(r.detail) + "</div>" : "") + "</td></tr>";
        });
        out += "</tbody></table></div>";
        g.table.forEach(function (r) { if (r.fix) out += '<div class="fix"><strong>' + esc(r.library) + ":</strong> " + esc(r.fix) + "</div>"; });
      }
      g.items.forEach(function (it) {
        out += '<div class="row">' + badge(it.status, STATUS_TEXT[it.status] || it.status) + '<div class="grow"><div class="what">' + esc(it.title) +
          '</div><div class="det">' + esc(it.detail) + "</div>";
        if (it.fix || it.fix_cmd) {
          out += '<div class="fix">' + esc(it.fix);
          if (it.fix_cmd) out += '<div class="cmd"><code>' + esc(it.fix_cmd) + '</code><button class="btn" data-copy="' + esc(it.fix_cmd) + '">Copy</button></div>';
          out += "</div>";
        }
        out += "</div>";
        if (g.action === "benchmark") {
          out += '<button class="btn" data-act="benchmark"' + (ST.benchmark.running ? " disabled" : "") + ">" + (ST.benchmark.running ? "Measuring…" : "Benchmark now") + "</button>";
        }
        out += "</div>";
      });
      if (g.action === "benchmark" && ST.benchmark.last && ST.benchmark.last.rows && ST.benchmark.last.rows.length) {
        out += '<details><summary>Last benchmark details</summary><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Type</th><th>MB/s</th><th>Read</th><th>File</th></tr></thead><tbody>';
        ST.benchmark.last.rows.forEach(function (r) {
          out += "<tr><td>" + esc(KIND_NAME[r.kind]) + "</td><td>" + esc(r.mb_s) + "</td><td>" + esc(r.mb) + ' MB</td><td class="mono">' + esc(r.file) + "</td></tr>";
        });
        out += "</tbody></table></div></details>";
      }
      out += "</section>";
    });
    out += '</div><aside class="card narrow"><h2>Recommended for this system</h2><p class="hint">Based on the checks. Turn features on in Features.</p>';
    var names = { warmup: "Library warm-up", nextepisode: "Next-episode prefetch", nowplaying: "Now-playing preload" };
    h.recommendations.forEach(function (r) {
      out += '<div class="row">' + badge(r.verdict, VERDICT_TEXT[r.verdict]) + '<div class="grow"><div class="what">' + esc(names[r.feature]) +
        (S[r.feature].enabled ? ' <span class="muted" style="font-weight:400">· on</span>' : "") + '</div><div class="det">' + esc(r.reason) + "</div></div></div>";
    });
    out += '<a class="btn pri" href="/features" data-nav style="width:100%;margin-top:14px">Go to Features</a></aside></div>';
    return out;
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
    var text = { use: "Recommended", maybe: "Check first", avoid: "Not for this system" }[r.verdict];
    return badge(r.verdict, text);
  }
  function toggle(sec, label) {
    return '<button class="sw" type="button" aria-pressed="' + (S[sec].enabled ? "true" : "false") + '" aria-label="' + esc(label) + '" data-toggle="' + sec + '"><span></span></button>';
  }
  function guideBtn(sec) {
    return '<button class="link" type="button" data-guide="' + sec + '" aria-expanded="' + guides[sec] + '">' + (guides[sec] ? "Hide how it works" : "How it works") + "</button>";
  }
  function guide(sec, cols) {
    if (!guides[sec]) return "";
    return '<div class="guide">' + cols.map(function (c) { return "<div><h3>" + c[0] + "</h3><p>" + c[1] + "</p></div>"; }).join("") + "</div>";
  }
  function range(sec, key, label, min, max, step, unit, help) {
    var id = "f-" + sec + "-" + key;
    return '<div class="fld"><label for="' + id + '">' + esc(label) + '<span class="val" data-val="' + id + '">' + esc(S[sec][key]) + " " + unit + "</span></label>" +
      '<input id="' + id + '" type="range" min="' + min + '" max="' + max + '" step="' + step + '" value="' + esc(S[sec][key]) + '" data-set="' + sec + "." + key + '" data-unit="' + unit + '">' +
      (help ? '<div class="help">' + help + "</div>" : "") + "</div>";
  }
  function select(sec, key, label, options, help) {
    var id = "f-" + sec + "-" + key;
    var cur = String(S[sec][key]);
    return '<div class="fld"><label for="' + id + '">' + esc(label) + '</label><select id="' + id + '" data-set="' + sec + "." + key + '">' +
      options.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (String(o[0]) === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join("") +
      "</select>" + (help ? '<div class="help">' + help + "</div>" : "") + "</div>";
  }
  function number(sec, key, label, min, max, help) {
    var id = "f-" + sec + "-" + key;
    return '<div class="fld"><label for="' + id + '">' + esc(label) + '</label><input id="' + id + '" type="number" min="' + min + '" max="' + max + '" value="' + esc(S[sec][key]) + '" data-set="' + sec + "." + key + '">' +
      (help ? '<div class="help">' + help + "</div>" : "") + "</div>";
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
      '<div style="width:' + pct(p.used.audio) + '%;background:var(--c-audio)"></div><div style="width:' + pct(p.reserve) + '%;background:var(--c-res)"></div></div>';
    out += '<div class="legend"><span><i style="background:var(--c-movie)"></i>Movies ' + fmtBytes(p.used.movie) + "</span><span><i style=\"background:var(--c-tv)\"></i>TV " + fmtBytes(p.used.tv) +
      '</span><span><i style="background:var(--c-audio)"></i>Audio ' + fmtBytes(p.used.audio) + '</span><span><i style="background:var(--c-res)"></i>Kept free ' + w.reserve_gb + " GB</span>" +
      (p.l2 != null ? "<span>Unused " + fmtBytes(Math.max(0, p.l2 - p.needMax - p.reserve)) + "</span>" : "") + "</div>";
    if (p.status === "reduced" || p.status === "insufficient") {
      out += '<div class="help muted" style="font-size:13px;margin-top:6px">Will use: ' + KINDS.map(function (k) { return KIND_NAME[k] + " " + p.seconds[k] + " s"; }).join(", ") + "</div>";
    }
    out += '<div class="muted" style="font-size:13px;margin-top:8px">Based on ' + fmtNum(st.count.movie) + " movies (avg " + (st.avg_kbps.movie / 1000).toFixed(1) + " Mbps), " +
      fmtNum(st.count.tv) + " episodes (" + (st.avg_kbps.tv / 1000).toFixed(1) + " Mbps) and " + fmtNum(st.count.audio) + " tracks (" + st.avg_kbps.audio +
      " kbps), plus a small safety margin. Each file is read using its own bitrate.</div></div>";
    return out;
  }

  function warmupStatus() {
    var wu = ST.warmup;
    if (wu.running) {
      var p = wu.progress || { done: 0, total: 0, bytes: 0 };
      var pct = p.total ? Math.round(p.done / p.total * 100) : 0;
      return '<div class="note" style="background:var(--guide-bg);border-color:var(--guide-line);color:var(--ink)"><div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between"><div>' +
        badge("running", wu.phase === "scanning" ? "Preparing" : "Running") + " " + (p.total ? fmtNum(p.done) + " of " + fmtNum(p.total) + " files · " + fmtBytes(p.bytes) + " read" : "Reading library list…") +
        '</div><button class="btn danger" data-act="warmup-stop">Stop</button></div><div class="meter"><div style="width:' + pct + '%"></div></div></div>';
    }
    return "";
  }

  function pageFeatures() {
    var out = top("Features", "Each feature can be switched on separately. Open “How it works” on any card for when to use it and what it needs. Changes save automatically.", "");
    var props = ST.zfs.dataset_props;
    // ---- warm-up
    var w = S.warmup;
    out += '<div class="stack"><section class="card' + (w.enabled ? "" : " off") + '"><div class="head">' + toggle("warmup", "Library warm-up") +
      "<h2>Library warm-up</h2>" + rec("warmup") + '<span style="flex:1"></span>' + guideBtn("warmup") + "</div>" +
      '<p class="lede">On a schedule, reads the first few seconds of every movie, episode and track so they land in the SSD cache (L2ARC). Playback then starts at SSD speed instead of waiting for the hard drives.</p>' +
      guide("warmup", [
        ["When it helps", "Pressing play on anything in the library, especially titles nobody has watched in a while. Large libraries on HDD pools see the biggest gain."],
        ["What it needs", "At least one L2ARC (cache) device on the media pool, and <span class=\"mono\">secondarycache=all</span> on the dataset. Health checks both."],
        ["Cost", "Disk time while it runs (paced below) and L2ARC space (estimated below). Re-runs keep cached starts from being evicted."]]);
    out += warmupStatus();
    if (w.enabled) {
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
        range("warmup", "movie_sec", "Movies — seconds to cache", 3, 180, 1, "s", "Plex buffers 3–15 s locally, up to 60 s for remote or transcoded streams.") +
        range("warmup", "tv_sec", "TV episodes — seconds to cache", 3, 180, 1, "s", "Episodes are usually lower bitrate, so more seconds cost less.") +
        range("warmup", "audio_sec", "Audio — seconds to cache", 1, 60, 1, "s", "Audiobooks and music. Tiny files; rarely matters for space.") +
        range("warmup", "reserve_gb", "Keep free in L2ARC", 0, 500, 10, "GB", "Room left for everything else ZFS wants to cache.");
      var libs = (ST.library.libraries || []).filter(function (l) { return l.kind; });
      if (libs.length) {
        out += '<div class="fld" style="grid-column:1/-1"><span class="lbl">Libraries</span><div class="chk">' + libs.map(function (l) {
          var row = ST.health && ST.health.groups.filter(function (g) { return g.id === "media"; })[0];
          var st = row && row.table.filter(function (r) { return r.key === l.key; })[0];
          var warn = st && (st.status === "warn" || st.status === "fail") ? " " + badge(st.status, "not readable") : "";
          return '<label><input type="checkbox" data-lib="' + esc(l.key) + '"' + (l.excluded ? "" : " checked") + ">" + esc(l.title) + warn + "</label>";
        }).join("") + '</div><div class="help">Unticked libraries are skipped by all features.</div></div>';
      }
      out += "</div>" + estimateHtml();
    }
    out += "</section>";

    // ---- now playing
    var n = S.nowplaying;
    out += '<section class="card' + (n.enabled ? "" : " off") + '"><div class="head">' + toggle("nowplaying", "Now-playing preload") +
      "<h2>Now-playing preload</h2>" + rec("nowplaying") + '<span style="flex:1"></span>' + guideBtn("nowplaying") + "</div>" +
      '<p class="lede">When someone presses play, waits for Plex to finish its first burst of reads, then reads ahead of the viewer so skipping forward and back comes from cache instead of the disks.</p>' +
      guide("nowplaying", [
        ["When it helps", "Skipping intros, scrubbing, jumping back. Works even with no SSDs, as long as file data can stay in RAM."],
        ["What it needs", "Ideally <span class=\"mono\">primarycache=all</span> on the dataset so reads stay in RAM. With <span class=\"mono\">metadata</span> it only helps if L2ARC keeps the data."],
        ["Cost", "RAM (ARC) roughly equal to what is read. On large remuxes, cap the amount so other cached data isn’t pushed out."]]);
    if (n.enabled) {
      if (props && props.primarycache !== "all") {
        out += '<div class="note"><strong>From Health:</strong> your media dataset uses <span class="mono">primarycache=' + esc(props.primarycache) +
          "</span>, so these reads won’t stay in RAM. <a href=\"/health\" data-nav>See the fix</a>, or keep the amount small.</div>";
      }
      out += '<div class="grid">' +
        select("nowplaying", "mode", "How much to read", [["minutes", "A number of minutes ahead"], ["size", "Up to a size limit"], ["whole", "The rest of the file"]],
          n.mode === "whole" ? "A 60 GB remux uses 60 GB of cache." : "") +
        (n.mode === "minutes" ? number("nowplaying", "minutes", "Minutes ahead of the play position", 1, 600, "Uses the file’s bitrate to work out the size.") : "") +
        (n.mode === "size" ? number("nowplaying", "size_gb", "Size limit (GB)", 1, 1000, "") : "") +
        select("nowplaying", "delay_sec", "Start delay", [[0, "Automatic (20 s local, 45 s remote/transcoding)"], [15, "15 seconds"], [30, "30 seconds"], [60, "60 seconds"]], "Lets Plex’s own startup reads finish first.") +
        number("nowplaying", "max_streams", "Streams at once", 1, 20, "Extra streams wait their turn.") +
        select("nowplaying", "min_kbps", "Skip when bitrate is below", [[0, "Never skip"], [2000, "2 Mbps"], [5000, "5 Mbps"], [10000, "10 Mbps"]], "Low-bitrate files rarely stall even from HDD.") +
        "</div>";
    }
    out += "</section>";

    // ---- next episode
    var x = S.nextepisode;
    out += '<section class="card' + (x.enabled ? "" : " off") + '"><div class="head">' + toggle("nextepisode", "Next-episode prefetch") +
      "<h2>Next-episode prefetch</h2>" + rec("nextepisode") + '<span style="flex:1"></span>' + guideBtn("nextepisode") + "</div>" +
      '<p class="lede">While a TV episode plays, reads the start of the next one so it starts instantly. Uses Plex’s own episode order, so it crosses seasons and works with any folder layout.</p>' +
      guide("nextepisode", [
        ["When it helps", "Binge watching. Removes the pause between episodes, even without L2ARC."],
        ["What it needs", "Nothing extra. Plex tells the app what is playing — no Tautulli, no scripts, no Plex Pass."],
        ["Cost", "One read of up to the limit below per episode watched, at a gentle pace."]]);
    if (x.enabled) {
      out += '<div class="grid">' +
        select("nextepisode", "start_at", "Start prefetch when playback passes", [["50", "50% of the episode"], ["0", "Right away"], ["75", "75% of the episode"], ["credits", "5 minutes before the credits"]],
          "Earlier gives more time; later avoids reading for episodes people abandon.") +
        select("nextepisode", "max_gb", "Read up to", [[0.5, "500 MB"], [2, "2 GB per episode"], [6, "6 GB"], [0, "The whole episode"]], "Split across versions if an episode has more than one file.") +
        "</div>";
    }
    out += "</section></div>";
    return out;
  }

  function pageActivity() {
    var z = ST.zfs, arc = z.arc, a = ST.activity;
    var out = top("Activity", "What the cache is doing now, and whether past runs actually landed in cache.", "");
    out += '<div class="tiles">';
    if (z.pool && z.pool.l2_total && arc) {
      var pct = Math.min(100, arc.l2_asize / z.pool.l2_total * 100);
      out += '<div class="card tile"><div class="muted" style="font-size:13px">L2ARC (SSD cache)</div><div class="big">' + esc(fmtBytes(arc.l2_asize)) +
        " <small>of " + esc(fmtBytes(z.pool.l2_total)) + '</small></div><div class="meter"><div style="width:' + pct.toFixed(1) + '%"></div></div><div class="muted" style="font-size:13px">' +
        (a.runs[0] && a.runs[0].l2_growth != null ? (a.runs[0].l2_growth >= 0 ? "+" : "") + esc(fmtBytes(Math.abs(a.runs[0].l2_growth))) + " from the last warm-up" : "&nbsp;") + "</div></div>";
    } else if (arc) {
      out += '<div class="card tile"><div class="muted" style="font-size:13px">L2ARC (SSD cache)</div><div class="big">' + esc(fmtBytes(arc.l2_asize)) + '</div><div class="muted" style="font-size:13px">filled · add the TrueNAS API key for device size</div></div>';
    }
    if (arc) {
      var hr = arc.arc_hit_ratio == null ? null : arc.arc_hit_ratio * 100;
      out += '<div class="card tile"><div class="muted" style="font-size:13px">ARC (RAM cache) hit rate since boot</div><div class="big">' + (hr == null ? "—" : hr.toFixed(0) + "%") +
        '</div><div class="meter"><div style="width:' + (hr || 0).toFixed(1) + '%;background:var(--c-tv)"></div></div><div class="muted" style="font-size:13px">' + esc(fmtBytes(arc.arc_size)) + " in use</div></div>";
    }
    var wk = a.week || {};
    out += '<div class="card tile"><div class="muted" style="font-size:13px">Prefetches in the last 7 days</div><div class="big">' + fmtNum((wk.nextepisode || 0) + (wk.nowplaying || 0)) +
      '</div><div class="muted" style="font-size:13px">' + fmtNum(wk.nextepisode || 0) + " next episodes · " + fmtNum(wk.nowplaying || 0) + " now-playing</div></div></div>";

    // sessions
    var ss = ST.playback.sessions;
    out += '<section class="card" style="margin-bottom:20px"><h2 style="margin-bottom:14px">Playing now</h2>';
    if (ST.playback.error) out += '<div class="note">' + esc(ST.playback.error) + "</div>";
    if (!ss.length) out += '<p class="muted" style="margin:0">Nothing is playing.</p>';
    ss.forEach(function (s) {
      out += '<div class="sess"><div><div class="what">' + esc(s.label) + '</div><div class="muted" style="font-size:13px">' + esc(s.player || "Unknown player") + " · " +
        (s.local === false ? "remote" : "local") + " · " + (s.transcoding ? "transcoding" : "direct play") + " · " + esc(s.progress) + "% watched</div></div><div>";
      if (!s.jobs.length) out += '<span class="muted" style="font-size:13px">No cache work for this stream yet</span>';
      s.jobs.forEach(function (j) {
        var label = { waiting: "Waiting", reading: "Reading", done: "Done", failed: "Failed", cancelled: "Stopped" }[j.state];
        var feat = j.feature === "nextepisode" ? "Next-episode prefetch" : "Now-playing preload";
        var amt = j.state === "reading" ? fmtBytes(j.read) + " of " + fmtBytes(j.length) : (j.state === "done" ? fmtBytes(j.read) + " cached " + ago(j.finished) : fmtBytes(j.length));
        out += '<div class="muted" style="font-size:13px">' + esc(feat) + '</div><div style="display:flex;gap:10px;align-items:center;margin:4px 0 8px;flex-wrap:wrap">' + badge(j.state, label) + "<span>" + esc(j.target) + " · " + esc(amt) + "</span></div>";
      });
      out += "</div></div>";
    });
    out += "</section>";

    // runs
    out += '<section class="card" style="margin-bottom:20px"><div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:baseline"><h2>Library warm-up runs</h2><span class="muted" style="font-size:13px">' +
      (S.warmup.enabled ? (ST.warmup.next_run ? "Next run " + esc(fmtIso(ST.warmup.next_run)) : "Manual only") : "Library warm-up is off") + "</span></div>" + warmupStatus();
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
      out += '<div class="muted" style="font-size:13px;margin-top:10px">“L2ARC grew” compares cache size before and after each run. ZFS fills L2ARC gradually, so it can lag a little. If it stays near zero and “Already cached” stays low on repeat runs, reads aren’t landing in L2ARC — Health will flag it.</div>';
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

  function secretInput(sec, key, label, id) {
    var set = S[sec][key + "_set"];
    return '<div class="fld"><label for="' + id + '">' + esc(label) + '</label><input id="' + id + '" type="password" autocomplete="off" placeholder="' + (set ? "Saved — type to replace" : "") +
      '" value="" data-secret="' + sec + "." + key + '"></div>';
  }
  function textInput(sec, key, label, id, help, placeholder) {
    return '<div class="fld"><label for="' + id + '">' + esc(label) + '</label><input id="' + id + '" type="text" value="' + esc(S[sec][key]) + '" placeholder="' + esc(placeholder || "") +
      '" data-set="' + sec + "." + key + '">' + (help ? '<div class="help">' + help + "</div>" : "") + "</div>";
  }

  function pageSettings() {
    var out = top("Settings", "Connections and system-wide options. Stored in the app’s config folder, so they survive updates.", "");
    out += '<div class="stack" style="max-width:980px">';
    out += '<section class="card"><h2>Plex server</h2><p class="hint">Used to list libraries and see what is playing.</p><div class="grid" style="margin-top:0">' +
      textInput("plex", "url", "Server address", "s-purl", "Your NAS’s IP with port 32400. Not 127.0.0.1 — inside this app that means the app itself.", "http://192.168.1.10:32400") +
      secretInput("plex", "token", "Token", "s-ptok") + '</div><div class="acts"><button class="btn pri" data-act="plex-signin">Sign in with Plex</button><button class="btn" data-act="test-plex">Test connection</button><span id="plex-result">' + (testNotes["plex-result"] || "") + '</span></div></section>';

    out += '<section class="card"><h2>TrueNAS</h2><p class="hint">Lets the app read pool, cache-device and dataset settings for Health. It only reads; it never changes anything.</p><div class="grid" style="margin-top:0">' +
      textInput("truenas", "url", "TrueNAS address", "s-turl", "Must be https:// — TrueNAS revokes API keys sent over plain http.", "https://192.168.1.10") +
      secretInput("truenas", "api_key", "API key", "s-tkey") + "</div>" +
      '<div class="chk" style="margin-top:12px"><label><input type="checkbox" data-check="truenas.verify_tls"' + (S.truenas.verify_tls ? " checked" : "") + ">Verify the TLS certificate (leave off for TrueNAS’s self-signed certificate)</label></div>" +
      "<details><summary>How to create an API key</summary>In TrueNAS, open your user menu (top right) → API Keys, add a key named “plex-cache”, and paste it here. Without a key, Health skips the cache-device and dataset checks and Features can’t size Library warm-up.</details>" +
      '<div class="acts"><button class="btn" data-act="test-truenas">Test connection</button><span id="tn-result">' + (testNotes["tn-result"] || "") + '</span></div></section>';

    var g = S.general;
    out += '<section class="card"><h2>Behaviour</h2><p class="hint">Applies to all features.</p><div class="grid" style="margin-top:0">' +
      select("general", "poll_seconds", "Check what’s playing every", [[5, "5 seconds"], [10, "10 seconds"], [30, "30 seconds"]], "Drives Now-playing and Next-episode.") +
      number("general", "hdd_mb_per_sec", "Disk speed (MB/s)", 10, 5000, g.hdd_measured_at ? "Measured " + esc(g.hdd_measured_at.replace("T", " ")) + ". Re-measure from Health." : "A guess until you benchmark from Health.") +
      select("general", "pause_when_streams", "Pause warm-up while", [[3, "3 or more streams are playing"], [1, "Any stream is playing"], [0, "Never pause"]]) +
      "</div></section>";

    out += '<section class="card"><h2>Path rewrites</h2><p class="hint">Only needed if you can’t mount media in this app at the same path Plex uses. Each Plex path prefix is replaced with the local one.</p>';
    out += '<div class="stack" style="gap:8px">';
    (g.path_map || []).forEach(function (r, i) {
      out += '<div class="pathmap"><input aria-label="Plex path" class="mono" style="min-height:44px;padding:0 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink)" value="' + esc(r.plex) +
        '" data-pm="' + i + '.plex"><input aria-label="Path in this app" class="mono" style="min-height:44px;padding:0 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink)" value="' + esc(r.local) +
        '" data-pm="' + i + '.local"><button class="btn" data-pm-del="' + i + '" aria-label="Remove rewrite">Remove</button></div>';
    });
    out += '</div><div class="acts"><button class="btn" data-act="pm-add">Add a rewrite</button></div></section>';

    out += '<section class="card"><h2>Coming from the scripts?</h2><p class="hint">Import your old <span class="mono">plex-l2arc.conf</span> and the values carry over. Then remove the Tautulli notifications and the TrueNAS cron job that ran the scripts.</p>' +
      '<div class="acts" style="margin-top:0"><label class="btn" for="imp">Import plex-l2arc.conf…</label><input id="imp" class="sr" type="file" accept=".conf,.txt,text/plain" data-import>' +
      '<a class="btn" href="/api/export">Export settings</a></div><div id="imp-result">' + importNote + '</div></section>';
    out += "</div>";
    return out;
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
    document.getElementById("version").textContent = "TrueNAS · v" + ST.version;
    main.innerHTML = PAGES[page]();
  }

  function editing() {
    var a = document.activeElement;
    return a && main.contains(a) && /INPUT|SELECT|TEXTAREA/.test(a.tagName);
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
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a.nav, a[data-nav]");
    if (a && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      go(a.getAttribute("href").replace("/", ""), true);
      return;
    }
    var t = e.target.closest("[data-toggle],[data-guide],[data-act],[data-copy],[data-pm-del]");
    if (!t) return;
    if (t.dataset.toggle) {
      var sec = t.dataset.toggle;
      queueSave(sec, "enabled", !S[sec].enabled, true);
      render();
    } else if (t.dataset.guide) {
      guides[t.dataset.guide] = !guides[t.dataset.guide];
      render();
    } else if (t.dataset.copy) {
      navigator.clipboard && navigator.clipboard.writeText(t.dataset.copy).then(function () { toast("Copied"); });
    } else if (t.dataset.pmDel !== undefined) {
      var pm = S.general.path_map.slice(); pm.splice(+t.dataset.pmDel, 1);
      queueSave("general", "path_map", pm, true); render();
    } else {
      action(t.dataset.act, t);
    }
  });

  function action(act, el) {
    if (act === "recheck") { api("/api/checks", { rescan: true }).then(function () { toast("Checking…"); ST.checking = true; render(); }); }
    else if (act === "benchmark") { api("/api/benchmark", {}).then(function () { toast("Measuring disk speed…"); ST.benchmark.running = true; render(); }); }
    else if (act === "warmup-run") { api("/api/warmup/run", {}).then(function (r) { toast(r.started ? "Warm-up started" : "Already running"); load(); }); }
    else if (act === "warmup-stop") { api("/api/warmup/stop", {}).then(function () { toast("Stopping…"); }); }
    else if (act === "test-plex") { flush().then(function () { return api("/api/test/plex", {}); }).then(function (r) { result("plex-result", r); }); }
    else if (act === "test-truenas") { flush().then(function () { return api("/api/test/truenas", {}); }).then(function (r) { result("tn-result", r); }); }
    else if (act === "pm-add") { var pm = (S.general.path_map || []).slice(); pm.push({ plex: "", local: "" }); S.general.path_map = pm; render(); }
    else if (act === "plex-signin") { signIn(); }
  }

  function result(id, r) {
    testNotes[id] = badge(r.ok ? "pass" : "fail", r.ok ? "Connected" : "Failed") + ' <span class="det">' + esc(r.message) + "</span>";
    var el = document.getElementById(id);
    if (el) el.innerHTML = testNotes[id];
  }

  function signIn() {
    var win = window.open("about:blank", "plexauth", "width=800,height=720");
    api("/api/plex/pin", {}).then(function (pin) {
      if (win) win.location = pin.auth_url; else window.open(pin.auth_url, "_blank");
      var tries = 0;
      var timer = setInterval(function () {
        if (++tries > 90) { clearInterval(timer); toast("Sign-in timed out"); return; }
        api("/api/plex/pin/" + pin.id).then(function (r) {
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
    if (el.type === "range" && el.dataset.set) {
      var parts = el.dataset.set.split(".");
      setLocal(parts[0], parts[1], Number(el.value));
      var lab = main.querySelector('[data-val="' + el.id + '"]');
      if (lab) lab.textContent = el.value + " " + el.dataset.unit;
      if (parts[0] === "warmup") {
        var est = main.querySelector(".est");
        if (est) est.outerHTML = estimateHtml();
      }
    }
  });

  main.addEventListener("change", function (e) {
    var el = e.target;
    if (el.dataset.set) {
      var p = el.dataset.set.split(".");
      queueSave(p[0], p[1], parseVal(p[0], p[1], el), el.tagName === "SELECT");
      if (el.tagName === "SELECT") render();
    } else if (el.dataset.secret) {
      var q = el.dataset.secret.split(".");
      if (el.value) { queueSave(q[0], q[1], el.value.trim(), true); }
    } else if (el.dataset.check) {
      var c = el.dataset.check.split(".");
      queueSave(c[0], c[1], el.checked, true);
    } else if (el.dataset.lib) {
      var keys = [];
      main.querySelectorAll("[data-lib]").forEach(function (cb) { if (!cb.checked) keys.push(cb.dataset.lib); });
      queueSave("general", "excluded_libraries", keys, true);
    } else if (el.dataset.pm) {
      var m = el.dataset.pm.split(".");
      var pm = S.general.path_map.map(function (r) { return { plex: r.plex, local: r.local }; });
      pm[+m[0]][m[1]] = el.value.trim();
      S.general.path_map = pm;
      if (pm.every(function (r) { return r.plex && r.local; })) queueSave("general", "path_map", pm, true);
    } else if (el.dataset.import !== undefined && el.files && el.files[0]) {
      el.files[0].text().then(function (text) { return api("/api/import", { text: text }); }).then(function (r) {
        S = r.settings;
        importNote = '<div class="fix">' + (r.imported.length ? "Imported " + r.imported.length + " settings: " + esc(r.imported.join(", ")) + "." : "Nothing imported.") +
          r.notes.map(function (n) { return "<div style=\"margin-top:6px\">• " + esc(n) + "</div>"; }).join("") + "</div>";
        toast("Imported"); render();
      }).catch(function (err) { toast("Import failed: " + err.message); });
    }
  });

  window.addEventListener("popstate", function () { go(location.pathname.replace("/", "") || "health", false); });

  page = location.pathname.replace("/", "") || "health";
  if (!PAGES[page]) page = "health";
  load().then(function () { go(page, false); });
  setInterval(function () { if (!document.hidden) load(); }, 4000);
})();
