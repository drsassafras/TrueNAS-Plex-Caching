/* Plex Cache web UI — plain JS, no build step. Text goes through i18n.js (T, H, P...). */
PCI18N.ready.then(function () {
  "use strict";

  var T = PCI18N.T, H = PCI18N.H, P = PCI18N.P, PH = PCI18N.PH, N = PCI18N.N, msg = PCI18N.msg;
  var LOCALE = PCI18N.locale;
  var ST = null;            // last /api/state
  var S = null;             // settings (local copy, updated optimistically)
  var page = "health";
  var guides = { nowplaying: false, leadin: false, wholefile: false, lasts: false };
  var openDetails = {};  // which <details> boxes are open, so re-renders keep them open
  var mapPending = {};   // library folder -> time its mapping was saved (shows "Checking…")
  var otherOpen = {};    // library folders where "Other path…" was chosen
  var main = document.getElementById("main");
  var MASK = "__unchanged__";
  var GB = 1e9;
  var KINDS = ["movie", "tv", "audio"];
  var KIND_NAME = { movie: T("Movies"), tv: T("TV"), audio: T("Audio") };
  // ------------------------------------------------------------ caching mode
  // L2ARC or RAM only (see plexcache/cachemode.py). Three marks, explained in the legend:
  //   ram-def  (green RAM chip)  the default without an L2ARC — and, beside a setting, "works on RAM"
  //   ssd-def  (blue SSD)        the default with an L2ARC
  //   ssd-only (grey SSD)        needs an L2ARC; without one the RAM default is used instead
  function modeIs(m) { return ((ST && ST.cache) || {}).mode === m; }
  var ICONS = {
    ssd: '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v11a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17.5zM7 8v3h10V8zm0 6v2h4v-2z"/>',
    ram: '<path d="M2 7h20v9H2zm3 2v5h2V9zm4 0v5h2V9zm4 0v5h2V9zm4 0v5h2V9zM4 17h2v2H4zm5 0h2v2H9zm5 0h2v2h-2zm5 0h2v2h-2z"/>'
  };
  var MARKS = {
    "ram-def": { icon: "ram", text: N("RAM default: used when there’s no L2ARC") },
    "ssd-def": { icon: "ssd", text: N("L2ARC default: used when the media pool has an L2ARC") },
    "ssd-only": { icon: "ssd", text: N("L2ARC option: without an L2ARC, the RAM default is used instead") },
    "ram-only": { icon: "ram", text: N("RAM option: ignored when there’s an L2ARC") }
  };
  function mark(kind) {
    var m = MARKS[kind];
    if (!m) return "";
    return '<span class="mode-ic mk-' + kind + '" role="img" title="' + esc(T(m.text)) + '" aria-label="' + esc(T(m.text)) + '">' +
      '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="currentColor">' + ICONS[m.icon] + "</svg></span>";
  }
  // A setting that's wholly one kind gets its mark beside its name (in dropdowns, each choice has
  // its own). Settings that apply with or without an L2ARC get none.
  var SETTING_ICON = {
    "warmup.reserve_gb": "ssd-only", "warmup.pacing_percent": "ssd-only",
    "general.l2arc_cooldown_lead_in_minutes": "ssd-only", "general.l2arc_cooldown_whole_minutes": "ssd-only",
    "general.ram_cooldown_lead_in_minutes": "ram-only", "general.ram_cooldown_whole_minutes": "ram-only"
  };
  function iconFor(sec, key) { return mark(SETTING_ICON[sec + "." + key]); }
  function modeLegend() {
    var c = ST.cache || {}, now;
    if (c.no_l2arc) now = H("<strong>RAM only</strong> — you chose not to use an L2ARC.");
    else if (c.mode === "l2arc") now = H("<strong>L2ARC</strong> — the media pool has an SSD cache, so L2ARC defaults are used.");
    else if (c.l2arc === "absent") now = H("<strong>RAM only</strong> — no L2ARC was found, so RAM defaults are used.");
    else if (ST.pending || ST.checking) now = H("<strong>Checking</strong> whether the media pool has an L2ARC…");
    else now = H("<strong>RAM only</strong> for now — couldn’t check for an L2ARC.");
    return '<div class="legendbar"><div>' + H("Caching mode:") + " " + now + "</div>" +
      '<div class="legend-ics">' + mark("ram-def") + H("RAM default") + mark("ssd-def") + H("L2ARC default") + mark("ssd-only") + H("L2ARC option (uses RAM default without L2ARC)") +
        mark("ram-only") + H("RAM option (ignored with L2ARC)") + "</div></div>";
  }

  // ---- A dropdown whose choices can carry marks (a native <select> can only show text).
  // opts: [{value, label, marks: [...], note}]. on: what a choice does (see PICK_ACTIONS).
  var pickSeq = 0;
  // The ↺ for a choice list, beside its name: back to its "Default — …" choice (or the one marked isDefault)
  function pickRevert(on, current, opts) {
    var dflt = opts.filter(function (o) { return o.value === "default" || o.isDefault; })[0];
    if (!dflt || String(current) === String(dflt.value)) return "";
    return '<button type="button" class="revert" data-pick-revert="' + esc(on) + '" data-pick-default="' + esc(dflt.value) + '" title="' + H("Back to {value}", { value: dflt.label }) +
      '" aria-label="' + H("Reset to {value}", { value: dflt.label }) + '">↺</button>';
  }
  function pickHtml(on, current, opts, aria) {
    var id = "pk" + (++pickSeq), cur = opts.filter(function (o) { return String(o.value) === String(current); })[0] || opts[0];
    function face(o) { return '<span class="pick-txt">' + esc(o.label) + "</span>" + (o.marks || []).map(mark).join(""); }
    return '<div class="pick" data-on="' + esc(on) + '">' +
      '<button type="button" class="pick-btn" id="' + id + '" aria-haspopup="listbox" aria-expanded="false" aria-label="' + esc(aria) + ": " + esc(cur.label) + '">' +
      face(cur) + '<svg class="pick-caret" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M7 10l5 5 5-5z"/></svg></button>' +
      '<ul class="pick-list" role="listbox" aria-label="' + esc(aria) + '" hidden>' +
      opts.map(function (o) {
        var sel = o === cur;
        return '<li role="option" tabindex="-1" data-value="' + esc(o.value) + '" aria-selected="' + sel + '"' + (sel ? ' class="sel"' : "") + ">" + face(o) +
          (o.note ? '<span class="pick-note">' + esc(o.note) + "</span>" : "") + "</li>";
      }).join("") + "</ul></div>";
  }
  // A mode-dependent setting (cachemode.DYNAMIC): "Default — <what it means now>" first, then
  // every choice with its marks. names: value -> label.
  function dynOptions(sid, order, names, notes, own) {
    var meta = ((ST.cache || {}).settings || {})[sid] || { l2arc: null, ram: null, l2arc_only: [] };
    if (own) meta = Object.assign({}, meta, own);  // this kind's own Default in a mode ({ram: ...} and/or {l2arc: ...})
    var inL2 = modeIs("l2arc"), now = inL2 ? meta.l2arc : meta.ram;
    var opts = [{ value: "default", label: T("Default — {value}", { value: names[now] || now }), marks: [inL2 ? "ssd-def" : "ram-def"],
      note: inL2 ? T("Follows the caching mode: this is the L2ARC default") : T("Follows the caching mode: this is the RAM default") }];
    order.forEach(function (v) {
      var m = [];
      if (v === meta.ram) m.push("ram-def");
      if (v === meta.l2arc) m.push("ssd-def");
      if (meta.l2arc_only.indexOf(v) >= 0) m.push("ssd-only");
      opts.push({ value: v, label: names[v], marks: m, note: (notes || {})[v] });
    });
    return opts;
  }
  // The note under a mode-dependent field when its choice can't be used right now.
  function fallbackNote(sid, value, names, own) {
    var meta = ((ST.cache || {}).settings || {})[sid];
    if (!meta || modeIs("l2arc") || meta.l2arc_only.indexOf(value) < 0) return "";
    return '<div class="fallback">' + mark("ssd-only") + H("Needs an L2ARC. Using “{value}” (the RAM default) until one is added.", { value: names[(own && own.ram) || meta.ram] }) + "</div>";
  }
  function dynSelect(sec, key, label, order, names, help) {
    var sid = sec + "." + key, h = splitHelp(help), cur = String(S[sec][key]), opts = dynOptions(sid, order, names);
    return '<div class="fld"><span class="lbl">' + esc(label) + info(h.more, label) + pickRevert("set:" + sid, cur, opts) + "</span>" +
      pickHtml("set:" + sid, cur, opts, label) + fallbackNote(sid, cur, names) + helpDiv(h) + "</div>";
  }

  // ------------------------------------------------------------------ utils
  // ---- Info buttons: longer explanations sit behind a small "i" next to what they explain.
  // Hover or focus shows them; a click (or tap) pins them open until you click elsewhere.
  var INFO = {}, INLINE_MAX = 70;
  window.PCINFO = INFO;  // (what's behind every ⓘ drawn: read by the tests, tests/render.js)
  function info(html, about) {
    if (!html) return "";
    var h = 0;
    for (var i = 0; i < html.length; i++) h = (h * 31 + html.charCodeAt(i)) | 0;
    var id = "i" + (h >>> 0).toString(36);
    INFO[id] = html;
    return '<button type="button" class="info" data-info="' + id + '" aria-label="' + esc(about ? T("About {what}", { what: about }) : T("About this")) +
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

  // Server text arrives either as a string or as a message to translate ({_t, _p}).
  function esc(v) {
    return String(v == null ? "" : (typeof v === "object" ? msg(v) : v)).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // A number with exactly `d` decimals, written the reader's way (1.5 / 1,5).
  function dec(x, d) { return Number(x).toLocaleString(LOCALE, { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function fmtBytes(n) {
    if (n == null) return T("unknown");
    var u = [[1e12, N("{n} TB")], [1e9, N("{n} GB")], [1e6, N("{n} MB")], [1e3, N("{n} KB")]];
    for (var i = 0; i < u.length; i++) if (n >= u[i][0]) return T(u[i][1], { n: dec(n / u[i][0], n < 10 * u[i][0] ? 1 : 0) });
    return T("{n} B", { n: Math.round(n) });
  }
  function fmtNum(n) { return (n || 0).toLocaleString(LOCALE); }
  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000), now = new Date();
    var t = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return T("Today {time}", { time: t });
    var y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return T("Yesterday {time}", { time: t });
    return T("{date}, {time}", { date: d.toLocaleDateString(LOCALE, { month: "short", day: "numeric" }), time: t });
  }
  function fmtIso(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return fmtTime(d.getTime() / 1000);
  }
  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? T("{h} h {m} min", { h: h, m: m }) : (m ? T("{m} min {s} s", { m: m, s: s }) : T("{s} s", { s: s }));
  }
  var RTF = window.Intl && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat(LOCALE, { numeric: "auto" }) : null;
  function ago(ts) {
    if (!ts) return T("never");
    var s = Date.now() / 1000 - ts;
    if (s < 90) return T("just now");
    var v = s < 3600 ? [Math.round(s / 60), "minute"] : s < 86400 ? [Math.round(s / 3600), "hour"] : [Math.round(s / 86400), "day"];
    if (RTF) return RTF.format(-v[0], v[1]);
    return v[1] === "minute" ? P(v[0], "{n} minute ago", "{n} minutes ago") : v[1] === "hour" ? P(v[0], "{n} hour ago", "{n} hours ago") : P(v[0], "{n} day ago", "{n} days ago");
  }
  // `long`: an error — shown long enough to read, and kept while the pointer is over it
  function toast(msg, long) {
    var t = document.getElementById("toast");
    t.textContent = msg; t.classList.add("show"); t.classList.toggle("err", !!long);
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.classList.remove("show"); }, long ? 12000 : 2600);
  }
  function api(path, body) {
    var opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) { location.reload(); return new Promise(function () {}); }  // signed out: show the sign-in page
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error ? msg(j.error) : "HTTP " + r.status);
        return j;
      });
    });
  }
  function light(status, title, detail, testing) {
    var st = testing ? "testing" : status;
    return '<div class="lightrow"><span class="light l-' + esc(st) + '" aria-hidden="true"></span><div class="grow"><div class="what">' +
      esc(testing ? T("Checking…") : title) + '</div>' + (detail && !testing ? '<div class="det">' + links(detail) + "</div>" : "") + "</div></div>";
  }
  function banner(sum, testing) {
    if (testing) return '<div class="banner bn-testing" role="status"><span class="light l-testing" aria-hidden="true"></span>' + H("Checking your settings…") + "</div>";
    if (!sum) return "";
    return '<div class="banner bn-' + esc(sum.status) + '" role="status"><span class="light l-' + esc(sum.status) + '" aria-hidden="true"></span>' + esc(sum.text) + "</div>";
  }
  // openByDefault: shown open until you close it (e.g. the API-key steps while there's no working key).
  function details(key, summary, body, openByDefault) {
    var open = key in openDetails ? openDetails[key] : !!openByDefault;
    return '<details data-key="' + esc(key) + '" data-def="' + (openByDefault ? 1 : 0) + '"' + (open ? " open" : "") + "><summary>" + summary + "</summary>" + body + "</details>";
  }
  function badge(status, text) { return '<span class="badge b-' + esc(status) + '">' + esc(text) + "</span>"; }
  // Event log categories (the server sends the English key).
  var EVENT_KINDS = { settings: N("settings"), "warm-up": N("warm-up"), "record size": N("record size"), benchmark: N("benchmark"), mqtt: N("mqtt"),
    playback: N("playback"), "now-playing": N("now-playing"), "next-episode": N("prefetch"), nowplaying: N("now-playing"), nextepisode: N("prefetch"), update: N("update"),
    optimizer: N("optimizer"), "library health": N("library health") };
  var STATUS_TEXT = { pass: T("Pass"), warn: T("Warning"), fail: T("Fail"), info: T("Info") };
  var VERDICT_TEXT = { use: T("Use"), maybe: T("Maybe"), avoid: T("Unavailable") };

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
      toast(T("Saved"));
      return load();
    }).catch(function (e) { toast(T("Not saved: {why}", { why: e.message })); return load(); });
  }

  // ------------------------------------------------------------- planning
  // The L2ARC plan (plexcache/warmup.py plan()) comes from the server with every state; a change
  // saves and reloads straight away, so it's always current.
  var DEFAULT_INDEX = { movie: 512 * 1024, tv: 256 * 1024, audio: 0 };  // per file, until a run measures yours

  // ---------------------------------------------------------------- pages
  function pageHealth() {
    var h = ST.health;
    if (!h) return top(T("Health"), H("Running the first checks…"), "") + '<p class="muted">' + H("Checking Plex, your media mounts and ZFS. This takes a few seconds.") + "</p>";
    var c = h.counts;
    var out = top(T("Health"),
      H("Checks run on startup, every 6 hours, and whenever settings change. Last run {when}.", { when: ago(h.at) }),
      '<button class="btn pri" data-act="recheck"' + (ST.checking ? " disabled" : "") + ">" + (ST.checking ? H("Checking…") : H("Re-run All Checks")) + "</button>");
    out += banner(h.summary, false);
    var hj = ST.job;
    if (hj && hj.running) {
      out += '<div class="note"><strong>' + esc(jobTitle(hj)) + "</strong>" + jobProgress(hj) +
        '<div><a href="/optimizer" data-nav>' + H("Open the Optimizer page") + '</a> · <button class="link" type="button" data-act="job" data-job="cancel">' + H("Cancel") + "</button></div></div>";
    }
    out += '<div class="tiles">' +
      tile(c.pass, P(c.pass, "check passed", "checks passed"), "var(--ok)") +
      tile(c.warn, P(c.warn, "warning — features still run", "warnings — features still run"), "var(--warn)") +
      tile(c.fail, P(c.fail, "failure", "failures"), c.fail ? "var(--bad)" : "var(--muted)") + "</div>";
    out += '<div class="stack">';
    h.groups.forEach(function (g) {
      out += '<section class="card" id="' + esc(g.id) + '">' + heading(esc(g.title), esc(g.hint));
      if (g.table && g.table.length) {
        out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">' + H("Library") + '</th><th scope="col">' + H("Folders (Plex → here)") +
          '</th><th scope="col">' + H("Items") + '</th><th scope="col">' + H("Readable here") + '</th><th scope="col">' + H("Record size") + info(RS_HELP, T("Record size")) + "</th></tr></thead><tbody>";
        g.table.forEach(function (r) {
          var folders = r.folders.length ? r.folders.map(function (f) {
            return '<div class="mono">' + esc(f.plex_path) + (f.local_path !== f.plex_path ? ' <span class="muted">→ ' + esc(f.local_path) + "</span>" : "") + "</div>";
          }).join("") : '<span class="muted">' + esc(r.detail || "") + "</span>";
          out += "<tr><td>" + libIcon(r.library) + esc(r.library) + "</td><td>" + folders + '</td><td class="nw">' + (r.status === "info" ? "—" : fmtNum(r.count)) +
            "</td><td>" + badge(r.status, r.badge || STATUS_TEXT[r.status] || r.status) +
            (r.status !== "pass" && r.status !== "info" ? '<div class="det">' + esc(r.detail) + "</div>" : "") + "</td><td>" + recordCell(r.record) + "</td></tr>";
        });
        out += "</tbody></table></div>";
        g.table.forEach(function (r) {
          if (r.fix) out += '<div class="fix">' + H("<strong>{library}:</strong> {fix}", { library: r.library, fix: r.fix }) + ' <a href="/system#mappings" data-nav>' + H("Open Media Mappings") + "</a></div>";
        });
      }
      if (g.datasets && g.datasets.length) {
        out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">' + H("Dataset") + "</th>" +
          DS_COLUMNS.map(function (col) { return '<th scope="col">' + col[0] + info(col[1], col[0]) + "</th>"; }).join("") + "</tr></thead><tbody>";
        g.datasets.forEach(function (r) {
          var c = function (x) { return x ? badge(x.status, x.badge || STATUS_TEXT[x.status] || x.status) + (x.text ? '<div class="det">' + esc(x.text) + "</div>" : "") : '<span class="muted">—</span>'; };
          out += '<tr><td class="mono nw">' + esc(r.dataset) + (r.mountpoint !== r.dataset ? '<div class="det">' + H("at {path}", { path: r.mountpoint }) + "</div>" : "") + "</td><td>" + c(r.in_use) + "</td><td>" + c(r.cache) + "</td><td>" + dsRecordCell(r) + "</td><td>" + c(r.file_rs) + "</td><td>" + c(r.compression) + "</td><td>" + c(r.l2arc) + "</td></tr>";
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

  // Health → Libraries: each library's record size, with the one recommended for its media.
  var RS_HELP = H("The block size ZFS uses for new files, read from the library’s dataset, and the one recommended for the library from its average bitrate: " +
    "<strong>512K</strong> under 1 Mbps (music, audiobooks), <strong>1M</strong> up to 15 Mbps (FLAC, SD to 1080p encodes), <strong>2M</strong> up to 40 Mbps (4K encodes, 1080p remuxes) " +
    "and <strong>4M</strong> above (4K remuxes); <strong>128K</strong> for files under 1 MB, which fit in a block or two anyway. The second best is " +
    "the neighbour on the side the bitrate leans to. <strong>Ideal</strong>: the dataset uses the recommended size. <strong>Pass</strong>: it uses the second best — " +
    "fine too, so a library near the line between two doesn’t flip back and forth. <strong>Warning</strong>: neither.<br><br>" +
    "<strong>To fix a warning</strong>: in TrueNAS, Datasets → the library’s dataset → Edit → Advanced Options → <strong>Record Size</strong>: the recommended size. " +
    "It only applies to files written afterwards; on the Optimizer page, <strong>TrueNAS compression &amp; record size</strong> writes the existing ones again. " +
    "If other libraries on the same dataset need a different size, see Media Datasets below.");
  var DS_RS_HELP = H("<strong>What it checks</strong>: that the dataset’s record size — the largest block ZFS writes a file in — suits every library on it. " +
    "Each library’s recommended size and second best come from its average bitrate (see Libraries above); one of them is <strong>Pass</strong>.<br><br>" +
    "<strong>Why it matters</strong>: ZFS reads, caches and compresses whole records. Too small, and big video files take many more reads and use more RAM to track in the cache; " +
    "too large, and small files (music, audiobooks) waste cache space on data nobody asked for.<br><br>" +
    "<strong>To fix a warning</strong>: in TrueNAS, Datasets → the dataset → Edit → Advanced Options → <strong>Record Size</strong>. " +
    "If no one size suits every library, give the library named a dataset of its own (TrueNAS → Datasets → Add Dataset), move its files there, " +
    "then point its folder in Plex and in <a href=\"/system#mappings\" data-nav>System → Media Mappings</a> at the new place.");
  var FILE_RS_HELP = H("<strong>What it checks</strong>: that every file on the dataset is written in blocks of the dataset’s record size. Each health check looks at every file " +
    "(their details, not their contents — a fraction of a second). Files smaller than one record don’t count: they’re a single block of their own size.<br><br>" +
    "<strong>Why it matters</strong>: ZFS uses a record size only for files written after it’s set. Files written before keep their old block size — and with it the slower reads " +
    "and cache use the dataset’s setting was chosen to avoid — until they’re written again.<br><br>" +
    "<strong>To fix a warning</strong>: on the Optimizer page, <strong>TrueNAS compression &amp; record size</strong> writes those files again, one at a time.");
  var COMP_HELP = H("<strong>What it checks</strong>: the dataset’s compression. Video barely compresses, but fonts, the index, tags and the padding at the end of each file do — and the index is cached with the start of every file, so the smaller it is, the more fits in the cache.<br><br>" +
    "<strong>Pass</strong>: ZSTD at levels 3 to 19. <strong>ZSTD-3</strong> (plain “ZSTD” in TrueNAS) is recommended. From level 3, ZFS gives up early on what won’t compress, so video costs almost nothing to try. " +
    "Higher levels make marginally smaller files but use more CPU when writing (imports, the optimizer); reading is no slower at any level. On a mostly idle NAS, a higher level is fine — your choice.<br><br>" +
    "<strong>Warning</strong>: ZSTD-1 and 2 (slightly less compression, and no faster in practice), LZ4 and ZSTD-FAST (less compression), and anything else (off, GZIP — also slower to read —, ZLE, LZJB).<br><br>" +
    "<strong>To fix a warning</strong>: in TrueNAS, Datasets → the dataset → Edit → Advanced Options → <strong>Compression Level</strong>: ZSTD. " +
    "Files keep the compression they were written with; on the Optimizer page, <strong>TrueNAS compression &amp; record size</strong> writes them again. Files already written with ZSTD 3 to 19 are never written again just because the level changed.");
  function recordCell(rec) {
    if (!rec || !rec.best) return '<span class="muted">—</span>';
    var badges = { ideal: badge("pass", T("Ideal")), pass: badge("pass", T("Pass")), warn: badge("warn", T("Warning")), info: badge("info", T("Not checked")) };
    var line = function (label, v) { return '<div class="det">' + esc(label) + ' <span class="mono">' + (v ? esc(fmtRs(v)) : "—") + "</span></div>"; };
    return badges[rec.status] + line(T("Dataset"), rec.rs) + line(T("Recommended"), rec.best) + line(T("Second best"), rec.alt);
  }
  // Media Datasets → Record size: the size, how the files compare, and whether it suits every library on it
  function dsRecordCell(r) {
    var x = r.recordsize;
    if (!x) return '<span class="muted">—</span>';
    return badge(x.status, x.badge || STATUS_TEXT[x.status] || x.status) + (x.text ? '<div class="det mono">' + esc(x.text) + "</div>" : "") +
      (x.lines || []).map(function (l) { return '<div class="det">' + links(msg(l)) + "</div>"; }).join("");
  }


  // Media Datasets table: each column's explanation sits behind an ⓘ in its header.
  var DS_COLUMNS = [
    [H("In use"), H("Which libraries are on the dataset. A dataset mounted in this app with no Plex library on it is flagged, " +
      "in case a mount is pointing at the wrong place. Libraries unticked in <a href=\"/system#mappings\" data-nav>System → Media Mappings</a> show as “caching off”: " +
      "only their record size is checked.")],
    [H("Cache settings"), H("<strong>primarycache=all</strong> is needed for any caching: ZFS only keeps in RAM, and only copies to the SSD, " +
      "data that passed through RAM. With <strong>metadata</strong>, nothing is cached and no feature can help.<br><br>" +
      "<strong>secondarycache=all</strong> lets that data go on to the SSD cache (L2ARC). Only caching that needs an L2ARC (warm-up, and Prefetch’s L2ARC-only choices) needs it.")],
    [H("Dataset record size"), DS_RS_HELP],
    [H("File record size"), FILE_RS_HELP],
    [H("Compression"), COMP_HELP],
    ["L2ARC", H("The SSD cache devices on the dataset’s pool, and how much they hold. Only warm-up and Prefetch’s L2ARC-only choices need an L2ARC; " +
      "Now Playing Cache and Prefetch work from RAM.")]
  ];

  // A place in the app named in server text is a link to it: "[System → Plex](/system#plex)" (the text escaped first)
  function links(text) {
    return esc(text).replace(/\[([^\]]+)\]\((\/[a-z0-9-]*(?:#[a-z0-9-]+)?)\)/g, '<a href="$2" data-nav>$1</a>');
  }
  // Text from the server for an ⓘ (a check's help, an option's): plain text with its [links](/page), and the few marks
  // help is written with kept — <strong>, <em>, <code>, <br>, bare — so they show as marks, not as code. Anything else
  // shows as written (escaped). Every ⓘ is tested for markup shown as text (test_info_boxes_show_no_code).
  function richText(text) {
    return links(text).replace(/&lt;(\/?)(strong|em|code)&gt;|&lt;br ?\/?&gt;/g, function (m, close, tag) { return tag ? "<" + close + tag + ">" : "<br>"; });
  }
  function itemRow(it, extra) {
    var out = '<div class="row">' + badge(it.status, STATUS_TEXT[it.status] || it.status) + '<div class="grow"><div class="what">' + esc(it.title) +
      (it.help ? info(it.help, it.title) : "") + '</div><div class="det">' + links(it.detail) + "</div>";
    if (it.fix || it.fix_cmd) {
      // {APP} in a command is this app's own address (e.g. to download a script it serves).
      var cmd = it.fix_cmd ? it.fix_cmd.split("{APP}").join(location.origin) : "";
      out += '<div class="fix">' + links(it.fix);
      if (cmd) out += '<div class="cmd"><code style="white-space:pre-wrap">' + esc(cmd) + '</code><button class="btn" data-copy="' + esc(cmd) + '">' + H("Copy") + "</button>" +
        '<button class="btn" type="button" data-act="recheck"' + (ST.checking ? " disabled" : "") + ">" + (ST.checking ? H("Checking…") : H("Re-test")) + "</button></div>";
      out += "</div>";
    }
    if (it.link && it.link.job) out += '<div class="det"><button class="link" type="button" data-act="proc-failed" data-job="' + esc(it.link.job) + '">' + esc(it.link.text) + "</button></div>";
    else if (it.link) out += '<div class="det"><a href="' + esc(it.link.href) + '" data-nav>' + esc(it.link.text) + "</a></div>";
    return out + (extra || "") + "</div></div>";
  }

  function tile(n, label, color) {
    return '<div class="card tile"><div class="big" style="color:' + color + '">' + esc(n || 0) + '</div><div class="det" style="margin:0">' + esc(label) + "</div></div>";
  }
  function top(title, sub, actions) {
    return '<div class="top"><div><h1>' + esc(title) + '</h1><div class="sub">' + sub + "</div></div>" + (actions || "") + "</div>" + pairAsks();
  }
  // Pair Now pressed on the Plex Cache at a mirror's site (peerlink.py asks): said at the top of every page here, with where
  // to press it — or that no library here is mirrored by that server yet, so the Mirror page comes first
  function pairAsks() {
    var asked = (ST && ST.peer && ST.peer.asked) || {};
    return Object.keys(asked).map(function (sid) {
      var a = asked[sid], n = Math.max(1, Math.ceil((a.left || 0) / 60));
      return '<div class="note">' + links(a.wanted
        ? T("The Plex Cache on {name} asks to pair: press Pair Now on [System → Mirror Plex Cache](/system#peer) within {n} minutes.", { name: a.name, n: n })
        : T("The Plex Cache on {name} asks to pair, but no library here is mirrored by {server}: choose one on the [Mirror page](/mirrors), then press Pair Now on [System → Mirror Plex Cache](/system#peer).", { name: a.name, server: a.server })) + "</div>";
    }).join("");
  }
  function rec(feature) {
    var r = ST.health && ST.health.recommendations.filter(function (x) { return x.feature === feature; })[0];
    if (!r) return "";
    var text = { use: T("Recommended"), maybe: T("Check first"), avoid: T("Unavailable") }[r.verdict];
    // Warm-up blocked by a missing SSD cache says so; anything else needs a setting changed.
    if (r.verdict === "avoid") text = feature === "warmup" && ST.l2arc === "absent" ? T("L2ARC required") : T("Configuration required");
    return badge(r.verdict, text);
  }
  function unavailable(feature) {
    var r = ST.health && ST.health.recommendations.filter(function (x) { return x.feature === feature; })[0];
    return r && r.verdict === "avoid" ? r.reason : "";
  }
  // Why a feature can't run on this NAS right now ("" if it can).
  function blocked(sec) {
    if (sec === "warmup" && ST.l2arc === "absent") return T("Needs an L2ARC (SSD cache) on the media pool");
    return unavailable(sec);
  }
  // On means switched on AND able to run. An unavailable feature shows as off and can't be
  // switched on; its saved choice is kept, so it comes back by itself once the problem is fixed.
  function isOn(sec) {
    if (sec === "warmup") return warmActive();
    return !!S[sec].enabled && !blocked(sec);
  }
  // Warm-up has no switch: it's on when a library has a schedule (and it can run here).
  function warmActive() {
    var sc = (ST.warmup && ST.warmup.schedules) || {};
    return !blocked("warmup") && Object.keys(sc).some(function (k) { return sc[k] !== "none"; });
  }
  function toggle(sec, label) {
    var why = blocked(sec);
    return '<button class="sw" type="button" aria-pressed="' + (isOn(sec) ? "true" : "false") + '" aria-label="' + esc(label) + '" data-toggle="' + sec + '"' +
      (why ? ' disabled title="' + esc(why) + '"' : "") + "><span></span></button>";
  }
  function unavailableNote(sec, html) {
    return '<div class="note">' + H("<strong>Not available:</strong>") + " " + html +
      (S[sec].enabled ? " " + H("It switches back on by itself once that’s fixed.") : "") + "</div>";
  }
  function guideBtn(sec, show, hide) {
    return '<button class="link" type="button" data-guide="' + sec + '" aria-expanded="' + !!guides[sec] + '">' + (guides[sec] ? hide || H("Hide how it works") : show || H("How it works")) + "</button>";
  }
  function guide(sec, cols) {
    if (!guides[sec]) return "";
    return '<div class="guide">' + cols.map(function (c) { return "<div><h3>" + c[0] + "</h3><p>" + c[1] + "</p></div>"; }).join("") + "</div>";
  }
  // A small "↺" that puts a setting back to its default; only shown once it's been changed.
  // Every setting with a default has one (tests/test_plexcache.py checks): the field builders
  // below add it, and hand-built fields use revertBtn too. Dropdowns note their choices' names
  // here, so the button can say "Back to the default (Automatic)".
  var REVERT_LABELS = {};
  function revertInner(sec, key, unit) {
    var d = ST.defaults && ST.defaults[sec] ? ST.defaults[sec][key] : undefined;
    if (d === undefined || String(S[sec][key]) === String(d)) return "";
    var labels = REVERT_LABELS[sec + "." + key];
    var shown = labels && labels[String(d)] != null ? labels[String(d)] : typeof d === "boolean" ? (d ? T("on") : T("off")) : d === "" ? T("empty") :
      d === 0 && sec + "." + key === "warmup.pause_disk_busy" ? T("off") : /_minutes$/.test(key) ? minutesText(d) : typeof d === "number" ? fmtNum(d) + (unit ? " " + unit : "") : String(d);
    return '<button type="button" class="revert" data-revert="' + sec + "." + key + '" title="' + H("Back to the default ({value})", { value: shown }) +
      '" aria-label="' + H("Reset to the default, {value}", { value: shown }) + '">↺</button>';
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
    return '<div class="fld"><label for="' + id + '">' + esc(label) + iconFor(sec, key) + info(h.more, label) + '<span class="val" data-val="' + id + '">' + esc(S[sec][key]) + " " + unit + "</span>" + revertBtn(sec, key, unit) + "</label>" +
      '<input id="' + id + '" type="range" min="' + min + '" max="' + max + '" step="' + step + '" value="' + esc(S[sec][key]) + '" data-set="' + sec + "." + key + '" data-unit="' + unit + '">' +
      helpDiv(h) + "</div>";
  }
  function select(sec, key, label, options, help) {
    var id = "f-" + sec + "-" + key;
    var cur = String(S[sec][key]), h = splitHelp(help);
    var unit = key === "poll_seconds" ? T("s") : "";  // for the ↺ button's "Back to the default (5 s)"
    var names = REVERT_LABELS[sec + "." + key] = {};
    options.forEach(function (o) { names[String(o[0])] = o[1]; });
    return '<div class="fld"><label for="' + id + '">' + esc(label) + iconFor(sec, key) + info(h.more, label) + revertBtn(sec, key, unit) + '</label><select id="' + id + '" data-set="' + sec + "." + key + '">' +
      options.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (String(o[0]) === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join("") +
      "</select>" + helpDiv(h) + "</div>";
  }
  function number(sec, key, label, min, max, help) {
    var id = "f-" + sec + "-" + key, h = splitHelp(help);
    return '<div class="fld"><label for="' + id + '">' + esc(label) + iconFor(sec, key) + info(h.more, label) + revertBtn(sec, key, "") + '</label><input id="' + id + '" type="number" min="' + min + '" max="' + max + '" value="' + esc(S[sec][key]) + '" data-set="' + sec + "." + key + '">' +
      helpDiv(h) + "</div>";
  }

  function estimateHtml() {
    var st = ST.library.stats, p = ST.plan;
    if (!st.scanned_at || !p) return '<div class="est"><div class="muted">' + (ST.library.scanning ? H("Reading your libraries to estimate space…") : H("Connect Plex to see a space estimate.")) + "</div></div>";
    var need = p.need_max + p.reserve, l2 = p.l2_total, used = p.planned_by_kind;
    var total = Math.max(l2 || 0, need, 1);
    function pct(x) { return Math.max(0, Math.min(100, x / total * 100)).toFixed(2); }
    var head, cls;
    if (l2 == null) { head = T("Needs about {size} of L2ARC (incl. a {free} GB system buffer)", { size: fmtBytes(need), free: S.warmup.reserve_gb }); cls = ["info", T("L2ARC size unknown")]; }
    else {
      head = T("Needs {size} of your {l2} L2ARC", { size: fmtBytes(need), l2: fmtBytes(l2) });
      cls = { fits: ["pass", T("Fits")], tight: ["warn", T("Tight fit")], reduced: ["warn", T("Too big — seconds reduced automatically")],
        insufficient: ["fail", T("Doesn’t fit even at minimum seconds")], none: ["fail", T("No L2ARC found")] }[p.status];
    }
    var out = '<div class="est"><div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;align-items:baseline"><div style="font-weight:600">' + esc(head) + "</div>" + badge(cls[0], cls[1]) + "</div>";
    out += '<div class="bar" role="img" aria-label="' + H("L2ARC space estimate") + '">' +
      '<div style="width:' + pct(used.movie) + '%;background:var(--c-movie)"></div><div style="width:' + pct(used.tv) + '%;background:var(--c-tv)"></div>' +
      '<div style="width:' + pct(used.audio) + '%;background:var(--c-audio)"></div>' +
      '<div style="width:' + pct(p.reserve) + '%;background:var(--c-res)"></div></div>';
    out += '<div class="legend"><span><i style="background:var(--c-movie)"></i>' + H("Movies {size}", { size: fmtBytes(used.movie) }) + "</span><span><i style=\"background:var(--c-tv)\"></i>" + H("TV {size}", { size: fmtBytes(used.tv) }) +
      '</span><span><i style="background:var(--c-audio)"></i>' + H("Audio {size}", { size: fmtBytes(used.audio) }) + "</span>" +
      '<span><i style="background:var(--c-res)"></i>' + H("System buffer {n} GB", { n: S.warmup.reserve_gb }) + "</span>" +
      (l2 != null ? "<span>" + H("Unused {size}", { size: fmtBytes(Math.max(0, l2 - need)) }) + "</span>" : "") + "</div>";
    if (p.status === "reduced" || p.status === "insufficient") {
      out += '<div class="help muted" style="font-size:13px;margin-top:6px">' + H("Will use: {list}", { list: p.libraries.map(function (r) { return T("{kind} {n} s", { kind: r.name, n: r.sec }); }) }) + "</div>";
    }
    out += "</div>";
    return out;
  }

  // How much cache the Lead-in Cache takes, worked out from your library: titles × average bitrate
  // × seconds of the start, plus a safety margin, counted in whole records as ZFS caches them.
  // Shown even without an L2ARC: the same lead-ins then take RAM instead.
  var SSD_SIZES = [128, 256, 500, 1000, 2000, 4000, 8000];  // GB, as drives are sold
  var L2_HEADER = 96;  // RAM the L2ARC uses per record it holds (plexcache/warmup.py L2ARC_HEADER_BYTES)

  // What one file takes in the cache at a record size (plexcache/warmup.py cached_bytes()): its start —
  // the lead-in, never less than the metadata in front of the video and 60% of a record of video — in
  // whole records, and the whole records its metadata at the end takes.
  function wholeRecords(n, record) { return n > 0 ? Math.ceil(n / record) * record : 0; }
  function cachedBytes(lead, index, share, record, size, first, front) {
    var head = Math.max(lead, (front || 0) + 0.6 * record) + (first ? index * share : 0);
    if (size && (size <= record || head >= size)) return size;
    var total = (head <= record ? record : head + record / 2) + (first || !index ? 0 : share * wholeRecords(index, record));
    return size ? Math.min(total, size) : total;
  }
  function fmtRs(n) { return n >= 1048576 ? T("{n}M", { n: n / 1048576 }) : T("{n}K", { n: n / 1024 }); }
  // The four sizes of one library, in bytes and in L2ARC records (each record costs RAM to track).
  // Target follows the sliders; the others use the library's own seconds.
  function libSizes(r) {
    var out = {
      // Target: the files as the optimizer lays them out — next to nothing in front of the video, the metadata at the end
      tmin: { bytes: r.count * cachedBytes(0, r.index_avg, 1, r.rs_best, r.avg_size, false) },
      target: { bytes: r.count * cachedBytes(r.bps * estSec(r), r.index_avg, 1, r.rs_best, r.avg_size, false) }
    };
    out.tmin.recs = out.tmin.bytes / r.rs_best;
    out.target.recs = out.target.bytes / r.rs_best;
    if (r.rs) {
      var ymin = cachedBytes(0, r.index_avg, r.index_share, r.rs, r.avg_size, false, r.front);
      out.ymin = { bytes: r.count * ymin, recs: r.count * ymin / r.rs };
      var lead = r.bps * r.set_sec, b = 0, n = 0;
      (r.blocks || []).forEach(function (m) {
        var c = cachedBytes(lead, r.index_avg, r.index_share, m[0], r.avg_size, false, r.front);
        b += m[1] * c; n += m[1] * c / m[0];
      });
      out.your = { bytes: r.count * b, recs: r.count * n };
    }
    return out;
  }
  var SIZE_COLS = ["tmin", "ymin", "target", "your"];
  function sizeTotals() {
    var t = {}, missing = false;
    SIZE_COLS.forEach(function (c) { t[c] = { bytes: 0, recs: 0 }; });
    (ST.plan ? ST.plan.libraries : []).forEach(function (r) {
      var z = libSizes(r);
      SIZE_COLS.forEach(function (c) { if (z[c]) { t[c].bytes += z[c].bytes; t[c].recs += z[c].recs; } else missing = true; });
    });
    t.missing = missing;
    return t;
  }

  function sizingHtml(standalone) {
    var st = ST.library.stats, p = ST.plan, w = S.warmup;
    if (!st || !st.scanned_at || !p) return standalone ? '<div class="sizing"><p class="muted" style="margin:14px 0 0">' +
      (ST.library.scanning ? H("Reading your libraries to work out how big an L2ARC you’d need…") : H("Connect Plex to see how big an L2ARC you’d need.")) + "</p></div>" : "";
    var rows = p.libraries, buffer = p.reserve, tot = sizeTotals();
    var kbps = function (v) { return v >= 1000 ? T("{n} Mbps", { n: dec(v / 1000, 1) }) : T("{n} kbps", { n: Math.round(v) }); };
    var notChecked = '<span class="muted">' + H("Not checked") + "</span>";
    var cell = function (z, sub, est) { return '<td class="nw' + (est ? " estc" : "") + '">' + (z ? esc(fmtBytes(z.bytes)) + (sub ? ' <span class="muted">· ' + esc(sub) + "</span>" : "") : notChecked) + "</td>"; };
    var head4 = [[H("Target minimum"), H("Each file optimized, at the record size recommended for the library: its first record (enough for 60% of a record of video), and the whole records its metadata at the end takes — the index, tags, fonts and cover art. The least warm-up could cache, if the files were laid out that way.")],
      [H("Your minimum"), H("Each file as it is, at your dataset’s record size: whole records through the metadata in front of the video and 60% of a record of video, plus the whole records its metadata at the end takes. The least warm-up caches: when the L2ARC is too small, it shortens lead-ins toward this.")],
      [H("Target"), H("Each file optimized, at the recommended record size, at the lead-in lengths on the sliders above. All the way down is the Target minimum.")],
      [H("Your library"), H("As your files are written now — the block sizes found on the dataset, their metadata where it is — at your lead-in lengths.")]];
    var why = H("Each title’s lead-in (its length is set per library on the Caching page) × its library’s average bitrate ({video}% extra for video, {audio}% for audio, for bitrate spikes), " +
      "and its metadata ({how}): what’s in front of the video, and at the end the index, tags, fonts and cover art. ZFS caches whole records, so a lead-in rounds up to the record size — never less than 60% of a record of video — and metadata at the end of a file costs the records it touches there as well. " +
      "Optimizing the files (<a href=\"/optimizer\" data-nav>the Optimizer page</a>) and using the recommended record size give the Target columns; <a href=\"/health#media\" data-nav>Health → Libraries</a> shows each library’s record size and the one recommended for it. " +
      "Sizes are for your libraries as they are today: leave room on top for how much they’ll grow.",
      { video: w.padding_video, audio: w.padding_audio, how: ST.index_avg ? T("measured by warm-up") : T("estimated until warm-up has measured yours") });
    // each heading a word to a line, so the columns stay narrow
    var wordy = function (t) { return String(t).split(/\s+/).join("<br>"); };
    var table = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col"></th><th scope="col">' + H("Titles") + '</th><th scope="col">' + wordy(H("Avg bitrate")) + "</th>" +
      '<th scope="col">' + wordy(H("Avg file")) + "</th>" + head4.map(function (h, i) { return '<th scope="col"' + (i === 2 ? ' class="estc"' : "") + ">" + wordy(h[0]) + info(h[1], h[0]) + "</th>"; }).join("") + "</tr></thead><tbody>";
    rows.forEach(function (r) {
      var kind = KIND_NAME[r.kind], z = libSizes(r);
      var mine = r.rs ? fmtRs(r.rs) : "";
      table += "<tr><td>" + libIcon(r.key) + '<a href="/library" data-nav data-libkey="' + esc(r.key) + '" data-libview="caching">' + esc(r.name) + "</a>" + (r.name.toLowerCase().indexOf(kind.toLowerCase()) < 0 ? ' <span class="muted">· ' + esc(kind) + "</span>" : "") + '</td><td class="nw">' + fmtNum(r.count) + '</td><td class="nw">' + (r.count ? kbps(r.kbps) : "—") +
        '</td><td class="nw">' + (r.avg_size ? esc(fmtBytes(r.avg_size)) : "—") + "</td>" +
        cell(z.tmin, fmtRs(r.rs_best)) + cell(z.ymin, mine) +
        '<td class="nw estc" data-estcell="' + esc(r.key) + '">' + estCell(r) + "</td>" +
        cell(z.your, secText(r.set_sec)) + "</tr>";
    });
    var four = function (f, cls) {
      return SIZE_COLS.map(function (c) { return '<td class="nw' + (c === "target" ? " estc" : "") + '"' + (cls ? " data-est" + cls + '="' + c + '"' : "") + ">" + esc(fmtBytes(f(tot[c]))) + "</td>"; }).join("");
    };
    var blank = "<td></td><td></td>";  // (the summary rows' heading spans the Titles column too: room for its name)
    table += '<tr class="sum"><th scope="row" colspan="2">' + H("Lead-ins") + info(H("What the lead-ins take: in RAM without an L2ARC, or on the SSD with one."), T("Lead-ins")) + "</th>" + blank + four(function (x) { return x.bytes; }, "lead") + "</tr>" +
      '<tr><td colspan="2">' + H("System buffer") + info(H("Room on the SSD for everything else ZFS caches. This size decides how quickly the L2ARC is emptied: it reuses its space oldest first, so the larger the buffer, the longer cached files stay cached. Set it with the slider above."), T("System buffer")) + "</td>" + blank +
      four(function () { return buffer; }) + "</tr>" +
      '<tr class="sum"><th scope="row" colspan="2">' + H("L2ARC size") + info(why, T("L2ARC sizing")) + "</th>" + blank + four(function (x) { return x.bytes + buffer; }, "l2") + "</tr></tbody></table></div>";
    // With an L2ARC, the bar above says whether it fits; without one, this says what to add
    var l2 = ST.zfs.l2_total, need = tot.your.bytes + buffer, head = "";
    if (ST.l2arc === "absent" || !l2) {
      var ssd = SSD_SIZES.filter(function (g) { return g * GB >= need; })[0];
      var drive = ssd >= 1000 ? T("{n} TB", { n: ssd / 1000 }) : T("{n} GB", { n: ssd });
      head = ssd ? T("For the Lead-in Cache as it is, add an L2ARC of at least {size}: a {drive} SSD.", { size: fmtBytes(need), drive: drive })
        : T("For the Lead-in Cache, add an L2ARC of at least {size}.", { size: fmtBytes(need) });
    }
    var titles = rows.reduce(function (a, r) { return a + r.count; }, 0);
    var body = (head ? '<p style="font-weight:600;margin:12px 0 8px">' + esc(head) + "</p>" : "") + table +
      '<p class="muted" style="font-size:13px;margin:8px 0 0">' + PH(titles, "From {n} title.", "From {n} titles.") +
      (tot.missing ? " " + H("Libraries whose dataset’s record size couldn’t be checked aren’t counted in Your minimum or Your library.") : "") + "</p>" +
      '<p style="margin:8px 0 0;font-weight:600" data-esthead>' + estHead() + "</p>";
    if (standalone) return '<div class="sizing">' + body + "</div>";
    return '<div class="sizing">' + details("l2-sizing", H("L2ARC sizing"), body) + "</div>";
  }

  // ---- L2ARC Sizing Estimator: what a different lead-in length per library would need (the
  // Target column). Only a what-if — kept in the page, never saved; each starts at the library's
  // default length whenever the page is opened.
  var EST = {};
  function estSec(r) { return EST[r.key] != null ? EST[r.key] : ((ST.kind_seconds || {})[r.kind] != null ? ST.kind_seconds[r.kind] : r.set_sec); }
  function estCell(r) {
    return esc(fmtBytes(libSizes(r).target.bytes)) + ' <span class="muted">· ' + esc(secText(estSec(r))) + " · " + esc(fmtRs(r.rs_best)) + "</span>";
  }
  function estTotal() { return ST.plan ? sizeTotals().target.bytes + ST.plan.reserve : 0; }
  function estHead() {
    var t = estTotal(), l2 = ST.zfs.l2_total;
    if (!l2) return esc(T("Target: {size}", { size: fmtBytes(t) }));
    return esc(t <= l2 ? T("Target: {size} — fits your {l2} L2ARC", { size: fmtBytes(t), l2: fmtBytes(l2) })
      : T("Target: {size} — {over} more than your {l2} L2ARC", { size: fmtBytes(t), over: fmtBytes(t - l2), l2: fmtBytes(l2) }));
  }
  function estimatorSliders() {
    var p = ST.plan;
    if (!p || !p.libraries.length) return "";
    return '<div class="estgrid">' + p.libraries.map(function (r) {
      var lim = SEC_LIMITS[r.kind], id = "est-" + r.key;
      return '<div class="fld" style="margin:0"><label for="' + id + '">' + libIcon(r.key) + esc(r.name) + ' <span class="val" data-val="' + id + '">' + esc(secText(estSec(r))) + "</span></label>" +
        '<input id="' + id + '" type="range" min="' + lim[0] + '" max="' + lim[1] + '" step="1" value="' + estSec(r) + '" data-est="' + esc(r.key) + '"></div>';
    }).join("") + "</div>";
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("[data-libkey]");
    if (a) { LIB.key = a.dataset.libkey; LIB.view = a.dataset.libview || "caching"; LIB.data = null; LIB.at = 0; }
  }, true);
  document.addEventListener("input", function (e) {
    var el = e.target;
    if (!el.dataset || el.dataset.est === undefined) return;
    EST[el.dataset.est] = Number(el.value);
    var r = ((ST.plan || {}).libraries || []).filter(function (x) { return x.key === el.dataset.est; })[0];
    var v = document.querySelector('[data-val="' + el.id + '"]');
    if (v) v.textContent = secText(el.value);
    var c = document.querySelector('[data-estcell="' + el.dataset.est + '"]');
    if (c && r) c.innerHTML = estCell(r);
    var t = sizeTotals().target, buf = ST.plan.reserve;
    [["lead", t.bytes], ["l2", t.bytes + buf]].forEach(function (x) {
      var td = document.querySelector('[data-est' + x[0] + '="target"]');
      if (td) td.textContent = fmtBytes(x[1]);
    });
    var head = document.querySelector("[data-esthead]");
    if (head) head.innerHTML = estHead();
  });

  function sizingControls() {
    return '<div class="grid" style="max-width:520px">' +
      range("warmup", "reserve_gb", T("System buffer"), 0, 500, 10, "GB",
        H("Room on the SSD for everything else ZFS caches. The larger the buffer, the longer cached files stay cached.")) + "</div>";
  }
  function sizingAdvanced() {
    return details("l2-advanced", H("Safety margins"),
      '<p class="help" style="margin:8px 0 0">' + H("When the L2ARC is too small for your seconds, warm-up shortens them, but never below each file’s metadata: at its start, whole records through 60% of a record of video, and at its end the index, tags, fonts and cover art.") + "</p>" +
      '<div class="grid" style="margin-top:8px">' +
      number("warmup", "padding_video", T("Video safety margin (%)"), 0, 100, H("Extra on top of the average bitrate, for scenes that run higher.")) +
      number("warmup", "padding_audio", T("Audio safety margin (%)"), 0, 100, "") + "</div>");
  }


  function checkbox(sec, key, label, help) {
    var h = splitHelp(help);
    return '<div class="chkblk"><div class="chk">' + infoOrGap(h.more, label) + '<label><input type="checkbox" data-check="' + sec + "." + key + '"' + (S[sec][key] ? " checked" : "") + ">" + esc(label) + "</label>" + revertBtn(sec, key, "") + "</div>" +
      helpDiv(h) + "</div>";
  }
  // every row of checks starts with its ⓘ, so they line up in one column; a row without one keeps the space
  function infoOrGap(html, about) {
    return html ? info(html, about) : '<span class="info gap" aria-hidden="true"></span>';
  }

  // ---- L2ARC Cache Settings (SSD Cache page): how all caching that needs an L2ARC behaves —
  // warm-up, and Prefetch's steps with the grey SSD icon. Caching that works on RAM never waits.
  // L2ARC pacing: automatic (just under how fast ZFS copies to the SSD) or a share of disk time.
  function pacingFields() {
    var w = S.warmup, fill = ST.l2arc_fill, rate = ST.l2arc_rate, mbs = function (b) { return T("{n} MB/s", { n: dec(b / 1e6, b < 1e7 ? 1 : 0) }); };
    var autoHelp = {
      s: w.pacing_auto ? (rate ? H("Reads from the disks at up to {rate} — just under the {fill} ZFS copies to the SSD.", { rate: mbs(rate), fill: mbs(fill) }) :
        H("Couldn’t read how fast ZFS fills the L2ARC (l2arc_write_max), so the share of disk time below is used until it can.")) : "",
      more: H("ZFS doesn’t write to the L2ARC directly: everything is read into RAM first, and about once a second ZFS copies a little of what’s about to leave RAM to the SSD — at most <span class=\"mono\">l2arc_write_max</span> a second. " +
        "Anything read from the disks faster than that leaves RAM before it’s copied, so the read was wasted. Automatic pacing reads just under that speed. " +
        "Reads that come from the SSD or RAM already aren’t held back: there’s nothing to copy.<br><br>" +
        "To warm up faster, raise <span class=\"mono\">l2arc_write_max</span> (Health shows how); automatic pacing follows it. Warm-up’s “Run Now” runs at full speed.") };
    var manual = !w.pacing_auto || !rate;
    return '<div style="margin-top:12px">' + checkbox("warmup", "pacing_auto", T("L2ARC pacing: match how fast ZFS fills the L2ARC (automatic)"), autoHelp) + "</div>" +
      (manual ? '<div class="grid" style="max-width:520px;margin-top:4px">' +
        range("warmup", "pacing_percent", T("L2ARC pacing"), 5, 100, 5, T("% of disk time"), H("A share of disk time instead. Lower is gentler on other workloads; too high, and reads outrun what ZFS can copy to the SSD (Health warns). Warm-up’s “Run Now” runs at full speed.")) + "</div>" : "");
  }
  function l2arcSettingsCard() {
    var q = ST.l2arc_queue || {};
    var wp = ST.warmup.running && ST.warmup.progress && ST.warmup.progress.paused;
    var queue = q.waiting || q.reading ?
      '<div class="note" style="background:var(--guide-bg);border-color:var(--guide-line);color:var(--ink)">' +
        (q.paused ? badge("warn", T("Paused")) : badge("running", T("Reading"))) + " " +
        H("Prefetch’s L2ARC-only queue: {n} waiting", { n: fmtNum(q.waiting || 0) }) +
        (q.reading ? " · " + H("now: {title}", { title: q.reading }) : "") +
        (q.paused ? '<div class="help" style="margin:4px 0 0">' + H("Paused: {why}.", { why: msg(q.paused) }) + "</div>" : "") + "</div>" : "";
    return '<section class="card" id="l2arc-settings"><h2>' + H("L2ARC Cache Settings") + "</h2>" +
      '<p class="help" style="margin:4px 0 0">' + H("These apply only when there’s an L2ARC, to all caching that needs one: warm-up, and the Prefetch choices with the grey SSD icon (the whole library’s lead-ins, the rest of a season in full…). " +
        "It reads in the background at this pace and steps aside for anything below, then carries on where it left off. " +
        "Prefetch’s share is one queue for every player: the newest request goes to the front, and everything in it is read in the end, even after its player stops. " +
        "Caching that works on RAM alone — Now Playing Cache, and Prefetch’s smaller steps like the next episode — never waits for these, so the reads that help most are never held up.") + "</p>" +
      inUseNote("l2arc") +
      (wp ? '<div class="note">' + badge("warn", T("Paused")) + " " + esc(T("Warm-up: {why}", { why: msg(wp) })) + "</div>" : "") + queue +
      pacingFields() +
      '<h3 class="subh">' + H("Pause while") + "</h3>" +
      checkbox("warmup", "pause_buffering", T("A player is buffering"), H("Plex reports a stream waiting for data. Stays paused for 3 minutes after it clears.")) +
      checkbox("warmup", "pause_preload", T("Now Playing Cache or Prefetch is reading for someone"), H("Someone watching now comes first — usually a pause of a minute or two while a title loads. (Other L2ARC-only reads don’t count.)")) +
      checkbox("warmup", "pause_scrub", T("A scrub or resilver is running"), H("They already keep every disk in the pool busy; caching would only slow them down. Needs the TrueNAS API.")) +
      '<div class="grid" style="margin-top:8px;max-width:520px">' +
      select("warmup", "pause_disk_busy", T("The pool’s disks are busy with other work"), [[40, T("{n}% busy or more", { n: 40 })], [50, T("{n}% busy or more", { n: 50 })], [60, T("{n}% busy or more", { n: 60 })], [0, T("Don’t pause for this")]],
        H("File copies, backups or other apps. Measured while L2ARC-only caching is idle, so its own reads don’t count. Warm-up’s “Run Now” ignores this.")) +
      "</div>" + cooldownFields("l2arc") + "</section>";
  }

  function warmupStatus() {
    var wu = ST.warmup;
    if (wu.running) {
      var p = wu.progress || { done: 0, total: 0, bytes: 0 };
      var pct = p.total ? Math.round(p.done / p.total * 100) : 0;
      return '<div class="note" style="background:var(--guide-bg);border-color:var(--guide-line);color:var(--ink)"><div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between"><div>' +
        (p.paused ? badge("warn", T("Paused")) : badge("running", wu.phase === "scanning" ? T("Preparing") : T("Running"))) + " " +
        (p.total ? H("{done} of {total} files · {size} read", { done: p.done, total: p.total, size: fmtBytes(p.bytes) }) : H("Reading library list…")) +
        (p.paused ? '<div class="help" style="margin:4px 0 0">' + H("Paused: {why}.", { why: p.paused }) + "</div>" : "") +
        '</div><button class="btn danger" data-act="warmup-stop">' + H("Stop") + '</button></div><div class="meter"><div style="width:' + pct + '%"></div></div></div>';
    }
    return "";
  }

  function reserveHelp() {
    var r = ST.ram, why = H("Now Playing Cache and Prefetch never fill this much of the ZFS cache, so TrueNAS, shares and other apps keep their cached data.");
    if (!r || !r.cache_max) return { more: why };
    return { s: H("Total memory available: {total} · Available now: {left}", { total: fmtBytes(r.available), left: fmtBytes(r.usable || 0) }),
      more: why + " " + H("<strong>Memory reserved</strong> is this setting. <strong>Total memory available</strong> is what the ZFS cache holds ({size}) plus free RAM; RAM used by apps and services doesn’t count. " +
        "<strong>Available now</strong> is that minus the reserve: what Now Playing Cache and Prefetch share. Both change as apps use more or less RAM.", { size: fmtBytes(r.arc_size || 0) }) };
  }

  // "≈ 20.8 GB of the 52 GB usable RAM cache, shared by all streams"
  function ramHelp(feature, how) {
    var r = ST.ram;
    if (!r || !r.usable) return { s: r && r.cache_max ? H("No ZFS cache is left after the reserve (<a href=\"/caching#arc\" data-nav>Caching → Cache Settings</a>).") : H("Share of the usable ZFS cache."), more: how };
    return { s: H("Up to {size} now, shared by all streams", { size: fmtBytes(r[feature]) }),
      more: how + " " + H("{pct}% of the {usable} available now: total memory available {avail} minus {reserve} left for the rest of the NAS (see “Reserve ZFS cache”).",
        { pct: r_pct(feature), usable: fmtBytes(r.usable), avail: fmtBytes(r.available), reserve: fmtBytes(r.reserve) }) };
  }
  function r_pct(feature) { return S[feature].ram_percent; }
  function speedGuess() {
    var sp = S.general.disk_speeds || {}, best = 0;
    Object.keys(sp).forEach(function (k) { best = Math.max(best, sp[k].mb_s); });
    return best || S.general.hdd_mb_per_sec;
  }

  function pageCaching() {
    var out = top(T("Caching"), H("What gets cached, when, and how much RAM it may use. Changes save automatically; open “How it works” on any card for the details."), "");
    out += modeLegend();
    // Datasets whose settings keep file data out of RAM: their libraries can't be cached.
    var badDs = (ST.zfs.datasets || []).filter(function (d) { return d.props && d.props.primarycache && d.props.primarycache !== "all"; });
    out += '<div class="stack">' + arcCard();

    // ---- Now Playing Cache
    var n = S.nowplaying;
    out += '<section class="card' + (isOn("nowplaying") ? "" : " off") + '" id="nowplaying"><div class="head">' + toggle("nowplaying", T("Now Playing Cache")) +
      "<h2>" + H("Now Playing Cache") + "</h2>" + rec("nowplaying") + '<span style="flex:1"></span>' + guideBtn("nowplaying") + "</div>" +
      '<p class="lede">' + H("The file being played, read ahead of whoever is watching, so skipping forward or back comes from cache instead of the hard drives.") + "</p>" +
      guide("nowplaying", [
        [H("When it helps"), H("Skipping intros, scrubbing, jumping back. Works even with no SSDs, as long as file data can stay in RAM.")],
        [H("What it needs"), H("<span class=\"mono\">primarycache=all</span> on the media dataset, so what’s read stays in RAM. With <span class=\"mono\">metadata</span> nothing is cached and this has no effect.")],
        [H("How it reads"), H("Once Plex reports the stream playing and its start-up buffer has had time to fill, it reads from the play position to the end, then the beginning — the whole file if it fits the RAM limit, otherwise as much as fits ahead of the viewer. If they jump past that, it starts again from there. " +
          "A file already in the Whole File Cache (Prefetch read it near the end of the last one) isn’t read again within its cooldown.")],
        [H("Cost"), H("RAM (the ZFS cache), up to its limit in <a href=\"/caching#arc\" data-nav>Cache Settings</a> for all streams together. Older one-off reads are what make room; data other apps use often stays.")]]);
    if (unavailable("nowplaying")) out += unavailableNote("nowplaying", esc(unavailable("nowplaying")));
    if (isOn("nowplaying")) {
      if (badDs.length) {
        out += '<div class="note">' + H("<strong>From Health:</strong> {list} can’t be cached, so they’re skipped.", { list: badDs.map(function (d) {
          return T("{libraries} (on {dataset}, primarycache={value})", { libraries: d.libraries, dataset: d.dataset, value: d.props.primarycache });
        }) }) + " " + H("<a href=\"/health\" data-nav>See the fix</a>.") + "</div>";
      }
      out += '<div class="grid">' +
        select("nowplaying", "delay_sec", T("When to start"), [[0, T("Automatic (after Plex’s buffer)")], [15, T("{n} s after playback starts", { n: 15 })], [30, T("{n} s after playback starts", { n: 30 })], [60, T("{n} s after playback starts", { n: 60 })]],
          n.delay_sec ? { s: H("Counted from when Plex reports “playing” — after the player’s start-up buffering."),
                          more: H("Plex reports a stream as playing once the player has finished its start-up buffering; the wait starts then.") } :
            { s: H("Plex reports “playing” after the player’s start-up buffering; this then waits for Plex’s buffer to fill."),
              more: H("After the start-up buffering, Plex keeps reading ahead to fill the rest of its buffer. Automatic waits for the time the disks take to read that " +
                "(see Automatic start timing below), so Now Playing Cache doesn’t compete with it.") }) +
        number("nowplaying", "max_streams", T("Files to read at the same time"), 1, 20,
          { more: H("If more people are watching, the others wait their turn — every stream still gets cached.<br><br>" +
              "Different disks cope with several reads at once very differently. <strong>Hard drives</strong> slow right down: the heads jump between the files, so one or two at a time finishes each sooner. " +
              "<strong>SATA SSDs</strong> have no heads to move, but a single link that a few reads fill. <strong>NVMe SSDs</strong> are built for many reads at once.<br><br>" +
              "Suggested: <strong>2</strong> for hard drives · <strong>4</strong> for SATA SSDs · <strong>8</strong> for NVMe SSDs.") }) +
        "</div>";
      if (!n.delay_sec) {
        out += details("np-timing", H("Automatic start timing"),
          '<p class="help" style="margin:8px 0 12px">' + H("The wait is Plex’s buffer read at the disks’ speed.") + info(H("Plex keeps filling its buffer after playback starts. The wait is that buffer — some seconds of playback at the file’s bitrate — read at the measured speed of its dataset (<a href=\"/system#disk-speed\" data-nav>System → Disk Speed</a>). " +
          "For example, 60 s of an 80 Mbps remux is 600 MB: about {s} s at {speed} MB/s.", { s: Math.max(n.min_delay_sec, Math.round(600 / speedGuess())), speed: speedGuess() }), T("automatic timing")) + "</p>" +
          '<div class="grid" style="margin-top:0">' +
          number("nowplaying", "buffer_direct_sec", T("Direct Play: seconds of playback buffer"), 0, 600, "") +
          number("nowplaying", "buffer_transcode_sec", T("Transcoding: seconds of playback buffer"), 0, 600, "") +
          number("nowplaying", "min_delay_sec", T("Never start sooner than (seconds)"), 0, 600, H("Gives the player time to open the file.")) + "</div>");
      }
    }
    out += "</section>";
    out += leadInCard() + wholeFileCard() + "</div>";
    return out;
  }

  // ---- Cache Settings (with or without an L2ARC: the RAM caching shares) and RAM Only Settings.
  // Cooldowns (see plexcache/cachelog.py): one pair for RAM only, one for an L2ARC (SSD Cache page).
  var AUTO = -1;  // the cooldown worked out from what's been measured (config.AUTO)
  var COOLDOWNS = { ram: [AUTO, 0, 10, 30, 60, 120, 180, 240, 360, 720], l2arc: [AUTO, 0, 10, 60, 180, 360, 720, 1440, 2880, 4320, 10080] };  // minutes
  function minutesText(m) {
    if (m === AUTO) return T("Automatic — measured on this NAS");
    if (!m) return T("No cooldown");
    if (m < 60) return P(m, "{n} minute", "{n} minutes");
    var h = m / 60;
    return h % 24 === 0 && h >= 48 ? P(h / 24, "{n} day", "{n} days") : P(h, "{n} hour", "{n} hours");
  }
  function cooldownInfo(kind) {
    var how = kind === "l2arc" ?
      H("With an L2ARC, everything read is taken to reach the SSD, where it stays for days — these apply, and the RAM ones on the Caching page don’t. " +
        "Long enough that the whole library’s lead-ins aren’t read again every time something plays.") :
      H("On RAM only, cached data stays in the ZFS cache (ARC) for hours at most — busy NASes push one-off reads out sooner. These apply then; with an L2ARC the ones on the SSD Cache page do.");
    return info(H("Everything cached is logged — lead-ins in one log, whole files in another — and isn’t read again until its cooldown has passed, whichever feature asks. " +
        "A whole file counts as a lead-in too.") + "<br><br>" + how + "<br><br>" +
        H("Too long, and something pushed out of the cache isn’t read again in time; too short, and it’s read again needlessly — cheap, since it comes from the cache. When unsure, shorter is safer.") + "<br><br>" +
        H("Every re-read is timed, so the app learns how long cached data really lasts on your NAS (<a href=\"/activity#lasts\" data-nav>Metrics → How Long Cached Data Lasts</a>) and suggests a cooldown from it. A short cooldown, even none, collects that data fastest. “Automatic” uses that suggestion, per library, and keeps adjusting it as new reads are measured.") +
        (kind === "l2arc" ? "<br><br>" + H("Timed warm-ups only skip lead-ins Prefetch cached since the last warm-up, so they always re-check their own: what’s still on the SSD is a quick hit, and what the L2ARC has since overwritten (it reuses its space oldest first) is read again. “Run Now” reads everything.") : ""),
        T("cooldown"));
  }
  // "Measured here: ..." under a pair of cooldowns, from the survival table (see pageActivity).
  function suggestLine(route) {
    var r = ((ST.survival || {}).routes || {})[route];
    if (!r) return "";
    return '<p class="help" style="margin-top:4px">' + (r.suggest ? H("Measured on this NAS over the last 7 days: everything read again up to {age} after it was cached was still there, so a cooldown up to that saves reading it again for nothing.", { age: ageShort(r.suggest) })
      : noSuggestion(r)) + ' <a href="/activity#lasts" data-nav>' + H("Details") + "</a></p>";
  }
  // Why a route has no suggested cooldown: too few re-reads in the last 7 days, or even the soonest
  // had to come from the disks (then Automatic uses its shortest, to measure shorter ages)
  var SUGGEST_MIN = 3;  // re-reads needed (plexcache/cachelog.py SUGGEST_MIN)
  function noSuggestion(r) {
    var fc = r.fully || { n: 0 };
    if (fc.n >= SUGGEST_MIN && !fc.age) return H("No cooldown to suggest: in the last 7 days, even the soonest re-read ({age} after it was cached) had to come from the disks.", { age: ageShort(fc.miss) });
    return H("Not enough re-reads in the last 7 days to suggest a cooldown ({n} so far).", { n: fmtNum(fc.n) });
  }
  function ageText(sec) { return minutesText(Math.round(sec / 60)); }
  function cooldownFields(kind) {
    var sk = kind + "_cooldown_", inUse = modeIs(kind === "l2arc" ? "l2arc" : "ram");
    var opts = function (key) {
      return COOLDOWNS[kind].map(function (m) { return [m, minutesText(m)]; });
    };
    var cl = ST.cachelog || { lead_ins: 0, whole_files: 0 };
    var r = ((ST.survival || {}).routes || {})[kind === "l2arc" ? "ssd" : "ram"] || {};
    var autoOn = S.general[sk + "lead_in_minutes"] === AUTO || S.general[sk + "whole_minutes"] === AUTO;
    var autoLine = !autoOn ? "" : '<p class="help" style="margin-top:8px">' + (r.auto ?
      H("Automatic now: up to {age}, or each library’s own where it’s been measured. It’s worked out again every few hours from recent reads.", { age: ageShort(r.auto) }) :
      H("Automatic: not enough recent reads measured yet, so a typical value is used for now.")) + "</p>";
    return '<h3 class="subh">' + H("Cooldown") + cooldownInfo(kind) + "</h3>" +
      '<div class="grid" style="margin-top:0">' +
      select("general", sk + "lead_in_minutes", T("Lead-in cooldown"), opts("lead_in_minutes"), H("Warm-up, and Prefetch’s lead-ins.")) +
      select("general", sk + "whole_minutes", T("Whole file cooldown"), opts("whole_minutes"), H("Prefetch near the end, and Now Playing Cache when the whole file fit.")) +
      "</div>" + (inUse ? '<p class="help" style="margin-top:8px">' + H("Logged now: {lead} and {whole}.", { lead: P(cl.lead_ins, "{n} lead-in", "{n} lead-ins"), whole: P(cl.whole_files, "{n} whole file", "{n} whole files") }) + "</p>" : "") +
      autoLine + suggestLine(kind === "l2arc" ? "ssd" : "ram");
  }
  // Whether a mode's settings are the ones in use right now, as a line under the card's description.
  function inUseNote(kind) {
    var on = modeIs(kind === "l2arc" ? "l2arc" : "ram");
    var text = kind === "l2arc" ? (on ? H("In use now: this NAS has an L2ARC.") : H("Not in use now: no L2ARC is in use, so the RAM Only Settings on the Caching page apply.")) :
      (on ? H("In use now: no L2ARC is in use.") : H("Not in use now: this NAS has an L2ARC, so the L2ARC Cache Settings on the <a href=\"/ssd#l2arc-settings\" data-nav>SSD Cache</a> page apply instead."));
    return '<p class="inuse ' + (on ? "on" : "off") + '">' + text + "</p>";
  }
  // The three RAM sliders of Cache Settings: the value and what it means go under the bar, on
  // lines of their own, worked out again as the slider moves.
  function ramLines(sec, key) {
    var r = ST.ram || {}, reserve = S.general.ram_reserve_gb * 1e9;
    if (!r.available) return '<div class="help">' + (key === "ram_reserve_gb" ? H("Memory reserved: {size}", { size: fmtBytes(reserve) }) : H("{pct}% of what’s available", { pct: S[sec][key] })) + "</div>";
    var usable = Math.max(0, r.available - reserve);
    if (key === "ram_reserve_gb") {
      return '<div class="help">' + H("Memory reserved: {size}", { size: fmtBytes(reserve) }) + "</div>" +
        '<div class="help">' + H("Total memory available: {size}", { size: fmtBytes(r.available) }) + "</div>" +
        '<div class="help">' + H("Available now: {size}", { size: fmtBytes(usable) }) + "</div>";
    }
    var pct = S[sec][key];
    return '<div class="help">' + H("{pct}% of what’s available", { pct: pct }) + "</div>" +
      '<div class="help">' + H("Up to {size} now, shared by all streams", { size: fmtBytes(usable * pct / 100) }) + "</div>";
  }
  function ramSlider(sec, key, label, min, max, step, unit, help) {
    var id = "f-" + sec + "-" + key, h = splitHelp(help);
    return '<div class="fld"><label for="' + id + '">' + esc(label) + info(h.more, label) + revertBtn(sec, key, unit) + "</label>" +
      '<input id="' + id + '" type="range" min="' + min + '" max="' + max + '" step="' + step + '" value="' + esc(S[sec][key]) + '" data-set="' + sec + "." + key + '" data-unit="' + unit + '">' +
      '<div class="ramlines" data-lines="' + sec + "." + key + '">' + ramLines(sec, key) + "</div></div>";
  }
  function refreshRamLines() {
    main.querySelectorAll("[data-lines]").forEach(function (el) { var p = el.dataset.lines.split("."); el.innerHTML = ramLines(p[0], p[1]); });
  }
  function arcCard() {
    return '<section class="card" id="arc"><h2>' + H("Cache Settings") + "</h2>" +
      '<p class="help" style="margin:4px 0 0">' + H("These apply whether or not there’s an L2ARC: how much of the RAM cache (the ZFS ARC) Now Playing Cache and Prefetch may use.") + "</p>" +
      '<div class="grid">' +
      ramSlider("general", "ram_reserve_gb", T("Reserve ZFS cache"), 0, Math.min(4096, Math.max(16, Math.ceil(((ST.ram || {}).cache_max || 0) / 1e9))), 1, "GB", { more: reserveHelp().more }) +
      ramSlider("nowplaying", "ram_percent", T("Now Playing Cache RAM limit"), 5, 90, 5, "%", { more: ramHelp("nowplaying", H("Reads the whole file if it fits, otherwise as much as fits ahead of the viewer.")).more }) +
      ramSlider("nextepisode", "ram_percent", T("Prefetch RAM limit"), 5, 90, 5, "%", { more: ramHelp("nextepisode", H("The Whole File Cache, and without an L2ARC the Lead-in Cache too. With an L2ARC, lead-ins only pass through RAM on their way to the SSD, so they don’t count.")).more }) +
      "</div></section>" +
      '<section class="card" id="ram-only"><h2>' + H("RAM Only Settings") + "</h2>" +
      '<p class="help" style="margin:4px 0 0">' + H("These apply only when there’s no L2ARC.") + "</p>" +
      inUseNote("ram") + cooldownFields("ram") + "</section>";
  }

  // ---- SSD Cache: the L2ARC, whether to use one, and how big it needs to be.
  // ------------------------------------------------------------ optimizer
  // Each library's choices, as optimizing.py reads them (DEFAULTS there: sync on, MKV off, Plex's
  // cover art only for unmatched items).
  // ↺ back to the default for one of a library's optimizer choices (when it isn't the default)
  var OPT_NAMES = { images: { unmatched: N("Plex poster, unmatched items only"), remove: N("Strip it"), compress: N("Keep it, compressed"), plex: N("Plex poster, every item") },
    tags: { keep: N("Keep tags"), excessive: N("Strip excessive tags"), strip: N("Strip tags") } };
  function optRevert(key, field) {
    var d = ((ST.optimizer_defaults || {}).library || {})[field];
    if (d === undefined || String(optChoices(key)[field]) === String(d)) return "";
    var shown = OPT_NAMES[field] ? T(OPT_NAMES[field][d]) : d ? T("on") : T("off");
    return ' <button type="button" class="revert" data-optlib-revert="' + esc(key) + '" data-field="' + field + '" title="' + H("Back to the default ({value})", { value: shown }) +
      '" aria-label="' + H("Reset to the default, {value}", { value: shown }) + '">↺</button>';
  }
  function optChoices(key) {
    var lib = ((S.optimizer || {}).libraries || {})[key] || {};
    return { sync: lib.sync !== false, mkv: lib.mkv === true, images: { remove: 1, compress: 1, plex: 1 }[lib.images] ? lib.images : "unmatched",
      tags: { excessive: 1, strip: 1 }[lib.tags] ? lib.tags : "keep", convert: lib.convert === true };
  }
  var AUDIO_COVER_HELP = H("In music and audiobooks, cover art belongs in the file: it’s what music players, phones, car stereos and other libraries (iTunes among them) show, and Plex’s own artwork lookups for music are limited. So for an audio library the optimizer always keeps it, compressed losslessly where that makes it smaller — every pixel the same. Plex’s posters aren’t put into audio files.");
  // The tag choice (optimizer.strips_tags): what tags are, who reads them, and what each choice gives up
  var TAGS_HELP = H("Tags are notes stored inside an MKV file: titles, credits, encoder settings, and the statistics MKVToolNix writes for each track (its duration, bitrate and size). " +
    "<strong>Plex doesn’t read them</strong>: it takes its titles and details from its own agents. They’re cached with the rest of the metadata at the end of the file, so they cost cache room on every file.") +
    "<br><br><strong>" + H("Keep tags") + "</strong><ul>" +
    "<li>" + H("Up: other players and tools keep what they read from them — ffprobe, Jellyfin and MediaInfo show track durations and bitrates from the statistics tags; home videos Plex can’t match keep their titles and notes; anything you noted in a file stays.") + "</li>" +
    "<li>" + H("Down: usually a few KB, but some tools write very large tags (hundreds of KB or more), cached with every file.") + "</li></ul>" +
    "<strong>" + H("Strip excessive tags") + "</strong><ul>" +
    "<li>" + H("Up: only the files whose tags are over 256 KB — the ones Library Health warns of — lose them; the rest keep theirs.") + "</li>" +
    "<li>" + H("Down: those files lose everything their tags held, statistics and notes alike.") + "</li></ul>" +
    "<strong>" + H("Strip tags") + "</strong><ul>" +
    "<li>" + H("Up: the least metadata to cache, on every file.") + "</li>" +
    "<li>" + H("Down: other players show less (durations and bitrates from the statistics tags go; MKVToolNix can add them again); titles and notes in home videos go. Plex is unaffected.") + "</li></ul>" +
    H("Whichever you choose, the optimizer’s own tag (its mark, and the cover art’s source) is always written back: it’s how a file is known to be done. Tags on tracks go with the rest when they’re stripped.");
  var COVER_HELP = H("What happens to the cover art in MKV files — the images some DLNA players and TVs show — when they’re first optimized. Fonts and other attachments are always kept. " +
    "A Plex poster already in a file is kept (unless you choose “Strip it”); <strong>Sync Cover Art</strong> updates optimized files later.<br><br>" +
    "<strong>Plex poster, unmatched items only</strong>: when Plex can’t match something to an online entry — home videos, concerts, rare releases — " +
    "it has nowhere to fetch artwork from again, and the most reliable place to keep it is in the file itself. So those get Plex’s poster (whatever you set in Plex), losslessly compressed; " +
    "matched items have their cover art taken out, since Plex fetches it anyway.<br><br>" +
    "<strong>Strip it</strong> takes it out of every file. <strong>Keep it, compressed</strong> keeps what’s there, losslessly compressed " +
    "(PNG with oxipng, JPEG with jpegtran: every pixel stays the same). <strong>Plex poster, every item</strong> puts Plex’s poster in every file — the movie’s, " +
    "or for an episode its season’s — and writes the file again whenever the poster in Plex changes.");
  var SYNC_COVER_HELP = H("Cover art is set once, when a file is first optimized: a later change of choice, or a new poster in Plex, doesn’t touch optimized files. " +
    "<strong>Sync Cover Art</strong> does, when you ask: files whose Plex poster has changed get the new one, and with <strong>Strip it</strong> chosen, cover art is taken out. " +
    "Each Plex poster in a file is marked with a checksum of the poster as Plex has it, so only the ones that changed are written again.");
  var SORT_HELP = H("The last record of a file is usually only partly filled: with a 1M record size, the end of a file might hold only 500K. " +
    "ZFS keeps a record compressed only when compression saves at least 12.5%, so a record that mixes a little compressible data with a lot that isn’t (video, images) is often stored as it is.<br><br>" +
    "So the optimizer puts the cover art straight after the video — it barely compresses, but it’s large enough to fill out a record — and the fonts, chapters, index and tags after it. " +
    "Those compress very well, and sitting together at the very end of the file, the record that holds them is very likely to be compressed: ZSTD-3 often halves them.<br><br>" +
    "The index is needed before a file can start playing, so it’s cached along with the start of every file. ZFS caches it as it’s stored on disk, compressed — " +
    "so the smaller it is, the more files fit in the same cache and the longer they stay there.");
  // Setting the optimizer up: each thing that has to be true, checked in order (optimizing.py
  // diagnose()); what passes in one line, then the first that doesn't, with what to do about it.
  function optimizerSetup(op) {
    var su = op.setup || {}, steps = su.steps || [];
    if (su.ready || (op.configured && !su.at)) return "";  // (answering, its steps not checked yet: nothing to set up that's known)
    var out = '<section class="card">' + heading(esc(T("Set Up the Optimizer")),
      H("Optional: a second service in this app, and the only part of Plex Cache that can change your media. Until it’s running, the choices below wait."));
    if (!su.at) return out + '<p class="muted">' + H("Checking…") + "</p></section>";
    var done = steps.filter(function (x) { return x.ok; }), next = steps.filter(function (x) { return !x.ok; })[0];
    if (done.length) out += '<div class="det">' + done.map(function (x) { return "✓ " + esc(x.title); }).join(" · ") + "</div>";
    if (next) {
      var cmd = next.cmd || "";
      out += '<div class="row">' + badge("warn", T("To do")) + '<div class="grow"><div class="what">' + esc(next.title) + "</div>" +
        (next.detail ? '<div class="det">' + esc(next.detail) + "</div>" : "") +
        (next.fix || cmd ? '<div class="fix">' + esc(next.fix || "") + (cmd ? '<div class="cmd"><code style="white-space:pre-wrap">' + esc(cmd) +
          '</code><button class="btn" data-copy="' + esc(cmd) + '">' + H("Copy") + "</button></div>" : "") + "</div>" : "") + "</div></div>";
    }
    out += '<div style="margin-top:10px"><button class="btn" type="button" data-act="optimizer-check">' + H("Check Again") + "</button> " +
      '<span class="muted">' + H("checked {when}", { when: ago(su.at) }) + "</span></div>";
    return out + "</section>";
  }
  var OPT_CONFIRM = null;  // the library whose Optimize Selected is waiting for "Optimize Anyway" (ZFS settings off)
  var ZFS_CFG_HELP = H("Whether the library’s dataset is set up for the best result: its record size (see <a href=\"/health#media\" data-nav>Health → Libraries</a>) and its compression. " +
    "<strong>Ideal</strong>: the recommended record size and ZSTD-3. <strong>Pass</strong>: the second-best record size, or ZSTD at levels 4 to 19. " +
    "<strong>Warning</strong>: either is off.<br><br>Files take their dataset’s settings when they’re written, so change these first, in TrueNAS " +
    "(Datasets → the dataset → Edit → Advanced Options): optimizing before that means writing every file twice.");
  // Is a file activity running (an optimizer job, or a folder being copied to its own dataset)? Only one at a time.
  function fileBusy() {
    var job = ST.job, mig = ((ST.migration || {}).state) || {};
    return !!((job && job.running) || mig.copying);
  }
  // The file job running now (jobs.py: optimizing, syncing cover art, copying a folder), one card for every kind:
  // its progress, Pause and Cancel — on the Optimizer page and on Health
  function jobProgress(job, extra) {
    var checking = job.phase === "checking";
    var n = checking ? job.checked : job.done, total = checking ? job.files : job.total;
    var pct = total ? Math.min(100, Math.round(100 * n / total)) : 0;
    // the count, then the file in hand (with its poster), a pause, and the estimate
    var lines = '<div class="det">' + (checking ? H("Checking which files need it: {n} of {total}", { n: fmtNum(n || 0), total: fmtNum(total || 0) })
        : H("{done} of {total} files · {pct}%", { done: fmtNum(n || 0), total: fmtNum(total || 0), pct: pct })) + "</div>" +
      (!checking && job.current ? '<div class="det">' + fileTitle(job.current_title, job.current) + (job.step ? ' <span class="muted">(' + esc(job.step) + ")</span>" : "") + "</div>" : "") +
      // the file in hand: how far through it is (remuxing, then checking both copies)
      (!checking && job.current && job.file_pct != null ? '<div class="filebar"><div class="meter thin" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' +
        Math.round(job.file_pct) + '" aria-label="' + esc(T("This file")) + '"><div style="width:' + Math.min(100, job.file_pct) + '%"></div></div></div>' : "") +
      (job.paused_by ? '<div class="det">' + badge("info", job.paused_by === "resilver" ? T("Paused: {why}", { why: job.paused_why || "" }) : T("Paused")) +
        (job.paused_by === "resilver" ? " " + H("It carries on when the resilver is done.") : "") + "</div>" : "") +
      (!checking ? '<div class="det">' + jobEta(job) + "</div>" : "") + (extra || "");
    // the poster as tall as everything beside it
    var poster = !checking && job.current && job.kind === "optimize";
    return '<div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '"><div style="width:' + pct + '%"></div></div>' +
      (poster ? '<div class="withposter tall">' + '<div class="posterbig">' + (job.poster ? '<img alt="" src="/api/poster?path=' + encodeURIComponent(job.poster) +
        '" data-hide-broken>' : "") + '</div><div class="grow">' + lines + "</div></div>" : lines);
  }
  // How long is left, from how fast files are really written here (the estimate firms up as it goes)
  function jobEta(job) {
    if (job.eta == null) return H("Working out how long it will take…");
    if (job.eta < 60) return H("Almost done");
    var t = job.eta, d = Math.floor(t / 86400), h = Math.floor((t % 86400) / 3600), m = Math.floor((t % 3600) / 60);
    var left = d ? T("{d} d {h} h", { d: d, h: h }) : h ? T("{h} h {m} min", { h: h, m: m }) : T("{m} min", { m: Math.max(1, m) });
    // the time it'll be done, rounded: estimates aren't that exact (to 5 minutes; 15 past an hour away; 30 past 6 hours)
    var step = (t > 6 * 3600 ? 30 : t > 3600 ? 15 : 5) * 60000;
    var at = new Date(Math.round((Date.now() + t * 1000) / step) * step);
    var when = at.toLocaleString(LOCALE, d >= 6 ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" } : { weekday: "short", hour: "2-digit", minute: "2-digit" });
    return H("About {left} left — done around {when}", { left: left, when: when }) + (job.paused_by ? " " + H("(not counting the pause)") : "");
  }
  function jobLibrary(job) {
    return (((ST.library || {}).libraries || []).filter(function (l) { return String(l.key) === String(job.library); })[0] || {}).title || job.title || job.library;
  }
  function jobTitle(job) {
    var lib = jobLibrary(job), t;
    if (job.kind === "tool") t = T("{tool} in {library} Library", { tool: toolTitle(job.tool), library: lib });
    else if (job.kind === "copy") t = T("Converting {library} Folder to a Child Dataset", { library: lib });
    else if (job.kind === "discovered") t = T("Found Optimized Elsewhere in {library} Library", { library: lib });
    else if (job.mode === "covers") t = T("Syncing Cover Art in {library} Library", { library: lib });
    else t = T("Optimizing {library} Library", { library: lib });
    var n = job.resumes || 0;  // restarts it carried on after
    return !n ? t : t + " " + (n === 1 ? T("(resumed)") : n === 2 ? T("(resumed twice)") : T("(resumed {n} times)", { n: n }));
  }
  // How Plex names a file ("Title (year)", "Show · S01E05"); click it for the file's name (and again to hide it)
  var SHOWFILE = {};
  function fileTitle(title, file, cls) {
    var name = (file || "").split("/").pop();
    if (!title) return '<span class="mono">' + esc(name) + "</span>";
    return '<button class="link ' + (cls || "") + '" type="button" data-act="file-toggle" data-file="' + esc(file) + '" aria-expanded="' + !!SHOWFILE[file] + '">' + esc(title) + "</button>" +
      (SHOWFILE[file] ? '<div class="det mono">' + esc(name) + "</div>" : "");
  }
  // Plex's poster for a file or what plays, small (through this app: /api/poster); all the same size
  function posterImg(path) {
    return path ? '<img class="poster" loading="lazy" alt="" src="/api/poster?path=' + encodeURIComponent(path) + '" data-hide-broken>'
      : '<span class="poster" aria-hidden="true"></span>';
  }
  // The files a job couldn't do: how many, and a link to them (and why) in its file history, failed first
  function jobFailures(job) {
    var n = (job.errors || []).length;
    if (!n) return "";
    return '<div class="det" style="margin-top:6px">' + badge("fail", P(n, "{n} file failed", "{n} files failed", { n: fmtNum(n) })) + " " +
      H("The originals are unchanged.") + ' <button class="link" type="button" data-act="proc-failed" data-job="' + esc(job.id || "") + '">' +
      H("See why") + "</button></div>";
  }
  // The file job's card. ``inlineCopy``: a folder copy isn't drawn here — the Convert Folder card shows
  // it where its steps are (the Optimizer page), so nothing appears above and pushes the page down
  function jobButtons(job) {
    var pauseBtn = job.paused ? '<button class="btn pri" type="button" data-act="job" data-job="resume"' + (job.paused_by === "resilver" ? " disabled" : "") + ">" + H("Resume") + "</button> "
      : '<button class="btn" type="button" data-act="job" data-job="pause">' + H("Pause") + "</button> ";
    if (job.paused_by === "resilver" && !job.paused) pauseBtn = "";  // a resilver pause can't be overridden
    return pauseBtn + (job.stopping ? '<button class="btn" type="button" disabled>' + H("Cancelling…") + "</button> " : '<button class="btn" type="button" data-act="job" data-job="cancel">' + H("Cancel") + "</button> ");
  }
  function jobCard(inlineCopy) {
    var job = ST.job;
    if (inlineCopy && job && job.kind === "copy") return "";
    if (job && job.waiting) {
      // after a restart, before it carries on (once the optimizer is ready)
      return '<section class="card">' + heading(esc(jobTitle(Object.assign({}, job, { resumes: (job.resumes || 0) + 1 }))), "") +
        '<div class="det">' + H("Carries on where it was ({done} of {total} files) once the optimizer is ready.", { done: fmtNum(job.done || 0), total: fmtNum(job.total || 0) }) +
        "</div>" + jobFailures(job) + "</section>";
    }
    if (job && !job.running) {
      // the last job that ended: its counts and failures, until dismissed
      return '<section class="card">' + heading(esc(jobTitle(job)), job.cancelled ? H("Cancelled") : H("Finished")) +
        '<div class="det">' + (job.kind === "copy" && job.mirror ? H("The last pass: {total} files checked, {copied} copied (changed since the copy)", { total: fmtNum(job.total || 0), copied: fmtNum(job.copied || 0) })
          : job.kind === "copy" ? H("{done} of {total} files copied", { done: fmtNum(job.done || 0), total: fmtNum(job.total || 0) })
          : H("{optimized} optimized, {rewritten} written again, {skipped} skipped, {failed} failed", job)) + "</div>" + jobFailures(job) +
        '<div style="margin-top:10px"><button class="btn" type="button" data-act="job" data-job="dismiss">' + H("Dismiss") + "</button></div></section>";
    }
    if (!job || !job.running) return "";
    var pauseBtn = job.paused ? '<button class="btn pri" type="button" data-act="job" data-job="resume"' + (job.paused_by === "resilver" ? " disabled" : "") + ">" + H("Resume") + "</button> "
      : '<button class="btn" type="button" data-act="job" data-job="pause">' + H("Pause") + "</button> ";
    if (job.paused_by === "resilver" && !job.paused) pauseBtn = "";  // a resilver pause can't be overridden
    var sub = job.kind === "copy" ? H("{folder} → {dataset}", { folder: job.folder || "", dataset: job.child || "" }) : "";
    return '<section class="card">' + heading(esc(jobTitle(job)), sub) + jobProgress(job, jobFailures(job)) +
      '<div style="margin-top:10px">' + pauseBtn + (job.stopping ? '<button class="btn" type="button" disabled>' + H("Cancelling…") + "</button> " : '<button class="btn" type="button" data-act="job" data-job="cancel">' + H("Cancel") + "</button> ") +
      '<span class="muted">' + (job.kind === "copy" ? H("Pausing and cancelling take effect straight away; a copy carries on where it was.")
        : H("Pausing and cancelling take effect after the file in hand.")) + " " + H("File jobs started meanwhile wait in the queue.") + "</span></div></section>";
  }
  // File work waiting its turn (jobs.py queue): each starts when the jobs before it are done; any can be removed
  function queuedFor(kind, args) {
    return (ST.queue || []).filter(function (q) {
      return q.kind === kind && Object.keys(args).every(function (k) { return String(q.args[k]) === String(args[k]); });
    })[0];
  }
  function queueCard() {
    var q = ST.queue || [];
    if (!q.length) return "";
    return '<section class="card">' + heading(esc(T("Queue")), H("Each starts when the ones before it are done.")) +
      (ST.queue_hold ? '<div class="note">' + esc(ST.queue_hold) + "</div>" : "") +
      q.map(function (item, i) {
        var sub = item.kind === "copy" ? H("{folder} → {dataset}", { folder: item.folder || "", dataset: item.child || "" }) : "";
        return '<div class="row">' + badge("info", String(i + 1)) + '<div class="grow"><div class="what">' + esc(jobTitle(item)) + "</div>" +
          '<div class="det">' + (sub ? sub + " · " : "") + H("Added {when}", { when: fmtWhen(item.queued) }) + "</div></div>" +
          '<button class="btn" type="button" data-act="queue-remove" data-id="' + esc(item.id) + '">' + H("Remove") + "</button></div>";
      }).join("") + "</section>";
  }
  // ---- The Optimizer page: a section per library — its choices, its dataset, Sync Cover Art — and under them a
  // comparison card (cmpRow, as every tool's rows) for each file there's something to do to, a page at a time, ticked
  // and optimized as chosen. The direction is fixed: the optimizer doesn't put a file back (the backup taken first is the
  // way back). Left, the file as it is now: what the optimizer changes, in yellow, and what's right already, in green.
  // Right, the file after: each setting at its best (Ideal), or the one that passes; a dataset setting that's off stays
  // grey — it's changed in TrueNAS, not here. Red when Optimize can't run (the service not set up): no card can be ticked.
  var OPT_WHY = { new: N("Not optimized yet"), version: N("Optimized by an older version"), layout: N("Metadata not at the end"), tags: N("Tags to strip"),
    compression: N("Not in the dataset’s compression"), blocks: N("Not in the dataset’s record size") };
  var OPT_WHY_HELP = { new: N("The optimizer hasn’t written this MKV file yet: its metadata is laid out as the program that made it left it."),
    version: N("Written by an older optimizer: done again, the way this version lays it out."), layout: N("Marked as optimized, but its metadata isn’t where the optimizer puts it any more (a tool wrote to it since): laid out again."),
    tags: N("Tags this library’s choice strips are still in it."), compression: N("Written before the dataset had its compression: ZFS applies it only to files as they’re written, so it’s written again, byte for byte."),
    blocks: N("Written with another record size than the dataset’s: written again, byte for byte, to take it.") };
  function cardsPer() { return (S && S.general && S.general.tool_page_size) || 40; }  // the cards on a page, on every tool's page, each Optimizer library's and the check pages' (System → Preferences)
  var OPT = {};  // per library: the page of its files open (offset), as fetched (data: /api/optimizer/files)
  function optState(key) { return OPT[key] || (OPT[key] = { offset: 0, asked: null, data: null, at: 0, todo: null, loading: false }); }
  // a page of a library's files to optimize: fetched again when the page changes, the survey's count does, or a minute on
  function optFiles(key, todo) {
    var o = optState(key);
    if (!o.loading && (o.asked !== o.offset || o.todo !== todo || Date.now() - o.at > 60000)) {
      o.loading = true; o.asked = o.offset; o.todo = todo; o.at = Date.now();
      api("/api/optimizer/files?key=" + encodeURIComponent(key) + "&offset=" + o.offset + "&limit=" + cardsPer()).then(function (d) {
        o.loading = false; o.data = d; o.error = null;
        var sel = TOOL.optimizer.sel, paths = {};  // (a file done since, or no longer to do: no longer ticked)
        (d.paths || []).forEach(function (p) { paths[p] = true; });
        Object.keys(sel).forEach(function (p) { if (sel[p] && (TOOL.optimizer.libOf[p] === key) && !paths[p]) delete sel[p]; });
        (d.paths || []).forEach(function (p) { TOOL.optimizer.libOf[p] = key; });
        render();
      }).catch(function (e) { o.loading = false; o.error = e.message || "?"; render(); });  // (said, with Try Again)
    }
    return o.data && o.data.library === String(key) ? o.data : null;
  }
  function optFileCard(f, sm, c, op) {
    var z = sm.zfs || {}, issues = z.issues || [], probs = f.problems || [], has = function (p) { return probs.indexOf(p) >= 0; };
    var now = [{ id: "file", status: "grey", label: T("File {n}", { n: 1 }), text: H("This is the file as it is now: the optimizer writes it again in its place, at the same path."), lines: ['<span class="mono">' + esc(f.path) + "</span>"] }];
    var after = [{ id: "file", status: "grey", label: T("File {n}", { n: 1 }), text: H("The same file at the same path, written again: Plex sees the same file, and keeps its metadata and watched state.") }];
    if (c.mkv && f.mkv) {
      var lay = ["new", "version", "layout"].filter(has)[0];
      now.push(lay ? { id: "layout", status: "warn", label: T(OPT_WHY[lay]), text: H(OPT_WHY_HELP[lay]) }
        : { id: "layout", status: "pass", label: T("Metadata at the end"), text: H("Laid out by this optimizer already: the cover art, fonts, chapters, index and tags at the end of the file.") });
      if (has("tags")) now.push({ id: "tags", status: "warn", label: T(OPT_WHY.tags), text: H(OPT_WHY_HELP.tags) });
      var cover = { unmatched: T("Plex poster, unmatched items only"), remove: T("Strip it"), compress: T("Keep it, compressed"), plex: T("Plex poster, every item") }[c.images];
      var tags = { keep: T("Keep tags"), excessive: T("Strip excessive tags"), strip: T("Strip tags") }[c.tags];
      after.push({ id: "layout", status: "pass", label: T("Metadata at the end") + " · " + T("Ideal"), text: H("The cover art, fonts, chapters, index and tags moved to the end of the file and sorted, so the start of the file is all content.") });
      after.push({ id: "cover", status: "pass", label: T("Cover art") + ": " + cover, text: H("As chosen for this library above.") });
      after.push({ id: "tags", status: "pass", label: T("Tags") + ": " + tags, text: H("As chosen for this library above.") });
      if (op.version) after.push({ id: "ver", status: "pass", label: T("Optimizer version {version}", { version: op.version }), text: H("Written into the file, so a copy synced elsewhere isn’t done again there.") });
    }
    if (c.sync) {
      var rsIdeal = z.rs_status === "ideal", compIdeal = z.compression_status === "ideal";
      var rsOff = z.rs_status === "warn" ? issues[0] : null, compOff = z.compression_status === "warn" ? issues[issues.length - 1] : null;
      var rsLabel = T("Record size {size}", { size: rsOr(z.rs) }), compLabel = T("Compression {value}", { value: String(z.compression || "?").toUpperCase() });
      if (has("blocks")) now.push({ id: "rs", status: "warn", label: T(OPT_WHY.blocks), text: H(OPT_WHY_HELP.blocks) });
      else if (z.rs) now.push({ id: "rs", status: "pass", label: rsLabel, text: H("Written with the dataset’s record size already.") });
      if (has("compression")) now.push({ id: "comp", status: "warn", label: T(OPT_WHY.compression), text: H(OPT_WHY_HELP.compression) });
      else if (f.compression) now.push({ id: "comp", status: "pass", label: compLabel, text: H("Written with the dataset’s compression already.") });
      if (z.rs) after.push(rsOff ? { id: "rs", status: "info", label: rsLabel, text: esc(msg(rsOff)) + " " + H("The file is written with the dataset’s record size: change it in TrueNAS first to write the file only once, with {best}.", { best: rsOr(z.best) }) }
        : { id: "rs", status: "pass", label: rsLabel + " · " + (rsIdeal ? T("Ideal") : T("Pass")), text: rsIdeal ? H("The record size that suits this library best.") : H("Not the best for this library, but good: it passes.") });
      if (z.compression) after.push(compOff ? { id: "comp", status: "info", label: compLabel, text: esc(msg(compOff)) + " " + H("The file is written with the dataset’s compression: change it to ZSTD-3 in TrueNAS first to write the file only once.") }
        : { id: "comp", status: "pass", label: compLabel + " · " + (compIdeal ? T("Ideal") : T("Pass")), text: compIdeal ? H("ZSTD-3: the compression that suits media best.") : H("Not the best for media, but good: it passes.") });
    }
    now.concat(after).forEach(function (x) { x.pair = x.id; });  // (each setting now opens with the same setting after)
    var notes = [];
    if (!op.configured) notes.push({ status: "fail", html: H("The optimizer service isn’t set up yet: files can be optimized once it is (the steps are at the top of this page).") });
    if (c.sync && z.status === "warn") notes.push({ status: "info", html: H("Optimizing before changing the dataset’s settings means the file is written now and again after the change. Change them in TrueNAS first to write it only once.") });
    return { tool: "optimizer", id: f.path, title: f.title,
      checks: [{ id: "size", status: "grey", label: fmtBytes(f.size), text: H("The file’s size: the optimizer writes all of it again, and a snapshot keeps the old copy’s space until it expires.") }],
      left: [{ kind: "drop", dim: false, label: T("File as it is now"), poster: f.poster, what: esc(f.title), lines: ['<span class="mono">' + esc(f.name) + "</span>"], checks: now }],
      right: [{ kind: "keep", label: T("File after optimizing"), poster: f.poster, what: esc(f.title), lines: ['<span class="mono">' + esc(f.name) + "</span>"], checks: after }],
      notes: notes, files: [f.path] };
  }
  // a library's choices, drawn as every tool's options are (toolOptions): each with its ⓘ and its ↺ back to the default —
  // what to do, then for MKV files the cover art and tags; under them its dataset, as set up for the best result
  function optOptions(l, key, c, sm, op) {
    var box = function (k, label, help, title, n) {
      return optionRow({ help: help, title: title, label: '<input type="checkbox" data-optlib="' + esc(key) + '" data-optkey="' + k + '"' + (c[k] ? " checked" : "") + "> " + label, revert: optRevert(key, k), count: sm ? n || 0 : null, on: c[k] });
    };
    var pick = function (k, title, help, choices, n) {
      return optionRow({ help: help, title: title, label: esc(title) + ' <select class="sel sm" data-optlib="' + esc(key) + '" data-optkey="' + k + '" aria-label="' + esc(title) + '">' +
        choices.map(function (o) { return '<option value="' + o[0] + '"' + (c[k] === o[0] ? " selected" : "") + ">" + o[1] + "</option>"; }).join("") + "</select>", revert: optRevert(key, k), count: n, on: true });
    };
    var why = (sm || {}).why || {};
    var rows = [box("sync", H("TrueNAS compression &amp; record size"), OPT_SYNC_HELP, T("TrueNAS compression & record size"), (sm || {}).sync_todo),
      box("mkv", H("Optimize MKV files"), OPT_MKV_HELP, T("Optimize MKV files"), (sm || {}).mkv_todo)];
    if (c.mkv && l.kind === "audio") {
      c.images = "compress";  // (optimizing.options: an audio library's cover art stays in the file)
      rows.push(optionRow({ help: AUDIO_COVER_HELP, title: T("Cover art"), label: H("Cover art") + ': <span class="det">' + H("Kept in the file, compressed") + "</span>" }));
    } else if (c.mkv) {
      rows.push(pick("images", T("Cover art"), COVER_HELP, [["unmatched", H("Plex poster, unmatched items only")], ["remove", H("Strip it")], ["compress", H("Keep it, compressed")], ["plex", H("Plex poster, every item")]], null));
      rows.push(pick("tags", T("Tags"), TAGS_HELP, [["keep", H("Keep tags")], ["excessive", H("Strip excessive tags")], ["strip", H("Strip tags")]], sm ? why.tags || 0 : null));
    }
    var out = optionGrid(rows), z = (sm || {}).zfs;
    if (c.sync && z) out += '<div class="det optds">' + H("Dataset") + info(ZFS_CFG_HELP, T("Dataset")) + " " +
      badge(z.status === "ideal" || z.status === "pass" ? "pass" : z.status, { ideal: T("Ideal"), pass: T("Pass"), warn: T("Warning"), info: T("Not checked") }[z.status]) +
      (z.issues || []).map(function (x) { return " " + esc(msg(x)); }).join(" ·") + "</div>";
    return out;
  }
  var OPT_SYNC_HELP = H("<strong>TrueNAS compression &amp; record size</strong>: writes each file again, byte for byte, so it takes its dataset’s current record size and compression — ZFS applies them only to files as they’re written. Any kind of file. A file’s compression can’t be read back, so every file counts until the optimizer has written it once.");
  var OPT_MKV_HELP = H("<strong>Optimize MKV files</strong>:") + "<ul>" +
    "<li>" + H("moves the cover art, fonts, chapters, index and tags to the end of the file") + "</li>" +
    "<li>" + H("sorts them so the parts not needed to play the file can be skipped, and the most compressible sit nearest the end — which maximizes how much TrueNAS can cache, sometimes significantly") + "</li>" +
    "<li>" + H("compresses image subtitles (PGS, VobSub) with zlib, as MKVToolNix does; video, audio and text subtitles are copied as they are") + "</li>" +
    "<li>" + H("handles cover art as you choose: keeps it, compressed losslessly, puts Plex’s poster in, or takes it out") + "</li>" +
    "<li>" + H("handles the file’s own tags as you choose: keeps them, or strips the excessive ones or all of them (Plex doesn’t read them; other players do)") + "</li>" +
    "<li>" + H("marks each file it optimizes with a tag inside the file (the optimizer’s version), so a file copied or synced to another system isn’t done again there") + "</li></ul>";
  // Sync Cover Art: cover art is set when a file is first optimized; this brings optimized files in line with the choice and Plex now
  function optCovers(key, c, sm, op) {
    if (!c.mkv || c.images === "compress") return "";
    var qCov = queuedFor("optimize", { library: key, mode: "covers" }), j = ST.job, runs = !!(j && j.running && j.kind === "optimize" && j.library === key && j.mode === "covers");
    return '<div class="optacts">' + (qCov || runs ? badge("info", qCov ? T("Cover art sync queued") : T("Syncing cover art"))
      : '<button class="btn" type="button" data-act="optimize-covers" data-key="' + esc(key) + '"' + (!op.configured || !sm || !sm.mkv_done ? " disabled" : "") + ">" +
        (fileBusy() ? H("Queue Cover Art Sync") : H("Sync Cover Art")) + "</button>") + info(SYNC_COVER_HELP, T("Sync Cover Art")) + "</div>";
  }
  // the ticks and Optimize Selected, as every tool's Apply: the readiness lights first (room, and the backup plan), what it
  // does, then All — every file of the library to optimize, on every page — and This page
  function optApply(key, sm, c, op, d, cards) {
    var u = TOOL.optimizer, can = op.configured ? (d.paths || []) : [];
    if (!can.length) return "";
    var n = can.filter(function (p) { return u.sel[p]; }).length, all = n === can.length;
    var page = cards.filter(function (x) { return x.sev.live && !x.sev.blocked; }).map(function (x) { return x.id; }), pageAll = page.length && page.every(function (p) { return u.sel[p]; });
    var q = queuedFor("optimize", { library: key, mode: "optimize" }), j = ST.job, runs = !!(j && j.running && j.kind === "optimize" && j.library === key && j.mode === "optimize");
    var gate = q || runs ? { ready: true, html: "" } : readinessPanel(key, "largest", "optimizer", "", 0);
    var out = gate.html + applyBox({ does: H("Each ticked file is written again in its place: its metadata laid out and its tags and cover art as chosen above (MKV files), in the dataset’s record size and compression. Plex sees the same files."),
      changes: H("The ticked files in this library’s folders. Plex records are left as they are.") }, gate);
    out += '<div class="row" style="align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px">' +
      '<label class="det"><input type="checkbox" data-act="opt-all" data-key="' + esc(key) + '"' + (all ? " checked" : "") + "> " + H("All {n} (every page)", { n: fmtNum(can.length) }) + "</label>" +
      (can.length > page.length ? '<label class="det"><input type="checkbox" data-act="opt-page" data-key="' + esc(key) + '"' + (pageAll ? " checked" : "") + "> " + H("This page") + "</label>" : "");
    if (q || runs) return out + badge("info", q ? T("Queued") : T("Running")) + "</div>";
    if (OPT_CONFIRM === key && n) {
      out += '<span class="det" style="max-width:420px">' + H("Optimizing before changing the ZFS settings means each file is written now and again after the change. Change the settings first to write each file only once.") + "</span>" +
        '<button class="btn pri" type="button" data-act="optimize-sel" data-key="' + esc(key) + '" data-confirmed="1">' + H("Optimize Anyway") + "</button>" +
        '<button class="btn" type="button" data-act="optimize-cancel">' + H("Cancel") + "</button>";
    } else {
      out += '<button class="btn pri" type="button" data-act="optimize-sel" data-key="' + esc(key) + '"' + (!n || !gate.ready ? " disabled" : "") + ">" +
        (fileBusy() ? H("Add {n} Selected to the Queue", { n: fmtNum(n) }) : H("Optimize {n} Selected", { n: fmtNum(n) })) + "</button>" +
        (!gate.ready && n ? ' <span class="det">' + H("Optimize opens once both lights above are green or grey.") + "</span>" : "");
    }
    var lj = ST.job;
    if (lj && lj.kind === "optimize" && !lj.running && lj.library === key && lj.finished)
      out += '<span class="det muted">' + H("Last run: {optimized} optimized, {rewritten} written again, {skipped} skipped, {failed} failed", lj) + "</span>";
    return out + "</div>";
  }
  // the library tabs, as every tool's (toolLibs): each library with its count of files to optimize
  function optLibTabs(libs) {
    var u = TOOL.optimizer;
    if (!u.key || !libs.some(function (l) { return String(l.key) === String(u.key); })) u.key = libs.length ? String(libs[0].key) : null;
    return libTabs({ tool: "optimizer", libs: libs, key: u.key, attrs: function (l) { return 'data-act="tool-lib" data-tool="optimizer" data-key="' + esc(l.key) + '"'; } });
  }
  // Scan / Scan Again, as every tool's: every library's files looked at again now (the optimizer also does it by itself)
  function optScanBar(op, more) {
    return '<div class="row" style="align-items:center;gap:12px;flex-wrap:wrap"><button class="btn" type="button" data-act="optimizer-survey"' + (op.surveying ? " disabled" : "") + ">" +
      (op.surveying ? H("Scanning…") : op.surveyed_at ? H("Scan Again") : H("Scan")) + "</button>" +
      (op.surveyed_at && !op.surveying ? '<span class="det">' + H("Scanned {when}", { when: ago(op.surveyed_at) }) + "</span>" : "") + (more || "") + "</div>" +
      (op.surveying ? loadingBar(T("Looking at every library’s files…"), op.survey_progress) : "");
  }
  // The Optimizer page, the standard tool page (toolPage's layout): the library tabs; the library's choices on top; Scan
  // Again; what the last scan found; its files' cards a page at a time — those that can be done first — and Optimize Selected
  function pageOptimizer() {
    var op = ST.optimizer || {}, sums = op.libraries || {};
    // The optimizer's version (written into every file it optimizes): the running service's, and this app's if they differ
    var svcV = (op.service || {}).version, appV = op.version;
    var ver = svcV ? H("Optimizer version {version}", { version: svcV }) + (appV && String(svcV) !== String(appV) ? " " + H("(this app expects {version})", { version: appV }) : "")
      : appV ? (op.configured ? H("Optimizer version {version} (not running)", { version: appV }) : H("Optimizer version {version} (service to set up below)", { version: appV })) : "";
    var out = top(T("Optimizer"), toolBlurb("optimizer") + (ver ? '<br><span class="muted">' + ver + "</span>" : ""), "");
    out += '<div class="stack">' + toolsNav("optimizer") + optimizerSetup(op) + '<section class="card">' +
      toolHead("optimizer", T("Optimizer"), OPT_SYNC_HELP + "<br><br>" + OPT_MKV_HELP + H("Sorting the metadata") + ": " + SORT_HELP + "<br><br>" +
        H("Each library has its choices; under them, a card for each file there’s something to do to: the file as it is now, and as optimizing leaves it. Tick the files (All ticks every file of the library, on every page), then Optimize Selected."));
    var libs = ((ST.library || {}).libraries || []).filter(function (l) { return l.kind && !l.excluded; });
    if (!libs.length) {
      // Right after a start the libraries haven't been read from Plex yet: say so, rather than "no libraries"
      var lib0 = ST.library || {}, plexSet = S.plex && S.plex.url && S.plex.token_set;
      var why = lib0.error ? esc(lib0.error) :
        (lib0.scanning || plexSet) ? H("Reading your libraries from Plex…") : H("No libraries yet. Connect Plex in <a href=\"/system\" data-nav>System</a>.");
      return out + '<p class="muted">' + why + "</p></section></div>";
    }
    var tabs = optLibTabs(libs);
    var key = String(TOOL.optimizer.key), l = libs.filter(function (x) { return String(x.key) === key; })[0], c = optChoices(key), cnt = countOf("optimizer", key);
    var sm = countDone(cnt) ? sums[key] : null;  // (what the scan found, once the library's count is done: until then the placeholder)
    var some = sm && (c.sync || c.mkv), nf = sm ? sm.files || 0 : null, conf = sm ? (sm.conflicts || []).length : 0;
    out += toolTop({ tabs: tabs, options: optOptions(l, key, c, sm, op), scan: optScanBar(op, optCovers(key, c, sm, op)),
      stats: sm && { last: !!op.surveying, read: nf, readText: P(nf, "{n} file read", "{n} files read", { n: fmtNum(nf) }), issues: some ? sm.todo || 0 : null,
        passNow: some ? nf - (sm.todo || 0) : null, passAll: sm.clean_all,
        extra: conf ? [statChip("grey", P(conf, "{n} Syncthing conflict copy", "{n} Syncthing conflict copies", { n: fmtNum(conf) }), T("Copies the folder sync made when two systems wrote a file at once: optimizing the whole library (All) deletes the ones the optimizer made that are the same as the file."))] : [] } });
    if (!c.sync && !c.mkv) return out + '<p class="muted toolempty">' + badge("info", T("Nothing chosen")) + " " + H("Tick what the optimizer should do for this library above.") + "</p></section>" + jobCard(true) + queueCard() + "</div>";
    if (!sm) return out + (countDone(cnt) ? loadingBar(T("Looking at this library’s files…")) : countBox(cnt)) + "</section>" + jobCard(true) + queueCard() + "</div>";
    if (!sm.todo) return out + '<p class="muted toolempty">' + badge("pass", T("Nothing to optimize")) + " " + H("Every file is optimized as chosen.") + "</p></section>" + jobCard(true) + queueCard() + "</div>";
    var d = optFiles(key, sm.todo);
    if (!d && optState(key).error) return out + '<p class="bad">' + H("The files to optimize couldn’t be read: {why}", { why: esc(optState(key).error) }) +
      ' <button class="btn sm" type="button" data-act="opt-retry" data-key="' + esc(key) + '">' + H("Try Again") + "</button></p></section>" + jobCard(true) + queueCard() + "</div>";
    if (!d) return out + loadingBar(T("Reading the files to optimize…")) + "</section>" + jobCard(true) + queueCard() + "</div>";
    // the cards: those that can be done first, then those stopped (the optimizer not set up: all of them)
    var cards = (d.files || []).map(function (f) { var x = optFileCard(f, sm, c, op); x.sev = rowSeverity(cmpStatuses(x)); return x; }).filter(function (x) { return x.sev.shown; });
    cards = cards.filter(function (x) { return !x.sev.blocked; }).concat(cards.filter(function (x) { return x.sev.blocked; }));
    var pg = pager(d, d.offset, cardsPer(), 'data-act="opt-page-go" data-key="' + esc(key) + '"');
    out += pg + cards.map(cmpRow).join("") + pg + optApply(key, sm, c, op, d, cards);
    return out + "</section>" + jobCard(true) + queueCard() + "</div>";
  }

  // ------------------------------------------------------------ a folder into its own dataset
  // The steps (migrating.py): Create → Copy → Switch → Verify. Each step's checks come from the
  // server; this shows them, what to do (shell and TrueNAS web UI), and the buttons.
  var MIG_CONFIRM_DELETE = false;
  // A word for the shell, quoted so nothing in it (a folder named with $ or quotes) can run as a command
  function shq(v) { return "'" + String(v).replace(/'/g, "'\\''") + "'"; }
  function cmdBox(cmd) {
    return '<div class="cmd"><code style="white-space:pre-wrap">' + esc(cmd) + '</code><button class="btn" data-copy="' + esc(cmd) + '">' + H("Copy") + "</button></div>";
  }
  // A check's badge: done; a step to do; a warning (said, stopping nothing); an error (something wrong that stops the step: red)
  function migBadge(c) {
    if (c.ok) return badge("pass", T("Done"));
    if (c.level === "error") return badge("fail", T("Error"));
    return badge("warn", c.level === "warn" ? T("Warning") : T("To do"));
  }
  // done, or only a warning: what lets the step go ahead (migrating.py: blocking)
  function migClear(c) { return c.ok || c.level === "warn"; }
  function migChecks(checks) {
    // each one's button with it, but for the one on Next, above (shown for every step but the copy)
    var next = (((ST.migration || {}).state) || {}).step === "copy" ? null : (checks || []).filter(function (c) { return !migClear(c); })[0];
    return (checks || []).map(function (c) {
      return '<div class="row">' + migBadge(c) + '<div class="grow"><div class="what">' + esc(c.title) + "</div>" +
        (c.detail ? '<div class="det">' + links(c.detail) + "</div>" : "") + (c.cmd ? cmdBox(c.cmd) : "") +
        (c !== next && (c.action === "scope" || !migClear(c)) ? migAction(c) : "") + "</div></div>";
    }).join("");
  }
  // What to do now: the first thing still to do (the list below it says the rest). Checked again by itself.
  function migNext(checks) {
    var first = (checks || []).filter(function (c) { return !migClear(c); })[0];
    if (!first) return checks && checks.length ? '<div class="note">' + H("<strong>Ready.</strong> Everything below is done.") + "</div>" : "";
    var at = (ST.migration || {}).checked_at;
    return '<div class="note"><strong>' + H("Next:") + "</strong> " + esc(first.title) + (first.detail ? '<div class="det">' + links(first.detail) + "</div>" : "") +
      (first.cmd ? cmdBox(first.cmd) : "") + migAction(first) +
      '<div class="det muted">' + (at ? H("Looked at {when}: not done yet. Plex Cache looks again by itself every 30 seconds, and this moves on once it is.", { when: ago(at) })
        : H("Plex Cache looks by itself every 30 seconds, and this moves on once it’s done.")) + "</div></div>";
  }
  // a check the app can put right itself: its button
  function migAction(c) {
    var b = function (mig, label, pri, extra) {
      return '<button class="btn' + (pri ? " pri" : "") + '" type="button" data-act="mig" data-mig="' + mig + '"' + (extra || "") + ">" + label + "</button> ";
    };
    var row = function (html) { return '<div style="margin-top:6px">' + html + "</div>"; };
    if (c.action === "restart") return row(b("restart", H("Restart Now"), true));
    // what you've done where Plex Cache can't see it
    if (c.action === "sync_paused") return row(b("sync_paused", H("I’ve Paused the Folder Sync Here"), true));
    if (c.action === "sync_resumed") return row(b("sync_resumed", H("I’ve Resumed the Folder Sync Here"), true));
    if (c.action === "sync_checked") return row(b("sync_checked", H("Nothing Else Carries Them Away"), true));
    // both servers, or this one only (chosen before the copy)
    if (c.action === "scope") {
      var both = (((ST.migration || {}).state) || {}).scope === "both";
      return row(both ? b("scope", H("Convert This Server Only"), false, ' data-scope="this"') : b("scope", H("Convert Both Servers"), false, ' data-scope="both"'));
    }
    return "";
  }
  // The mirror's light, at the top (migrating.py mirror_view): grey, none (this server alone); green, its Plex Cache
  // talking directly; yellow, heard of only through its marker, or not at all — with how the conversion goes with it
  // — and, while the library's state isn't chosen, Mark as Mirror (a library found that looks like one) or No Mirror, the
  // Mirror page's choice: one or the other is needed before the copy (migrating.py mirror_chosen)
  function migMirror(mv) {
    if (!mv) return "";
    var acts = !mv.watched && mv.library ? '<div class="acts" style="margin:4px 0 0 26px">' +
      (mv.how === "suspected" && mv.id ? '<button class="btn sm pri" type="button" data-act="mirror-set" data-key="' + esc(mv.library) + '" data-state="mirror" data-server="' + esc(mv.id) + '" data-their="' + esc(mv.their_key || "") + '">' + H("Mark {name} as the Mirror", { name: mv.name }) + "</button> " : "") +
      '<button class="btn sm" type="button" data-act="mirror-set" data-key="' + esc(mv.library) + '" data-state="none">' + H("No Mirror for This Library") + "</button></div>" : "";
    return '<div class="lights">' + light(mv.status, mv.title, mv.detail) + acts + "</div>";
  }
  // a library's mirror chosen (plex2.choose: its server reached first when it isn't yet — a refusal said in a toast); a
  // conversion under way looks at its checks again at once
  function mirrorChoose(key, state, server, their) {
    return api("/api/plex2/choose", { key: key, state: state, server: server || "", their_key: their || "" }).then(migRecheck).then(load).catch(function (e) { toast(e.message, true); load(); });
  }
  // a mirror chosen or ignored: a conversion under way looks at its checks again at once, not in 30 seconds
  function migRecheck() {
    return ((ST.migration || {}).state || {}).step ? api("/api/migration", { action: "check" }).catch(function () {}) : null;
  }
  function rsOr(n) { return n ? fmtRs(n) : "?"; }
  function migStep(step) { return { create: T("Create"), copy: T("Copy"), switch: T("Switch"), verify: T("Verify") }[step] || step || ""; }
  // a folder as it is at the other site (migrating.py folders): a dataset, a folder, a conversion under way or left partway
  function migThere(r) {
    if (!r) return '<span class="muted">' + H("Not known") + "</span>";
    if (r.state === "dataset") return badge("pass", T("Child dataset")) + (r.rs ? ' <span class="muted">' + H("record size {size}", { size: rsOr(r.rs) }) + "</span>" : "");
    if (r.state === "converting" || r.state === "partial") return badge("warn", r.state === "partial" ? T("Partly converted") : T("Converting")) + ' <span class="muted">' + esc(migStep(r.step)) + "</span>";
    if (r.state === "folder") return '<span class="muted">' + H("A folder") + "</span>";
    return '<span class="muted">' + H("Can’t become one") + "</span>";
  }
  // Converting to MKV (tools/convert.py, optimize/convert.py, converting.py): which kinds, and the converted files on their
  // way into Plex. Each kind: [key, name, what it is, what could be lost, how converting handles it] — for its ⓘ
  var CONVERT_TYPES = [
    ["avi", N("AVI"), N("Old DivX and Xvid files. MKV keeps every frame’s timing better and adds a proper index."),
      [N("DivX subtitles (XSUB): MKVToolNix can’t carry them."), N("The file’s own notes (comments, software) beyond a title.")],
      N("A file with either is left as it is. Video and audio are checked frame for frame (packet counts) after converting: if a count differs — DivX “packed” frames, say — the original is kept.")],
    ["ts", N("MPEG-TS / M2TS"), N("TV recordings (Plex DVR’s too) and Blu-ray rips. MKV adds an index (faster seeking) and drops the 2–5% transport overhead."),
      [N("Teletext subtitles."), N("Data tracks: the programme guide, ad-break markers, timed metadata."), N("Other channels in a recording of a whole multiplex.")],
      N("A file with any of these is left as it is. Kept and checked: every audio track, DVB and PGS subtitles, and closed captions inside the video.")],
    ["ps", N("MPEG-PS (VOB, MPG)"), N("DVD video."),
      [N("DVD subtitles inside a VOB: MKVToolNix doesn’t read them from there."), N("Menus and navigation — they’re in the DVD’s IFO files, not in the video file, and aren’t touched.")],
      N("A file with DVD subtitles is left as it is.")],
    ["flv", N("FLV"), N("Flash video, from old downloads."),
      [N("Its cue points and details (the encoder’s notes): nothing you watch.")],
      N("Technical details are dropped; anything beyond them (a title is kept) leaves the file as it is.")],
    ["ogm", N("OGM / OGV"), N("Ogg video."),
      [N("Its comments beyond a title (artist, description…)."), N("Chapters MKVToolNix doesn’t read.")],
      N("A file with either is left as it is; chapters it reads become MKV chapters.")],
    ["rm", N("RealMedia (RM, RMVB)"), N("Old RealPlayer files."),
      [N("RealText subtitles."), N("Its title, author and copyright details beyond the title.")],
      N("A file with either is left as it is.")],
    ["3gp", N("3GP"), N("Old phone video."),
      [N("The styling of its text subtitles (the text is kept, as SRT)."), N("The phone’s details: where and when it was filmed.")],
      N("Subtitles become SRT; a file with location or date details is left as it is.")],
    ["mp4", N("MP4 / M4V"), N("Apple’s TV app, iTunes and some TVs play MP4 directly — Plex plays MKV everywhere, repackaging it on the fly where needed, at next to no cost."),
      [N("iTunes details: show, season, episode, description, rating, HD flag."), N("Cover art in the file."), N("The styling of text subtitles (the text is kept, as SRT)."),
       N("A separate closed-caption track."), N("Store-bought (DRM) files can’t be read at all."), N("iTunes itself loses track of files in a library it manages.")],
      N("A file with iTunes details, cover art, a caption track or DRM is left as it is. Kept: its title, chapters (as MKV chapters) and every audio track.")],
    ["mov", N("MOV (QuickTime)"), N("Camera and editing files."),
      [N("The timecode track: dropped — nothing you watch."), N("Camera details: where and when it was filmed, make and model."), N("Trims made without re-encoding (edit lists) — kept as far as MKV can; worth a look after.")],
      N("A file with camera details is left as it is; the timecode track is dropped.")]];
  var CONVERT_HELP = H("Each file is inspected first: what MKVToolNix reads of it against everything in it. If anything wouldn’t be kept — a track, chapters, cover art, details like an iTunes show and episode — or it’s protected by DRM, it’s left as it is, with the reason on its row, and isn’t tried again while it stays as it is. Converted, the MKV is checked against the original (every video and audio track with the same number of packets, every subtitle track, captions in the video), then optimized like any MKV.") +
    "<br><br>" + H("<strong>Plex keeps its metadata.</strong> The MKV is written next to the original, and Plex is asked to scan that folder (needed when libraries are scanned by hand). Plex adds it as a second version of the same item; once it has — on the library’s mirror too, where it has one (<a href=\"/mirrors\" data-nav>Mirror</a>) — the original is deleted and the folder scanned again: the item keeps its edits, watch history and collections. If Plex makes the MKV an item of its own instead, nothing is deleted: merge the two in Plex, or keep both.") +
    "<br><br>" + H("<strong>The way back.</strong> The run backs up first, like every tool that changes files; while the backup lasts, Revert on the run removes the MKVs and puts back any original that has gone, and the run is on the <a href=\"/processes\" data-nav>Processes page</a> like any other.");
  // Where each Plex server is with a converted file: this one, and the second one if it's watched
  function serversLine(e) {
    var sv = e.servers || {};
    if (!sv.there) return "";
    var words = { same: T("has it"), other: T("an item of its own"), absent: T("doesn’t have this file"), waiting: T("waiting"),
      unpaired: T("paths don’t line up (Mirror → Second Plex Server)") };
    return '<div class="det">' + esc(T("This server: {here} · {name}: {there}", { here: words[sv.here] || sv.here, name: (ST.plex2 || {}).name || T("the other server"), there: words[sv.there] || sv.there })) + "</div>";
  }
  // Each kind's ⓘ on the Convert to MKV page's options (the standard block draws the tool's options; this is the help it shows)
  var TOOL_OPTION_HELP = { convert: {} };
  CONVERT_TYPES.forEach(function (t) {
    TOOL_OPTION_HELP.convert[t[0]] = "<strong>" + H("What it is") + "</strong><br>" + H(t[2]) + "<br><br><strong>" + H("What MKV wouldn’t keep") + "</strong><ul>" + t[3].map(function (x) { return "<li>" + H(x) + "</li>"; }).join("") + "</ul>" +
      "<strong>" + H("How converting handles it") + "</strong><br>" + H(t[4]);
  });
  // The converted files on their way into Plex, under the tool's runs: where Plex is with each, and the originals gone
  function convertWaiting(t) {
    var items = (t || {}).conversions || [];
    if (!items.length) return "";
    var state = function (e) {
      if (e.state === "waiting") return badge("info", T("Waiting for Plex")) + '<div class="det">' + H("Its folder was scanned; Plex hasn’t added the MKV yet.") + "</div>" + serversLine(e);
      if (e.state === "seen") return badge("pass", T("In Plex")) + '<div class="det">' + H("A version of the same item: the original goes next.") + "</div>";
      if (e.state === "separate") return badge("warn", T("A separate item")) + '<div class="det">' + H("Plex added the MKV as an item of its own. Merge the two in Plex (select both → Merge) and this carries on — or keep both files.") + "</div>" + serversLine(e);
      if (e.state === "missing") return badge("warn", T("MKV missing")) + '<div class="det">' + H("The MKV isn’t there any more: the original stays.") + "</div>";
      if (e.state === "deleted") return badge("pass", T("Done")) + '<div class="det">' + H("Original deleted {when}.", { when: fmtWhen(e.deleted) }) + "</div>";
      return esc(e.state);
    };
    var act = function (e, a, label) {
      return '<button class="btn sm" type="button" data-act="conv" data-conv="' + a + '" data-orig="' + esc(e.original) + '">' + label + "</button> ";
    };
    return '<section class="card" id="conversions">' + heading(esc(T("Converted Files in Plex")),
      H("Each converted file, and where Plex is with it: the original is deleted only once Plex has the MKV as a version of the same item — on the library’s mirror too, where it has one. <strong>Check Again</strong> asks Plex now; <strong>Keep Both</strong> stops following the file, both staying; <strong>Delete Original Now</strong> is there when Plex has the MKV but the original couldn’t go yet (it was playing, say).")) +
      '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">' + H("Converted") + '</th><th scope="col">' + H("Now") + '</th><th scope="col"></th></tr></thead><tbody>' +
      items.map(function (e) {
        return "<tr><td>" + esc(e.title) + '<div class="det mono">' + esc(e.original.split("/").pop()) + " → " + esc(e.new.split("/").pop()) + "</div></td><td>" + state(e) +
          (e.error ? '<div class="det bad">' + esc(e.error) + "</div>" : "") + (e.note ? '<div class="det muted">' + esc(e.note) + "</div>" : "") + "</td><td>" +
          (e.state === "seen" ? act(e, "delete", H("Delete Original Now")) : "") +
          (e.state === "waiting" || e.state === "separate" ? act(e, "check", H("Check Again")) : "") +
          (e.state !== "deleted" ? act(e, "keep", H("Keep Both")) : "") + "</td></tr>";
      }).join("") + "</tbody></table></div></section>";
  }
  // The folder copy (or the last pass) as it runs, inside the Convert Folder card: its progress, Pause, Cancel
  function migProgress() {
    var job = ST.job;
    if (!job || job.kind !== "copy" || !job.running) return '<p class="help">' + H("Starting…") + "</p>";
    return '<div class="migprog"><div class="what">' + (job.mirror ? H("The last pass: copying what changed, then comparing") : H("Copying")) + "</div>" +
      jobProgress(job, jobFailures(job)) + '<div style="margin-top:8px">' + jobButtons(job) + "</div></div>";
  }
  function migrationCard() {
    var mg = ST.migration || {}, st = mg.state, checks = mg.checks || [];
    // (one heading, its explanation behind one ⓘ: an ⓘ inside another can't be opened)
    var out = '<section class="card" id="child-dataset">' + heading(esc(T("Convert Folder to Child Dataset")),
      H("A record size suits some media better than others, and a dataset has just one. A library folder that needs a different one than the rest of its dataset can become a dataset of its own — a child dataset, at the same path, with the record size that suits it.") +
      "<br><br>" + H("<strong>Ideal already</strong>: its dataset has the record size recommended for it. <strong>Sufficient</strong>: the second best — fine as it is. <strong>Split dataset</strong>: neither; converting the folder lets it read in the size that suits it.") +
      "<br><br>" + H("How: a new dataset is made next to the folder, the files are copied into it (which also gives them its record size and compression), and it takes the folder’s place. Plex keeps every file’s metadata, and is restarted once, at the end. You’ll need room for a second copy until the old one is deleted; snapshots keep the old copy’s space until they expire."));
    out += migMirror(mg.mirror);
    if (!st || st.step === "done") {
      if (st && st.step === "done") {
        var mvd = mg.mirror && mg.mirror.watched ? mg.mirror : null, thereD = mvd && st.scope === "both" ? (mvd.folders || {})[st.folder] : null;
        out += '<div class="note">' + H("{folder} is now the dataset {name}.", { folder: st.host_folder || st.folder, name: st.child }) +
          (mvd && st.scope === "both" ? " " + (thereD && thereD.state === "dataset" ? H("{name} has converted its copy too: both servers are done.", { name: mvd.name })
            : H("Now it’s {name}’s turn: open its Convert Folder to Child Dataset page and start this library there.", { name: mvd.name }) +
              (mvd.url ? ' <a href="' + esc(mvd.url) + '" target="_blank" rel="noopener">' + H("Open {name}", { name: mvd.name }) + "</a>" : "")) : "") +
          ' <button class="link" type="button" data-act="mig" data-mig="cancel">' + H("Close") + "</button></div>";
      }
      var cands = mg.candidates || [], dc = countOf("dataset");
      if (dc.s !== "done") return out + countBox(dc) + "</section>";  // (what each folder would become: once the datasets are looked at — or why it doesn't apply)
      if (!cands.length) return out + '<p class="muted">' + (librariesPending() ? H("Reading your libraries from Plex…") : H("No library folders yet.")) + "</p></section>";
      var mv = mg.mirror && mg.mirror.watched ? mg.mirror : null;  // with a mirror watched: each folder as it is there (migrating.py mirror_view)
      out += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">' + H("Library folder") + '</th><th scope="col">' + H("Would become") +
        '</th><th scope="col">' + H("Size") + "</th>" + (mv ? '<th scope="col">' + esc(mv.name) + "</th>" : "") + '<th scope="col"></th></tr></thead><tbody>';
      cands.forEach(function (c) {
        var advice = { ideal: badge("pass", T("Ideal already")), sufficient: badge("pass", T("Sufficient")), split: badge("warn", T("Split dataset")) }[c.advice];
        out += "<tr><td>" + libIcon(c.library) + esc(c.title) + '<div class="det mono">' + esc(c.folder) + "</div>" +
          (advice ? '<div class="det">' + advice + (c.now_rs ? ' <span class="muted">' + H("record size {size} now", { size: rsOr(c.now_rs) }) + "</span>" : "") + "</div>" : "") +
          "</td><td>" + (c.child ? '<span class="mono">' + esc(c.child) + '</span><div class="det">' + H("Record size {size}, ZSTD-3", { size: rsOr(c.rs) }) + "</div>" : '<span class="muted">' + esc(c.why || "") + "</span>") +
          (c.partial ? '<div class="det">' + badge("warn", T("Partly converted")) + " " + H("Left at {step}: picked up where it is.", { step: migStep(c.partial.step) }) + "</div>" : "") +
          (c.leftover ? '<div class="det">' + badge("warn", T("Left over")) + " " + H("The new dataset {name} is still there beside it: delete it in TrueNAS.", { name: c.leftover.split("/").pop() }) + "</div>" : "") +
          '</td><td class="nw">' + esc(fmtGB(c.size)) + "</td>" + (mv ? "<td>" + migThere((mv.folders || {})[c.folder]) + "</td>" : "") +
          "<td>" + (c.child ? '<button class="btn" type="button" data-act="mig" data-mig="plan" data-lib="' + esc(c.library) +
          '" data-folder="' + esc(c.folder) + '">' + (c.partial ? H("Pick Up This Conversion") : H("Start")) + "</button>" : "") + "</td></tr>";
      });
      return out + "</tbody></table></div></section>";
    }
    var steps = [["create", T("Create the dataset")], ["copy", T("Copy")], ["switch", T("Switch")], ["verify", T("Verify")]];
    var at = steps.map(function (s) { return s[0]; }).indexOf(st.step);
    // what changes: the folder (as TrueNAS sees it) becomes a dataset of its own — nothing moves to another place
    out += '<div class="det"><strong>' + esc(st.title) + "</strong>: " + H("the folder {folder} becomes the dataset {dataset}. Its path stays the same, so Plex and this app see the same files.",
      { folder: st.host_folder || st.folder, dataset: st.child }) + "</div>";
    out += '<div class="det" style="margin:8px 0">' + steps.map(function (s, i) {
      return (i < at ? "✓ " : i === at ? "▶ " : "") + (i === at ? "<strong>" + esc(s[1]) + "</strong>" : esc(s[1]));
    }).join(" &nbsp;→&nbsp; ") + "</div>";
    if (st.corrected) out += '<div class="note">' + esc(st.corrected) + "</div>";
    if (st.step !== "copy") out += migNext(checks);
    var optBusy = !!((ST.job || {}).running);  // one file job at a time
    var btn = function (act, label, pri, disabled) {
      var blocked = optBusy && ["copy", "catchup", "pass_then_move", "rename_old", "undo_rename", "delete_old", "ask_delete"].indexOf(act) >= 0;
      return '<button class="btn' + (pri ? " pri" : "") + '" type="button" data-act="mig" data-mig="' + act + '"' + (disabled || blocked ? " disabled" : "") + ">" + label + "</button> ";
    };
    var temp = st.temp, rs = rsOr(st.rs), owner = st.owner || {};
    var made = checks.some(function (c) { return c.id === "dataset" && c.ok; });
    if (st.step === "create" && made) {  // created: what's left is in Next and the list
      out += '<p class="help">' + H("✓ The dataset {name} is created.", { name: temp }) + "</p>";
      out += migChecks(checks);
      var ready0 = checks.length && checks.every(migClear);
      var gateM = readinessPanel(String(st.library), 0, "dataset", T("the old folder itself: set aside once the copy is checked, deleted only when you say"), 0);
      out += gateM.html + '<div style="margin-top:10px">' + btn("check", H("Check Again")) + btn("copy", H("Start Copying"), true, !ready0 || !gateM.ready) + btn("cancel", H("Cancel")) + "</div>";
    } else if (st.step === "create") {
      out += '<p class="help">' + H("Create the new dataset next to the folder, with these settings. It’s checked before anything is copied.") + "</p>";
      out += '<div class="fix"><strong>' + H("In the TrueNAS shell") + "</strong>" + cmdBox("sudo zfs create -o recordsize=" + rs + " -o compression=zstd " + shq(temp) + "\n" +
        "sudo chown " + (owner.uid != null ? Number(owner.uid) + ":" + Number(owner.gid) : "568:568") + " " + shq(st.temp_host)) + "</div>";
      out += '<div class="fix"><strong>' + H("Or in the TrueNAS web UI") + "</strong><ol style=\"margin:4px 0 0 18px\">" +
        "<li>" + H("Datasets → select {parent} → Add Dataset.", { parent: st.dataset }) + "</li>" +
        "<li>" + H("Name: {name}. Under Advanced Options: Record Size {size}, Compression Level ZSTD. Save.", { name: temp.split("/").pop(), size: rs }) + "</li>" +
        "<li>" + H("Select it → Edit Permissions: owner {uid}, group {gid} (the folder’s). Save.", { uid: owner.uid != null ? owner.uid : 568, gid: owner.gid != null ? owner.gid : 568 }) + "</li></ol></div>";
      out += migChecks(checks);
      var ready = checks.length && checks.every(migClear);
      out += '<div style="margin-top:10px">' + btn("check", H("Check Again")) + btn("copy", H("Start Copying"), true, !ready) + btn("cancel", H("Cancel")) + "</div>";
    } else if (st.step === "copy" || (st.step === "switch" && st.copying)) {
      // the copy runs as the file job: its progress, Pause and Cancel are in the card above
      if (st.copying && st.queued) out += '<p class="help">' + H("Waiting in the queue: it starts when the file jobs before it are done.") + "</p>" +
        '<div style="margin-top:10px"><button class="btn" type="button" data-act="queue-remove" data-id="' + esc((queuedFor("copy", { folder: st.folder }) || {}).id || "") + '">' +
        H("Remove from the Queue") + "</button></div>";
      else if (st.copying) out += migProgress();
      else out += '<div class="det">' + H("Stopped: {why}", { why: (st.copy || {}).reason || (st.copy || {}).result || "" }) + "</div>" +
        '<div style="margin-top:10px">' + btn("copy", H("Carry On Copying"), true) + btn("cancel", H("Cancel")) + "</div>";
    } else if (st.step === "switch") {
      // the last pass ran by itself after the copy; it runs again, always, just before the old folder is set aside
      var cmp = st.compare, lp = st.last_pass || {}, cat = st.compare_at || (st.copy || {}).finished;
      var aside = "." + (st.folder || "").split("/").pop() + ".plexcache-old";
      var passLine = st.copying ? migProgress()
        : !cmp ? '<div class="det">' + H("It runs by itself once the copy is done.") + "</div>"
        : '<div class="det">' + (cmp.same ? badge("pass", T("Both the same")) + " " + H("{files} files, {size}, compared {when}. {copied} copied by the pass (changed since the copy), {removed} removed.",
            { files: fmtNum(cmp.files || 0), size: fmtGB(cmp.bytes || 0), when: ago(cat), copied: fmtNum(lp.copied || 0), removed: fmtNum(lp.removed || 0) })
          : badge("warn", T("Different")) + " " + H("Compared {when}: {list}", { when: ago(cat), list: (cmp.missing || []).concat(cmp.different || [], cmp.extra || []).slice(0, 5).join(", ") })) + "</div>";
      var stepRow = function (btns, text) { return '<li><div class="migstep">' + (btns ? '<div class="migbtns">' + btns + "</div>" : "") + '<div class="migtext">' + text + "</div></div></li>"; };
      out += '<ol class="migsteps">' +
        stepRow(st.copying ? "" : btn("catchup", H("Copy Changes &amp; Compare")),
          H("<strong>The last pass</strong>: copies anything that changed in the folder since the copy, then compares both copies, file by file (names, sizes, modification times). It ran by itself when the copy finished.") + passLine) +
        stepRow("", H("In Plex, turn off <strong>Empty trash automatically after every scan</strong> (Settings → Library) until the end, so files briefly out of sight don’t lose their metadata.")) +
        stepRow(st.old_path ? btn("undo_rename", H("Put the Old Folder Back"))
            : btn("pass_then_move", H("Compare, Then Set the Old Folder Aside"), true, st.copying || !(cmp && cmp.same) ||
                checks.some(function (c) { return c.gate === "aside" && !migClear(c); })),
          (st.old_path ? badge("pass", T("Set aside")) + " " + H("As {name}, in the same place. Nothing is deleted until the very end (after Verify). Plex Cache’s other file jobs wait until the dataset is renamed.", { name: st.old_path.split("/").pop() })
            : H("<strong>Set the old folder aside</strong>: the new dataset needs the folder’s name, so the folder is renamed to {name}, in the same place. Nothing is deleted — that’s at the very end, after Verify — and Put Back undoes it.", { name: aside }) +
              '<div class="det">' + H("The last pass runs first, every time: anything changed since is copied and both copies are compared, and only if they’re the same is the folder set aside. Plex Cache’s other file jobs wait until the dataset is renamed. Pause anything else that writes to this folder first: downloads, Sonarr or Readarr, Syncthing.") + "</div>") +
          (st.move_error ? '<div class="det bad">' + esc(st.move_error) + "</div>" : "")) +
        stepRow("", H("<strong>Rename the new dataset</strong> to the folder’s name, in the TrueNAS shell:") + cmdBox("sudo zfs rename " + shq(temp) + " " + shq(st.child)) +
          '<div class="det">' + H("Or in the TrueNAS web UI, if your version offers it: Datasets → select {name} → Rename.", { name: temp.split("/").pop() }) + "</div>") +
        "</ol>" + migChecks(checks) + '<div style="margin-top:10px">' + btn("check", H("Check Again")) + (st.old_path ? "" : btn("cancel", H("Cancel"))) + "</div>";
    } else if (st.step === "verify") {
      out += '<p class="help">' + H("Now restart Plex (TrueNAS: Apps → Plex → Restart) so it sees the dataset, and let it scan. Then check again.") + "</p>" + migChecks(checks);
      var ok = checks.length && checks.every(migClear);
      out += '<div style="margin-top:10px">' + btn("check", H("Check Again"));
      if (MIG_CONFIRM_DELETE) out += '<span class="det">' + H("Delete {path} for good?", { path: (st.old_path || "").split("/").pop() }) + "</span> " + btn("delete_old", H("Delete"), true) +
        '<button class="link" type="button" data-act="mig-nodelete">' + H("Cancel") + "</button>";
      else out += btn("ask_delete", H("Delete the Old Copy"), true, !ok);
      out += "</div>" + '<p class="help">' + H("Then turn Plex’s Empty trash automatically after every scan back on, if you use it.") + "</p>";
    }
    return out + "</section>";
  }
  function fmtGB(n) { return ((n || 0) / 1e9).toLocaleString(undefined, { maximumFractionDigits: 1 }) + " GB"; }

  // ------------------------------------------------------------ processes
  // Every file job (jobs.py, joblog.py): the one running now, with its controls, and the log — each
  // job with its resume points and pauses, and its files. The log is fetched on its own.
  var PROC = null, procAt = 0, procOpen = null, procFiles = {}, procOffset = {}, procSort = {}, procChart = {};  // procChart: "job|file" -> its chart shown
  function procFetch(force) {
    if (!force && PROC && Date.now() - procAt < 10000) return;
    procAt = Date.now();
    api("/api/processes").then(function (d) { PROC = d; if ((page === "processes" || TOOL_PAGES[page]) && !editing()) render(); }).catch(function () { procAt = 0; });
    if (procOpen) procFilesFetch(procOpen, procOffset[procOpen] || 0);  // the open list follows too: files done, retries' results
  }
  // A retry starting or ending: the lists again straight away (not on the next round)
  var retrySig = "";
  function retriesChanged(st) {
    var sig = (st.retries || []).map(function (r) { return r.file + (r.active ? "*" : ""); }).join("|");
    if (sig !== retrySig && page === "processes") procFetch(true);
    retrySig = sig;
  }
  function procFilesFetch(id, offset) {
    api("/api/processes/files?job=" + encodeURIComponent(id) + "&offset=" + (offset || 0) + "&limit=" + perPage() + (procSort[id] ? "&sort=" + encodeURIComponent(procSort[id]) : "")).then(function (d) {
      procFiles[id] = d; procOffset[id] = offset || 0; if (page === "processes") render();
    });
  }
  // The pages of a long list, each a span of what it holds ("A–Be"): the one open is plain text, the others open on a click.
  // At most PAGER_MAX spans around the open one; past them, Beginning and End reach the rest. Every paged list uses this.
  var PAGER_MAX = 12;
  // the libraries aren't read from Plex yet (the first scan, or one running with nothing listed before): a page
  // waits with "Reading…" rather than saying there are none
  function librariesPending() { var l = (ST || {}).library || {}; return !((l.stats || {}).scanned_at) && !l.error; }
  function perPage() { return (S && S.general && S.general.page_size) || 200; }  // (System → Preferences)
  // @testable: plainTitle — a title without the quote, apostrophe or bracket it opens with: how every list sorts and
  // the page spans read (the server's copy: paging.plain)
  function plainTitle(label) { return String(label == null ? "" : label).replace(/^[^\p{L}\p{N}_]+/u, ""); }
  // @end
  // @testable: pageRanges — the spans of a list the page holds whole: as the server works them out (plexcache/paging.py)
  function pageRanges(labels, per) {
    var MAXP = 3, out = [];
    var plain = function (l) { return String(l == null ? "" : l).replace(/^[^\p{L}\p{N}_]+/u, ""); };  // (plainTitle, held here so the function stands alone)
    var common = function (a, b) { a = a.toLowerCase(); b = b.toLowerCase(); var n = 0; while (n < a.length && n < b.length && a[n] === b[n]) n++; return n; };
    var cut = function (l, n) { var o = l.slice(0, Math.max(1, Math.min(n, MAXP))).replace(/\s+$/, ""); return o || l.slice(0, 1) || "…"; };
    var pages = [];
    for (var off = 0; off < labels.length; off += per) pages.push({ offset: off, first: plain(labels[off]), last: plain(labels[Math.min(off + per, labels.length) - 1]) });
    pages.forEach(function (p, i) {
      var kf = i ? common(pages[i - 1].last, p.first) + 1 : 1, kt = i + 1 < pages.length ? common(p.last, pages[i + 1].first) + 1 : 1;
      out.push({ offset: p.offset, from: cut(p.first, kf), to: cut(p.last, kt) });
    });
    return out;
  }
  // @end
  function pager(d, offset, perPage, act) {
    var pages = d.pages || [], total = d.total || 0;
    if (!pages.length || total <= perPage) return "";
    var cur = 0;
    pages.forEach(function (p, i) { if (p.offset <= offset) cur = i; });
    var lo = Math.max(0, cur - Math.floor(PAGER_MAX / 2)), hi = Math.min(pages.length, lo + PAGER_MAX);
    lo = Math.max(0, hi - PAGER_MAX);
    var chip = function (p, i) {
      var label = p.from === p.to ? p.from : p.from + "–" + p.to;
      return i === cur ? '<span class="pg cur" aria-current="page">' + esc(label) + "</span>"
        : '<button class="link pg" type="button" ' + act + ' data-offset="' + p.offset + '" title="' + esc(T("Items {from}–{to}", { from: fmtNum(p.offset + 1), to: fmtNum(Math.min(p.offset + perPage, total)) })) + '">' + esc(label) + "</button>";
    };
    return '<div class="pager det"><span class="pgcount">' + H("{from}–{to} of {total}", { from: fmtNum(offset + 1), to: fmtNum(Math.min(offset + perPage, total)), total: fmtNum(total) }) + "</span>" +
      (lo > 0 ? '<button class="link pg" type="button" ' + act + ' data-offset="0">' + H("Beginning") + "</button><span class=\"pgdots\">…</span>" : "") +
      pages.slice(lo, hi).map(function (p, k) { return chip(p, lo + k); }).join("") +
      (hi < pages.length ? '<span class="pgdots">…</span><button class="link pg" type="button" ' + act + ' data-offset="' + pages[pages.length - 1].offset + '">' + H("End") + "</button>" : "") + "</div>";
  }
  function fmtWhen(t) { return t ? new Date(t * 1000).toLocaleString(LOCALE, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"; }
  function fmtBytesShort(n) {
    if (n == null) return "—";
    if (n < 1000) return T("{n} B", { n: fmtNum(n) });
    return n >= 1e9 ? dec(n / 1e9, 2) + " GB" : n >= 1e6 ? dec(n / 1e6, 1) + " MB" : fmtNum(Math.round(n / 1e3)) + " KB";
  }
  function fmtSecs(t) {
    if (t == null) return "—";
    if (t < 60) return T("{n} s", { n: Math.round(t) });
    if (t < 3600) return T("{m} min {s} s", { m: Math.floor(t / 60), s: Math.round(t % 60) });
    return T("{h} h {m} min", { h: Math.floor(t / 3600), m: Math.round((t % 3600) / 60) });
  }
  function procEvent(ev) {
    var at = fmtWhen(ev.at), where = ev.total ? T("{done} of {total}", { done: fmtNum(ev.done || 0), total: fmtNum(ev.total) }) : "";
    var name = { started: T("Started"), resumed: T("Resumed after a restart, at {where}", { where: where || "0" }), paused: T("Paused: {why}", { why: ev.why || "" }),
      unpaused: T("Carried on"), finished: T("Finished"), cancelled: T("Cancelled"),
      backup: ev.backup && ev.backup.kind === "snapshot" ? T("Snapshot taken first: {names} (kept until {until})", { names: (ev.backup.names || []).join(", "), until: fmtWhen(ev.backup.until) })
        : ev.backup && ev.backup.kind === "copies" ? T("The copies the tool keeps are the backup (kept until {until})", { until: fmtWhen(ev.backup.until) })
        : ev.backup && ev.backup.kind === "records" ? T("Plex records backed up first: {n} (kept until {until})", { n: fmtNum((ev.backup.plex || {}).records || 0), until: fmtWhen(ev.backup.until) }) : T("No backup taken, as chosen in Preferences"),
      backup_failed: T("Stopped before anything changed: the backup couldn’t be made — {why}", { why: msg(ev.why || "") }),
      reverted: T("Reverted: {ok} put back, {failed} not", { ok: (ev.counts || {}).reverted || 0, failed: (ev.counts || {}).failed || 0 }),
      rolled_back: T("Rolled back: the whole dataset as it was at the snapshot"),
      revert_failed: T("Revert failed: nothing put back ({failed} not) — the files say why", { failed: (ev.counts || {}).failed || 0 }) }[ev.event] || ev.event;
    if (ev.event === "backup" && ev.backup && ev.backup.plex && ev.backup.kind !== "records") {  // (a file tool whose rows change Plex's records too)
      name += " · " + T("Plex records backed up first: {n} (kept until {until})", { n: fmtNum(ev.backup.plex.records || 0), until: fmtWhen(ev.backup.until) });
    }
    return "<li>" + esc(at) + " — " + esc(name) + "</li>";
  }
  function pageProcesses() {
    procFetch();
    var out = top(T("Processes"), H("File jobs — optimizing, syncing cover art, converting a folder to a child dataset — one at a time: the one running now, the ones queued after it, and what each did, file by file."), "");
    out += '<div class="stack">';
    var cur = jobCard();
    out += cur || '<section class="card">' + heading(esc(T("Running Now")), "") + '<p class="muted">' + H("No file job is running. Start one on the Optimizer page.") + "</p></section>";
    out += queueCard();
    out += '<section class="card">' + heading(esc(T("History")), H("Kept for good. Open a job to see its files."));
    if (!PROC) return out + '<p class="muted">' + H("Loading…") + "</p></section></div>";
    var jobs = PROC.jobs || [];
    if (!jobs.length) return out + '<p class="muted">' + H("Nothing yet.") + "</p></section></div>";
    jobs.forEach(function (j) { out += runCard(j); });
    // one export, under the history it's of: every job and file, for a spreadsheet
    return out + '</section><div><a class="btn" href="/api/processes/export?format=csv" download>' + H("Export CSV") + "</a></div>" + plexBackupsCard() + "</div>";
  }
  // The backups of Plex's records (plexbackup.py), newest first: what each was for, its records, how long it's kept — and
  // Revert while it lasts: merges split apart, each record given its own metadata back
  function plexBackupsCard() {
    var bs = (PROC || {}).plex_backups || [];
    if (!bs.length) return "";
    return '<section class="card">' + heading(esc(T("Plex Metadata Backups")), H("Made before any change to Plex’s records — a merge, an edit, a match, a poster — per the Backup preference in <a href=\"/system#prefs\" data-nav>System → Preferences</a>, and kept as long as every backup. Revert puts the records back: merges split apart, each record with its own metadata again.")) +
      bs.map(function (b) {
        var status = b.reverted ? badge("info", T("Reverted")) : b.revertable ? badge("pass", T("Backup")) : badge("warn", T("Expired"));
        return '<div class="row runcard">' + status + '<div class="grow"><div class="what">' + esc(msg(b.what)) + "</div>" +
          '<div class="det">' + esc(P(b.records, "{n} record on {server}", "{n} records on {server}", { n: fmtNum(b.records), server: b.server_name || "" })) + (b.merges ? " · " + esc(P(b.merges, "{n} merge", "{n} merges", { n: fmtNum(b.merges) })) : "") +
          " · " + esc(fmtWhen(b.at)) + " · " + esc(T("Kept until {until}", { until: fmtWhen(b.until) })) + "</div>" +
          ((b.titles || []).length ? '<div class="det muted">' + esc(b.titles.join(", ")) + (b.records > b.titles.length ? "…" : "") + "</div>" : "") +
          (b.reverted ? '<div class="det">' + esc(T("Put back {when}: {ok} records, {failed} not", { when: fmtWhen(b.reverted_at), ok: (b.result || {}).restored || 0, failed: (b.result || {}).failed || 0 })) + "</div>" +
            (b.left || []).map(function (l) { return '<div class="det bad">' + esc(msg(l)) + "</div>"; }).join("") : "") +
          (b.revertable ? '<div class="det undo"><button class="btn sm" type="button" data-act="plex-revert" data-id="' + esc(b.id) + '">' + H("Revert Plex Changes") + "</button></div>" : "") +
          "</div></div>";
      }).join("") + "</section>";
  }
  // One run of the history (jobs.py / joblog.py), as a card: its status, when, what it did, its backup and the ways back
  // (undoButtons), its events, and its files on request. The Processes page lists every run so; a tool's page lists its own
  // (toolHistory), the one running first with its progress — the one card everywhere, so the ways back travel with it.
  function runCard(j) {
    var titleJob = { kind: j.kind, mode: j.mode, library: j.library, title: j.title, tool: j.tool, resumes: j.events.filter(function (e) { return e.event === "resumed"; }).length };
    var live = ST.job && ST.job.id === j.job ? ST.job : null, now = live && (live.running || live.waiting);
    var status = j.status === "running" ? (now ? badge("info", live.waiting ? T("Waiting") : T("Running")) : badge("warn", T("Stopped")))
      : j.reverted ? badge("info", j.undone_how === "rolled_back" ? T("Rolled back") : T("Reverted")) : { finished: badge("pass", T("Finished")), cancelled: badge("warn", T("Cancelled")), failed: badge("warn", T("Not run")) }[j.status] || "";
    var c = j.counts || {}, bk = j.backup || (live && live.backup) || null;
    var progress = "";
    if (now && live.running) {  // the run under way: its step and progress, on the card
      progress = '<div class="migprog"><div class="what">' + (live.stopping ? H("Cancelling… (after the file in hand)") : live.phase === "backup" ? H("Backing up…") : H("Applying…")) + "</div>" +
        (live.step ? '<div class="det">' + esc(live.step) + "</div>" : "") + (live.phase !== "backup" ? '<div class="det">' + H("{done} of {total}", { done: fmtNum(live.done || 0), total: fmtNum(live.total || 0) }) + (live.current ? " · " + esc(live.current) : "") + "</div>" : "") +
        '<div class="meter thin' + (live.phase === "backup" ? " busy" : "") + '" role="progressbar"><div' + (live.phase === "backup" ? "" : ' style="width:' + (live.total ? Math.round(100 * (live.done || 0) / live.total) : 0) + '%"') + "></div></div></div>";
    }
    var backupLine = !bk ? "" : bk.kind === "snapshot" ? '<div class="det">' + badge("pass", T("Backup created")) + " " + esc(T("Snapshot {names}, kept until {until}.", { names: (bk.names || []).join(", "), until: fmtWhen(bk.until) })) + "</div>"
      : bk.kind === "copies" ? '<div class="det">' + badge("pass", T("Backup")) + " " + esc(T("The copies this tool keeps, until {until}.", { until: fmtWhen(bk.until) })) + "</div>"
      : bk.kind === "records" ? ""
      : '<div class="det">' + badge("info", T("No backup")) + " " + esc(T("As chosen in Preferences: you back up yourself.")) + "</div>";
    if (bk && bk.plex) {  // the Plex records the run changes, backed up before the first row (plexbackup.py)
      backupLine += '<div class="det">' + badge("pass", T("Backup created")) + " " + esc(P(bk.plex.records || 0, "{n} Plex record as it was before, kept until {until}.", "{n} Plex records as they were before, kept until {until}.",
        { n: fmtNum(bk.plex.records || 0), until: fmtWhen(bk.until) })) + "</div>";
    }
    var failedWhy = j.status === "failed" ? '<div class="det bad">' + esc(msg((j.events.filter(function (e) { return e.event === "backup_failed"; })[0] || {}).why || "")) + "</div>" : "";
    return '<div class="row runcard" id="proc-' + esc(j.job) + '">' + status + '<div class="grow"><div class="what">' + esc(jobTitle(titleJob)) + "</div>" +
      '<div class="det">' + (j.planned > j.files ? H("{start} → {end} · {files} of {planned} files tried", { start: fmtWhen(j.started), end: j.ended ? fmtWhen(j.ended) : T("now"), files: fmtNum(j.files), planned: fmtNum(j.planned) })
        : H("{start} → {end} · {files} files logged", { start: fmtWhen(j.started), end: j.ended ? fmtWhen(j.ended) : T("now"), files: fmtNum(j.files) })) +
      (j.status === "running" && !now ? " · " + H("Stopped without finishing (the app was stopped or restarted)") : "") +
      (j.kind === "discovered" ? " · " + H("Brought here optimized differently (by Syncthing from another NAS, say): their history carries on") : "") +
      (j.status !== "running" && j.kind === "tool" ? " · " + H("{done} done, {failed} failed", { done: fmtNum(Math.max(0, (j.files || 0) - (j.failed || 0))), failed: fmtNum(j.failed || 0) }) : "") +
      (j.status !== "running" && j.kind !== "copy" && j.kind !== "discovered" && j.kind !== "tool" ? " · " + H("{optimized} optimized, {rewritten} written again, {skipped} skipped, {failed} failed",
        { optimized: c.optimized || 0, rewritten: c.rewritten || 0, skipped: c.skipped || 0, failed: c.failed || 0 }) : "") + "</div>" +
      progress + (j.kind === "tool" ? backupLine : "") + failedWhy +
      (j.revertable ? undoButtons(j.job, j.backup) : "") +
      procTally(j.tally) +
      (j.events.length > 2 ? '<ul class="help">' + j.events.map(procEvent).join("") + "</ul>" :
        '<ul class="help">' + j.events.filter(function (e) { return e.event !== "started"; }).map(procEvent).join("") + "</ul>") +
      '<div class="det"><button class="link" type="button" data-act="proc-open" data-job="' + esc(j.job) + '">' + (procOpen === j.job ? H("Hide files") : H("Show files")) + "</button></div>" +
      "</div></div>" + (procOpen === j.job ? '<div class="procfiles">' + procFilesTable(j.job) + "</div>" : "");
  }
  // A tool's run, file by file: the common columns — poster, the item as Plex names it, when, the result as a badge — then the
  // tool's own (its `history`, sent as the columns), each value as logged (a number, a size, a text)
  function toolFilesTable(id, d) {
    var cols = d.columns || [];
    var res = { done: badge("pass", T("Done")), failed: badge("fail", T("Failed")), reverted: badge("info", T("Reverted")), revert_failed: badge("warn", T("Not reverted")),
      waiting: badge("info", T("Waiting")), "not done": badge("info", T("Not done")), cancelled: badge("warn", T("Cancelled")), pending: badge("info", T("Waiting")) };
    // the general rule for every run: the item in hand says Processing — or Paused while the run is — whatever the log has for it yet
    var live = ST.job && ST.job.id === id && ST.job.running ? ST.job : null;
    var inHand = function (f) { return live && live.current && (live.current === f.label || live.current === f.title || live.current_file === f.file); };
    var cell = function (cid, v) {
      if (v == null || v === "") return "—";
      if (typeof v === "number") return /written|frees|freed|bytes|size/.test(cid) ? fmtBytesShort(v) : fmtNum(v);
      return esc(msg(v));
    };
    var out = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col"><button class="link hidelist" type="button" data-act="proc-open" data-job="' + esc(id) +
      '" title="' + esc(T("Hide files")) + '">▲ ' + H("Hide") + "</button></th><th scope=\"col\">" + H("Item") + '</th><th scope="col">' + H("Result") + "</th>" +
      cols.map(function (c) { return '<th scope="col">' + esc(msg(c.name)) + "</th>"; }).join("") + "</tr></thead><tbody>";
    d.files.forEach(function (f) {
      var h = f.history || {};
      out += "<tr><td>" + posterImg(f.poster) + '</td><td style="min-width:240px">' + fileTitle(f.label || f.title, f.file) +
        (f.reason && (f.result === "failed" || f.result === "revert_failed") ? '<div class="det bad">' + esc(sentence(f.reason)) + "</div>" : "") +
        (f.at ? '<div class="det muted">' + esc(fmtWhen(f.reverted_at || f.at)) + "</div>" : "") + "</td><td>" + (inHand(f) ? badge("info", live.paused || live.paused_by ? T("Paused") : T("Processing")) : res[f.result] || esc(f.result || "")) + "</td>" +
        cols.map(function (c) { return "<td>" + cell(c.id, h[c.id]) + "</td>"; }).join("") + "</tr>";
    });
    return out + "</tbody></table></div>";
  }
  // The process under way, as the Processes page shows it (its progress, Pause and Cancel), on a tool's page while it's this
  // tool's: above its history, so the page reads checks, Apply, process, history
  function toolProcess(name) {
    var j = ST.job;
    return j && j.kind === "tool" && j.tool === name && (j.running || j.waiting) ? jobCard() : "";
  }
  // A tool's own runs, at the bottom of its page: the history's cards for this tool, the one under way first. Every tool page
  // has it (toolPage), so the ways back travel with the card.
  function toolHistory(name) {
    procFetch();
    var u = TOOL[name];
    var mine = ((PROC || {}).jobs || []).filter(function (j) { return j.kind === "tool" && j.tool === name; });
    var live = ST.job && ST.job.kind === "tool" && ST.job.tool === name && (ST.job.running || ST.job.waiting) ? ST.job : null;
    var queued = (ST.queue || []).some(function (q) { return q.kind === "tool" && (q.args || {}).tool === name; });
    if (live && !mine.some(function (j) { return j.job === live.id; })) {  // (just started: not in the log's listing yet)
      mine.unshift({ job: live.id, kind: "tool", tool: name, library: live.library, title: live.title, started: live.started, ended: null, status: "running", events: [], counts: {}, files: 0, failed: 0, planned: live.total || 0, backup: live.backup || null });
    }
    if (live && u.openLatest) { procOpen = live.id; u.openLatest = false; procFilesFetch(live.id, 0); }  // (a run just started: its files open, every other run's hidden)
    if (!mine.length) return "";
    var open = u.history !== undefined ? u.history : !!(live || queued);  // (shown by itself while a run goes, or is about to; else on request)
    var toggle = '<button class="link" type="button" data-act="tool-history" data-tool="' + name + '">' + (open ? H("Hide history") : esc(P(mine.length, "Show history ({n} run)", "Show history ({n} runs)", { n: fmtNum(mine.length) }))) + "</button>";
    if (!open) return '<section class="card toolhistory"><div class="det">' + toggle + "</div></section>";
    return '<section class="card toolhistory">' + heading(esc(T("History")), H("This tool’s runs: each with its backup and the ways back while it lasts. Every run is on the <a href=\"/processes\" data-nav>Processes page</a> too."), '<div class="det">' + toggle + "</div>") +
      mine.slice(0, 20).map(runCard).join("") + "</section>";
  }
  // A job's running tally, as its files are done: what was put right, the space saved, the posters
  function procTally(t) {
    t = t || {};
    if (!t.written) return "";
    var mb = function (n) { return (n < 0 ? "+" : "") + fmtBytesShort(Math.abs(n)); };
    var bits = [P(t.written, "{n} file written", "{n} files written", { n: fmtNum(t.written) })];
    if (t.rs_fixed) bits.push(P(t.rs_fixed, "{n} now at the dataset’s record size", "{n} now at the dataset’s record size", { n: fmtNum(t.rs_fixed) }));
    if (t.compression_set) bits.push(P(t.compression_set, "{n} now with the dataset’s compression", "{n} now with the dataset’s compression", { n: fmtNum(t.compression_set) }));
    if (t.disk_saved) bits.push(T("{size} saved on disk", { size: mb(t.disk_saved) }));
    if (t.size_saved) bits.push(T("files {size} smaller", { size: mb(t.size_saved) }));
    if (t.posters_removed) bits.push(P(t.posters_removed, "{n} poster removed", "{n} posters removed", { n: fmtNum(t.posters_removed) }));
    if (t.posters_added) bits.push(P(t.posters_added, "{n} Plex poster added", "{n} Plex posters added", { n: fmtNum(t.posters_added) }));
    if (t.posters_saved) bits.push(T("posters compressed: {size} saved", { size: fmtBytesShort(t.posters_saved) }));
    return '<div class="det tally">' + esc(bits.join(" · ")) + "</div>";
  }
  // A poster that can't be fetched (Plex unreachable, no such poster) is hidden, not shown broken. One
  // listener for the page: the page's security policy forbids inline handlers (onerror="…").
  document.addEventListener("error", function (e) {
    var t = e.target;
    if (t && t.tagName === "IMG" && t.hasAttribute("data-hide-broken")) t.style.visibility = "hidden";
  }, true);
  // A library's icon, like Plex's own (film, TV, music, photos), by its key or its name; nothing if it's not a library
  var LIB_ICONS = {
    movie: '<path d="M3 2h10v12H3z"/><path d="M5 2v12M11 2v12M3 5h2M3 8h2M3 11h2M11 5h2M11 8h2M11 11h2"/>',
    show: '<path d="M2 3h12v8H2z"/><path d="M5 14h6M8 11v3"/>',
    artist: '<path d="M6 12V3l7-1.5v9"/><circle cx="4.5" cy="12" r="1.8"/><circle cx="11.5" cy="10.5" r="1.8"/><path d="M6 5.5l7-1.5"/>',
    photo: '<path d="M2 4.5h3l1-1.5h4l1 1.5h3v8.5H2z"/><circle cx="8" cy="8.5" r="2.3"/>'
  };
  function libIcon(k) {
    var l = ((ST.library || {}).libraries || []).filter(function (x) { return String(x.key) === String(k) || x.title === k; })[0];
    var svg = l && LIB_ICONS[l.type];
    return svg ? '<svg class="libicon" viewBox="0 0 16 16" aria-hidden="true">' + svg + "</svg>" : "";
  }
  // A message as a sentence: a capital to start, a full stop to end
  function sentence(x) {
    x = String(x || "").trim();
    if (!x) return x;
    x = x.charAt(0).toUpperCase() + x.slice(1);
    return /[.!?…)]$/.test(x) ? x : x + ".";
  }
  function procFilesTable(id) {
    var d = procFiles[id];
    if (!d) return '<p class="muted">' + H("Loading…") + "</p>";
    if (!d.files.length) return '<p class="muted">' + H("No files logged.") + "</p>";
    if (d.kind === "tool") return toolFilesTable(id, d);
    // a header sorts by its column (Result: failed at the top; Size: most smaller first, then the biggest); again, back to the order done
    var th = function (key, label) {
      var on = procSort[id] === key || (!procSort[id] && key === "file");
      return '<th scope="col"' + (on ? ' aria-sort="ascending"' : "") + '><button class="link sorter" type="button" data-act="proc-sort" data-job="' + esc(id) + '" data-key="' + key + '">' +
        label + (on ? " ▾" : "") + "</button></th>";
    };
    var rs = function (n) { return n ? fmtRs(n) : "—"; };
    var comp = function (c) { return !c || c === "unknown" ? "?" : String(c).toUpperCase(); };  // "?" until the optimizer wrote it
    // the header stays at the top while the list scrolls by, with a button to hide the list
    var out = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col"><button class="link hidelist" type="button" data-act="proc-open" data-job="' + esc(id) +
      '" title="' + esc(T("Hide files")) + '">▲ ' + H("Hide") + "</button></th>" + th("file", H("File")) + th("result", H("Result")) + th("time", H("Time")) +
      th("size", H("Size")) + th("verified", H("Verified")) + th("rs", H("Record size")) + th("compression", H("ZFS compression")) + th("optimized", H("Optimized")) +
      th("cover", H("Cover art")) + "</tr></thead><tbody>";
    // a failed file can be tried again, after the file in hand: waiting for that, or being tried now
    var queuedFiles = {}, nowFiles = {};
    (ST.retries || []).forEach(function (r) { (r.active ? nowFiles : queuedFiles)[r.file] = true; });
    // by title (the default): each title's (each edition's) files grouped under one poster and title
    var byTitle = !procSort[id] || procSort[id] === "file", key = function (x) { return x.label || x.file; };
    var job = ST.job && ST.job.running ? ST.job : null;
    d.files.forEach(function (f, i) {
      var first = !byTitle || i === 0 || key(d.files[i - 1]) !== key(f);
      var span = 1;
      while (byTitle && first && i + span < d.files.length && key(d.files[i + span]) === key(f)) span++;
      var rows = span;  // the poster's cell spans the title's files, and the charts shown under them
      for (var c = i; first && c < i + span; c++) if (d.files[c].views && procChart[id + "|" + d.files[c].file]) rows++;
      // the file being optimized now: Optimizing, and how far through it is
      var now = job && job.current_file === f.file;
      var retry = f.result !== "failed" || f.kind !== "optimize" ? "" : (nowFiles[f.file] || queuedFiles[f.file] ? '<button class="btn sm" type="button" disabled>' + H("Retrying") + "</button>"
        : '<button class="btn sm" type="button" data-act="proc-retry" data-job="' + esc(id) + '" data-file="' + esc(f.file) + '">' + H("Retry") + "</button>");
      var res = { optimized: badge("pass", T("Optimized")), discovered: badge("info", T("Found optimized")), converted: badge("pass", T("Converted to MKV")), rewritten: badge("pass", T("Written again")), already: badge("info", T("Already done")),
        copied: badge("pass", T("Copied")), skipped: badge("info", T("Skipped")), failed: badge("fail", T("Failed")),
        waiting: badge("info", T("Waiting")), "not done": badge("info", T("Not done")), cancelled: badge("warn", T("Cancelled")) }[f.result] || esc(f.result || "");
      var tried = f.result !== "waiting" && f.result !== "not done" && f.result !== "cancelled";  // not tried yet: just the first line (the file as it is)
      if (now) {
        res = badge("info", T("Optimizing"));
        tried = false;
        if (job.file_pct != null) retry = '<div class="meter thin" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' +
          Math.round(job.file_pct) + '" aria-label="' + esc(T("This file")) + '"><div style="width:' + Math.min(100, job.file_pct) + '%"></div></div>';
      }
      // each column: the file as it was on the first line, as written on the second (and a third: what changed)
      var wrote = !now && (f.result === "optimized" || f.result === "rewritten" || f.result === "converted" || f.result === "discovered");
      var ln = function (x, cls) { return '<div class="ln' + (cls ? " " + cls : "") + '">' + (x == null || x === "" ? "&nbsp;" : x) + "</div>"; };
      var pct = function (b, a) {  // how much smaller (−) or larger (+), to 3 decimals; nothing when that rounds to 0
        if (a == null || !b) return null;
        var v = Math.round((a - b) / b * 100000) / 1000;
        return v ? '<span class="' + (v < 0 ? "good" : "muted") + '">' + esc((v < 0 ? "−" : "+") + dec(Math.abs(v), 3) + "%") + "</span>" : null;
      };
      var thumb = posterImg(f.poster);
      var size = ln(esc(fmtBytesShort(f.size_before))) + (wrote ? ln(esc(fmtBytesShort(f.size_after))) + (pct(f.size_before, f.size_after) ? ln(pct(f.size_before, f.size_after)) : "") : "");
      var zc = ln(esc(comp(f.compression_before))) + (wrote && f.compression_after ? ln(esc(comp(f.compression_after))) : "");
      var onDisk = wrote && f.disk_before != null && f.disk_after != null && f.disk_after !== f.disk_before ? f.disk_after - f.disk_before : null;
      if (onDisk != null) zc += ln('<span class="' + (onDisk < 0 ? "good" : "muted") + '">' + esc(T("{size} on disk", { size: (onDisk < 0 ? "−" : "+") + fmtBytesShort(Math.abs(onDisk)) })) + "</span>");
      // the optimizer's version: the one already in the file (grey), and the one that just laid it out (green)
      var v = function (x) { return "v" + String(x).replace(/^v/, ""); };
      var opt = ln(f.version_before ? '<span class="muted">' + esc(v(f.version_before)) + "</span>" : "—") +
        (f.layout ? ln(f.version ? '<span class="good">' + esc(v(f.version)) + "</span>" : esc(T("Yes"))) : "");
      // cover art: the images it had; then what happened (removed, Plex's poster added, compressed: how much smaller)
      var cov1 = f.images_before == null ? "" : f.images_before ? esc(P(f.images_before, "{n} image", "{n} images", { n: f.images_before })) : esc(T("None"));
      var cov2 = f.images_before == null ? (f.cover ? esc(f.cover) : null)
        : f.poster_added ? esc(T("Plex poster added"))
        : (f.images_after || 0) < f.images_before ? esc(f.images_after ? T("{n} removed", { n: f.images_before - f.images_after }) : T("Removed"))
        : f.images_saved ? '<span class="good">' + esc(T("Compressed, −{size}", { size: fmtBytesShort(f.images_saved) })) + "</span>"
        : f.cover ? esc(f.cover) : null;
      var cover = ln(cov1) + (cov2 ? ln(cov2) : "");
      // a title with several files: its poster and title once, then each file on its own line
      var name = span > 1 || (byTitle && !first) ? (first ? '<div class="what">' + esc(f.label) + "</div>" : "") + '<div class="det mono">' + esc((f.file || "").split("/").pop()) + "</div>"
        : fileTitle(f.label, f.file);
      var more = byTitle && i + 1 < d.files.length && key(d.files[i + 1]) === key(f);  // the title's next file follows
      var chart = f.views && procChart[id + "|" + f.file];
      out += '<tr' + (more || chart ? ' class="ingrp"' : "") + ">" + (first ? "<td" + (rows > 1 ? ' rowspan="' + rows + '"' : "") + ">" + thumb + "</td>" : "") +
        '<td style="min-width:240px">' + name + (f.reason && !now && f.result !== "cancelled" ? '<div class="det bad">' + esc(sentence(f.reason)) + "</div>" : "") +
        (f.at ? '<div class="det muted">' + esc(fmtWhen(f.at)) + "</div>" : "") +
        (f.views ? '<div class="det"><button class="link" type="button" data-act="proc-chart" data-key="' + esc(id + "|" + f.file) + '" aria-expanded="' + !!procChart[id + "|" + f.file] + '">' +
          (procChart[id + "|" + f.file] ? H("Hide before and after") : H("Before and after")) + "</button></div>" : "") + "</td><td>" + ln(res) + (retry ? '<div class="ln">' + retry + "</div>" : "") +
        '</td><td class="nw">' + (tried ? ln("") + ln(esc(fmtSecs(f.seconds))) : "") +
        '</td><td class="nw">' + size +
        "</td><td>" + (!tried ? "" : ln("") + ln(esc({ passed: T("Passed"), failed: T("Failed"), skipped: T("Not checked"), "not needed": T("Not needed") }[f.verified] || "—"))) +
        '</td><td class="nw">' + ln(esc(rs(f.rs_before))) + (wrote && f.rs_after ? ln(esc(rs(f.rs_after))) : "") +
        '</td><td class="nw">' + zc + '</td><td class="nw">' + opt + '</td><td class="nw">' + cover + "</td></tr>";
      // what caching keeps of the file just before and just after this (hidden until asked for)
      if (chart) out += '<tr class="procchart' + (more ? " ingrp" : "") + '"><td colspan="9"><div class="lmixsum">' +
        mixChart([[T("Before"), f.views.before], [T("After"), f.views.after]]) + "</div></td></tr>";
    });
    out += "</tbody></table></div>";
    out += pager(d, procOffset[id] || 0, 200, 'data-act="proc-page" data-job="' + esc(id) + '"');
    return out;
  }

  // ------------------------------------------------------------ Library Health
  // Each title's files (the files list's layout: poster and title once, a line per file), each shown
  // as the blocks caching always keeps of it: at its start, through the metadata in front of the
  // video and 60% of a block of video; at its end, every block holding metadata. Every block the
  // same size, filled in proportion with what's in it. Layouts come from Read the Files (filemap.py);
  // a file opened shows its blocks larger: click one for what's in it and how ZFS might store it.
  var LIB = { key: null, data: null, at: 0, offset: 0, q: "", open: null, detail: {}, est: {}, block: {}, hold: { inspect: {} }, finishedSeen: {}, rev: null, view: "health", health: {}, healthOffset: 0 };
  var PART_NAMES = { header: N("Matroska header"), segment: N("Segment start"), seekhead: N("Table of contents"), info: N("File info"), tracks: N("Track list"), chapters: N("Chapters"),
    attachments: N("Attachments list"), tags: N("Tags"), index: N("Index"), padding: N("Padding"), checksum: N("Checksum"), video: N("Video"),
    other: N("Other"), image: N("Cover art"), font: N("Fonts"), attachment: N("Attachment"), empty: N("Unused"), head: N("File header"),
    id3: N("ID3 tag header"), flac: N("FLAC header"), streams: N("Video & audio") };
  // The parts every Matroska file starts with, a player reads before anything else: one colour, "File header"
  var HEAD_PARTS = { header: 1, segment: 1, seekhead: 1, info: 1, tracks: 1, checksum: 1, id3: 1, flac: 1 };
  // What each part is, and whether a file needs it (behind an ⓘ in a block's list)
  var PART_HELP = {
    header: N("Says the file is Matroska, and which version. Required: the first thing in every file."),
    segment: N("Where the rest of the file begins: every other part is inside it. Required."),
    seekhead: N("Where each part is in the file, so a player can go straight to it. Optional, but every muxer writes one and players rely on it."),
    info: N("The file’s duration, timing scale and title, and what wrote it. Required."),
    tracks: N("Each track’s codec, language, name and the settings its decoder needs. Required: nothing plays without it."),
    chapters: N("Chapter names and where each starts. Optional."),
    attachments: N("The attachments’ own list: a few bytes saying where they start and how much room they take. MKVToolNix doesn’t show it, as it isn’t a file. Optional, with the attachments."),
    image: N("A picture some players and TVs show for the file. Optional: Plex has its own posters."),
    font: N("A font styled (ASS) subtitles need to look right. Optional, but those subtitles need it."),
    attachment: N("Some other file carried inside this one. Optional."),
    index: N("Where each keyframe is, so a player can seek. Optional, but without it seeking is slow or impossible."),
    tags: N("Extra details — titles, encoder settings, and the optimizer’s own mark. Optional."),
    padding: N("Space left empty after the part above it, so that part can grow without rewriting the file. Optional: only zeros."),
    checksum: N("A checksum of the part it’s in. Optional."),
    video: N("The video, audio and subtitles themselves, in clusters. Required."),
    other: N("Something this page doesn’t know. Optional."),
    empty: N("Zeros past the end of the file that fill out its last block. Not part of the file: ZFS adds them and compresses them away.") };
  function libFetch(force) {
    if (!LIB.key) return;
    if (!force && LIB.data && LIB.data.library === LIB.key && Date.now() - LIB.at < 15000) return;
    LIB.at = Date.now();
    api("/api/library?key=" + encodeURIComponent(LIB.key) + "&offset=" + LIB.offset + "&limit=" + perPage() + (LIB.q ? "&q=" + encodeURIComponent(LIB.q) : ""))
      .then(function (d) {
        LIB.data = d;
        LIB.finishedSeen[d.library] = libJob(d.library).finished;  // (the inspection as it stood when these files were fetched)
        if (page === "library") libAutoDrive();  // (the files to inspect are known now)
        var q = document.activeElement && document.activeElement.id === "lib-q" ? document.activeElement : null;
        if (page === "library" && (q || !editing())) {
          var at = q ? [q.selectionStart, q.selectionEnd] : null;
          render();
          var nq = at && document.getElementById("lib-q");
          if (nq) { nq.focus(); nq.setSelectionRange(at[0], at[1]); }
        }
      }).catch(function () { LIB.at = 0; });
  }
  // What the Library page does by itself for the library opened (libAutoPlan, pure): the files inspected for Caching
  // when that's needed, and the inspection paused off its view — only the view you're on, only the library opened
  // (Health runs no job: it reads what's been inspected). Paused by hand (Pause), it stays paused until Resume, or the
  // library is opened again.
  var LIB_DRIVE = { at: 0, last: "" };
  function libAutoDrive() {
    var key = LIB.key;
    if (!key || Date.now() - LIB_DRIVE.at < 2000) return;
    var ins = libJob(key), d = LIB.data && LIB.data.library === key ? LIB.data : null;
    var acts = libAutoPlan(LIB.view, {
      inspect: { running: ins.running, paused: ins.paused, needed: !!d && d.surveyed < d.total, known: !!d },
      hold: { inspect: !!LIB.hold.inspect[key] } });
    var sig = key + ":" + acts.join(",");
    if (!acts.length || (sig === LIB_DRIVE.last && Date.now() - LIB_DRIVE.at < 15000)) return;
    LIB_DRIVE.at = Date.now(); LIB_DRIVE.last = sig;
    acts.forEach(function (a) {
      if (a === "inspect:start") api("/api/library/survey", { library: key }).then(function () { load(); }).catch(function () {});
    });
  }
  // @testable: libAutoPlan — pure: the actions for the library opened, given the view ("health" | "caching") and each
  // job's state {running, paused, needed, known} (hold: paused by hand). Returns ["inspect:start"]: the view's own job
  // started when it's needed and known. Nothing is paused off its view: the inspector reads every library in the
  // background (File Checks' counts wait for it), the library opened first (evaluate.py steers it).
  function libAutoPlan(view, st) {
    var acts = [], job = { inspect: st.inspect }, hold = st.hold || {};
    var mine = view === "caching" ? "inspect" : null;
    var wants = function (name) {  // a job that should run: needed (or paused with work left), not held
      var j = job[name];
      if (!j || hold[name]) return false;
      return !!(j.needed || j.paused);
    };
    var busy = function (name) { var j = job[name]; return !!(j && (j.running || j.queued)); };
    if (mine && job[mine] && job[mine].known && wants(mine) && !busy(mine)) acts.push(mine + ":start");
    return acts;
  }
  // @end
  // The inspection of a library: {running, paused (with files left), done, total}
  function libJob(key) {
    var j = ((ST.inspect || {}).libs || {})[key] || {};
    return { running: !!j.running, paused: !!j.paused && j.done < j.total, done: j.done || 0, total: j.total || 0, finished: j.finished || 0 };
  }
  function libDetailFetch(path) {
    api("/api/library/file?path=" + encodeURIComponent(path)).then(function (d) {
      LIB.detail[path] = d;
      // one block of each run is enough: the run's others hold the same
      var bm = d.blocks, ns = bm ? libRuns(bm.start).concat(libRuns(bm.end)).map(function (r) { return r.b.n; }).slice(0, 32) : [];
      if (ns.length) api("/api/library/blocks?path=" + encodeURIComponent(path) + "&n=" + ns.join(",")).then(function (e) {
        var by = {};
        (e.blocks || []).forEach(function (b) { by[b.n] = b; });
        LIB.est[path] = by;
        if (page === "library") render();
      });
      if (page === "library") render();
    }).catch(function (e) { LIB.detail[path] = { error: e.message }; if (page === "library") render(); });
  }
  // Blocks in a row holding only one thing, full (200 blocks of index): one block, marked ×200
  function libRuns(blocks) {
    var out = [];
    blocks.forEach(function (b) {
      var one = b.pieces.length === 1 ? b.pieces[0].kind : null, last = out[out.length - 1];
      if (one && last && last.only === one) { last.count++; last.last = b.n; return; }
      out.push({ b: b, only: one, count: 1, last: b.n });
    });
    return out;
  }
  // One block: its contents side by side, in proportion; past the end of the file, the unused rest
  // A part's name; padding is named after the part it belongs to (the one before it): "Tags padding"
  function partName(p) {
    if (p.kind === "video" && p.stream === "audio") return T("Audio");
    return p.kind === "padding" && p.of ? T("{part} padding", { part: T(PART_NAMES[p.of] || p.of) }) : T(PART_NAMES[p.kind] || p.kind);
  }
  // Its colour: the file header's parts share one; padding takes its part's
  function partClass(p) {
    var k = p.kind === "padding" && p.of ? p.of : p.kind;
    return "lp-" + (HEAD_PARTS[k] ? "head" : k);
  }
  function libBlock(b, path, big, sel, count, est) {
    var inner = b.pieces.map(function (p) {
      return '<span class="' + partClass(p) + '" style="flex:' + p.bytes + ' 0 0" title="' + esc(partName(p) + (p.name ? " · " + p.name : "") + " · " + fmtBytesShort(p.bytes)) + '"></span>';
    }).join("");
    // under a big block: how much ZFS compresses it (estimated), or its number until that's known
    var e = est && est[b.n], rs = b.data + (b.pieces.filter(function (p) { return p.kind === "empty"; })[0] || { bytes: 0 }).bytes;
    var what = e ? (e.compressed ? T("≈{pct}% compressed", { pct: Math.round(100 * (1 - e.stored / rs)) }) : T("Not compressed")) : T("Block {n}", { n: fmtNum(b.n) });
    var label = big ? '<span class="lbn">' + esc(what) + "</span>" : "";
    var times = count > 1 ? '<span class="lx">×' + esc(fmtNum(count)) + "</span>" : "";
    return '<button type="button" class="lblk' + (big ? " big" : "") + (sel ? " sel" : "") + '"' + (big ? ' data-act="lib-block" data-file="' + esc(path) + '" data-n="' + b.n + '"' : "") +
      ' aria-label="' + esc(T("Block {n}", { n: fmtNum(b.n) })) + '"><span class="lbin">' + inner + times + "</span>" + label + "</button>";
  }
  // What a file's streams are: "Video, Audio, Subs", or just "Audio"
  function streamNames(bm) {
    var names = { video: T("Video"), audio: T("Audio"), subtitles: T("Subs") };
    return (bm.streams || ["video"]).map(function (k) { return names[k] + (k === "subtitles" && bm.forced ? " " + T("({n} forced)", { n: bm.forced }) : ""); }).join(", ");
  }
  // A file's blocks: the start's, then the video (its size, not to scale), then the end's
  function libBlocks(row, big, path) {
    var bm = row.blocks;
    if (!bm) return '<span class="muted det">' + (row.missing ? H("File not found") : row.read ? H("Record size not known yet") : H("Not read yet")) + "</span>";
    var sel = (LIB.block[path] || null);
    var est = big ? LIB.est[path] : null;
    var draw = function (list) { return libRuns(list).map(function (r) { return libBlock(r.b, path, big, sel === r.b.n, r.count, est); }).join(""); };
    var start = draw(bm.start), end = draw(bm.end);
    // the video between them, not to scale (clickable when big: its tracks, and how much of it is cached)
    var vid = '<span class="lvid">' + esc(streamNames(bm)) + "</span>" +
      (bm.lead_blocks ? '<span class="muted">' + esc(P(bm.lead_blocks, "{n} lead-in block", "{n} lead-in blocks", { n: fmtNum(bm.lead_blocks) })) + "</span>" : "") +
      (big ? '<span class="muted">' + esc(T("Not compressed")) + "</span>" : "");  // (in the list, blocks don't say either: room is short)
    var gap = big ? '<button type="button" class="lgap' + (sel === "video" ? " sel" : "") + '" data-act="lib-block" data-file="' + esc(path) + '" data-n="video">' + vid + "</button>"
      : '<span class="lgap">' + vid + "</span>";
    return '<div class="lblocks' + (big ? " big" : "") + '">' + start + gap + end + "</div>";
  }
  function libLegend(after) {
    // in the order an optimized file has them: its header, the streams, then the metadata at its end
    return '<div class="llegend">' + ["head", "video", "image", "font", "chapters", "index", "tags", "other", "empty"].map(function (k) {
      return '<span><i class="lsw lp-' + k + '"></i>' + esc(T(PART_NAMES[k === "video" ? "streams" : k])) + "</span>";
    }).join("") + (after || "") + "</div>";
  }
  // Whether a picture in the file has been compressed losslessly: by the optimizer (its tag is on the
  // file), or — for a JPEG — whether its Huffman tables are the standard ones (not optimized) or its own
  function coverState(a, optimized) {
    if (optimized) return T("{name}: compressed losslessly by Plex Cache (where it helped)", { name: a.name });
    if (a.jpeg_tables === "standard") return T("{name}: not compressed losslessly (standard JPEG tables)", { name: a.name });
    if (a.jpeg_tables) return T("{name}: already compressed losslessly (its own JPEG tables)", { name: a.name });
    return T("{name}: not compressed by Plex Cache", { name: a.name });
  }
  // What the always-cached blocks take in the cache — compressed, as ZFS keeps them (each run of like
  // blocks counted from its first block's estimate)
  function cachedSize(bm, est) {
    var total = 0, guess = false, known = true;
    libRuns(bm.start).concat(libRuns(bm.end)).forEach(function (r) {
      var e = est[r.b.n];
      if (!e) known = false;
      if (e && e.compressed) guess = true;
      total += r.count * (e ? e.stored : bm.record);
    });
    if (!known) return "";
    return " · " + (guess ? T("≈{size} in the cache", { size: fmtBytesShort(total) }) : T("{size} in the cache", { size: fmtBytesShort(total) }));
  }
  var HEALTH_BADGE = { ideal: ["pass", N("Ideal")], pass: ["pass", N("Pass")], warn: ["warn", N("Warning")] };
  function healthBadge(st) { var b = HEALTH_BADGE[st]; return b ? badge(b[0], T(b[1])) : ""; }
  // How many of a file's checks are Ideal, Pass and Warning (the checks themselves are under its parts)
  function checkCounts(list) {
    if (!list || !list.length) return "";
    var by = { ideal: [], pass: [], warn: [] };
    list.forEach(function (c) { by[c.status].push(msg(c.name)); });
    return '<div class="lcount">' + H("Checks:") + ["ideal", "pass", "warn"].map(function (k) {
      return ' <span class="lcountn" title="' + esc(by[k].join(" · ")) + '">' + healthBadge(k) + " " + esc(fmtNum(by[k].length)) + "</span>";
    }).join("") + '<span class="det muted"> ' + H("— each beside what it’s about, in the blocks below") + "</span></div>";
  }
  // Each check beside what it's about: a part's (chapters, index, tags, cover art) on that part's line in a
  // block; the block size and compression on every block; the start of the file and the container — and
  // any check whose part isn't in the file (no index, say) — on the first block. Its explanation is behind ⓘ.
  var CHECK_PARTS = { chapters: ["chapters"], index: ["index"], tags: ["tags"], image: ["cover", "cover_setting"] };
  function checkMarks(checks, ids, named) {
    return (checks || []).filter(function (c) { return ids.indexOf(c.id) >= 0; }).map(function (c) {
      return '<span class="lcheck">' + (named ? '<span class="muted">' + esc(msg(c.name)) + "</span> " : "") + healthBadge(c.status) +
        info("<strong>" + esc(msg(c.name)) + "</strong>: " + richText(msg(c.text)), msg(c.name)) + "</span>";
    }).join("");
  }
  function blockChecks(d, b, shown, isStart) {
    var ids = ["blocks", "compression"];
    if (isStart) {
      ids = ["front", "container"].concat(ids);
      // a check about a part that isn't in any of the blocks shown (no index, say): here, so it isn't lost
      var kinds = {};
      shown.forEach(function (x) { x.pieces.forEach(function (p) { kinds[p.kind] = true; }); });
      Object.keys(CHECK_PARTS).forEach(function (k) { if (!kinds[k]) ids = ids.concat(CHECK_PARTS[k]); });
    }
    return checkMarks(d.checks, ids, true);
  }
  // The library's files through their checks: how many are Ideal, Pass or Warning, and what warns most
  function libSummary(sm) {
    if (!sm || !sm.checked) return "";
    // the files checked, then how many are Ideal, Pass and Warning: chips with the numbers in them, as the tools' pages say it
    var out = '<div class="lsummary">' + libStats({ read: sm.checked, readText: P(sm.checked, "{n} file read", "{n} files read", { n: fmtNum(sm.checked) }),
      extra: ["ideal", "pass", "warn"].map(function (k) { var b = HEALTH_BADGE[k], n = sm[k] || 0; return statChip(k === "warn" && !n ? "pass" : b[0], T(b[1]) + " " + fmtNum(n)); }) });
    if ((sm.warnings || []).length) out += '<div class="det">' + H("Warnings:") + "<ul>" + sm.warnings.map(function (w) { return "<li>" + esc(msg(w.text)) + "</li>"; }).join("") + "</ul></div>";
    if (sm.cover && sm.cover.files) out += '<div class="det">' + esc(T("Cover art compressed losslessly: {pct}% of the {n} files with cover art",
      { pct: Math.round(100 * sm.cover.compressed / sm.cover.files), n: fmtNum(sm.cover.files) })) + "</div>";
    var sv = sm.views || {};
    // the library as it is now; underneath, once optimized (est.) — only while some of it is still to optimize
    // (the optimized files, first seen against now, are on the Metrics page)
    if (sv.all && sm.mix) {
      var after = sv.after && sm.projected;
      out += '<div class="lmixsum"><div class="what">' + (after ? H("Always cached, all {n} inspected files: existing, and optimized", { n: fmtNum(sm.mix.files) }) +
        info(PROJ_HELP, T("If optimized")) : H("Always cached, all {n} inspected files, as they are now", { n: fmtNum(sm.mix.files) })) + "</div>" +
        (after ? '<div class="det muted">' + esc(P(sm.projected.files, "{n} of them still to optimize", "{n} of them still to optimize", { n: fmtNum(sm.projected.files) })) + "</div>" : "") +
        mixChart(after ? [[T("Existing"), sv.all, ROW_TIPS.existing], [T("Optimized"), sv.after, sv.rewrite ? ROW_TIPS.rewrite : ROW_TIPS.optimized]]
          : [[T("Existing"), sv.all, ROW_TIPS.existing]]) +
        (after ? blocksLine(sv.all.blocks, sv.after.blocks) : "") + "</div>";
    }
    return out + "</div>";
  }
  var PROJ_HELP = H("Estimated from what inspecting each file measured — nothing is read or changed. The optimizer takes out the metadata the library’s choices strip (cover art, tags), moves the rest to the end of the file where it compresses, and writes the file in the dataset’s record size. Plex’s poster, where it’s put in, counts at its real size once that’s known — fetched when you open a file, and learnt in the background for a library; until then the file’s own cover art stands in.");
  function sumValues(o) { return Object.keys(o || {}).reduce(function (a, k) { return a + (o[k] || 0); }, 0); }
  function blocksLine(now, after) {
    return '<div class="det lgains">' + esc(T("Blocks always cached: {now} → {after}", { now: fmtNum(now || 0), after: fmtNum(after || 0) })) + "</div>";
  }
  // What the always-cached blocks hold (filemap.cache_mix): video, metadata and unused room, as ZFS
  // stores them — a bar, then the numbers. More video and less metadata is better.
  var MIX_HELP = H("The blocks caching always keeps of each file — its start, and the metadata at its end; not the lead-in. Every bar is on one scale. Point at a part to see what’s in it.") + "<ul>" +
    "<li>" + H("<strong>Streamed</strong>: what a play reads of them — the file’s own bytes. The unused room after a file’s end, in its last block, is never read and isn’t counted.") + "</li>" +
    "<li>" + H("<strong>Meaningful</strong> (the coloured part of the line above each bar): what a play needs of it — the lead-in video, and the metadata players use (header, track list, index, chapters, subtitle fonts). The grey rest is dead weight: unused video, tags, cover art. Cached, it’s estimated: that metadata compressed as well as the rest.") + "</li>" +
    "<li>" + H("<strong>Cached</strong>: what they take in RAM and on the SSD — as ZFS stores them, compressed where that saves at least an eighth of a block. A block’s unused room counts as metadata here: it takes space when its block isn’t compressed.") + "</li>" +
    "<li>" + H("<strong>Lead-in video</strong>: the first blocks of the video — what a play starts with, and what the lead-in is for. The more, the better.") + "</li>" +
    "<li>" + H("<strong>Unused video</strong>: video that shares the last blocks with the metadata there. It’s cached with them, but it’s no use to the lead-in: dead weight.") + "</li>" +
    "<li>" + H("<strong>Metadata</strong>: needed, but the less the better.") + "</li>" +
    "<li>" + H("<strong>ZFS rounding</strong>: ZFS stores a compressed block in whole 4 KB sectors, so the rest of each block’s last sector takes space but holds nothing. Cached only.") + "</li>" +
    "<li>" + H("<strong>Taken out</strong>: what optimizing removes, as the library’s choices say (cover art, tags). It’s read and cached now like the rest — players read cover art when they open a file, wherever it is — so it’s part of today’s total; the optimized bar is that much shorter.") + "</li></ul>" +
    H("The line above each bar ends at its total. Compression is estimated (zlib standing in for ZFS’s ZSTD, which is similar on metadata).");
  // The always-cached blocks compared (projection.view's rows): the rows' names once, down the side;
  // Streamed and Cached side by side, on one scale each. Each bar: video at the start, video at the end,
  // metadata, and what optimizing takes out (now: read and cached like the rest; optimized: gone, so its
  // bar is that much shorter — one scale per column); above it, a line as long as the bar, with its total.
  // Each part says what's in it when pointed at.
  function kindLines(kinds, unit) {
    return Object.keys(kinds || {}).sort(function (a, b) { return kinds[b] - kinds[a]; }).map(function (k) {
      return "\n" + T(PART_NAMES[k] || k) + ": " + fmtBytesShort(kinds[k]) + (unit || "");
    }).join("");
  }
  // What each row of the charts is (pointed at): the file as it is, and as optimizing would leave it
  var ROW_TIPS = {
    existing: { stream: T("The existing file as it is on the disk now"), cache: T("The existing file as it is on the disk now, with zfs compression estimated") },
    optimized: T("The estimated size of the optimized file"),
    rewrite: T("The file is already optimal, but optimizing zfs storage would likely result in these changes")
  };
  function mixChart(rows) {
    rows = rows.filter(function (r) { return r[1]; });
    if (!rows.length) return "";
    var hasRound = rows.some(function (r) { return r[1].cache.round; });
    var hasOut = rows.some(function (r) { return r[1].stream.out && !r[1].gone; }), hasTail = rows.some(function (r) { return r[1].stream.tail; });
    // one scale for every bar: Streamed, Meaningful and Cached stacked, so they compare directly
    var groups = [["stream", T("Streamed")], ["cache", T("Cached")]];
    var scale = Math.max.apply(null, [1].concat.apply([1], groups.map(function (g) { return rows.map(function (r) { return r[1][g[0]].total; }); })));
    var bar = function (key, r) {
      var v = r[1], x = v[key];
      var seg = function (cls, n, tip) {
        if (!n) return "";
        var pct = 100 * n / scale;
        // its size, as much of it as fits (fitBars: the size, or just the number, or nothing)
        var full = fmtBytesShort(n);
        return '<span class="' + cls + '" style="width:' + pct.toFixed(2) + '%" title="' + esc(tip) + '" data-full="' + esc(full) + '" data-short="' +
          esc(full.replace(/[^\d.,]+$/, "")) + '">' + esc(full) + "</span>";
      };
      var inFile = key === "cache" ? T(" in the file") : "";
      var kinds = key === "meaningful" ? v.useful_kinds : v.kinds;
      var metaTip = (key === "cache" ? T("Metadata, as stored: {size}", { size: fmtBytesShort(x.meta) }) : T("Metadata: {size}", { size: fmtBytesShort(x.meta) })) +
        kindLines(kinds, inFile) + (key === "cache" && v.unused_disk ? "\n" + T("Unused room, stored: {size}", { size: fmtBytesShort(v.unused_disk) }) : "");
      var outTip = T("To be taken out by optimizing: {size}", { size: fmtBytesShort(x.out) }) + kindLines(v.out_kinds, inFile);
      var left = Object.keys(v.left_out || {}).length ? "\n" + T("Left out:") + Object.keys(v.left_out).sort(function (a, b) { return v.left_out[b] - v.left_out[a]; }).map(function (k) {
        return "\n" + (k === "unused_video" ? T("Unused video") : T(PART_NAMES[k] || k)) + ": " + fmtBytesShort(v.left_out[k]);
      }).join("") : "";
      var totalTip = key === "meaningful" ? T("Meaningful: {size} — what a play needs of what’s streamed", { size: fmtBytesShort(x.total) }) + left
        : v.gone && x.out ? T("Total: {size} — {out} less, taken out by optimizing:", { size: fmtBytesShort(x.total), out: fmtBytesShort(x.out) }) + kindLines(v.out_kinds, inFile)
        : T("Total: {size}", { size: fmtBytesShort(x.total) });
      // the line above: the meaningful part (lead-in video, then the metadata a play uses) in colour, the
      // rest in grey — "Meaningful" at the end of the colour, "Total" at the end of the line
      var useful = x.useful != null ? x.useful : x.total, w = function (n) { return (100 * n / scale).toFixed(2) + "%"; };
      var usefulTip = T("Meaningful: {size} — what a play needs: the lead-in video and the metadata players use", { size: fmtBytesShort(useful) }) + left;
      var close = (x.total - useful) / scale < 0.2;
      var line = '<div class="lcline">' +
        '<span class="lclv" style="width:' + w(x.lead) + '" title="' + esc(usefulTip) + '"></span>' +
        '<span class="lclm" style="width:' + w(x.useful_meta || 0) + '" title="' + esc(usefulTip) + '"></span>' +
        '<span class="lclr" style="width:' + w(Math.max(0, x.total - useful)) + '" title="' + esc(totalTip) + '"></span>' +
        (close ? '<em style="left:' + w(x.total) + '">' + esc(T("Meaningful: {a} · Total: {b}", { a: fmtBytesShort(useful), b: fmtBytesShort(x.total) })) + "</em>"
          : '<em style="left:' + w(useful) + '">' + esc(T("Meaningful: {size}", { size: fmtBytesShort(useful) })) + '</em><em style="left:' + w(x.total) + '">' + esc(T("Total: {size}", { size: fmtBytesShort(x.total) })) + "</em>") + "</div>";
      return '<div class="lccell">' + line +
        '<div class="lcbar">' + seg("lp-video", x.lead, (v.split ? T("Lead-in video: {size}", { size: fmtBytesShort(x.lead) }) + "\n" + T("The first blocks: what a play starts with.") : T("Video: {size}", { size: fmtBytesShort(x.lead) }))) +
        seg("lcvidend", x.tail, T("Unused video: {size}", { size: fmtBytesShort(x.tail) }) + "\n" + T("In the last blocks, with the metadata: cached with it, but no use to the lead-in.")) +
        seg("lp-index", x.meta, metaTip) + seg("lcround", x.round || 0, T("ZFS rounding: {size}", { size: fmtBytesShort(x.round || 0) }) + "\n" + T("ZFS stores a compressed block in whole 4 KB sectors: the rest of each block’s last sector holds nothing, but takes space.")) +
        (v.gone ? "" : seg("lcout", x.out, outTip)) + "</div></div>";
    };
    // Streamed, then Cached: each a heading over its rows, so the bars get the width; each row's name says
    // what it is when pointed at (r[2]: {stream, cache} — or one text for both)
    var out = '<div class="lcgrid">' + groups.map(function (g) {
      return '<div class="lcgroup">' + esc(g[1]) + "</div>" + rows.map(function (r) {
        var tip = r[2] && (typeof r[2] === "string" ? r[2] : r[2][g[0]]);
        return '<div class="lclbl"' + (tip ? ' title="' + esc(tip) + '"' : "") + ">" + esc(r[0]) + "</div>" + bar(g[0], r);
      }).join("");
    }).join('<div class="lcgap"></div>') + "</div>";
    var legend = '<div class="llegend lclegend"><span><i class="lsw lp-video"></i>' + (hasTail ? H("Lead-in video") : H("Video &amp; audio")) + "</span>" +
      (hasTail ? '<span><i class="lsw lcvidend"></i>' + H("Unused video") + "</span>" : "") + '<span><i class="lsw lp-index"></i>' + H("Metadata") + "</span>" +
      (hasRound ? '<span><i class="lsw lcround"></i>' + H("ZFS rounding") + "</span>" : "") +
      (hasOut ? '<span><i class="lsw lcout"></i>' + H("Taken out by optimizing") + "</span>" : "") + info(MIX_HELP, T("Always cached")) + "</div>";
    return out + legend;
  }
  // Each bar part's size, as much of it as fits: the size ("774 KB"), else just its number ("774"), else
  // nothing (it's in the part's tip either way) — worked out again as the page's width changes
  // the two sides of each comparison card (cmpRow): the cards one height, their buttons starting level (the titles made as tall)
  function levelSides(root) {
    (root || main).querySelectorAll(".ssitem").forEach(function (it) {
      var whats = it.querySelectorAll(".sscard .what");
      whats.forEach(function (w) { w.style.minHeight = ""; });
      var h = 0;
      whats.forEach(function (w) { h = Math.max(h, w.offsetHeight); });
      if (whats.length > 1 && window.innerWidth > 760) whats.forEach(function (w) { w.style.minHeight = h + "px"; });
    });
  }
  function fitBars(root) {
    levelSides(root);
    (root || main).querySelectorAll(".lcbar>span[data-full]").forEach(function (el) {
      var w = el.clientWidth;
      el.textContent = el.dataset.full;
      if (el.scrollWidth <= w) return;
      el.textContent = el.dataset.short;
      if (el.scrollWidth > w) el.textContent = "";
    });
    // a label over a short bar ("Total: 3 KB") would run off its start, over the row's name: moved along to start there
    (root || main).querySelectorAll(".lcline em").forEach(function (em) {
      em.style.transform = "";
      var over = em.parentNode.getBoundingClientRect().left - em.getBoundingClientRect().left;
      if (over > 0) em.style.transform = "translateX(calc(-100% + " + Math.ceil(over) + "px))";
    });
  }
  var fitTimer = null;
  window.addEventListener("resize", function () { clearTimeout(fitTimer); fitTimer = setTimeout(function () { fitBars(); }, 80); });
  // charts drawn later (a file's details, a job's before and after, opened on a click) are fitted as they appear
  // A Plex match pointed at: what it is (title, year, poster, description), looked up once; its link
  // made the public page (watch.plex.tv) when Plex names one
  var MATCHES = {};
  document.addEventListener("mouseover", function (ev) {
    var a = ev.target.closest && ev.target.closest("a.ssguid");
    if (!a || a.dataset.peeked) return;
    a.dataset.peeked = "1";
    var g = a.dataset.guid;
    (MATCHES[g] = MATCHES[g] || api("/api/match?guid=" + encodeURIComponent(g))).then(function (m) {
      if (m.url) a.href = m.url;
      var peek = document.createElement("span");
      peek.className = "sspeek";
      peek.innerHTML = '<img src="/api/match?image=1&guid=' + encodeURIComponent(g) + '" alt="">' +
        "<span><strong>" + esc(m.title || g) + "</strong>" + (m.year ? " (" + esc(m.year) + ")" : "") + (m.summary ? '<span class="det">' + esc(m.summary.length > 300 ? m.summary.slice(0, 300) + "…" : m.summary) + "</span>" : "") + "</span>";
      a.parentNode.classList.add("sspeekwrap");
      a.parentNode.appendChild(peek);
    }).catch(function () { delete MATCHES[g]; delete a.dataset.peeked; });
  });
  var fitPending = false;
  new MutationObserver(function (muts) {
    if (fitPending) return;
    for (var i = 0; i < muts.length; i++) {
      var t = muts[i].target;
      if (t.nodeType !== 1 || !t.closest(".lcbar")) {
        fitPending = true;
        requestAnimationFrame(function () { fitPending = false; fitBars(document.body); });
        return;
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
  function libDetail(path) {
    var d = LIB.detail[path];
    if (!d) return '<p class="muted">' + H("Reading the file…") + "</p>";
    if (d.error && !d.blocks) return '<p class="bad">' + esc(sentence(d.error)) + "</p>";
    var bm = d.blocks, est = LIB.est[path] || {}, rec = d.record;
    if (LIB.block[path] === undefined && bm && bm.start.length) LIB.block[path] = bm.start[0].n;  // the first block, until another is picked
    var shown = bm ? bm.start.concat(bm.end) : [];
    var imgs = (d.attachments || []).filter(function (a) { return a.image; });
    var facts = [[T("On disk"), d.disk != null ? fmtBytesShort(d.disk) : "—"]];
    // measured: the whole file's size against what it takes on disk — said only when ZFS saved something
    // (its own bookkeeping can outweigh a few hundred KB compressed at the end)
    if (d.saved > 0) facts.push([T("ZFS compression saved"), fmtBytesShort(d.saved)]);
    facts.push(
      [T("Always cached"), bm ? P(bm.start.length + bm.end.length, "{n} block", "{n} blocks", { n: bm.start.length + bm.end.length }) + cachedSize(bm, est) : "—"]);
    if (imgs.length) facts.push([T("Cover art"), imgs.map(function (a) { return coverState(a, d.optimized); }).join(" · ")]);
    var out = '<div class="ldetail"><div class="det mono" style="margin-bottom:6px">' + esc(path.split("/").pop()) + "</div>" + checkCounts(d.checks) + '<div class="lfacts">' + facts.map(function (f) { return "<div><span class=\"muted\">" + esc(f[0]) + "</span> " + esc(f[1]) + "</div>"; }).join("") + "</div>";
    var dv = d.views;
    if (dv && dv.after) out += mixChart([[T("Existing"), dv.now, ROW_TIPS.existing], [T("Optimized"), dv.after, dv.rewrite ? ROW_TIPS.rewrite : ROW_TIPS.optimized]]) +
      blocksLine(dv.now.blocks, dv.after.blocks);
    else if (dv) out += mixChart(dv.before ? [[T("First seen"), dv.before], [T("Existing"), dv.now, ROW_TIPS.existing]] : [[T("Existing"), dv.now, ROW_TIPS.existing]]);
    out += libBlocks(d, true, path);
    var sel = LIB.block[path];
    if (sel === "video" && bm) {
      // the video: its tracks, and how much of it is cached with the lead-in
      out += '<div class="lblkinfo"><div class="what">' + esc(streamNames(bm)) + checkMarks(d.checks, ["container"], true) + "</div>" +
        '<div class="det">' + esc(T("{size} in {n} blocks of {rs}", { size: fmtBytesShort(bm.video), n: fmtNum(Math.ceil(bm.video / rec)), rs: fmtRs(rec) })) +
        (bm.lead_blocks ? " · " + esc(P(bm.lead_blocks, "the first {n} cached with the lead-in", "the first {n} cached with the lead-in", { n: fmtNum(bm.lead_blocks + bm.start.length) })) : "") + "</div>";
      if ((d.tracks || []).length) {
        out += '<table class="tbl compact"><thead><tr><th scope="col">#</th><th scope="col">' + H("Track") + '</th><th scope="col">' + H("Codec") +
          '</th><th scope="col">' + H("Language") + '</th><th scope="col">' + H("Name") + '</th><th scope="col">' + H("Flags") + '</th><th scope="col">' + H("Compression") + "</th></tr></thead><tbody>" +
          d.tracks.map(function (t) {
            return "<tr><td>" + esc(t.number) + "</td><td>" + esc(T({ video: "Video", audio: "Audio", subtitles: "Subtitles" }[t.type] || t.type || "")) + (t.width ? ' <span class="muted">' + esc(t.width + "×" + t.height) + "</span>" : "") +
              '</td><td>' + (t.codec_name ? '<span title="' + esc(t.codec) + '">' + esc(t.codec_name) + "</span>" : '<span class="mono">' + esc(t.codec) + "</span>") + "</td><td>" + (t.language ? (t.language_name ? esc(t.language_name) + ' <span class="muted">(' + esc(t.language) + ")</span>" : esc(t.language)) : "—") + "</td><td>" + esc(t.name || "—") + "</td><td>" +
              // a subtitle track shown by itself (forced), and the track a player picks first (default)
              ((t.forced ? badge("info", T("Forced")) + " " : "") + (t.default && t.type !== "video" ? '<span class="muted">' + H("default") + "</span>" : "") || "—") +
              (t.channels ? '<div class="det">' + esc(T("{n} channels", { n: t.channels })) + (t.sample_rate ? " · " + esc(T("{khz} kHz", { khz: dec(t.sample_rate / 1000, 1) })) : "") + (t.bit_depth ? " · " + esc(T("{n}-bit", { n: t.bit_depth })) : "") + "</div>" : "") +
              "</td><td>" + esc(t.compression || "—") + "</td></tr>";
          }).join("") + "</tbody></table>";
      }
      out += "</div>";
    }
    // a block: its file data and what it takes on disk, then what's in it
    var b = shown.filter(function (x) { return x.n === sel; })[0];
    if (b) {
      var run = libRuns(shown).filter(function (r) { return r.b.n === b.n; })[0] || { count: 1 };
      var e = est[b.n], zeros = (b.pieces.filter(function (p) { return p.kind === "empty"; })[0] || { bytes: 0 }).bytes;
      // Streamed: what Plex reads of it (ZFS hands it back uncompressed; the zeros past the end of the
      // file are never read). On disk / cached: what it takes on the disks, in RAM and on the SSD — ZFS
      // keeps it compressed in all three. With zeros, how well the file's own data compressed, apart.
      var line = T("Streamed as {data}", { data: fmtBytesShort(b.data) }) +
        (e ? " · " + (e.compressed ? T("On disk and cached: ≈{stored}", { stored: fmtBytesShort(e.stored) })
          : T("On disk and cached: {stored}", { stored: fmtBytesShort(e.stored) })) : "") +
        (zeros ? " · " + T("plus {z} of zeros past the end of the file, which compress to nearly nothing", { z: fmtBytesShort(zeros) }) : "") +
        (zeros && e && e.compressed && e.stored < b.data ? " · " + T("the data alone ≈{pct}% smaller", { pct: Math.round(100 * (1 - e.stored / b.data)) }) : "");
      var isStart = bm.start.some(function (x) { return x.n === b.n; }) && b.n === bm.start[0].n;
      out += '<div class="lblkinfo"><div class="what">' + esc(run.count > 1 ? T("Blocks {a}–{b} ({n}, all like this)", { a: fmtNum(b.n), b: fmtNum(run.last), n: fmtNum(run.count) }) : T("Block {n}", { n: fmtNum(b.n) })) +
        blockChecks(d, b, shown, isStart) + "</div>" +
        '<div class="det">' + esc(line) + "</div>" +
        '<table class="tbl compact"><thead><tr><th scope="col">' + H("In this block") + '</th><th scope="col">' + H("Size") + '</th><th scope="col">' + H("Share") + "</th></tr></thead><tbody>" +
        b.pieces.map(function (p) {
          // the part's ⓘ first (a slot where it has none, so the lines stay aligned), its checks after its name
          var ids = CHECK_PARTS[p.kind] || [];
          // (padding belonging to the part above: indented after its ⓘ, so every ⓘ lines up)
          return "<tr><td>" + (PART_HELP[p.kind] ? info(H(PART_HELP[p.kind]), partName(p)) : '<span class="infoslot"></span>') +
            (p.kind === "padding" && p.of ? '<span class="lpadgap"></span>' : "") + '<i class="lsw ' + partClass(p) + '"></i> ' + esc(partName(p)) + (p.name ? ' <span class="mono muted">' + esc(p.name) + "</span>" : "") +
            checkMarks(d.checks, ids, ids.length > 1) +
            '</td><td class="nw">' + esc(fmtBytesShort(p.bytes)) + '</td><td class="nw">' + esc(dec(100 * p.bytes / rec, 1) + "%") + "</td></tr>";
        }).join("") +
        // what ZFS stores of it that's none of the file's: the rest of its last sector
        (e && e.rounding ? "<tr><td>" + info(H("ZFS stores a compressed block in whole 4 KB sectors: the rest of its last sector holds nothing, but takes space on disk and in the cache."), T("ZFS rounding")) +
          '<i class="lsw lcround"></i> ' + H("ZFS rounding") + '</td><td class="nw">' + esc(fmtBytesShort(e.rounding)) + '</td><td class="nw">' + esc(dec(100 * e.rounding / rec, 1) + "%") + "</td></tr>" : "") +
        "</tbody></table></div>";
    }
    return out + "</div>";
  }
  // Two Plex records side by side (Sync Libraries: the two servers'; Merge Duplicates: one film's) — the sides of a
  // comparison card (cmpRow), each record's fields that differ a chip, opening to its value with the other side's beside it
  // Plex's names for the fields you can edit (and lock), as the page says them
  var FIELD_NAMES = { title: N("Title"), titleSort: N("Sort title"), originalTitle: N("Original title"), summary: N("Description"), tagline: N("Tagline"), year: N("Year"),
    studio: N("Studio"), contentRating: N("Content rating"), originallyAvailableAt: N("Release date"), genre: N("Genres"), writer: N("Writers"), director: N("Directors"),
    producer: N("Producers"), country: N("Countries"), collection: N("Collections"), label: N("Labels"), thumb: N("Poster"), art: N("Background"), rating: N("Rating"),
    userRating: N("Rating"), editionTitle: N("Edition"), audienceRating: N("Rating") };
  function fieldName(f) { return FIELD_NAMES[f] ? T(FIELD_NAMES[f]) : f; }
  function fieldText(x) {  // "title: A" → "Title: A"; field lists too
    return String(x).replace(/\b([A-Za-z]+)(?=:)/, function (m) { return FIELD_NAMES[m] ? T(FIELD_NAMES[m]) : m; })
      .replace(/: ([A-Za-z, ]+)$/, function (m, list) { return ": " + list.split(", ").map(fieldName).join(", "); });
  }
  // One side of an item: its poster, title and match state, then — the source — what it copies, or
  // — the other server — what it has that would be overwritten. Only the kinds that differ, each a
  // button: pressed, its value shows, and the same field's on the other side with it, to compare.
  function sideValues(x, other, kinds, isSource) {
    var lines = [];
    var has = function (k) { return kinds.indexOf(k) >= 0; };
    // nothing in a field: no button — unless it's locked with nothing in it (yellow: it would copy that nothing)
    var add = function (id, label, text, status) { lines.push({ id: id, label: label, text: text == null ? "" : String(text), status: status || "pass" }); };
    // grey: what comes with the item's match, not copied as it is (only an edited, locked field is)
    var mine = function (f) { return (x.locked || []).indexOf(f) >= 0; };
    if (has("match") || has("missing") || has("grouping")) {
      if (x.summary_text) add("summary", T("Description"), x.summary_text, mine("summary") ? "pass" : "grey");
    }
    if (has("edits")) {
      var fields = (x.locked || []).filter(function (f) { return !isSource || !other || !other.locked || other.locked.indexOf(f) < 0 || String(x.fields[f]) !== String((other.fields || {})[f]); });
      // (each its own button already: collections, the edition, the description)
      fields = fields.filter(function (f) {
        return !(has("collections") && f === "collection") && !(has("editions") && f === "editionTitle") && !(f === "summary" && (has("match") || has("missing") || has("grouping")));
      });
      fields.forEach(function (f) { var v = fieldValue(x, f); add("f:" + f, fieldName(f), v || T("Locked, with nothing in it: Plex’s agents can’t fill it in either."), v ? "pass" : "warn"); });
    }
    // the source: the other's edits it doesn't have — what it has there instead comes from its match
    if (has("edits") && isSource && other) (other.locked || []).filter(function (f) { return !mine(f) && !(has("collections") && f === "collection"); }).forEach(function (f) {
      var v = fieldValue(x, f);
      if (v) add("f:" + f, fieldName(f), v, "grey");
    });
    if (has("art")) add("art", T("Poster"), x.art.length ? (isSource ? T("Poster chosen by hand") : T("Poster: chosen by hand, replaced")) : (isSource ? T("Plex’s own poster") : T("Poster: replaced")));
    if (has("collections") && (x.collections.length || !isSource)) add("collections", T("Collections"), x.collections.join(", ") || "—");
    if (has("labels") && (x.labels.length || !isSource)) add("labels", T("Labels"), x.labels.join(", ") || "—");
    if (has("editions") && (x.edition || !isSource)) add("editions", T("Edition"), x.edition || "—");
    return lines;
  }
  // a Plex match, as something to look at: its entry on Plex (plex://), IMDb, TMDB or TheTVDB — else as it is
  function guidLink(g) {
    var m, url = null, name = g;
    if ((m = /^plex:\/\/(movie|show|season|episode|artist|album|track)\/([0-9a-f]+)/i.exec(g))) {
      // (its public page, watch.plex.tv, once looked up: pointing at it shows what it is — matchPeek)
      url = "https://app.plex.tv/desktop/#!/provider/tv.plex.provider.discover/details?key=" + encodeURIComponent("/library/metadata/" + m[2]);
      return '<a class="ssguid" href="' + esc(url) + '" target="_blank" rel="noopener" data-guid="' + esc(g) + '">' + esc(g) + "</a>";
    } else if ((m = /(?:imdb):\/\/(tt\d+)/i.exec(g))) { url = "https://www.imdb.com/title/" + m[1] + "/"; name = "IMDb " + m[1]; }
    else if ((m = /themoviedb:\/\/(\d+)/i.exec(g))) { url = "https://www.themoviedb.org/movie/" + m[1]; name = "TMDB " + m[1]; }
    else if ((m = /thetvdb:\/\/(\d+)/i.exec(g))) { url = "https://thetvdb.com/?tab=series&id=" + m[1]; name = "TheTVDB " + m[1]; }
    return url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(name) + '</a> <span class="mono muted">' + esc(g) + "</span>" : '<span class="mono">' + esc(g) + "</span>";
  }
  function fieldValue(x, f) {
    if (!x) return "";
    return x.fields && x.fields[f] != null ? String(x.fields[f]) : x.tags && x.tags[f] ? x.tags[f].join(", ") : "";
  }
  // a field's value on a side that has no button for it (the other side's opened): what it has there, if anything
  function sideValueOf(x, id) {
    if (!x) return "";
    if (id === "summary") return x.summary_text || "";
    if (id === "match") return x.matched ? x.guid || "" : "";
    if (id.indexOf("f:") === 0) return fieldValue(x, id.slice(2));
    if (id === "collections") return (x.collections || []).join(", ");
    if (id === "labels") return (x.labels || []).join(", ");
    if (id === "editions") return x.edition || "";
    return "";
  }
  // its files, one to a line — bulleted when there are several
  function fileList(files) {
    if (!(files || []).length) return "";
    var many = files.length > 1;
    return '<div class="ssfiles' + (many ? " many" : "") + '">' + files.map(function (f) { return "<div>" + (many ? "• " : "") + esc(f) + "</div>"; }).join("") + "</div>";
  }
  // the row's files numbered: File 1, File 2… over both sides (fidx: name -> n); a chip per file on a card,
  // green when the other side has the file too, yellow when only this side does — or, with ``explain`` (a tool's
  // comparison card), grey (green: the files of the record kept, ``x.own``), each opening to what happens to the file
  // (explain(file, "own" | "gains")) and named for it: Original File n (the kept record's own), Imported File n (the rest)
  function fileChips(x, otherNames, fidx, act, openId, explain) {
    return (x.files || []).map(function (f) {
      var n = fidx[f], id = "file:" + n, on = openId === id, both = otherNames.indexOf(f) >= 0;
      return '<button type="button" class="badge b-' + (explain ? (x.own ? "pass" : "grey") : both ? "pass" : "warn") + " sschip" + (on ? " on" : "") + '" aria-expanded="' + on + '" ' + act + ' data-f="' + id + '" title="' + esc(f) + '">' + esc(!explain ? T("File {n}", { n: n }) : x.own ? T("Original File {n}", { n: n }) : T("Imported File {n}", { n: n })) + "</button>";
    }).join("") + (x.gains || []).map(function (f) {  // files it would gain (greyed: what the record looks like after)
      var n = fidx[f], id = "file:" + n, on = openId === id;
      return '<button type="button" class="badge b-grey sschip' + (on ? " on" : "") + '" aria-expanded="' + on + '" ' + act + ' data-f="' + id + '" title="' + esc(T("{file} — joins this record as a version", { file: f })) + '">' + esc(explain ? T("Imported File {n}", { n: n }) : T("File {n}", { n: n })) + "</button>";
    }).join("");
  }
  // ``slots``: true on a comparison card's side — CHIP_SLOT and SHOWN_SLOT left in its chips' row and under it, where
  // cmpRow puts the side's checks (another tool's red ones, cmpBlocks) among its own chips
  function syncSide(x, other, server, isSource, kinds, act, openId, tags, fidx, otherNames, explain, slots) {
    if (!x) return '<div class="sscard"><div class="det">' + H("Not on this server.") + "</div></div>";
    // the Plex checks on this record (libhealth): its versions named for different films, say — data that looks bad
    // (an empty locked field is its own orange chip above: not said twice)
    var tagHtml = (tags || []).filter(function (t) { return t.id !== "locked_empty"; }).map(function (t) { return '<div class="det"><span title="' + esc(msg(t.text)) + '">' + badge("warn", T("Looks wrong")) + " " + esc(msg(t.text)) + "</span></div>"; }).join("");
    // its border says where the poster's from: chosen by hand (green), the match's (grey), unmatched (yellow)
    var pcls = (x.art || []).length ? " p-hand" : x.matched ? " p-match" : " p-warn";
    var ptip = (x.art || []).length ? T("Poster chosen by hand") : x.matched ? T("Poster from its match") : T("Unmatched: the poster isn’t from a match");
    var poster = x.thumb ? '<img class="ssposter' + pcls + '" title="' + esc(ptip) + '" src="/api/poster?path=' + encodeURIComponent(x.thumb) + (server ? "&server=" + encodeURIComponent(server) : "") + '" alt="" loading="lazy">' : '<div class="ssposter none' + pcls + '" title="' + esc(ptip) + '"></div>';
    var lines = sideValues(x, other, kinds, isSource), chips = "", shown = "";
    // its match: a button when the two are matched differently, its entry shown (a link to it) when pressed
    // its match: only when that's what differs (a button: a change); else nothing — the poster's border says unmatched
    var facts = "";
    if (kinds.indexOf("match") >= 0) {
      var mon = openId === "match";
      facts = '<button type="button" class="badge b-' + (x.matched ? "pass" : "warn") + " sschip" + (mon ? " on" : "") + '" aria-expanded="' + mon + '" ' + act + ' data-f="match">' + esc(x.matched ? T("Matched") : T("Unmatched")) + "</button>";
    }
    if (openId === "match") shown = '<div class="ssval">' + (x.matched && x.guid ? guidLink(x.guid) : esc(T("Not matched"))) + "</div>";
    lines.forEach(function (l) {
      var on = l.id === openId;
      chips += '<button type="button" class="badge b-' + l.status + ' sschip' + (on ? " on" : "") + '" aria-expanded="' + on + '" ' + act + ' data-f="' + esc(l.id) + '">' + esc(l.label) + "</button>";
      if (on) shown = '<div class="ssval">' + esc(l.text) + "</div>";
    });
    chips += fileChips(x, otherNames || [], fidx || {}, act, openId, explain);
    if (openId && openId.indexOf("file:") === 0) {
      var isFile = function (f) { return "file:" + (fidx || {})[f] === openId; };
      var fn = (x.files || []).filter(isFile)[0], fg = explain && !fn ? (x.gains || []).filter(isFile)[0] : null;
      if (fn || fg) shown = explain ? '<div class="ssval">' + explain(fn || fg, fn ? "own" : "gains") + '<div class="det mono">' + esc(fn || fg) + "</div></div>" : '<div class="ssval mono">' + esc(fn) + "</div>";
    }
    // (a file opened on another side that this one hasn't: nothing open here — only the same file pairs)
    if (openId && openId !== "match" && !shown) {  // opened on the other side: this side's value, to compare
      var v = sideValueOf(x, openId);
      if (v) shown = '<div class="ssval">' + esc(v) + "</div>";
    }
    return '<div class="sscard' + (isSource ? " src" : "") + '"><div class="ssbody">' + poster + '<div class="ssinfo"><div class="what">' + esc(x.label) + (x.year && String(x.label).indexOf("(" + x.year + ")") < 0 ? ' <span class="muted">(' + esc(x.year) + ")</span>" : "") + "</div>" +
      '<div class="sschips">' + facts + chips + (slots ? CHIP_SLOT : "") + "</div>" + shown + (slots ? SHOWN_SLOT : "") + tagHtml + "</div></div></div>";
  }
  var CHIP_SLOT = "\u0001chips\u0001", SHOWN_SLOT = "\u0001shown\u0001";
  // The Library page's Health view: the content checks (libhealth.py) — file checks on the files as
  // inspected, Plex checks on how Plex has the items — each with its switch, and the items flagged
  // a view's data on its way: a bar that moves, not a word — how far it is where that's known (pg: {done, total}: a bar
  // that fills, with the count), else one that sweeps back and forth. Every waiting bar is this one.
  function loadingBar(text, pg) {
    if (pg && pg.total) {
      var pct = Math.min(100, Math.round(100 * (pg.done || 0) / pg.total));
      return '<div class="loading"><div class="meter thin" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '" aria-label="' + esc(text || T("Loading")) + '"><div style="width:' + pct + '%"></div></div>' +
        '<div class="det">' + esc(text || T("Loading…")) + " " + H("{done} of {total} · {pct}%", { done: fmtNum(pg.done || 0), total: fmtNum(pg.total), pct: pct }) + "</div></div>";
    }
    return '<div class="loading"><div class="meter thin busy" role="progressbar" aria-busy="true" aria-label="' + esc(text || T("Loading")) + '"><div></div></div><div class="det">' + esc(text || T("Loading…")) + "</div></div>";
  }
  var LH_OPEN = {};  // the issue chips opened on the Health view: "item key/check id" -> true
  var HEALTH_PAGES = { library: 1, filechecks: 1, plexchecks: 1, mirrorchecks: 1 };  // the pages that draw the Health view
  // each category's own page: where its checks are set (checksPage, every one of them)
  var CHECK_PAGES = { file: ["filechecks", "File Checks"], plex: ["plexchecks", "Plex Checks"], mirror: ["mirrorchecks", "Mirror Checks"] };
  function checkCategory(name) { return Object.keys(CHECK_PAGES).filter(function (c) { return CHECK_PAGES[c][0] === name; })[0]; }
  // a library's answer fetched (again): the one on the page kept while a newer one comes — the state's count names the
  // answer that's out (health_counts' rev: worked out again in the background as the data changes, or a scan finishes)
  var LH_NEXT = {}, LH_ASKED = {};
  function healthFetch(key, rev) {
    if (LH_NEXT[key]) return;
    LH_NEXT[key] = 1;
    LH_ASKED[key] = rev;  // (asked once for each answer the state names: an answer newer than the state isn't asked for again)
    if (LIB.health[key] === undefined) LIB.health[key] = null;
    api("/api/library/health?key=" + encodeURIComponent(key)).then(function (got) { LIB.health[key] = got; })
      .catch(function () { if (!LIB.health[key]) LIB.health[key] = { error: true }; })  // (said, with Try Again)
      .then(function () { delete LH_NEXT[key]; if (HEALTH_PAGES[page]) render(); });
  }
  // a check page whose findings are worked out in the background (Mirror Checks: libhealth.SOURCES): Scan / Scan Again, as
  // every tool's, for a library it can scan
  // Why findings (their check ids) can't be acted on now in library ``key`` — their action, Ignore, Bring Back: the source
  // whose checks they are (Mirror Checks) is scanning it, what was found being worked out again (the state's health_scans
  // "busy"; the server refuses them the same way, libhealth.scanning) — else "". busyAttr: a button's, disabled saying why
  function scanBusy(key, ids) {
    var sc = ST.health_scans || {}, cats = Object.keys(sc);
    for (var i = 0; i < cats.length; i++) {
      var s = sc[cats[i]];
      if ((s.busy || []).map(String).indexOf(String(key)) >= 0 && (ids || []).some(function (id) { return (s.checks || []).indexOf(id) >= 0; }))
        return T("{page} is scanning this library: wait for it to finish.", { page: CHECK_PAGES[cats[i]] ? toolTitle(CHECK_PAGES[cats[i]][0]) : cats[i] });
    }
    return "";
  }
  function busyAttr(why, tip) { return why ? ' disabled title="' + esc(why) + '"' : tip ? ' title="' + tip + '"' : ""; }
  function lhScanBar(only, key) {
    var sc = (ST.health_scans || {})[only];
    if (!sc || (sc.libraries || []).map(String).indexOf(String(key)) < 0) return "";
    var at = (sc.at || {})[key], scanning = String(sc.running) === String(key);
    return '<div class="row" style="align-items:center;gap:12px;flex-wrap:wrap"><button class="btn" type="button" data-act="lh-scan" data-key="' + esc(key) + '" data-cat="' + esc(only) + '"' + (sc.running ? " disabled" : "") + ">" +
      (scanning ? H("Scanning…") : at ? H("Scan Again") : H("Scan")) + "</button>" +
      (at && !scanning ? '<span class="det">' + H("Scanned {when}", { when: ago(at) }) + "</span>" : "") + "</div>";
    // (no bar of its own: a scan makes the page's count pending, and its placeholder — countBox — is the one bar shown)
  }
  // only: a check page's category ("file", "plex", "mirror"), drawn as every tool's page — its library tabs (tabs) and the
  // top (toolTop: what was found on their right, the checks as options under them, Scan for a category that scans); without,
  // the Library page's Health view
  function libHealthView(only, tabs) {
    var key = LIB.key, d = LIB.health[key], scanBar = only ? lhScanBar(only, key) : "", head = only ? toolTop({ tabs: tabs, scan: scanBar }) : "";
    // the page's count for the library (countOf: a check page's, or the Library page's own): until it's done, what was found
    // isn't counted — no stats, no counts by the checks, and on a check page the placeholder in place of the items, or, with
    // a partial count (the files still being inspected), above the items found so far
    var cnt = countOf(only ? CHECK_PAGES[only][0] : "library", key), ready = countDone(cnt), partial = !ready && cnt.s === "pending" && cnt.partial != null;
    var hcNow = (ST.health_counts || {})[key];
    if (d === undefined) healthFetch(key, hcNow && hcNow.rev);
    else if (d && d.rev && hcNow && hcNow.rev && !hcNow.stale && hcNow.rev !== d.rev && hcNow.rev !== LH_ASKED[key]) healthFetch(key, hcNow.rev);
    d = LIB.health[key];
    if (d && d.error) return head + '<p class="bad">' + H("The checks couldn’t be loaded.") + ' <button class="btn sm" type="button" data-act="lh-retry">' + H("Try Again") + "</button></p>";
    if (!d) return head + (ready || !only ? loadingBar(T("Running the checks…")) : countBox(cnt));
    // (a check page's category: its ⓘ on the Library page)
    var cats = { file: T("What’s in each file, as inspected: its tracks."), plex: T("How Plex has each item."), mirror: T("Whether the two sites of a mirror hold the same files, and each Plex lists them.") };
    var dflt = ((ST.library_health_defaults || {}).checks) || {};
    var stt = d.stats || {};
    var mig = d.migration ? '<div class="note">' + H("A folder of this library is on its way to a dataset of its own ({step}): <a href=\"/dataset\" data-nav>Child Dataset</a> carries on.", { step: d.migration.step }) + "</div>" : "";
    var out = only ? "" : mig;
    // the language the subtitle check wants: chosen beside it, from the languages the library's files hold
    var langSel = '<select class="sel sm" data-lhlang aria-label="' + H("Subtitle language") + '">' + (d.languages || []).map(function (l) {
      return '<option value="' + esc(l.code) + '"' + (l.code === d.language ? " selected" : "") + ">" + esc(l.name) + "</option>";
    }).join("") + "</select>";
    // the audio quality level: every codec's floor scaled alike
    var LEVELS = { poor: T("Only clearly poor"), fair: T("Fair"), good: T("Good"), best: T("Transparent") };
    var qualSel = '<select class="sel sm" data-lhquality aria-label="' + H("Audio quality level") + '">' + Object.keys(LEVELS).map(function (k) {
      return '<option value="' + k + '"' + (k === d.audio_quality ? " selected" : "") + ">" + esc(LEVELS[k]) + "</option>";
    }).join("") + "</select>";
    // the findings held are every check's: the switches as they are now (the settings, not the answer's copy of them —
    // a switch flipped shows at once, with no new scan) decide what's shown and counted
    var chosen = (S.library_health || {}).checks || {}, onIds = {}, parentOn = {};
    Object.keys(d.checks).forEach(function (cat) { (d.checks[cat] || []).forEach(function (c) { c.on = chosen[c.id] !== undefined ? !!chosen[c.id] : dflt[c.id] !== undefined ? dflt[c.id] : !!c.on; parentOn[c.id] = c.on; }); });
    Object.keys(d.checks).forEach(function (cat) { (d.checks[cat] || []).forEach(function (c) { onIds[c.id] = c.on && (!c.of || !!parentOn[c.of]); }); });
    // every item of the library: the ones flagged (by the checks switched on — and, on a check page, that page's) first,
    // then the ones with nothing flagged, each said so
    var catOf = {};
    Object.keys(d.checks).forEach(function (cat) { (d.checks[cat] || []).forEach(function (c) { catOf[c.id] = cat; }); });
    // on the Library page, File Checks' and Plex Checks' chips shown or not as a tool's are (page_<page>: on unless turned off)
    var pageOn = {}, catCounts = {};  // (and the items each check page flags, its findings ignored left out)
    Object.keys(CHECK_PAGES).forEach(function (cat) { pageOn[cat] = chosen["page_" + CHECK_PAGES[cat][0]] !== false; catCounts[cat] = 0; });
    (d.items || []).forEach(function (it) {
      Object.keys(catCounts).forEach(function (cat) { if (it.results.some(function (r) { return onIds[r.id] && catOf[r.id] === cat && !r.ignored; })) catCounts[cat]++; });
    });
    var shownItems = (d.items || []).map(function (it) {
      return Object.assign({}, it, { results: it.results.filter(function (r) { return onIds[r.id] && (only ? catOf[r.id] === only : !CHECK_PAGES[catOf[r.id]] || pageOn[catOf[r.id]]); }) });
    });
    var flaggedItems = shownItems.filter(function (it) { return it.results.length; }).length;
    // a check page (only: one category) shows only what its checks found — the items with an orange chip or a grey one
    // (ignored), as a tool's page shows only its rows with something on them; every item is the Library page's
    if (only) shownItems = shownItems.filter(function (it) { return it.results.length; });
    var counts = {};
    shownItems.forEach(function (it) { it.results.forEach(function (r) { if (!r.ignored) counts[r.id] = (counts[r.id] || 0) + 1; }); });
    // the Library page: every tool a line of its own, File Checks and Plex Checks among them — its name a link to its page,
    // how many items it flags, and Hide or Show: its chips shown here or not, a view and nothing more (no box, which would
    // read as a setting). The checks themselves are set on their own pages and nowhere else: never their switches here,
    // nor the subtitle language or the quality level
    var allCounts = {};  // (each tool's items whether its chips are shown or not, its findings ignored left out)
    (d.items || []).forEach(function (it) {
      var seen = {};
      it.results.forEach(function (r) { if (!r.ignored && !seen[r.id]) { seen[r.id] = 1; allCounts[r.id] = (allCounts[r.id] || 0) + 1; } });
    });
    // (each line's number once its page's count for the library is done — countChip's placeholder until then)
    var toolLine = function (id, on, href, name, help, n, bad) {
      var pc = countOf(href.replace(/^\//, "").replace(/[#?].*$/, ""), key);
      return '<div class="chk lhchk lhtool' + (on ? "" : " lhhidden") + '">' + info(help, name) +
        '<button class="btn sm" type="button" data-act="lh-show" data-id="' + esc(id) + '" data-on="' + (on ? "0" : "1") + '" aria-pressed="' + !on + '" aria-label="' + (on ? H("Hide {tool}", { tool: name }) : H("Show {tool}", { tool: name })) + '">' +
        (on ? H("Hide") : H("Show")) + "</button>" + '<a href="' + esc(href) + '" data-nav>' + esc(name) + "</a>" +
        (!countDone(pc) ? countChip(pc, bad) : bad ? statChip("fail", fmtNum(n), T("Some checks couldn’t run: this count leaves them out")) : badge(n ? "warn" : "pass", fmtNum(n))) + "</div>";
    };
    var checksHtml = only ? "" : (d.checks.tools || []).map(function (c) { return toolLine(c.id, c.on, c.link, msg(c.name), richText(msg(c.help)), allCounts[c.id] || 0); })
      .concat(Object.keys(CHECK_PAGES).map(function (cat) { var pg = CHECK_PAGES[cat]; return toolLine("page_" + pg[0], pageOn[cat], "/" + pg[0], T(pg[1]), esc(cats[cat]), catCounts[cat],
        ((d.unchecked || {})[cat] || []).some(function (u) { return (u.checks || []).some(function (id) { return onIds[id]; }); })); })).join("");
    // the checks the category's last scan couldn't run (Mirror Checks: the other site's files while the link to its Plex
    // Cache is down), a group for each reason: a red chip on top, a link to where it's put right, and their counts red
    var unchecked = only ? (d.unchecked || {})[only] || [] : [], notRun = {};
    unchecked.forEach(function (u) { (u.checks || []).forEach(function (id) { notRun[id] = u; }); });
    if (only) checksHtml = [only].map(function (cat) {
      var all = d.checks[cat] || [], byId = {};
      all.forEach(function (c) { byId[c.id] = c; });
      var row = function (c) {
        var n = counts[c.id] || 0, sub = !!c.of;
        var rv = dflt[c.id] !== undefined && c.on !== dflt[c.id] ? ' <button type="button" class="revert" data-lhcheck-revert="' + c.id + '" title="' + H("Back to the default ({value})", { value: dflt[c.id] ? T("on") : T("off") }) + '">↺</button>' : "";
        var help = richText(msg(c.help)) + (c.link ? '<p><a href="' + esc(c.link) + '" target="_blank" rel="noopener">' + H("The research behind it ↗") + "</a></p>" : "");
        return '<div class="chk lhchk optrow' + (sub ? " lhsub" : "") + '">' + info(help, msg(c.name)) + '<label><input type="checkbox" data-lhcheck="' + c.id + '"' + (c.on ? " checked" : "") + ">" + esc(msg(c.name)) + "</label>" + rv +
          (!ready ? "" : sub ? (c.on && n ? badge("warn", fmtNum(n)) : "") : c.on ? (notRun[c.id] ? statChip("fail", fmtNum(n), T("Not checked: {why}", { why: msg(notRun[c.id].why) })) : n ? badge("warn", fmtNum(n)) : badge("pass", "0")) : "") +
          (c.id === "subtitles_language" ? '<div class="lhlang"><span>' + H("Language") + "</span>" + langSel +
            (d.language !== ((ST.library_health_defaults || {}).subtitle_language || "en") ? ' <button type="button" class="revert" data-lhlang-revert title="' + H("Back to the default ({value})", { value: "English" }) + '">↺</button>' : "") + "</div>" : "") +
          (c.id === "audio_low_bitrate" && c.on ? '<div class="lhlang"><span>' + H("Quality level") + "</span>" + qualSel +
            (d.audio_quality !== ((ST.library_health_defaults || {}).audio_quality || "poor") ? ' <button type="button" class="revert" data-lhquality-revert title="' + H("Back to the default ({value})", { value: LEVELS.poor }) + '">↺</button>' : "") + "</div>" : "") + "</div>";
      };
      // a check's options (c.of) under it, only while it's on — on a check page, the check and its options one cell of the
      // options' grid (optionGrid: the tools' options' layout)
      var rows = all.filter(function (c) { return !c.of; }).map(function (c) {
        return row(c) + (c.on ? all.filter(function (o) { return o.of === c.id; }).map(row).join("") : "");
      });
      return optionGrid(rows.map(function (r) { return '<div class="optcell">' + r + "</div>"; }));
    }).join("");
    if (only) {
      // what was found, as on every tool's page: the items read, those flagged by this page's checks as they're set, and
      // with every check of the page on (each item once, whatever its chips)
      // (an item whose findings are all ignored isn't an issue — as the tab's count — nor does it pass)
      var total = (d.items || []).length, failAll = (d.items || []).filter(function (it) { return it.results.some(function (r) { return catOf[r.id] === only; }); }).length;
      var issues = shownItems.filter(function (it) { return it.results.some(function (r) { return !r.ignored; }); }).length;
      out += mig + toolTop({ tabs: tabs, options: checksHtml, scan: scanBar, stats: ready && { read: total, readText: P(total, "{n} item read", "{n} items read", { n: fmtNum(total) }), issues: issues,
        passNow: total - flaggedItems, passAll: total - failAll,
        extra: unchecked.map(function (u) {
          return u.link ? '<a class="badge b-fail" href="' + esc(u.link) + '" data-nav title="' + esc(msg(u.why)) + '">' + esc(msg(u.label)) + "</a>" : statChip("fail", msg(u.label), msg(u.why));
        }).concat(d.unread ? [statChip("grey", P(d.unread, "{n} file not inspected yet", "{n} files not inspected yet", { n: fmtNum(d.unread) }), T("Files the inspector hasn’t read yet: their tracks aren’t checked until it has."))] : []) } });
      // what the category's source says above its findings (Mirror Checks: the link not healthy, the library held, what
      // couldn't be compared): a warning boxed, the rest as a line
      out += ((d.notes || {})[only] || []).map(function (n) {
        return n.status === "warn" ? '<div class="note warnbox">' + links(msg(n.text)) + "</div>" : '<p class="muted">' + links(msg(n.text)) + "</p>";
      }).join("");
      if (!ready) { out += countBox(cnt); if (!partial) return out; }
    } else {
      // what was found, as chips — the tools' pages' (toolStats): the items checked, those with something shown here (each
      // once), those with nothing, then those with more than one issue, the files not inspected yet and the items' pages read
      var live = shownItems.filter(function (it) { return it.results.some(function (r) { return !r.ignored; }); });
      var multi = live.filter(function (it) { var ids = {}; it.results.forEach(function (r) { if (!r.ignored) ids[r.id] = 1; }); return Object.keys(ids).length > 1; }).length;
      var checked = d.checked || 0, extra = [];
      if (multi) extra.push(statChip("grey", P(multi, "{n} item with more than one issue", "{n} items with more than one issue", { n: fmtNum(multi) })));
      if (d.unread) extra.push(statChip("grey", P(d.unread, "{n} file not inspected yet", "{n} files not inspected yet", { n: fmtNum(d.unread) }), T("Files the inspector hasn’t read yet: their tracks aren’t checked until it has.")));
      if (d.pages_read != null) extra.push(statChip("grey", T("{n} items’ pages read", { n: fmtNum(d.pages_read) }), T("The locked-field check needs them: they’re read after the records, and again as items change.")));
      out += ready ? libStats({ read: checked, readText: P(checked, "{n} item read", "{n} items read", { n: fmtNum(checked) }), issues: live.length,
        passNow: checked - flaggedItems - (d.total || 0) + (d.items || []).length, extra: extra }) : countBox(cnt);
      out += '<div class="lhtools">' + checksHtml + "</div>";
    }
    if (!shownItems.length) return out + (partial && only ? "" : '<p class="muted">' + (only && (d.items || []).length ? badge("pass", T("Nothing found")) + " " + H("No item has anything these checks flag.") : H("No items in this library yet.")) + "</p>");
    var hper = only ? cardsPer() : perPage();  // (a check page is a tool's page: the tools' page size)
    var hpages = { pages: pageRanges(shownItems.map(function (it) { return it.label; }), hper), total: shownItems.length };
    if (REVEAL && String(REVEAL.lib) === String(key)) {  // (a link to a record: the page of the list that holds it)
      var at = shownItems.map(function (it) { return String(it.key); }).indexOf(String(REVEAL.rk));
      if (at >= 0) LIB.healthOffset = Math.floor(at / hper) * hper;
    }
    if (LIB.healthOffset >= shownItems.length) LIB.healthOffset = 0;
    var hoff = LIB.healthOffset;
    shownItems = shownItems.slice(hoff, hoff + hper);
    // each item flagged as a record card — its poster, its name as Plex has it (title, year, edition) — with a chip per
    // issue on the right; a chip opened shows what the check found (the file concerned, when the item has several)
    out += '<div class="sshdr lhhdr"><div>' + H("Item") + "</div><div></div><div>" + H("Flagged") + "</div></div>";
    out += shownItems.map(function (it, i) {
      var poster = it.poster ? '<img class="ssposter" src="/api/poster?path=' + encodeURIComponent(it.poster) + '" alt="" loading="lazy">' : '<div class="ssposter none"></div>';
      // one chip per check (an item flagged by the same check several times — nine files alike — says ×9), its findings under it
      var chips = "", shown = "", live = [], muted = 0, byCheck = {}, order = [];
      it.results.forEach(function (r) {
        var ck = r.id + (r.ignored ? ":off" : "");
        if (!byCheck[ck]) { byCheck[ck] = []; order.push(ck); }
        byCheck[ck].push(r);
        if (r.ignored) muted++; else if (live.indexOf(r.id) < 0) live.push(r.id);
      });
      order.forEach(function (ck) {
        var rs = byCheck[ck], r = rs[0], id = i + "/" + ck, on = !!LH_OPEN[key + ":" + id];
        var name = ((d.checks[r.category] || []).filter(function (c) { return c.id === r.id; })[0] || {}).name;
        chips += '<button type="button" class="badge b-' + (r.ignored ? "grey" : "warn") + " sschip" + (on ? " on" : "") + '" aria-expanded="' + on + '" data-act="lh-open" data-key="' + esc(key) + '" data-id="' + esc(id) + '"' +
          (r.ignored ? ' title="' + H("Ignored") + '"' : "") + ">" + esc(name ? msg(name) : r.id) + (rs.length > 1 ? " ×" + rs.length : "") + "</button>";
        if (on) rs.forEach(function (x) {
          shown += '<div class="ssval' + (x.ignored ? " muted" : "") + '">' + esc(msg(x.text)) + (x.file && (it.files || []).length > 1 ? ' <span class="mono muted">' + esc(x.file) + "</span>" : "") +
            (x.lines || []).map(function (l) { return '<div class="det">' + esc(msg(l)) + "</div>"; }).join("") +
            (x.link ? '<div class="det"><a href="' + esc(x.link) + '" data-nav>' + H("Open {tool}", { tool: name ? msg(name) : r.id }) + "</a></div>" : "") + "</div>";
        });
        // what can be done about it (Mirror Checks' Scan Folder), once under the chip, and when it was last asked for
        var ra = r.action;
        if (on && ra && !r.ignored) shown += '<div class="det"><button class="btn sm" type="button" data-act="lh-action" data-key="' + esc(key) + '" data-item="' + esc(it.key) + '" data-check="' + esc(r.id) + '"' +
          busyAttr(scanBusy(key, [r.id]), ra.tip ? esc(msg(ra.tip)) : "") + ">" + esc(msg(ra.label)) + "</button>" + (ra.done_at ? " " + H("Asked {when}", { when: ago(ra.done_at) }) : "") + "</div>";
      });
      // ignored: the item's issues as they are now greyed out and kept so (a new kind of issue still warns); brought back on request
      var ignTip = H("Greys out this item’s issues as they are now, for good. A new kind of issue on it still shows as a warning."), backTip = H("Shows this item’s ignored issues as warnings again.");
      var acts = (live.length ? '<button class="btn sm" type="button" data-act="lh-ignore" data-key="' + esc(key) + '" data-item="' + esc(it.key) + '" data-ids="' + esc(live.join(",")) + '"' + busyAttr(scanBusy(key, live), ignTip) + ">" + H("Ignore") + "</button> " : "") +
        (muted ? '<button class="btn sm" type="button" data-act="lh-restore" data-key="' + esc(key) + '" data-item="' + esc(it.key) + '"' + busyAttr(scanBusy(key, it.results.filter(function (r) { return r.ignored; }).map(function (r) { return r.id; })), backTip) + ">" + H("Bring Back") + "</button>" : "");
      if (!it.results.length) chips = badge("pass", T("Nothing flagged"));
      var rid = recordId(key, it.key);
      return '<div class="ssitem lhitem' + (live.length ? "" : " lhquiet") + (REVEAL && REVEAL.id === rid ? " revealed" : "") + '" id="' + esc(rid) + '"><div class="sscard"><div class="ssbody">' + poster + '<div class="ssinfo"><div class="what">' + esc(it.label) + '</div><div class="lhacts">' + acts + "</div></div></div></div>" +
        '<div class="ssarrow"></div><div class="lhflags"><div class="sschips">' + chips + "</div>" + shown + "</div></div>";
    }).join("");
    out += pager(hpages, hoff, hper, 'data-act="lh-page"');
    if ((d.items || []).length < (d.total || 0)) out += '<p class="muted">' + H("The first {n} shown.", { n: fmtNum(d.items.length) }) + "</p>";
    return out;
  }
  function pageLibrary() {
    var libs = ((ST.library || {}).libraries || []).filter(function (l) { return l.kind; });
    // the settings changed (an optimizer choice, say): the checks and estimates are fetched again —
    // they're worked out from the settings, never kept past a change
    if (LIB.rev !== ST.settings_rev) {
      if (LIB.rev !== null) {
        LIB.at = 0;
        Object.keys(LIB.detail).forEach(function (p) { if (p !== LIB.open) delete LIB.detail[p]; });
        if (LIB.open) libDetailFetch(LIB.open);  // (shown as it was until the new one is in: no flicker)
      }
      LIB.rev = ST.settings_rev;
    }
    if (!LIB.key || !libs.some(function (l) { return String(l.key) === String(LIB.key); })) LIB.key = libs.length ? String(libs[0].key) : null;
    libFetch();
    libAutoDrive();
    var lj = LIB.key ? libJob(LIB.key) : {}, job = lj.running || lj.paused ? lj : null;
    if (lj.running) LIB.inspecting = true;
    else if (LIB.inspecting) { LIB.inspecting = false; libFetch(true); }  // just finished (or paused): the files as they are now
    // an inspection that ended since the files were fetched (even one that began and ended between two refreshes): fetched again
    if (lj.finished && LIB.data && LIB.data.library === LIB.key && LIB.finishedSeen[LIB.key] !== lj.finished) libFetch(true);
    // one button: Pause while inspecting, Resume when paused, otherwise inspect every file again
    var btn = !LIB.key ? "" : lj.running ? '<button class="btn" type="button" data-act="lib-survey" data-how="pause">' + H("Pause") + "</button>"
      : lj.paused ? '<button class="btn" type="button" data-act="lib-survey" data-how="resume">' + H("Resume") + "</button>"
      : '<button class="btn" type="button" data-act="lib-survey" data-how="again">' + H("Inspect Files Again") + "</button>";
    if (LIB.view !== "caching") LIB.view = "health";
    var out = top(T("Library"), LIB.view === "health" ? H("The content itself: what’s in each file, and how Plex has each item. Each check can be turned off.")
      : H("What’s where in each file — its start, the video, and the metadata at its end — the blocks they land on, and what caching keeps of it.") +
      " " + H("Files are inspected when the page opens: only ones new or changed since."), LIB.view !== "caching" ? "" : btn);
    out += '<div class="stack"><section class="card">';
    if (!LIB.key) {  // (until the first scan: Plex's libraries are still being read)
      var plexSet = S.plex && S.plex.url && S.plex.token_set;
      return out + '<p class="muted">' + ((ST.library || {}).scanning || plexSet ? H("Reading your libraries from Plex…")
        : H("No libraries yet. Connect Plex in <a href=\"/system\" data-nav>System</a>.")) + "</p></section></div>";
    }
    out += '<div class="lctl"><div class="seg small strong" role="tablist" aria-label="' + H("Library") + '">' + libs.map(function (l) {
      return '<button role="tab" type="button" aria-selected="' + (String(l.key) === String(LIB.key)) + '" data-act="lib-tab" data-key="' + esc(l.key) + '">' + libIcon(l.key) + esc(l.title) + "</button>";
    }).join("") + "</div>" +
      // the views: the content checks, and caching health (the files' blocks)
      '<div class="seg small" role="tablist" aria-label="' + H("View") + '">' + [["health", T("Health")], ["caching", T("Caching")]].map(function (v) {
        return '<button role="tab" type="button" aria-selected="' + (LIB.view === v[0]) + '" data-act="lib-view" data-view="' + v[0] + '">' + esc(v[1]) + "</button>";
      }).join("") + "</div></div>";
    if (LIB.view === "health") return out + libHealthView() + "</section></div>";
    var d = LIB.data && LIB.data.library === LIB.key ? LIB.data : null;
    // while inspecting (or waiting to): a progress bar
    if (job) {
      var pct = job.total ? Math.round(100 * (job.done || 0) / job.total) : 0;
      out += '<div class="linspect"><div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '"><div style="width:' + pct + '%"></div></div>' +
        '<div class="det">' + (job.paused ? H("Inspecting paused: {done} of {total} files", { done: fmtNum(job.done || 0), total: fmtNum(job.total || 0) })
          : H("Inspecting files: {done} of {total}", { done: fmtNum(job.done || 0), total: fmtNum(job.total || 0) })) + "</div></div>";
    }
    // the legend with the search after it (taking the room the legend's last line leaves), kept at the
    // top of the window while the files scroll under it, with the table's column names
    out += "<!--lsummary-->" + '<div class="lstick">' + libLegend('<input type="search" id="lib-q" class="lsearch" placeholder="' + esc(T("Find a title")) + '" value="' + esc(LIB.q) + '">') + "</div>";
    if (!d) return out + "</section></div>";
    out = out.replace("<!--lsummary-->", libSummary(d.summary));
    if (!d.files.length) return out + '<p class="muted">' + H("No files.") + "</p></section></div>";
    var key = function (x) { return x.label; };
    out += '<div class="tbl-wrap lfwrap"><table class="tbl lfiles"><thead><tr><th scope="col"></th><th scope="col">' + H("File") + '</th><th scope="col">' + H("Size") +
      '</th><th scope="col" style="width:60%">' + H("Blocks always cached: the start · the end") + "</th></tr></thead><tbody>";
    var openHere = null;
    d.files.forEach(function (f, i) {
      var first = i === 0 || key(d.files[i - 1]) !== key(f), span = 1;
      while (first && i + span < d.files.length && key(d.files[i + span]) === key(f)) span++;
      var more = i + 1 < d.files.length && key(d.files[i + 1]) === key(f);
      var name = (first ? '<div class="what">' + esc(f.label) + "</div>" : "") + (span > 1 || !first ? '<div class="det mono">' + esc(f.file.split("/").pop()) + "</div>" : "");
      out += '<tr class="lrow' + (more ? " ingrp" : "") + (LIB.open === f.file ? " open" : "") + '" data-act="lib-open" data-file="' + esc(f.file) + '">' +
        (first ? "<td" + (span > 1 ? ' rowspan="' + span + '"' : "") + ">" + posterImg(f.poster) + "</td>" : "") +
        '<td style="min-width:200px">' + name + '</td><td class="nw">' + esc(fmtBytesShort(f.size)) + (f.health ? '<div class="det">' + healthBadge(f.health) + "</div>" : "") +
        "</td><td>" + libBlocks(f, false, f.file) + "</td></tr>";
      if (LIB.open === f.file) openHere = f.file;
      if (openHere && !more) { out += '<tr class="ldrow"><td colspan="4">' + libDetail(openHere) + "</td></tr>"; openHere = null; }
    });
    out += "</tbody></table></div>";
    out += pager(d, d.offset, perPage(), 'data-act="lib-page"');
    return out + "</section></div>";
  }
  var libTyping = null;
  document.addEventListener("input", function (e) {
    if (!e.target || e.target.id !== "lib-q") return;
    clearTimeout(libTyping);
    libTyping = setTimeout(function () { LIB.q = e.target.value.trim(); LIB.offset = 0; LIB.open = null; libFetch(true); }, 250);
  });

  function pageSsd() {
    var c = ST.cache || {}, pools = ST.zfs.pools || {}, names = Object.keys(pools).filter(function (k) { return pools[k]; });
    var out = top(T("SSD Cache"), H("Caching is planned around an SSD cache (L2ARC) on the media pool. Here’s what you have, and how big it needs to be."), "");
    out += modeLegend();
    out += '<div class="stack">';
    // ---- what's there
    var rows = names.map(function (k) {
      var p = pools[k];
      return "<tr><td class=\"mono\">" + esc(p.name || k) + "</td><td>" + (p.l2_online ? badge("pass", P(p.l2_online, "{n} device", "{n} devices")) : p.cache_devices ? badge("warn", T("Not ONLINE")) : badge("info", T("None"))) +
        '</td><td class="nw">' + (p.l2_total ? esc(fmtBytes(p.l2_total)) : "—") + "</td></tr>";
    }).join("");
    var arc = ST.zfs.arc;
    out += '<section class="card"><h2>' + H("L2ARC on Your Media Pools") + "</h2>" +
      (rows ? '<div class="tbl-wrap"><table class="tbl mid"><thead><tr><th scope="col">' + H("Pool") + '</th><th scope="col">' + H("Cache devices") + '</th><th scope="col">' + H("Size") + "</th></tr></thead><tbody>" + rows + "</tbody></table></div>"
        : '<p class="muted">' + (ST.pending || ST.checking ? H("Checking the pools…") : ST.l2arc === "unknown" ? H("Couldn’t check the pools yet. <a href=\"/system\" data-nav>Connect TrueNAS in System</a> to see them.") : H("No media pools found yet.")) + "</p>") +
      (arc && arc.l2_present ? '<p class="muted" style="font-size:13px;margin:10px 0 0">' + H("L2ARC (SSD cache): {used} of {total} filled", { used: fmtBytes(arc.l2_asize), total: fmtBytes(ST.zfs.l2_total || arc.l2_size || 0) }) + "</p>" : "") +
      (ST.l2arc === "absent" && !c.no_l2arc && !ST.pending ? '<div class="note">' + H("No L2ARC found. To add one in TrueNAS: Storage → your media pool → Manage Devices → Add VDEV → Cache, and pick an SSD.") + "</div>" : "") +
      '<div style="margin-top:14px">' + checkbox("general", "no_l2arc", T("I won’t use an L2ARC"),
        { s: H("Runs on RAM only, without a warning in Health."),
          more: H("Settings then always use their RAM-only defaults, choices that need an SSD fall back, and warm-up stays off. Untick it if you add an L2ARC later.") }) + "</div>" +
      "</section>";
    out += l2arcSettingsCard();
    // ---- how big
    out += '<section class="card"><h2>' + H("L2ARC Sizing Estimator") + "</h2>" +
      '<p class="help" style="margin:4px 0 0">' + H("How much cache it takes to keep every library’s lead-ins in the Lead-in Cache — on the SSD with an L2ARC, in RAM without one. Libraries without a warm-up need less. " +
        "Try a different lead-in length per library below to see what it would need — the <strong>Target</strong> column follows. These sliders are only a what-if: they don’t change your settings (lead-in length is set per library in <a href=\"/caching#leadin\" data-nav>Caching → Lead-in Cache</a>), and they start from the default lengths each time this page opens. All the way down is one record.") + "</p>" +
      estimatorSliders() +
      sizingControls() + estimateHtml() + sizingHtml(true) + sizingAdvanced() + "</section>";
    out += "</div>";
    return out;
  }

  // Prefetch's choices per library (see plexcache/prefetch.py): the same ladder for every kind,
  // in the library's own words.
  var PF_NAMES = {
    tv: { off: T("Don’t prefetch"), none: T("Nothing"), next: T("Next episode"), rest: T("Rest of the season"), whole: T("Whole season"), next_group: T("Next season"),
      all_groups: T("All seasons of the show"), library: T("Every show in the library") },
    music: { off: T("Don’t prefetch"), none: T("Nothing"), next: T("Next track"), rest: T("Rest of the album"), whole: T("Whole album"),
      all_groups: T("All the artist’s albums"), library: T("All music in the library") },
    books: { off: T("Don’t prefetch"), none: T("Nothing"), next: T("Next chapter"), rest: T("Rest of the book"), whole: T("Whole book"), next_group: T("Next book"),
      all_groups: T("All the author’s books"), library: T("Every book in the library") },
    movies: { off: T("Don’t prefetch"), none: T("Nothing"), next: T("Next movie in the collection"), rest: T("Rest of the collection"), whole: T("Whole collection"),
      library: T("Every movie in the library") }
  };
  var PF_KIND = { tv: T("TV"), music: T("music"), books: T("audiobooks"), movies: T("movies") };
  var PF_START = ["off", "next", "rest", "whole", "next_group", "all_groups", "library"], PF_END = ["none", "next", "rest"];
  // How each choice is read: what's needed soon straight into RAM, what's needed later to the SSD
  // (in the background, at the L2ARC pacing). The notes say so in each dropdown.
  var PF_NOTES = {
    start: { next: T("Straight away, into RAM"), rest: T("Straight away, into RAM"), whole: T("To the SSD, in the background"),
      next_group: T("To the SSD, in the background"), all_groups: T("To the SSD, in the background"), library: T("To the SSD, in the background") },
    end: { next: T("Straight away, into RAM"), rest: T("The next one into RAM; the rest to the SSD, in the background") }
  };
  var PF_END_AT = [{ value: "80", label: T("At 80% watched") }, { value: "90", label: T("At 90% watched"), note: T("About when Plex marks it watched"), isDefault: true }];
  function libChoice(key, field) {
    var libs = S.nextepisode.libraries || {}, c = (libs[key] || libs["*"] || {})[field];
    return c || (field === "end_at" ? "90" : "default");
  }
  // All the way down (0) is the least: each file's metadata (its start through 60% of a record of video, and its end).
  var SEC_LIMITS = { movie: [0, 180], tv: [0, 180], audio: [0, 60] };
  function secText(n, unit) { return Number(n) === 0 ? T("Metadata only") : n + " " + (unit || "s"); }
  var WARM_NAMES = { none: T("No warm-up"), boot: T("At boot only"), twice_monthly: T("At boot + twice monthly"), weekly: T("At boot + weekly"),
    twice_weekly: T("At boot + twice weekly"), daily: T("At boot + daily") };
  var WARM_ORDER = ["none", "boot", "twice_monthly", "weekly", "twice_weekly", "daily"];
  var WARM_TIMED = ["twice_monthly", "weekly", "twice_weekly", "daily"];
  var WARM_KIND = { movies: "movie", tv: "tv", music: "audio", books: "audio" };
  function warmChoice(key, field) { return ((S.warmup.libraries || {})[key] || {})[field]; }
  function cacheLibs() { return ((ST.library && ST.library.libraries) || []).filter(function (l) { return !l.excluded && l.kind; }); }
  function libKind(l) { return l.prefetch_kind || (l.kind === "movie" ? "movies" : l.kind === "tv" ? "tv" : "music"); }
  function libHead(l, kind, extra) {
    var k = PF_KIND[kind], says = l.title.toLowerCase().indexOf(k.toLowerCase()) >= 0;  // "Movies · movies": once is enough
    return '<div class="pflib-head">' + libIcon(l.key) + esc(l.title) + (says ? "" : ' <span class="muted">· ' + esc(k) + "</span>") + (extra || "") + "</div>";
  }

  var L2ARC_NOTE = H("What’s needed soon is read straight away, into RAM: RAM keeps one-off reads for hours at most. What’s needed later goes to the SSD: choices with the grey SSD icon need an L2ARC, " +
    "and read in the background at the L2ARC pacing, pausing for players, scrubs and busy disks (<a href=\"/ssd#l2arc-settings\" data-nav>SSD Cache → L2ARC Cache Settings</a>). " +
    "They don’t count against the RAM limit — they only pass through RAM on their way to the SSD — and never hold up the steps before them. Without an L2ARC they use the RAM default instead, and Health says so.");

  // ---- Lead-in Cache: a file's first seconds plus its index, per library — how long a lead-in
  // is, Warm-up (whole libraries on a schedule) and Prefetch (what's likely to play next).
  function leadInCard() {
    var w = S.warmup, noL2 = ST.l2arc === "absent";
    var out = '<section class="card" id="leadin"><div class="head"><h2>' + H("Lead-in Cache") + '</h2><span style="flex:1"></span>' + guideBtn("leadin") + "</div>" +
      '<p class="lede">' + H("The first seconds of each file, plus its metadata, kept ready so anything you press play on starts instantly.") + "</p>" +
      guide("leadin", [
        [H("Lead-in length"), H("How many seconds of playback each lead-in holds, set per library and scaled by each file’s own bitrate, so a 4K remux gets more bytes than a 720p episode. " +
          "Its metadata is found from the file’s own layout and always included, read first — the seek index (MKV Cues, MP4 <span class=\"mono\">moov</span>), tags, subtitle fonts and cover art at the end (players read them all when they open a file), and at the start everything in front of the video, plus at least 60% of a record of video.")],
        [H("Warm-up"), H("Reads the lead-in of every movie, episode and track in a library onto the SSD cache (L2ARC): at boot, and if you like again twice a month (the 1st and 15th), weekly, twice weekly (the chosen day and three days later) or daily. " +
          "Press play on anything — especially titles nobody has watched in a while — and it starts at SSD speed. Large libraries on HDD pools gain the most. " +
          "Needs an L2ARC on the media pool, plus <span class=\"mono\">primarycache=all</span> and <span class=\"mono\">secondarycache=all</span> on the media dataset; Health checks all three. " +
          "If the lead-ins don’t fit the L2ARC, they’re shortened automatically (see <a href=\"/ssd\" data-nav>SSD Cache</a>).")],
        [H("Prefetch"), H("Once Now Playing Cache is done, reads the lead-ins of what’s likely to play next, in Plex’s own order (across seasons and any folder layout): " +
          "next episode · rest of the season · whole season · next season · all seasons · the whole library. Music and audiobooks use the same ladder for tracks and chapters, albums and books; movies follow their Plex collections. " +
          "Each step includes the ones before it, read in order. The season is that of what plays next: at the last episode of season 1, “Rest of the season” means season 2.")],
        [H("L2ARC and cooldown"), L2ARC_NOTE + " " + H("Nothing is read again within its cooldown (<a href=\"/caching#ram-only\" data-nav>RAM Only Settings</a>, or with an L2ARC <a href=\"/ssd#l2arc-settings\" data-nav>L2ARC Cache Settings</a>). Without an L2ARC, lead-ins stay in RAM within Prefetch’s RAM limit.")]]);
    if (unavailable("nextepisode")) out += unavailableNote("nextepisode", esc(unavailable("nextepisode")));
    // Warm-up's runs (what each library warms, and when, is chosen per library below)
    if (!noL2 && !unavailable("warmup")) {
      out += '<div class="cachegrp">';
      if (ST.l2arc === "unknown") out += '<div class="note">' + (ST.pending || ST.checking ? H("Checking whether the media pool has an L2ARC…") : H("Couldn’t confirm the media pool has an L2ARC. <a href=\"/system\" data-nav>Connect TrueNAS in System</a> to check.")) + "</div>";
      var scheds = ST.warmup.schedules || {};
      var used = Object.keys(scheds).map(function (k) { return scheds[k]; });
      var timed = used.filter(function (v) { return WARM_TIMED.indexOf(v) >= 0; }).length > 0;
      var weekly = used.indexOf("weekly") >= 0 || used.indexOf("twice_weekly") >= 0;
      out += warmupStatus() + '<div class="grid" style="margin-top:0">' +
        '<div class="fld"><label for="f-warmup-time">' + H("Timed warm-ups start at") + '</label><div class="inline">' +
          (weekly ? '<select aria-label="' + H("Day") + '" data-set="warmup.weekday">' + [0, 1, 2, 3, 4, 5, 6].map(function (i) {
            var d = new Date(2024, 0, 1 + i).toLocaleDateString(LOCALE, { weekday: "long" });  // 1 Jan 2024 was a Monday
            (REVERT_LABELS["warmup.weekday"] = REVERT_LABELS["warmup.weekday"] || {})[String(i)] = d;
            return '<option value="' + i + '"' + (w.weekday === i ? " selected" : "") + ">" + esc(d) + "</option>"; }).join("") + "</select>" + revertBtn("warmup", "weekday", "") : "") +
          '<input id="f-warmup-time" type="time" value="' + esc(w.time) + '" data-set="warmup.time">' + revertBtn("warmup", "time", "") + '</div><div class="help">' +
          (ST.warmup.next_run ? H("Next run {when}", { when: fmtIso(ST.warmup.next_run) }) : timed ? "" : H("Every library warms up at boot only.")) +
          (used.indexOf("twice_weekly") >= 0 ? " " + H("Twice weekly: this day and three days later.") : "") +
          (used.indexOf("twice_monthly") >= 0 ? " " + H("Twice monthly: the 1st and 15th.") : "") + "</div></div>" +
        '<div class="fld"><span class="lbl">' + H("Manual warm-up") + '</span><div class="acts" style="margin-top:0"><button class="btn" data-act="warmup-run"' + (ST.warmup.running ? " disabled" : "") +
        ">" + H("Run Now") + '</button></div><div class="help">' + (ST.warmup.last_run ? H("Last run {when}", { when: fmtIso(ST.warmup.last_run) }) : H("Hasn’t run yet")) + " · " +
        H("Full speed, every library with a warm-up.") + "</div></div>" +
        "</div>" + estimateHtml() + "</div>";
    }
    var libs = cacheLibs();
    if (!libs.length) return out + '<p class="muted">' + H("No libraries yet. Connect Plex in <a href=\"/system\" data-nav>System</a>.") + "</p></section>";
    out += '<div class="pflibs">' + libs.map(function (l) {
      var kind = libKind(l), names = PF_NAMES[kind];
      var wk = WARM_KIND[kind], lim = SEC_LIMITS[wk], def = (ST.kind_seconds || {})[wk];
      var secs = warmChoice(l.key, "seconds") != null ? warmChoice(l.key, "seconds") : def, sched = warmChoice(l.key, "schedule") || "default";
      var order = PF_START.filter(function (v) { return names[v]; });
      var start = libChoice(l.key, "start");
      // Video and audiobooks (M4B) have metadata players read before starting; music files don't
      var id = "f-lib-" + l.key, unit = kind === "music" ? T("s of playback") : T("s of playback + metadata");
      return '<div class="pflib">' + libHead(l, kind) +
        '<div class="pflib-grid">' +
        '<div><label class="sublbl" for="' + id + '" aria-label="' + H("Lead-in length") + '"><span class="val" data-val="' + id + '">' + esc(secText(secs, unit)) + "</span>" +
          (secs !== def ? ' <button type="button" class="revert" data-libsec-revert="' + esc(l.key) + '" title="' + H("Back to the default ({value})", { value: def + " s" }) + '">↺</button>' : "") + "</label>" +
          '<input id="' + id + '" type="range" min="' + lim[0] + '" max="' + lim[1] + '" step="1" value="' + esc(secs) + '" data-libsec="' + esc(l.key) + '" data-unit="' + esc(unit) + '"></div>' +
        '<div><span class="sublbl">' + H("Warm-up") + pickRevert("warm:" + l.key, sched, dynOptions("warmup.schedule", WARM_ORDER, WARM_NAMES)) + "</span>" +
          pickHtml("warm:" + l.key, sched, dynOptions("warmup.schedule", WARM_ORDER, WARM_NAMES), T("{name}: warm-up", { name: l.title })) +
          fallbackNote("warmup.schedule", sched, WARM_NAMES) + "</div>" +
        '<div><span class="sublbl">' + H("Prefetch after Now Playing Cache") + pickRevert("lib:" + l.key + ".start", start, dynOptions("prefetch.start", order, names, PF_NOTES.start)) + "</span>" +
          pickHtml("lib:" + l.key + ".start", start, dynOptions("prefetch.start", order, names, PF_NOTES.start), T("{name}: Lead-in Cache prefetch", { name: l.title })) +
          fallbackNote("prefetch.start", start, names) + "</div>" +
        "</div></div>";
    }).join("") + "</div></section>";
    return out;
  }

  // ---- Whole File Cache: whole files Prefetch reads near the end of what's playing, per library.
  function wholeFileCard() {
    var out = '<section class="card" id="wholefile"><div class="head"><h2>' + H("Whole File Cache") + '</h2><span style="flex:1"></span>' + guideBtn("wholefile") + "</div>" +
      '<p class="lede">' + H("Whole files read near the end of what’s playing, so the next one plays entirely from cache.") + "</p>" +
      guide("wholefile", [
        [H("When it helps"), H("Binge watching, and listening to an album or audiobook straight through: no pause before the next episode or track, and skips in it land in cache.")],
        [H("How it reads"), H("At 80% or 90% through, reads whole files in Plex’s own order: the next episode, or the rest of the season (tracks and albums, chapters and books, movies in a collection likewise). " +
          "The next one always comes first, straight into RAM; with “Rest of the season” the others follow to the SSD in the background. The season is that of what plays next. “Nothing” turns it off for a library. " +
          "Episodes already watched aren’t offered: reading them in full would only push lead-ins off the SSD.")],
        [H("Cost"), H("The next one counts against Prefetch’s RAM limit (<a href=\"/caching#arc\" data-nav>Cache Settings</a>); the rest of the season only passes through RAM on its way to the SSD, where each episode takes the space of many lead-ins. " +
          "A file isn’t read again within its cooldown, and Now Playing Cache doesn’t re-read one that’s here.")],
        [H("L2ARC"), L2ARC_NOTE]]);
    var libs = cacheLibs();
    if (!libs.length) return out + '<p class="muted">' + H("No libraries yet. Connect Plex in <a href=\"/system\" data-nav>System</a>.") + "</p></section>";
    out += '<div class="pflibs">' + libs.map(function (l) {
      var kind = libKind(l), names = PF_NAMES[kind];
      var end = libChoice(l.key, "end"), endAt = libChoice(l.key, "end_at");
      var endOpts = dynOptions("prefetch.end", PF_END, names, PF_NOTES.end, ((ST.prefetch_kind_defaults || {})[kind] || {}).end);
      // (the "when" choice has no name of its own: its ↺ goes beside the library's)
      return '<div class="pflib">' + libHead(l, kind, pickRevert("lib:" + l.key + ".end_at", endAt, PF_END_AT)) +
        // Reads as a sentence: "At 90% watched, prefetch: the next episode"
        '<div class="pf-end">' + pickHtml("lib:" + l.key + ".end_at", endAt, PF_END_AT, T("{name}: when to prefetch whole files", { name: l.title })) +
        '<span class="pf-word">' + H("Prefetch:") + pickRevert("lib:" + l.key + ".end", end, endOpts) + "</span>" +
        pickHtml("lib:" + l.key + ".end", end, dynOptions("prefetch.end", PF_END, names, PF_NOTES.end, ((ST.prefetch_kind_defaults || {})[kind] || {}).end), T("{name}: Whole File Cache prefetch", { name: l.title })) + "</div>" +
        fallbackNote("prefetch.end", end, names, ((ST.prefetch_kind_defaults || {})[kind] || {}).end) + "</div>";
    }).join("") + "</div></section>";
    return out;
  }

  // ---- How Long Cached Data Lasts: every read again is timed, so each one says whether what
  // was cached N hours earlier was still there (plexcache/cachelog.py). RAM and SSD apart, and
  // per library. Two views of the same reads: "Hit age" (still cached, by age) and "Fresh vs
  // stale" (hit rate on each side of an age you pick; only changes what's shown).
  var lastsView = "age", lastsTier = "ram", lastsStale = {}, lastsFirst = true;
  try {
    lastsView = localStorage.getItem("pc-lasts-view") || "age";
    lastsTier = localStorage.getItem("pc-lasts-tier") || "ram";
    lastsFirst = localStorage.getItem("pc-lasts-first") !== "hide";
  } catch (e) { /* storage unavailable */ }
  // The Hit age columns: each is "up to" this many seconds (they line up with cachelog.AGES)
  var LASTS_COLS = [600, 1800, 7200, 21600, 86400, 172800, 604800, 2592000, 15552000];
  var LASTS_FEW = 3;  // fewer reads than this at an age: shown faded (the same minimum as a suggested cooldown)

  // A measured age as a time, abbreviated: "45 min", "2 h 50 min", "3 d 4 h"
  function ageShort(sec) {
    var m = Math.max(1, Math.round(sec / 60));
    if (m < 60) return T("{m} min", { m: m });
    if (m < 2 * 1440) { var h = Math.floor(m / 60), mm = m % 60; return mm ? T("{h} h {m} min", { h: h, m: mm }) : T("{h} h", { h: h }); }
    var d = Math.floor(m / 1440), hh = Math.round((m % 1440) / 60);
    if (hh === 24) { d += 1; hh = 0; }
    return hh ? T("{d} d {h} h", { d: d, h: hh }) : T("{d} d", { d: d });
  }
  // "30m", "2h", "7d", "6mo": for the narrow column headings (the full text is on hover)
  function ageTiny(sec) {
    var m = Math.round(sec / 60);
    if (m < 60) return T("{n}m", { n: m });
    if (m < 2880) return T("{n}h", { n: Math.round(m / 60) });
    if (m >= 43200 * 2) return T("{n}mo", { n: Math.round(m / 43200) });
    return T("{n}d", { n: Math.round(m / 1440) });
  }
  function lastsLibs(route) {
    // "All libraries" (every read, including those from before reads were noted per library), then each library
    return [{ key: "*", name: T("All libraries"), rows: route.rows, all: true, suggest: route.suggest, fully: route.fully }].concat((route.libs || []).map(function (l) {
      return { key: l.key, name: l.name || (l.key ? T("Library {key}", { key: l.key }) : T("Other")), rows: l.rows, fine: l.fine, first: l.first, cached: l.cached !== false, l2arc: l.l2arc, suggest: l.suggest, fully: l.fully };
    }));
  }

  function lastsAgeHtml(route) {
    var cols = LASTS_COLS.length, prev = 0;
    var heads = LASTS_COLS.map(function (upto) {
      var t = prev ? T("{from} to {to}", { from: ageText(prev), to: ageText(upto) }) : T("Up to {to}", { to: ageText(upto) });
      var short = prev ? T("{from} to {to}", { from: ageTiny(prev), to: ageTiny(upto) }) : T("Up to {to}", { to: ageTiny(upto) });
      prev = upto;
      return '<div class="lh" title="' + esc(t) + '">' + esc(short) + "</div>";
    }).join("");
    var out = '<div class="tbl-wrap"><div class="lgrid" style="--cols:' + cols + '"><div class="lh lh0">' + H("Read again after") + "</div>" + heads;
    lastsLibs(route).forEach(function (lib) {
      var lo = 0;
      var pts = LASTS_COLS.map(function (upto) {
        var kept = 0, n = 0;
        lib.rows.forEach(function (r) { if (r.upto > lo && r.upto <= upto) { kept += r.kept; n += r.kept + r.gone; } });
        lo = upto;
        return { n: n, pct: n ? Math.round(100 * kept / n) : null };
      });
      // The line: one point per column, at the height the share still cached reaches
      var W = 100 * cols, Hh = 60, pad = 7, X = function (i) { return (i + 0.5) * 100; }, Y = function (p) { return pad + (100 - p) / 100 * (Hh - 2 * pad); };
      var d = "";
      pts.forEach(function (q, i) { if (q.pct != null) d += (d ? "L" : "M") + X(i) + " " + Y(q.pct); });
      var dots = pts.map(function (q, i) {
        return q.pct == null ? "" : '<span class="ldot' + (q.n < LASTS_FEW ? " few" : "") + '" style="left:' + ((i + 0.5) / cols * 100) + "%;top:" + Y(q.pct) + 'px"></span>';
      }).join("");
      out += '<div class="lname' + (lib.all ? " all" : "") + '">' + esc(lib.name) +
        '<div class="lcool">' + (lib.suggest ? H("Cooldown: up to {age}", { age: ageShort(lib.suggest) }) :
          lib.fully && lib.fully.n >= SUGGEST_MIN && !lib.fully.age ? H("Cooldown: none to suggest") : H("Cooldown: not enough data")) + "</div></div>" +
        '<div class="lline"><svg viewBox="0 0 ' + W + " " + Hh + '" preserveAspectRatio="none" aria-hidden="true">' +
        '<line x1="0" x2="' + W + '" y1="' + Y(100) + '" y2="' + Y(100) + '"/><line x1="0" x2="' + W + '" y1="' + Y(0) + '" y2="' + Y(0) + '"/>' +
        (d ? '<path d="' + d + '"/>' : "") + "</svg>" + dots +
        (d ? "" : '<span class="lnone">' + (lib.all ? H("No history yet") : lastsNone(lib, H("No history yet"))) + "</span>") + "</div>" +
        '<div class="lsub"></div>' + pts.map(function (q) {
          var cls = !q.n ? " muted" : q.n < LASTS_FEW ? " muted" : q.pct >= 95 ? " ok" : q.pct < 70 ? " bad" : "";
          return '<div class="lpct' + cls + '">' + (q.n ? q.pct + "%" : "—") + "</div>";
        }).join("") +
        '<div class="lsub lsamp">' + H("Samples") + "</div>" + pts.map(function (q) {
          return '<div class="lsamp">' + (q.n ? fmtNum(q.n) : "") + "</div>";
        }).join("");
    });
    return out + "</div></div>" + '<p class="help" style="margin:8px 0 0">' + H("How much was still cached when read again that long after it was cached. Faded: fewer than {n} reads so far.", { n: LASTS_FEW }) + "</p>";
  }

  function lastsStaleIndex(fine, route) {
    if (lastsStale[lastsTier] != null) return lastsStale[lastsTier];
    var want = route.suggest || 7200;  // the suggested cooldown, or 2 hours
    for (var i = 0; i < fine.length; i++) if (fine[i] >= want) return i;
    return fine.length - 1;
  }
  function lastsFreshRows(route, fine, idx) {
    var libs = lastsLibs(route).filter(function (l) { return !l.all; });
    if (!libs.length) return '<p class="muted" style="margin:8px 0 0">' + H("No libraries yet. They show up here once Plex has been scanned.") + "</p>";
    // "All libraries" here adds up the libraries (reads from before they were noted apart don't have the finer ages)
    var all = { name: T("All libraries"), all: true, fine: fine.map(function () { return [0, 0]; }), first: [0, 0] };
    libs.forEach(function (l) {
      l.fine.forEach(function (c, i) { all.fine[i][0] += c[0]; all.fine[i][1] += c[1]; });
      all.first[0] += l.first[0]; all.first[1] += l.first[1];
    });
    // A segment holds its share and its count, one above the other (cut short if it's narrow: the tooltip has both)
    var seg = function (cls, flex, top, bottom, tip) {
      return '<div class="' + cls + '" style="flex:' + flex + ' 1 0" title="' + esc(tip) + '"><span>' + top + "</span><span>" + bottom + "</span></div>";
    };
    var side = function (hit, miss, left, flex) {
      var n = hit + miss, cls = "fhalf" + (left ? " L" : " R"), fl = ' style="flex:' + flex + ' 1 0"';
      if (!n) return '<div class="' + cls + '"' + fl + '><div class="fbar"><div class="fempty">' + H("No reads") + "</div></div></div>";
      var hp = Math.round(100 * hit / n), bar = "";
      if (hit) bar += seg("fhit", hp, hp + "%", PH(hit, "{n} hit", "{n} hits"), T("{pct}% hit · {n} of {total} reads", { pct: hp, n: fmtNum(hit), total: fmtNum(n) }));
      if (miss) bar += seg("fmiss", 100 - hp, (100 - hp) + "%", PH(miss, "{n} miss", "{n} misses"), T("{pct}% miss · {n} of {total} reads", { pct: 100 - hp, n: fmtNum(miss), total: fmtNum(n) }));
      return '<div class="' + cls + '"' + fl + '><div class="fbar">' + bar + "</div></div>";
    };
    // Each row: fresh | virgin | stale. The two sides are each their own 100% (hits and misses);
    // virgin has no hit rate (how long it had been cached isn't known), its width is its share of all reads.
    var split = function (l) {
      var o = { fh: 0, fm: 0, sh: 0, sm: 0, first: lastsFirst ? l.first[0] + l.first[1] : 0 };
      l.fine.forEach(function (c, i) { if (i <= idx) { o.fh += c[0]; o.fm += c[1]; } else { o.sh += c[0]; o.sm += c[1]; } });
      o.n = o.fh + o.fm + o.sh + o.sm + o.first;
      o.mid = !o.first ? 0 : Math.max(4, 100 * o.first / o.n);
      return o;
    };
    var age = ageShort(fine[idx]), mid = lastsFirst ? 24 : 0;  // (the rows' middles vary; the headings just mark left, middle and right)
    var out = '<div></div><div class="fband fheads' + (lastsFirst ? " ends" : "") + '"><div class="fhead L" style="flex:' + (100 - mid) / 2 + ' 1 0"><strong>' + H("Fresh cache") + '</strong><div class="help">' + H("Cached {age} ago or less", { age: age }) + "</div></div>" +
      (lastsFirst ? '<div class="fhead M" style="flex:' + mid + ' 1 0"><strong>' + H("Virgin cache") + '</strong><div class="help">' + H("Not in the log before") + "</div></div>" : "") +
      '<div class="fhead R" style="flex:' + (100 - mid) / 2 + ' 1 0"><strong>' + H("Stale cache") + '</strong><div class="help">' + H("Cached more than {age} ago", { age: age }) + "</div></div></div>";
    return out + [all].concat(libs).map(function (l) {
      var o = split(l), half = (100 - o.mid) / 2;
      var name = '<div class="lname' + (l.all ? " all" : "") + '">' + (l.all ? "" : libIcon(l.key)) + esc(l.name) + "</div>";
      // Nothing to show is worth knowing too: say why instead of leaving the library out
      if (!l.all && !(o.n + l.first[0] + l.first[1])) {
        var why = lastsNone(l, H("No reads"));
        return name + '<div class="fband"><div class="fhalf L" style="flex:1 1 0"><div class="fbar"><div class="fempty">' + why + "</div></div></div>" +
          '<div class="fhalf R" style="flex:1 1 0"><div class="fbar"><div class="fempty">' + why + "</div></div></div></div>";
      }
      var pct = o.n ? Math.round(100 * o.first / o.n) : 0;
      var midHtml = !o.mid ? "" : '<div class="fmid" style="flex:' + o.mid + ' 1 0"><div class="fbar">' +
        seg("ffirst", 1, pct + "%", esc(T("{n} of {total} virgin", { n: fmtNum(o.first), total: fmtNum(o.n) })),
          T("Virgin: {n} of {total} reads ({pct}%)", { n: fmtNum(o.first), total: fmtNum(o.n), pct: pct })) + "</div></div>";
      return name + '<div class="fband">' + side(o.fh, o.fm, true, half) + midHtml + side(o.sh, o.sm, false, half) + "</div>";
    }).join("");
  }
  // Why a library has nothing to show: not cached at all, no SSD cache on its pool (SSD view), or
  // simply nothing measured yet (``otherwise``)
  function lastsNone(lib, otherwise) {
    if (lib.cached === false) return H("Not being cached");
    if (lastsTier === "ssd" && lib.l2arc === false) return H("No SSD cache (L2ARC) on this library’s pool");
    return otherwise;
  }
  function lastsFreshHtml(route, fine) {
    var idx = lastsStaleIndex(fine, route), age = ageShort(fine[idx]);
    return '<div class="fslider"><label for="lasts-stale">' + H("Stale after") + "</label>" +
      '<input type="range" id="lasts-stale" min="0" max="' + (fine.length - 1) + '" step="1" value="' + idx + '" data-lasts-stale>' +
      '<strong id="lasts-stale-val">' + esc(age) + "</strong></div>" +
      '<div class="fopts"><button class="link" type="button" data-lasts-first aria-expanded="' + lastsFirst + '">' + (lastsFirst ? H("Hide virgin cache") : H("Show virgin cache")) + "</button></div>" +
      '<div class="frows" id="lasts-fresh">' + lastsFreshRows(route, fine, idx) + "</div>" +
      '<p class="help" style="margin:8px 0 0"><span class="fkey fhit"></span> ' + H("Hit: read from cache") + ' &nbsp; <span class="fkey fmiss"></span> ' + H("Miss: read from the disk") +
      '<span id="lasts-first-key"' + (lastsFirst ? "" : " hidden") + '> &nbsp; <span class="fkey ffirst"></span> ' + H("Virgin: established cache entry, history unknown") + "</span></p>";
  }
  function lastsFreshUpdate(idx) {
    var sv = ST.survival, route = sv && sv.routes[lastsTier];
    if (!route) return;
    if (idx == null) idx = lastsStaleIndex(sv.fine, route);
    lastsStale[lastsTier] = idx;
    main.querySelector("#lasts-stale-val").textContent = ageShort(sv.fine[idx]);
    main.querySelector("#lasts-fresh").innerHTML = lastsFreshRows(route, sv.fine, idx);
    main.querySelector("#lasts-first-key").hidden = !lastsFirst;
    var b = main.querySelector("[data-lasts-first]");
    b.setAttribute("aria-expanded", String(lastsFirst));
    b.textContent = lastsFirst ? T("Hide virgin cache") : T("Show virgin cache");
  }
  // Fully cached age, day by day (cachelog.fully_cached_days): one line per library, every
  // library in bold. A hollow point means no misses that day, so it lasted at least that long.
  var FC_COLORS = ["#3987e5", "#e66b39", "#1baf7a", "#e0a000", "#d96a95", "#7f6bd9", "#2f9e2f", "#e05555"];
  var lastsRange = "30d";
  try { lastsRange = localStorage.getItem("pc-lasts-range") || "30d"; } catch (e) { /* storage unavailable */ }
  function lastsFullyHtml(route) {
    // 24 hours and 7 days hour by hour, 30 days day by day. Points: [age, no misses, re-reads, youngest miss]
    var src = lastsRange === "30d" ? route.days : route.hours;
    if (!src) return "";
    var cut = lastsRange === "24h" ? src.starts.length - 24 : 0;
    var pick = function (a) { return (a || []).slice(cut).map(function (p) { return p && { age: p[0], bound: !!p[1], n: p[2], miss: p[3] }; }); };
    var d = { days: src.starts.slice(cut), all: pick(src.all), libs: {} };
    (route.libs || []).forEach(function (l) { d.libs[l.key] = pick(src.libs[l.key]); });
    var hourly = lastsRange !== "30d";
    var series = [{ name: T("All libraries"), pts: d.all, all: true, color: "var(--ink)" }].concat((route.libs || []).map(function (l, i) {
      return { name: l.name || T("Library {key}", { key: l.key }), pts: d.libs[l.key] || [], color: FC_COLORS[i % FC_COLORS.length] };
    }));
    var seg = function (v, label) { return '<button role="tab" type="button" aria-selected="' + (v === lastsRange) + '" data-lasts-range="' + v + '">' + label + "</button>"; };
    var ranges = '<div class="seg small" role="tablist" aria-label="' + H("Range") + '" style="margin:0 0 10px">' + seg("24h", H("24 hours")) + seg("7d", H("7 days")) + seg("30d", H("30 days")) + "</div>";
    var W = 700, Hh = 260, L = 64, R = 12, Tp = 12, B = 30, n = d.days.length;
    var lo = Math.log(60), hi = Math.log(15552000);
    var X = function (i) { return L + (W - L - R) * (n > 1 ? i / (n - 1) : 0.5); };
    var Y = function (sec) { var v = Math.log(Math.max(60, Math.min(15552000, sec || 60))); return Tp + (Hh - Tp - B) * (1 - (v - lo) / (hi - lo)); };
    var svg = '<svg viewBox="0 0 ' + W + " " + Hh + '" class="fcchart" role="img" aria-label="' + esc(T("Fully cached age by day, for every library and each one apart")) + '">';
    [[60, "0"], [600, "10m"], [3600, "1h"], [21600, "6h"], [86400, "1d"], [604800, "7d"], [2592000, "30d"]].forEach(function (t) {
      svg += '<line class="fcgrid" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(t[0]) + '" y2="' + Y(t[0]) + '"/><text class="fctick" x="' + (L - 8) + '" y="' + (Y(t[0]) + 4) + '" text-anchor="end">' + t[1] + "</text>";
    });
    d.days.forEach(function (t, i) {
      var at = new Date(t * 1000), label = null;
      if (lastsRange === "30d") { if (i % 7 === (n - 1) % 7) label = at.toLocaleDateString(LOCALE, { month: "short", day: "numeric" }); }
      else if (lastsRange === "7d") { if (at.getHours() === 0) label = at.toLocaleDateString(LOCALE, { weekday: "short", day: "numeric" }); }
      else if (at.getHours() % 3 === 0) label = at.toLocaleTimeString(LOCALE, { hour: "numeric" });
      if (label) svg += '<line class="fcgrid" x1="' + X(i) + '" x2="' + X(i) + '" y1="' + Tp + '" y2="' + (Hh - B) + '" opacity=".5"/><text class="fctick" x="' + X(i) + '" y="' + (Hh - 8) + '" text-anchor="' + (i > n - 3 ? "end" : "middle") + '">' + esc(label) + "</text>";
    });
    var any = false;
    series.slice().reverse().forEach(function (s) {
      var path = "", pen = false;
      s.pts.forEach(function (p, i) {
        if (!p) { pen = false; return; }
        any = true;
        path += (pen ? "L" : "M") + X(i) + " " + Y(p.age);
        pen = true;
      });
      if (path) svg += '<path d="' + path + '" fill="none" style="stroke:' + s.color + '" stroke-width="' + (s.all ? 3 : 2) + '" stroke-linejoin="round" stroke-linecap="round"/>';
      s.pts.forEach(function (p, i) {
        if (!p) return;
        var at = new Date(d.days[i] * 1000);
        var day = hourly ? at.toLocaleString(LOCALE, { weekday: "short", hour: "numeric", minute: "2-digit" }) : at.toLocaleDateString(LOCALE, { weekday: "short", month: "short", day: "numeric" });
        var what = p.bound ? T("at least {age}", { age: ageShort(p.age) }) : (p.age ? ageShort(p.age) : T("under {age}", { age: ageShort(p.miss) }));
        svg += '<circle cx="' + X(i) + '" cy="' + Y(p.age) + '" r="' + (lastsRange === "7d" ? (s.all ? 3 : 2.5) : (s.all ? 5 : 4)) + '" style="stroke:' + s.color + ";fill:" + (p.bound ? "var(--panel)" : s.color) + '" stroke-width="2"><title>' +
          esc(day + " · " + s.name + ": " + what + " · " + P(p.n, "{n} re-read", "{n} re-reads")) + "</title></circle>";
      });
    });
    svg += "</svg>";
    var legend = '<div class="fclegend">' + series.map(function (s) {
      return '<span><i style="background:' + s.color + (s.all ? ";height:4px" : "") + '"></i>' + esc(s.name) + "</span>";
    }).join("") + '<span><i class="ring"></i>' + H("no misses: at least this long") + "</span></div>";
    if (!any) return ranges + legend + '<p class="muted" style="margin:8px 0 0">' + H("No history yet. A point shows up for each hour in which something cached is read again.") + "</p>";
    return ranges + legend + '<div class="fcwrap">' + svg + "</div>" +
      '<p class="help" style="margin:8px 0 0">' + (hourly ? H("Each hour: everything read again in that hour, up to this age, was still cached. Hover a point for the details.") :
        H("Each day: everything read again that day, up to this age, was still cached. Hover a point for the details.")) + "</p>";
  }

  // ---- Library cached over time (coverage.py): each hour, the share of a library cached within
  // the last hour, 24 hours, 7 days, 30 days and 6 months. Fetched on its own (it's big), every few minutes.
  var COV = null, covAt = 0, covLib = "*", covMode = "bands", covBy = 0, covRange = 1000, covHover = null;
  try { covMode = localStorage.getItem("pc-cov-mode") || "bands"; covBy = Number(localStorage.getItem("pc-cov-by") || 0); } catch (e) { /* storage unavailable */ }
  var COV_NAMES = [N("1 hour"), N("24 hours"), N("7 days"), N("30 days"), N("6 months")];
  function covFetch() {
    if (Date.now() - covAt < 5 * 60 * 1000) return;
    covAt = Date.now();
    api("/api/coverage").then(function (d) { COV = d; if (page === "activity" && !editing()) render(); }).catch(function () { covAt = 0; });
  }
  function covData() {
    if (!COV) return null;
    var pts = covLib === "*" ? COV.all : (COV.libs[covLib] || []);
    var hours = Math.max(1, Math.round(Math.exp(covRange / 1000 * Math.log(Math.max(2, COV.hours.length)))));
    var from = Math.max(0, COV.hours.length - hours);
    return { t: COV.hours.slice(from), v: pts.slice(from).map(function (p) { return p ? p[covBy].map(function (x) { return x / 10; }) : null; }), hours: hours };
  }
  function covBody() {
    var d = covData();
    if (!d || !d.t.length) return '<p class="muted" style="margin:8px 0 0">' + H("No history yet: the first hour is recorded once the libraries have been scanned.") + "</p>";
    var n = d.t.length, last = null;
    for (var j = n - 1; j >= 0 && !last; j--) last = d.v[j] && { i: j, v: d.v[j] };
    var at = covHover != null && d.v[covHover] ? { i: covHover, v: d.v[covHover] } : last;
    var when = at ? new Date(d.t[at.i] * 1000).toLocaleString(LOCALE, { weekday: "short", month: "short", day: "numeric", hour: "numeric" }) : "";
    // the shares first, then the hour on the right: a changing date doesn't push the numbers about
    var legend = '<div class="covlegend">' + [4, 3, 2, 1, 0].map(function (k) {
      return '<span><i class="covsw cov' + k + '"></i>' + esc(T(COV_NAMES[k])) + " <b>" + (at ? (Math.round(at.v[k] * 10) / 10) + "%" : "—") + "</b></span>";
    }).join("") + '<span class="covwhen">' + esc(at && covHover != null ? when : T("Now")) + "</span></div>";
    var W = 700, Hh = 260, L = covMode === "lines" ? 44 : 10, R = 10, Tp = 10, B = 26;
    var X = function (i) { return L + (W - L - R) * (n > 1 ? i / (n - 1) : 0.5); };
    var step = Math.max(1, Math.ceil(n / 350)), pts = [];
    for (var i = 0; i < n; i += step) {
      var chunk = d.v.slice(i, i + step).filter(Boolean);
      if (chunk.length) pts.push({ i: i + Math.floor((Math.min(n, i + step) - i - 1) / 2), v: [0, 1, 2, 3, 4].map(function (k) { return chunk.reduce(function (a, r) { return a + r[k]; }, 0) / chunk.length; }) });
    }
    // the scale: up to the most shown, rounded up to a round share (a library mostly in RAM may be a few %)
    var most = pts.reduce(function (a, p) { return Math.max(a, Math.max.apply(null, p.v)); }, 0);
    var topPct = [1, 2, 5, 10, 20, 25, 50, 100].filter(function (v) { return v >= most * 1.1; })[0] || 100;
    var svg = '<svg viewBox="0 0 ' + W + " " + Hh + '" class="covchart" role="img" aria-label="' + esc(T("Share of the library cached, hour by hour")) + '">';
    if (covMode === "lines") {
      var Y = function (v) { return Tp + (Hh - Tp - B) * (1 - v / topPct); };
      [0, 0.25, 0.5, 0.75, 1].forEach(function (f) {
        var v = topPct * f;
        svg += '<line class="fcgrid" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text class="fctick" x="' + (L - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + (Math.round(v * 100) / 100) + "%</text>";
      });
      [4, 3, 2, 1, 0].forEach(function (k) {
        var path = pts.map(function (p, m) { return (m ? "L" : "M") + X(p.i).toFixed(1) + " " + Y(p.v[k]).toFixed(1); }).join("");
        svg += '<path class="covline cov' + k + '" d="' + path + '"/>';
      });
    } else {
      // Bands: each span centred on the middle line, as wide as its share — the smallest in the centre
      var mid = Tp + (Hh - Tp - B) / 2, half = (Hh - Tp - B) / 2;
      svg += '<line class="fcgrid" x1="' + L + '" x2="' + (W - R) + '" y1="' + mid + '" y2="' + mid + '"/>';
      [4, 3, 2, 1, 0].forEach(function (k) {
        var top = pts.map(function (p) { return X(p.i).toFixed(1) + "," + (mid - half * p.v[k] / topPct).toFixed(1); });
        var bot = pts.map(function (p) { return X(p.i).toFixed(1) + "," + (mid + half * p.v[k] / topPct).toFixed(1); }).reverse();
        svg += '<polygon class="cov' + k + '" points="' + top.concat(bot).join(" ") + '"/>';
      });
      svg += '<text class="fctick" x="' + (W - R) + '" y="' + (Tp + 10) + '" text-anchor="end">' + esc(topPct === 100 ? T("full height = the whole library") : T("full height = {pct}% of the library", { pct: topPct })) + "</text>";
    }
    for (var q = 0; q <= 5; q++) {
      var ii = Math.round((n - 1) * q / 5), dd = new Date(d.t[ii] * 1000);
      var lab = d.hours <= 48 ? dd.toLocaleTimeString(LOCALE, { hour: "numeric" }) : dd.toLocaleDateString(LOCALE, { month: "short", day: "numeric" });
      svg += '<text class="fctick" x="' + X(ii) + '" y="' + (Hh - 6) + '" text-anchor="' + (q === 0 ? "start" : q === 5 ? "end" : "middle") + '">' + esc(lab) + "</text>";
    }
    if (covHover != null) svg += '<line class="covcross" x1="' + X(covHover) + '" x2="' + X(covHover) + '" y1="' + Tp + '" y2="' + (Hh - B) + '"/>';
    svg += '<rect class="covhit" x="' + L + '" y="0" width="' + (W - L - R) + '" height="' + Hh + '" data-cov-n="' + n + '"/></svg>';
    return legend + svg;
  }
  function coverageCard() {
    covFetch();
    var libs = COV ? (COV.names || []).map(function (x) { return { key: x[0], title: x[1] || x[0] }; }) : [];
    var tab = function (attr, v, cur, label) { return '<button role="tab" type="button" aria-selected="' + (String(v) === String(cur)) + '" ' + attr + '="' + v + '">' + label + "</button>"; };
    var hours = COV ? Math.max(1, Math.round(Math.exp(covRange / 1000 * Math.log(Math.max(2, COV.hours.length))))) : 1;
    var span = hours < 48 ? P(hours, "{n} hour", "{n} hours") : hours < 24 * 60 ? P(Math.round(hours / 24), "{n} day", "{n} days") : P(Math.round(hours / 720), "{n} month", "{n} months");
    return '<section class="card" id="coverage" style="margin-bottom:20px"><h2>' + H("Library Cached Over Time") + "</h2>" +
      '<p class="hint">' + H("Each hour: the share of the library this app cached within the last hour, 24 hours, 7 days, 30 days and 6 months — in RAM or on the SSD. Wider means more of it. Hover for any hour.") + "</p>" +
      '<div class="lctl"><div class="seg small strong" role="tablist" aria-label="' + H("Library") + '">' + tab("data-cov-lib", "*", covLib, H("All libraries")) +
      libs.map(function (l) { return tab("data-cov-lib", l.key, covLib, libIcon(l.key) + esc(l.title)); }).join("") + "</div>" +
      '<div class="seg small strong" role="tablist" aria-label="' + H("Chart") + '">' + tab("data-cov-mode", "bands", covMode, H("Bands")) + tab("data-cov-mode", "lines", covMode, H("Lines")) + "</div>" +
      '<div class="seg small strong" role="tablist" aria-label="' + H("Count") + '">' + tab("data-cov-by", 0, covBy, H("By files")) + tab("data-cov-by", 1, covBy, H("By size")) + "</div></div>" +
      '<div class="fslider"><label for="cov-range">' + H("Show the last") + '</label><input type="range" id="cov-range" min="0" max="1000" step="1" value="' + covRange + '" data-cov-range><strong id="cov-range-val">' + esc(span) + "</strong></div>" +
      '<div id="cov-body">' + covBody() + "</div></section>";
  }

  function lastsCard() {
    var sv = ST.survival;
    if (!sv) return "";
    var noL2 = !!(S.general && S.general.no_l2arc);
    if (noL2) lastsTier = "ram";  // "I won't use an L2ARC": only RAM is measured, so no switch
    var route = sv.routes[lastsTier] || sv.routes.ram;
    var segBtn = function (attr, val, cur, label) { return '<button role="tab" type="button" aria-selected="' + (val === cur) + '" ' + attr + '="' + val + '">' + label + "</button>"; };
    var sug = (route.suggest ? H("Suggested cooldown: up to {age} (fully cached age, last 7 days)", { age: ageShort(route.suggest) }) : noSuggestion(route)) +
      (route.auto ? " · " + H("Automatic uses up to {age} now", { age: ageShort(route.auto) }) : "");
    var out = '<section class="card" id="lasts" style="margin-bottom:20px"><div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:baseline"><h2>' + H("How Long Cached Data Lasts") + "</h2>" +
      guideBtn("lasts") + "</div>" +
      '<div class="muted" style="font-size:13px">' + H("Since {when}", { when: fmtTime(sv.since) }) + ' · <button class="link" type="button" data-act="lasts-reset">' + H("Start again") + "</button>" +
      ' · <a class="link" href="/api/cachelog/export" download>' + H("Export log") + "</a></div>" +
      guide("lasts", [
        [H("What’s measured"), H("Every time something is read again — by Now Playing Cache, Prefetch or warm-up — the read is timed, so it tells us whether what was cached that long ago was still there. " +
          "Nothing is read again within its cooldown, so ages shorter than the cooldown aren’t measured: a short cooldown, even none, collects data fastest.")],
        [H("RAM and SSD"), H("<strong>RAM</strong>: reads meant to stay in RAM. Every re-read counts, its age measured from the last time the app read that data (what the cooldown counts from too); a write from before the last reboot isn’t counted, since a reboot empties RAM. " +
          "<strong>SSD</strong>: reads on their way to the L2ARC; a hit there doesn’t move the data, so every re-read counts. The L2ARC is per pool, so some libraries may have one and others not.")],
        [H("Hit age"), H("How much was still cached when read again, by how long ago it was cached.")],
        [H("Fully cached age"), H("Of the re-reads in a window, ordered by age: the age of the oldest one still cached before the first that had to come from the disks. Everything read again up to that age was still cached; with no misses at all, it lasted at least that long (≥). " +
          "Shown hour by hour (or day by day over 30 days), for every library and each one apart: the peaks and valleys show how much what the NAS is doing changes it. " +
          "The suggested cooldown is the fully cached age over the last 7 days (with at least 3 re-reads), and so is the Automatic one — per library where it has enough reads. A miss at the youngest age measured sets Automatic to 10 minutes, so shorter ages get measured again.")],
        [H("Fresh vs stale"), H("Pick an age with the slider — it only changes what’s shown here, not caching. Reads of data cached that long ago or less are fresh, older ones stale, and each side shows its hits and misses. " +
          "<strong>Virgin cache</strong>, in the middle: data the log hasn’t seen in its 6 months. It may have been cached before that, or by Plex itself, so its history is unknown and it has no hit rate — but a wide middle means mostly new data is being read, not data read again.")]
      ]);
    out += '<div class="lctl"><div class="seg small strong" role="tablist" aria-label="' + H("View") + '">' +
      segBtn("data-lasts-view", "age", lastsView, H("Hit age")) + segBtn("data-lasts-view", "fresh", lastsView, H("Fresh vs stale")) + segBtn("data-lasts-view", "fully", lastsView, H("Fully cached age")) + "</div>" +
      (noL2 ? "" : '<div class="seg small strong" role="tablist" aria-label="' + H("Cache") + '">' +
      segBtn("data-lasts-tier", "ram", lastsTier, H("RAM") + " " + mark("ram-only")) + segBtn("data-lasts-tier", "ssd", lastsTier, H("SSD (L2ARC)") + " " + mark("ssd-only")) + "</div>") + "</div>";
    if (!(lastsTier === "ssd" && ST.l2arc === "absent"))
      out += '<p class="help" style="margin:0 0 12px">' + sug + " · " + PH(route.first, "{n} cached for the first time", "{n} cached for the first time") + "</p>";
    // The SSD cache is per pool: none at all, or only under some libraries, is worth saying
    if (lastsTier === "ssd") {
      var noL2 = (route.libs || []).filter(function (l) { return l.cached !== false && l.l2arc === false; });
      if (ST.l2arc === "absent") out += '<div class="note" style="margin:0 0 12px">' + H("There’s no SSD cache (L2ARC) on the pools holding your libraries, so nothing is cached on an SSD to measure. See <a href=\"/ssd\" data-nav>SSD Cache</a>.") + "</div>";
      else if (noL2.length) out += '<div class="note" style="margin:0 0 12px">' + H("No SSD cache (L2ARC) on the pool of: {list}. Their data only stays in RAM.", { list: noL2.map(function (l) { return l.name; }).join(", ") }) + "</div>";
    }
    out += lastsView === "fresh" ? lastsFreshHtml(route, sv.fine) : lastsView === "fully" ? lastsFullyHtml(route) : lastsAgeHtml(route);
    return out + "</section>";
  }

  // Each library's optimized files: what caching keeps of them, on average per file — as each was when this
  // app first saw it (before the optimizer touched it), and now. Optimizing again widens the gap. Files that
  // were optimized somewhere else before they came here have no known original: left out, and counted.
  function optimizedHistoryCard() {
    var libs = ST.optimized_history || [];
    if (!libs.length) return "";
    var out = '<section class="card" style="margin-bottom:20px">' + heading(esc(T("Optimized Files")),
      H("What caching keeps of each library’s optimized files, on average per file: as each file was when first seen, before any optimizing, and as it is now. Kept for good."));
    libs.forEach(function (l) {
      out += '<div class="lmixsum"><div class="what">' + libIcon(l.key) + esc(l.title || l.key) + "</div>";
      if (l.files) out += '<div class="det muted">' + esc(P(l.files, "{n} optimized file", "{n} optimized files · average per file", { n: fmtNum(l.files) })) + "</div>" +
        mixChart([[T("Original"), l.original], [T("Existing"), l.now, ROW_TIPS.existing]]);
      if (l.unknown) out += '<div class="det muted">' + esc(P(l.unknown, "The origin of {n} file is unknown (it was optimized before it came here), so it’s left out.",
        "The origins of {n} files are unknown (they were optimized before they came here), so they’re left out.", { n: fmtNum(l.unknown) })) + "</div>";
      out += "</div>";
    });
    return out + "</section>";
  }

  function pageActivity() {
    var z = ST.zfs, arc = z.arc, a = ST.activity;
    var out = top(T("Metrics"), H("Whether caching is making playback better — and where it needs a hand."), "");
    out += periodSwitch() + reportHtml(ST.reports && ST.reports[actPeriod], z, arc, a);
    out += optimizedHistoryCard();

    // sessions
    var ss = ST.playback.sessions;
    out += '<section class="card" style="margin-bottom:20px"><h2 style="margin-bottom:14px">' + H("Playing Now") + "</h2>";
    if (ST.playback.error) out += '<div class="note">' + esc(ST.playback.error) + "</div>";
    if (!ss.length) out += '<p class="muted" style="margin:0">' + H("Nothing is playing.") + "</p>";
    ss.forEach(function (s) {
      out += '<div class="sess"><div class="withposter">' + posterImg(s.poster) + '<div><div class="what">' + esc(s.label) + '</div><div class="muted" style="font-size:13px">' + esc(s.player || T("Unknown player")) + " · " +
        (s.local === false ? H("remote") : H("local")) + " · " + (s.transcoding ? H("transcoding") : H("direct play")) + " · " + H("{pct}% watched", { pct: s.progress }) + "</div></div></div><div>";
      if (!s.jobs.length) out += '<span class="muted" style="font-size:13px">' + H("No cache work for this stream yet") + "</span>";
      var nextJobs = s.jobs.filter(function (j) { return j.feature === "nextepisode" && j.state !== "cancelled"; });
      var shown = s.jobs;
      if (nextJobs.length > 3) {
        // A long queue (music, audiobooks): one line for it, plus what's being read now.
        var doneN = nextJobs.filter(function (j) { return j.state === "done"; }).length;
        var bytes = nextJobs.reduce(function (t, j) { return t + (j.read || 0); }, 0);
        var now = nextJobs.filter(function (j) { return j.state === "reading" && !j.paused; })[0];
        var held = nextJobs.filter(function (j) { return j.paused; })[0];
        out += '<div class="muted" style="font-size:13px">' + H("Prefetch") + '</div><div style="display:flex;gap:10px;align-items:center;margin:4px 0 8px;flex-wrap:wrap">' +
          badge(doneN === nextJobs.length ? "done" : "running", doneN === nextJobs.length ? T("Done") : T("Reading")) + "<span>" +
          PH(nextJobs.length, "{done} of {n} track ready · {size}", "{done} of {n} tracks ready · {size}", { done: doneN, size: fmtBytes(bytes) }) +
          (now ? " · " + H("now: {title}", { title: now.target }) : "") +
          (held ? " · " + H("L2ARC-only reads paused: {why}", { why: msg(held.paused) }) : "") + "</span></div>";
        shown = s.jobs.filter(function (j) { return j.feature !== "nextepisode"; });
      }
      shown.forEach(function (j) {
        var label = { waiting: T("Waiting"), reading: T("Reading"), done: T("Done"), failed: T("Failed"), cancelled: T("Stopped") }[j.state];
        var feat = j.feature === "nextepisode" ? T("Prefetch") : T("Now Playing Cache");
        var of = j.whole ? " " + T("(whole file)") : (j.size ? " " + T("({pct}% of the file)", { pct: Math.round(j.length / j.size * 100) }) : "");
        var amt = j.state === "reading" ? T("{read} of {size} loaded", { read: fmtBytes(j.read), size: fmtBytes(j.length) }) + of :
          (j.state === "done" ? T("{size} loaded", { size: fmtBytes(j.read) }) + of + " " + ago(j.finished) :
          (j.state === "waiting" ? fmtBytes(j.length) + of + (j.delay ? " · " + T("starts {n} s after playback began", { n: j.delay }) : "") : fmtBytes(j.length)));
        if (j.paused) amt += " · " + T("paused: {why}", { why: msg(j.paused) });
        out += '<div class="muted" style="font-size:13px">' + esc(feat) + '</div><div style="display:flex;gap:10px;align-items:center;margin:4px 0 8px;flex-wrap:wrap">' +
          (j.paused ? badge("warn", T("Paused")) : badge(j.state, label)) + "<span>" + esc(j.target) + " · " + esc(amt) + "</span></div>";
      });
      out += "</div></div>";
    });
    out += "</section>";

    // runs
    out += '<section class="card" style="margin-bottom:20px"><div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:baseline"><h2>' + H("Warm-up Runs") + info(H("“L2ARC grew” compares cache size before and after each run. ZFS fills the L2ARC gradually, so it can lag a little. " +
      "If it stays near zero and “Already cached” stays low on repeat runs, reads aren’t landing in the L2ARC — Health will flag it."), T("warm-up runs")) + '</h2><span class="muted" style="font-size:13px">' +
      (isOn("warmup") ? (ST.warmup.next_run ? H("Next run {when}", { when: fmtIso(ST.warmup.next_run) }) : H("No timed runs")) : H("No library has a warm-up")) + "</span></div>" + warmupStatus();
    if (!a.runs.length) out += '<p class="muted">' + H("No runs yet.") + "</p>";
    else {
      out += '<div class="tbl-wrap"><table class="tbl"><thead><tr>' + [N("Started"), N("Files"), N("Read"), N("Already cached"), N("L2ARC grew"), N("Next day"), N("Took"), N("Result")].map(function (c) {
        return '<th scope="col">' + H(c) + (c === "Next day" ? info(H("A day after each run, one quick read from the start of a sample of the files it warmed up shows how many are still on the SSD. " +
          "The L2ARC reuses its space oldest first, so this is how long lead-ins really last on your NAS: if it’s low, warm up more often, or send less of everything else to the SSD (fewer whole files in the Whole File Cache)."), T("Next day")) : "") + "</th>"; }).join("") + "</tr></thead><tbody>";
      a.runs.slice(0, 20).forEach(function (r) {
        out += '<tr><td class="nw">' + esc(fmtTime(r.started)) + (r.manual ? " " + badge("manual", T("manual")) : "") + '</td><td class="nw">' + fmtNum(r.files) + '</td><td class="nw">' + esc(fmtBytes(r.bytes)) +
          '</td><td class="nw">' + (r.hit_rate == null ? "—" : Math.round(r.hit_rate * 100) + "%") + '</td><td class="nw">' +
          (r.l2_growth == null ? "—" : (r.l2_growth >= 0 ? "+" : "−") + esc(fmtBytes(Math.abs(r.l2_growth)))) + '</td><td class="nw">' +
          (r.next_day == null ? '<span class="muted">' + (Date.now() / 1000 - r.finished < 86400 ? H("tomorrow") : "—") + "</span>" : r.next_day + "%") + '</td><td class="nw">' +
          esc(fmtDur(r.finished - r.started)) + "</td><td>" +
          badge(r.result, { ok: T("OK"), stopped: T("Stopped"), errors: T("Errors") }[r.result] || r.result) + (r.errors ? ' <span class="muted" style="font-size:13px">' + H("{n} unreadable", { n: r.errors }) + "</span>" : "") + "</td></tr>";
      });
      out += "</tbody></table></div>";

    }
    out += "</section>";

    out += lastsCard();
    out += coverageCard();

    // events
    out += '<section class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center"><h2>' + H("Recent Events") + '</h2></div><div class="log" style="margin-top:12px">';
    if (!a.events.length) out += '<div class="muted">' + H("Nothing yet.") + "</div>";
    a.events.forEach(function (e) {
      var t = new Date(e.t * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      var kind = EVENT_KINDS[e.kind] ? T(EVENT_KINDS[e.kind]) : e.kind;
      out += '<div class="' + (e.level === "warn" ? "w" : "") + '">' + esc(t) + "  " + esc((kind + "             ").slice(0, Math.max(13, kind.length + 1))) + " " + esc(e.message) + "</div>";
    });
    out += "</div></section>";
    return out;
  }

  // ---- Activity: period switch and the "Over time" chart
  var actPeriod = "week", chartMetric = "skips";
  try { actPeriod = localStorage.getItem("pc-period") || "week"; chartMetric = localStorage.getItem("pc-chart") || "skips"; } catch (e) { /* storage unavailable */ }
  var PERIOD_NAMES = [["today", T("Today")], ["week", T("7 days")], ["month", T("30 days")], ["year", T("12 months")]];
  function periodSwitch() {
    return '<div class="seg" role="tablist" aria-label="' + H("Period") + '">' + PERIOD_NAMES.map(function (p) {
      return '<button role="tab" aria-selected="' + (p[0] === actPeriod) + '" data-period="' + p[0] + '">' + p[1] + "</button>";
    }).join("") + "</div>";
  }
  var CHARTS = {
    skips: { name: T("Skips"), keys: [["skips_cached", T("From cache")], ["skips_missed", T("From disk")]], fmt: fmtNum,
      hint: T("Each bar: skips that landed in cached data, and those that had to wait on the disks.") },
    preloaded: { name: T("Data cached ahead"), keys: [["preloaded", T("Read ahead")]], fmt: fmtBytes,
      hint: T("How much Now Playing Cache and Prefetch read ahead for viewers.") },
    retention: { name: T("Still cached"), keys: [["retention", T("Still in RAM 30 min later")]], fmt: pctFmt, max: 100,
      hint: T("Average share of cached-ahead data still in RAM 30 minutes after loading.") },
    busy: { name: T("Disk load"), keys: [["busy", T("Disks busy during streams")]], fmt: pctFmt, max: 100,
      hint: T("How busy the media pool’s disks were, on average, while people watched.") }
  };
  function pctFmt(v) { return T("{n}%", { n: v }); }
  // A bar's label in the reader's language: the server sends each bar's start time.
  function barLabel(d, period) {
    if (d.start == null) return d.label;
    var t = new Date(d.start * 1000);
    if (period === "today") return t.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    if (period === "week") return t.toLocaleDateString(LOCALE, { weekday: "short" });
    if (period === "month") return t.toLocaleDateString(LOCALE, { month: "short", day: "numeric" });
    return t.toLocaleDateString(LOCALE, { month: "short" });
  }
  // ---- Skips served from cache, in detail: each library and the total, and over the period.
  // Each skip counts by how likely it came from cache (report.py: skips_p, skips_by).
  var skipsLib = "*";
  function skipsCard(r) {
    var t = r.tiles.skips, libs = (t.by_lib || []);
    var pct = function (p, n) { return n ? Math.round(100 * p / n) : null; };
    var bar = function (p, n) {
      var v = pct(p, n);
      if (v == null) return '<div class="sbar"><div class="fempty" style="flex:1">' + H("No skips") + "</div></div>";
      return '<div class="sbar" title="' + esc(T("{pct}% expected from cache, over {n} skips", { pct: v, n: fmtNum(n) })) + '">' +
        (v ? '<div class="fhit" style="flex:' + v + ' 1 0">' + (v >= 10 ? v + "%" : "") + "</div>" : "") +
        (v < 100 ? '<div class="fmiss" style="flex:' + (100 - v) + ' 1 0">' + (100 - v >= 10 ? (100 - v) + "%" : "") + "</div>" : "") + "</div>";
    };
    var known = libs.reduce(function (a, l) { return a + l.total; }, 0);
    var rows = [{ key: "*", name: T("All libraries"), total: t.total, expected: t.expected, all: true }].concat(libs);
    if (t.total > known) rows.push({ key: "?", name: T("Library not recorded"), total: t.total - known, expected: Math.max(0, t.expected - libs.reduce(function (a, l) { return a + l.expected; }, 0)) });
    var out = '<section class="card" id="skips" style="margin-bottom:20px"><h2>' + H("Skips Served from Cache") + "</h2>" +
      '<p class="hint">' + H("{period}: each skip counted by how likely it came from cache — where it landed, how long ago that part was cached, and how long cached data lasts in its library.", { period: r.span }) + "</p>";
    out += '<div class="srows">' + rows.map(function (l) {
      return '<div class="lname' + (l.all ? " all" : "") + '">' + (l.all ? "" : libIcon(l.key)) + esc(l.name) + '</div><div>' + bar(l.expected, l.total) + "</div>" +
        '<div class="scount">' + (l.total ? PH(l.total, "{n} skip", "{n} skips") : "") + "</div>";
    }).join("") + "</div>";
    // Over the period: one bar per hour, day or month, as tall as the share expected from cache
    var chips = rows.filter(function (l) { return l.key !== "?"; }).map(function (l) {
      return '<button role="tab" type="button" aria-selected="' + (l.key === skipsLib) + '" data-skips-lib="' + l.key + '">' + libIcon(l.key) + esc(l.name) + "</button>";
    }).join("");
    out += '<div class="seg small strong" role="tablist" aria-label="' + H("Library") + '" style="margin:16px 0 8px">' + chips + "</div>";
    var data = (r.series || []).map(function (d) {
      var nv = skipsLib === "*" ? [d.skips || 0, d.skips_p || 0] : ((d.skips_by || {})[skipsLib] || [0, 0]);
      return { label: barLabel(d, r.period), n: nv[0], p: nv[1] };
    });
    var narrow = window.innerWidth < 640, W = narrow ? 360 : 720, CH = 170, L = 40, R = 8, CT = 8, B = 24, n = Math.max(1, data.length);
    var band = (W - L - R) / n, bw = Math.min(24, Math.max(4, band * 0.6));
    var svg = '<svg class="chart" viewBox="0 0 ' + W + " " + CH + '" role="img" aria-label="' + esc(T("Share of skips expected from cache, {period}", { period: r.span })) + '">';
    [0, 50, 100].forEach(function (v) {
      var y = CT + (CH - CT - B) * (1 - v / 100);
      svg += '<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/><text class="axis" x="' + (L - 6) + '" y="' + (y + 4) + '" text-anchor="end">' + v + "%</text>";
    });
    var every = Math.ceil(n / (narrow ? 6 : 12)), any = false;
    data.forEach(function (d, i) {
      var x = L + band * i + (band - bw) / 2, full = CH - CT - B, base = CH - B;
      if (d.n) {
        any = true;
        var v = d.p / d.n, hit = full * v;
        svg += '<g><title>' + esc(d.label + " — " + T("{pct}% expected from cache, over {n} skips", { pct: Math.round(100 * v), n: fmtNum(d.n) })) + "</title>" +
          '<rect class="smissbar" x="' + x + '" y="' + CT + '" width="' + bw + '" height="' + (full - hit) + '" rx="2"/>' +
          '<rect class="shitbar" x="' + x + '" y="' + (base - hit) + '" width="' + bw + '" height="' + hit + '" rx="2"/></g>';
      }
      if (i % every === 0) svg += '<text class="axis" x="' + (x + bw / 2) + '" y="' + (CH - 6) + '" text-anchor="middle">' + esc(d.label) + "</text>";
    });
    svg += "</svg>";
    out += any ? svg : '<p class="muted" style="margin:6px 0 0">' + H("No skips in this period.") + "</p>";
    return out + '<p class="help" style="margin:8px 0 0"><span class="fkey fhit"></span> ' + H("From cache") + ' &nbsp; <span class="fkey fmiss"></span> ' + H("From the disks") + "</p></section>";
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }
  function chartCard(r) {
    if (!CHARTS[chartMetric]) chartMetric = "skips";  // (e.g. "Next episodes", no longer shown)
    var c = CHARTS[chartMetric], data = (r.series || []).map(function (d) { return Object.assign({}, d, { label: barLabel(d, r.period) }); });
    var tabs = Object.keys(CHARTS).map(function (k) {
      return '<button role="tab" aria-selected="' + (k === chartMetric) + '" data-chart="' + k + '">' + esc(CHARTS[k].name) + "</button>";
    }).join("");
    var totals = data.map(function (d) { return c.keys.reduce(function (t, k) { return t + (d[k[0]] || 0); }, 0); });
    var top = c.max || niceMax(Math.max.apply(null, totals.concat([0])));
    if (!c.max && c.fmt === fmtNum && top % 2) top += 1;  // counts: keep the middle gridline a whole number
    var narrow = window.innerWidth < 640, W = narrow ? 360 : 720, CH = narrow ? 180 : 200, L = 44, R = 8, CT = 10, B = 26, n = Math.max(1, data.length);
    var band = (W - L - R) / n, bw = Math.min(24, Math.max(4, band * 0.6));
    var svg = '<svg class="chart" viewBox="0 0 ' + W + " " + CH + '" role="img" aria-label="' + H("{chart}, {period}", { chart: c.name, period: r.span }) + '">';
    [0, 0.5, 1].forEach(function (f) {
      var y = CT + (CH - CT - B) * (1 - f);
      svg += '<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/><text class="axis" x="' + (L - 6) + '" y="' + (y + 4) + '" text-anchor="end">' +
        esc(c.fmt(Math.round(top * f * 10) / 10)) + "</text>";
    });
    var every = Math.ceil(n / (narrow ? 6 : 12)), any = false;
    data.forEach(function (d, i) {
      var x = L + band * i + (band - bw) / 2, y0 = CH - B, tip = d.label + " — ";
      var parts = [];
      c.keys.forEach(function (k, j) {
        var v = d[k[0]];
        parts.push(T("{what}: {value}", { what: k[1], value: v == null ? "—" : c.fmt(v) }));
        if (!v) return;
        any = true;
        var h = Math.max(1, (CH - CT - B) * Math.min(v, top) / top), last = j === c.keys.length - 1 || !d[c.keys[j + 1] && c.keys[j + 1][0]];
        var gap = j > 0 ? 2 : 0;  // a 2px surface gap between stacked segments
        y0 -= gap;
        var yTop = y0 - h;
        svg += last ? '<path class="s' + j + '" d="M' + x + " " + y0 + "V" + (yTop + Math.min(4, h)) + "q0 -" + Math.min(4, h) + " " + Math.min(4, bw / 2) + " -" + Math.min(4, h) +
          "H" + (x + bw - Math.min(4, bw / 2)) + "q" + Math.min(4, bw / 2) + " 0 " + Math.min(4, bw / 2) + " " + Math.min(4, h) + "V" + y0 + 'Z"/>' :
          '<rect class="s' + j + '" x="' + x + '" y="' + yTop + '" width="' + bw + '" height="' + h + '"/>';
        y0 = yTop;
      });
      svg += '<rect class="hit" x="' + (L + band * i) + '" y="' + CT + '" width="' + band + '" height="' + (CH - CT - B) + '" data-tip="' + esc(tip + parts.join(" · ")) + '"/>';
      if (i % every === 0 || i === n - 1) svg += '<text class="axis" x="' + (L + band * i + band / 2) + '" y="' + (CH - 8) + '" text-anchor="middle">' + esc(d.label) + "</text>";
    });
    svg += "</svg>";
    var legend = c.keys.length > 1 ? '<div class="legend">' + c.keys.map(function (k, j) { return '<span><i class="k' + j + '"></i>' + esc(k[1]) + "</span>"; }).join("") + "</div>" : "";
    var table = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">' + esc(r.period === "today" ? T("Hour") : r.period === "year" ? T("Month") : T("Day")) + "</th>" +
      c.keys.map(function (k) { return '<th scope="col">' + esc(k[1]) + "</th>"; }).join("") + "</tr></thead><tbody>" +
      data.map(function (d) { return "<tr><td>" + esc(d.label) + "</td>" + c.keys.map(function (k) { return "<td>" + (d[k[0]] == null ? "—" : esc(c.fmt(d[k[0]]))) + "</td>"; }).join("") + "</tr>"; }).join("") +
      "</tbody></table></div>";
    return '<section class="card chartcard" style="margin-bottom:20px"><div class="head"><h2>' + H("Over Time") + '</h2><span style="flex:1"></span><div class="seg small" role="tablist" aria-label="' + H("Chart") + '">' + tabs + "</div></div>" +
      '<p class="hint">' + esc(c.hint) + "</p>" + legend +
      (any ? '<div class="chartwrap">' + svg + '<div class="chart-tip" hidden></div></div>' : '<p class="muted" style="margin:18px 0">' + H("Nothing measured in this period yet.") + "</p>") +
      details("chart-table", H("Show as a table"), table) + "</section>";
  }

  // The Activity report card: a verdict, four numbers, a chart, and what needs attention.
  function reportHtml(r, z, arc, a) {
    if (!r) return "";
    var t = r.tiles, out = banner(r.verdict, false);
    function rtile(title, big, sub, status, why, go) {
      // go: the detailed view this tile opens (a card further down the page)
      return '<div class="card tile rtile r-' + status + (go ? ' rgo" data-goto="' + go + '" role="button" tabindex="0" title="' + esc(T("Show the details")) : "") + '"><div class="muted" style="font-size:13px">' + esc(title) + info(esc(why), title) + '</div><div class="big">' + big +
        '</div><div class="muted" style="font-size:13px">' + sub + (go ? ' <span class="rmore" aria-hidden="true">›</span>' : "") + "</div></div>";
    }
    function frac(n, d) { return d ? H("{n} <small>of {total}</small>", { n: n, total: d }) : "—"; }
    out += '<div class="tiles rtiles">';
    // Weighted by how likely each one really came from cache: how long ago that part was cached,
    // and how long cached data lasts in its library (Metrics → How Long Cached Data Lasts)
    function est(x, total) { return total ? Math.round(100 * x / total) : null; }
    var sk = est(t.skips.expected, t.skips.total);
    out += rtile(T("Skips served from cache"), sk == null ? "—" : H("{n}%", { n: sk }),
      t.skips.total ? PH(t.skips.total, "expected, over {n} skip", "expected, over {n} skips") : H("no skips yet"),
      sk == null ? "none" : (sk >= 90 ? "ok" : "warn"),
      T("When someone jumps ahead or back (more than 30 seconds), was that part cached? Each skip counts by how likely it really came from cache: where it landed, how long ago that part was cached (by Now Playing Cache, a whole file, or the lead-in), and how long cached data lasts in that library. Added up, that’s the share of skips expected to have started straight away instead of waiting on the disks."), "skips");
    var fc = t.fully || {};
    out += rtile(T("Fully cached age"), fc.n ? (fc.age ? esc(ageShort(fc.age)) : H("under {age}", { age: ageShort(fc.miss) })) : "—",
      // (the first miss only when it says something the age above doesn't)
      fc.n ? (fc.bound ? PH(fc.n, "no misses in {n} re-read", "no misses in {n} re-reads") :
        (fc.age && ageShort(fc.miss) !== ageShort(fc.age) ? H("first miss at {age}", { age: ageShort(fc.miss) }) + " · " : "") + PH(fc.n, "{n} re-read", "{n} re-reads")) : H("no history yet"),
      !fc.n ? "none" : "ok",
      T("Everything read again up to this age was still cached: of the re-reads in this period, ordered by age, the oldest one before the first that had to come from the disks. With no misses, it lasted at least this long."), "lasts:fully");
    out += rtile(T("Plays started from cache"), t.lead.pct == null ? "—" : H("{n}%", { n: t.lead.pct }),
      t.lead.streams ? PH(t.lead.streams, "expected, over {n} play", "expected, over {n} plays") : H("no plays yet"),
      t.lead.pct == null ? "none" : (t.lead.pct >= 50 ? "ok" : "warn"),
      T("Of everything played, how much started from a lead-in (or whole file) that was already cached — by warm-up, Prefetch, or an earlier play. Each play counts by how likely that lead-in was still there, from how long ago it was cached and how long cached data lasts in its library.", "lasts:fresh"));
    out += rtile(T("Data cached ahead"), t.preloaded.bytes ? esc(fmtBytes(t.preloaded.bytes)) : "—",
      (t.preloaded.cached_pct != null ? H("{pct}% was in cache already", { pct: t.preloaded.cached_pct }) : H("by Now Playing Cache and Prefetch")) +
        (t.preloaded.ssd_bytes ? " · " + H("{size} more to the SSD", { size: fmtBytes(t.preloaded.ssd_bytes) }) : ""), t.preloaded.bytes ? "ok" : "none",
      T("How much Now Playing Cache and Prefetch read ahead for viewers, into RAM. The part already in cache cost the disks nothing (each 32 MiB is timed: four times faster than the disks means it came from cache). Prefetch’s L2ARC-only reads, on their way to the SSD in the background, are shown separately."));
    out += "</div>" + chartCard(r) + skipsCard(r);

    if (r.attention.length) {
      out += '<section class="card" style="margin-bottom:20px"><h2>' + H("Needs Attention") + '</h2><p class="hint">' + H("Only shows up when something could be better.") + "</p>";
      r.attention.forEach(function (x) {
        out += itemRow({ status: x.status, title: x.title, detail: x.detail, fix: x.fix },
          x.link ? '<a href="' + esc(x.link) + '" data-nav style="font-size:13px">' + H("Go there") + "</a>" : "");
      });
      out += "</section>";
    }

    // RAM limits right now, and how busy the disks were while people watched
    function ramShort(r) {  // what ran out of room in the period, or that nothing did
      var bits = [];
      if (r.ram.limited) bits.push(P(r.ram.limited, "{n} file only partly fitted", "{n} files only partly fitted"));
      if (r.ram.no_room) bits.push(P(r.ram.no_room, "{n} time there was no room at all", "{n} times there was no room at all"));
      return bits.length ? H("{period}: {list}", { period: r.span, list: bits.join(" · ") }) : H("{period}: nothing ran out of room.", { period: r.span });
    }
    function meter(label, used, limit) {
      var pct = limit ? Math.min(100, used / limit * 100) : 0;
      return '<div style="margin:6px 0 12px"><div style="display:flex;justify-content:space-between;font-size:13px"><span>' + esc(label) + '</span><span class="muted">' +
        H("{used} of {limit}", { used: fmtBytes(used), limit: fmtBytes(limit) }) + '</span></div><div class="meter"><div style="width:' + pct.toFixed(1) + '%"></div></div></div>';
    }
    out += '<div class="cols2" style="margin-bottom:20px"><section class="card"><h2>' + H("RAM Limits") + '</h2><p class="hint">' + H("In use right now, by streams that are playing.") + "</p>" +
      meter(T("Now Playing Cache"), r.ram.nowplaying.in_use, r.ram.nowplaying.limit) + meter(T("Prefetch"), r.ram.nextepisode.in_use, r.ram.nextepisode.limit) +
      '<div class="muted" style="font-size:13px">' + ramShort(r) + "</div></section>";
    out += '<section class="card"><h2>' + H("Disks While People Watched") + '</h2><p class="hint">' + H("How busy the media pool’s disks were during streams.") + "</p>";
    if (r.disk.avg == null) out += '<p class="muted" style="margin:0">' + H("Measured during streams; nothing yet.") + "</p>";
    else out += '<div class="big" style="font-size:28px">' + H("{n}% <small class=\"muted\" style=\"font-size:14px\">average</small>", { n: r.disk.avg }) + '</div><div class="meter"><div style="width:' + r.disk.avg + '%"></div></div>' +
      '<div class="muted" style="font-size:13px;margin-top:6px">' + PH(r.disk.streams, "Peak {peak}% · over {n} stream", "Peak {peak}% · over {n} streams", { peak: r.disk.peak }) + "</div>";
    if (z.l2_total && arc) {
      out += '<div class="muted" style="font-size:13px;margin-top:12px">' + H("L2ARC (SSD cache): {used} of {total} filled", { used: fmtBytes(arc.l2_asize), total: fmtBytes(z.l2_total) }) + "</div>";
    }
    out += "</section></div>";
    return out;
  }

  function secretInput(sec, key, label, id) {
    var set = S[sec][key + "_set"];
    // A plain text field, not a password field or one masked with CSS: browsers treat either as a
    // login and offer to save it. It's cleared the moment it's saved, so the key isn't left on screen.
    return '<div class="fld"><label for="' + id + '">' + esc(label) + '</label><input id="' + id + '" type="text" class="secret" autocomplete="off" autocapitalize="off" spellcheck="false" ' +
      'data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other" placeholder="' + (set ? H("Saved — paste to replace") : H("Paste it here")) +
      '" value="" data-secret="' + sec + "." + key + '"></div>';
  }
  // Fields with no meaningful default (where your servers are): no ↺
  var NO_REVERT = { "plex.url": 1, "truenas.url": 1, "mqtt.host": 1, "mqtt.username": 1 };
  function textInput(sec, key, label, id, help, placeholder) {
    var h = splitHelp(help);
    return '<div class="fld"><label for="' + id + '">' + esc(label) + info(h.more, label) + (NO_REVERT[sec + "." + key] ? "" : revertBtn(sec, key, "")) + '</label><input id="' + id + '" type="text" value="' + esc(S[sec][key]) + '" placeholder="' + esc(placeholder || "") +
      '" data-set="' + sec + "." + key + '">' + helpDiv(h) + "</div>";
  }

  // The Plex Cache at each mirror's site (peerlink.py): found from that Plex server's addresses, paired by itself through the
  // synced folders when that's safe, then asked directly — what Mirror Checks' disk comparison relies on. One block per
  // server some library is mirrored by, with its five lights and Pair Now, Unpair; the found/paired/talking light is on the Mirror page too.
  var PEER_LIGHTS = [["found", N("Plex Cache at the mirror’s site")], ["paired", N("Paired")], ["talking", N("Talking")], ["folders", N("Folder sync")], ["version", N("Version and clock")]];
  function mirrorName(key) {
    var p2 = ST.plex2 || {}, p = (p2.pairs || {})[key], st = (p2.states || {})[key] || {};
    return (p && p.name) || st.name || T("the mirror");
  }
  function peerServerBlock(lk, pe) {
    var lt = lk.lights || {}, sid = lk.server, url = ((S.peer || {}).servers || {})[sid] || {};
    var out = '<fieldset class="sub"><legend>' + esc(lk.name) + "</legend>" +
      '<div class="lights">' + PEER_LIGHTS.map(function (l) { return lightFor(lt[l[0]], T(l[1])); }).join("") + "</div>" +
      '<div class="grid"><div class="fld"><label for="s-peerurl-' + esc(sid) + '">' + H("Its address") + '</label>' +
      '<input id="s-peerurl-' + esc(sid) + '" type="text" value="' + esc(url.url || "") + '" placeholder="' + esc(lk.detected || "http://192.168.1.20:8642") + '" data-peerurl="' + esc(sid) + '">' +
      (url.url ? ' <button type="button" class="revert" data-peerurl-revert="' + esc(sid) + '" title="' + H("Back to the default ({value})", { value: T("found by itself") }) + '">↺</button>' : "") +
      '<div class="help">' + H("Found by itself. Enter it when the other one runs on another port, or is reached at another address.") + "</div></div></div>";
    if (lk.paired) out += '<div class="det">' + H("Fingerprint {fp}: the other one shows the same when the two are paired with each other.", { fp: lk.fingerprint }) + "</div>";
    if (lk.window) {  // (open here: a link to the other one's Pair Now, where it's known)
      var there = ((lk.paired || {}).url || lk.detected || url.url || "").replace(/\/+$/, "");
      out += '<div class="note">' + H("Pairing is open for {n} more minutes: press Pair Now on the other Plex Cache too.", { n: Math.max(1, Math.ceil(lk.window / 60)) }) +
        (there ? ' <a href="' + esc(there) + '/system#peer" target="_blank" rel="noopener">' + H("Open Pair Now on the other one ↗") + "</a>" : "") + "</div>";
    }
    var asked = !!((pe.asked || {})[sid]);
    out += '<div class="acts"><button class="btn' + ((lt.found && lt.found.status === "ok" && !lk.paired) || asked ? " pri" : "") + '" type="button" data-act="peer-pair" data-server="' + esc(sid) + '"' + (lk.window ? " disabled" : "") + ">" + H("Pair Now") + "</button>" +
      info(H("Opens pairing for 10 minutes, without waiting for the synced folder or the mirror’s address: press it on both, then compare the fingerprints. For when pairing by itself can’t, or is off."), T("Pair Now")) +
      (lk.paired ? ' <button class="link" type="button" data-act="peer-unpair" data-server="' + esc(sid) + '" title="' + H("Forgets the pairing on both sides. Neither pairs again by itself: Pair Now on both does.") + '">' + H("Unpair") + "</button>" : "") + "</div>";
    return out + "</fieldset>";
  }
  function peerCard() {
    var pe = ST.peer || {}, ps = S.peer || {}, own = pe.own || {}, links = Object.keys(pe.links || {}).map(function (k) { return pe.links[k]; });
    var out = '<section class="card" id="peer"><h2>' + H("Mirror Plex Cache") + ' <span class="muted" style="font-weight:400;font-size:14px">· ' + H("optional") + "</span></h2>" +
      '<p class="hint">' + H("The Plex Cache at the site of each server your libraries are mirrored by, if there is one. The two talk directly, over your network or a VPN, so Mirror Checks can compare both sites’ files.") +
      info(H("They find each other from the mirror’s Plex addresses, and pair by themselves when it’s safe: each writes a public key into the synced .plexcache folder, and a pairing is taken only from the mirror’s address, with the key the folder carried. " +
        "Every request after that is signed with a key the two work out from those, which never leaves either side. What they ask each other only reads: how each side is, and a library’s list of files.<br><br>" +
        "Meant for your network, a VPN or Tailscale: don’t open its port to the internet."), T("Mirror Plex Cache")) + "</p>" +
      '<div style="margin-top:10px">' + checkbox("peer", "enabled", T("Look for a Plex Cache at each mirror’s site"),
        { s: H("Untick when there’s none there: Mirror Checks then compare the Plexes only.") }) + "</div>";
    if (!ps.enabled) return out + "</section>";
    out += checkbox("peer", "auto_pair", T("Pair by itself"),
      { s: H("When the other one answers from the mirror’s address with the key the synced folder carried."),
        more: H("Off: the two pair only when you press Pair Now on both, within 10 minutes of each other. Then compare the fingerprints the two show: they’re the same when each is paired with the other.") });
    out += '<div class="grid">' + number("peer", "port", T("The port this one answers on"), 0, 65535,
      { s: own.port ? H("0 works it out: {port} now.", { port: String(own.port) }) : H("0 works it out."),
        more: H("The port the other Plex Cache reaches this one on, told to it through the synced folder. 0 works it out: from TrueNAS’s settings for this app, or the port this page is open on. Set it when a port mapping or a proxy changes it.") }) + "</div>";
    if ((own.addresses || []).length) out += '<div class="det">' + H("This one answers at {addresses}, port {port}.", { addresses: own.addresses.join(", "), port: String(own.port) }) + "</div>";
    if (!links.length) return out + '<div class="det muted">' + H("No library is mirrored yet: the Plex Cache at a mirror’s site is looked for once one is (<a href=\"/mirrors\" data-nav>Mirror</a>).") + "</div></section>";
    out += links.map(function (lk) { return peerServerBlock(lk, pe); }).join("") +
      '<div class="acts"><button class="btn" type="button" data-act="peer-test"' + (pe.busy ? " disabled" : "") + ">" + (pe.busy ? H("Testing…") : H("Test Again")) + "</button></div>";
    return out + "</section>";
  }
  // Mirrored Libraries: one row per library here, its mirror chosen from a list — not decided (nothing assumed), no mirror,
  // or a library of one of the account's servers, each with how many of a sample of this library's files it has (sync.py
  // discover: Look for Mirrors, weekly, and by itself when the page opens — lookIfDue; the same look compares the mirror
  // chosen over all of them: plex2.match_rate) — under a light per server in use (reached, its libraries read) and one for
  // the Plex Cache at its site. The state is per library (plex2.libraries): any server for any library. Nothing is said of
  // a library before it was looked at, and a mirror chosen whose files aren't this library's gets a warning in place of
  // Mirrored.
  var MIRROR_LIBS_HELP = N("Whether another Plex server has the same files is a choice per library, since one server can mirror some of your libraries and not others, and two libraries can be mirrored by two servers. <strong>Not decided</strong> (how every library starts): nothing is assumed — Health asks, and converted files’ originals aren’t deleted, since a mirror that isn’t known could lose its item. <strong>No mirror</strong>: this server’s alone. <strong>A library of another server</strong>: converting waits for both servers, Sync Libraries and Mirror Checks compare the two, the file tools refuse what a folder sync would carry wrongly, and Convert Folder to Child Dataset converts both sides — through the Plex Cache at the other site (<a href=\"/system#peer\" data-nav>System → Mirror Plex Cache</a>), whose light is above the table.<br><br>The list holds every library of the kind on your account’s other servers, each with how many of a sample of this library’s files it has, by path (<strong>Look for Mirrors</strong>, and weekly while a library isn’t decided): nearly all of them, and it looks like the mirror. The same look compares the mirror chosen over all of this library’s files, and runs by itself when this page opens after an hour, or with a library it hasn’t scored: most of them not there, and its row warns instead of saying Mirrored — the wrong library, or a folder sync that’s behind. A server is reached when a library of it is chosen: its addresses are tried, local ones first and each at the address itself rather than through its plex.direct name, and the first that answers from here is kept; Plex’s relay is the last resort, and the light says when it’s the one in use.");
  // the lights: per server some library is mirrored by, whether it answers and its libraries are read, and the Plex Cache at
  // its site — the first of that link's lights that isn't green, since that one says what's missing, with where to fix it
  function mirrorLights(p2, pe) {
    var servers = p2.servers || [], ps = S.peer || {}, links = pe.links || {};
    if (!servers.length) return '<div class="lights">' + light("off", T("No mirror yet"), T("Choose a library’s mirror below: its server is reached from then on.")) + "</div>";
    var out = "";
    servers.forEach(function (sv) {
      // (reached through Plex's relay only — a Plex-run proxy, slow, limited and apt to drop: said, with the way out)
      var relayHint = T("Only Plex’s relay reaches {name} from here: slow, limited, and it drops. Give this NAS a direct way to it — Remote Access on in its Plex settings, a VPN between the sites, or enter an address this NAS can reach, below.", { name: sv.name });
      if (!sv.reached) out += light("fail", T("{name} isn’t reached", { name: sv.name }), T("None of its addresses answered from here. Choose its library again, or enter an address this NAS can reach, below."));
      else if (sv.error && !sv.known) out += light("fail", sv.relay ? T("Plex’s relay to {name} isn’t answering", { name: sv.name }) : T("{name} isn’t answering", { name: sv.name }), sv.error + (sv.relay ? " " + relayHint : ""));
      else if (!sv.known) out += light("testing", T("{name}: reading its libraries…", { name: sv.name }), sv.url);
      else if (sv.relay) out += light("warn", sv.error ? T("{name} answered through Plex’s relay, then stopped", { name: sv.name }) : T("{name} answers through Plex’s relay", { name: sv.name }), (sv.error ? sv.error : T("{url} · {n} libraries", { url: sv.url, n: fmtNum((sv.libraries || []).length) })) + " " + relayHint);
      else out += light(sv.error ? "warn" : "ok", sv.error ? T("{name} answered, then stopped", { name: sv.name }) : T("{name} answers", { name: sv.name }), sv.error ? sv.error : T("{url} · {n} libraries", { url: sv.url, n: fmtNum((sv.libraries || []).length) }));
      var lk = links[sv.id], lt = (lk || {}).lights || {};
      if (!ps.enabled) out += light("off", T("No Plex Cache at {name}’s site", { name: sv.name }), T("Not looked for: Mirror Checks compare the two Plexes only. [System → Mirror Plex Cache](/system#peer)"));
      else if (!lk) out += light("off", T("Plex Cache at {name}’s site", { name: sv.name }), T("Looked for once the server is reached."));
      else {
        var first = ["found", "paired", "talking"].map(function (k) { return lt[k]; }).filter(function (r) { return r && r.status !== "ok"; })[0] || lt.talking;
        out += first ? light(first.status, msg(first.text), msg(first.detail || "") + (first.status === "ok" ? "" : " " + T("How to fix it: [System → Mirror Plex Cache](/system#peer).")))
          : light("testing", T("Plex Cache at {name}’s site", { name: sv.name }), "");
      }
    });
    return '<div class="lights">' + out + "</div>";
  }
  // the list a library's mirror is chosen from: not decided, no mirror, then every library of its kind on each server — the
  // ones in use first (their libraries as read), then those found by looking — each named with its server, and with how
  // many of this library's files it has: of a sample, where looking counted; of all of them, for the mirror chosen
  function mirrorOptions(l, st, p2, sy) {
    var key = String(l.key), servers = [], byId = {}, counts = (p2.counts || {})[key];
    (p2.servers || []).forEach(function (sv) {
      byId[sv.id] = { id: sv.id, name: sv.name, libs: (sv.libraries || []).filter(function (t) { return t.kind === l.kind; }).map(function (t) { return { key: t.key, title: t.title, found: null, of: 0 }; }) };
      servers.push(byId[sv.id]);
    });
    (sy.candidates || []).forEach(function (c) {
      if (!c.url) return;
      var libs = (c.libraries || []).filter(function (t) { return t.kind === l.kind; }).map(function (t) {
        var m = (t.matches || {})[key]; return { key: t.key, title: t.title, found: m ? m.found : null, of: m ? m.of : 0 }; });
      if (byId[c.id]) {  // (the same server, as looking saw it: its counts added to what was read)
        var have = {};
        byId[c.id].libs.forEach(function (t) { have[t.key] = t; });
        libs.forEach(function (t) { if (have[t.key]) { have[t.key].found = t.found; have[t.key].of = t.of; } else byId[c.id].libs.push(t); });
      } else { byId[c.id] = { id: c.id, name: c.name, libs: libs }; servers.push(byId[c.id]); }
    });
    var cur = st.state === "mirror" ? (st.server || "") + ":" + (st.there_key || st.key) : st.state, seen = false;
    var opts = '<option value="unset"' + (cur === "unset" ? " selected" : "") + ">" + H("Not decided") + '</option><option value="none"' + (cur === "none" ? " selected" : "") + ">" + H("No mirror") + "</option>";
    servers.forEach(function (sv) {
      if (!sv.libs.length) return;
      opts += '<optgroup label="' + esc(sv.name) + '">' + sv.libs.map(function (t) {
        var v = sv.id + ":" + t.key, sel = v === cur || (st.state === "mirror" && st.server === sv.id && !st.key && t.title.toLowerCase() === String(l.title).toLowerCase()); seen = seen || sel;
        var found = sel && counts ? counts.found : t.found, of = sel && counts ? counts.of : t.of;
        return '<option value="' + esc(v) + '"' + (sel ? " selected" : "") + ">" + esc(T("{library} ({server})", { library: t.title, server: sv.name })) +
          (found != null ? " · " + esc(T("{found} of {of} files there", { found: fmtNum(found), of: fmtNum(of) })) : "") + "</option>";
      }).join("") + "</optgroup>";
    });
    if (st.state === "mirror" && !seen) {
      var sv = (p2.servers || []).filter(function (x) { return x.id === st.server; })[0];
      opts += '<option value="' + esc(cur) + '" selected>' + esc(!sv || !sv.known ? T("{library} ({server}): reading…", { library: st.key || l.title, server: st.name || st.server }) : T("{library} ({server}): not there now", { library: st.key || l.title, server: st.name || st.server })) + "</option>";
    }
    return opts;
  }
  function mirrorLibrariesCard() {
    var p2 = ST.plex2 || {}, sy = ST.sync || {}, pe = ST.peer || {}, libs = ((ST.library || {}).libraries || []).filter(function (l) { return l.kind; });
    var out = '<section class="card" id="mirrorlibs">' + heading(esc(T("Mirrored Libraries")), MIRROR_LIBS_HELP) +
      '<p class="hint">' + H("Which libraries another server mirrors — chosen per library, from your account’s servers; any server for any library. A library not decided holds back what would act on a mirror.") + "</p>";
    out += mirrorLights(p2, pe);
    // a server in use that isn't reached, stopped answering, or is reached through Plex's relay only: an address this NAS can reach, by hand
    (p2.servers || []).forEach(function (sv) {
      if (sv.reached && !sv.error && !sv.relay) return;
      out += '<div class="grid" style="margin-top:8px"><div class="fld"><label for="s-p2addr-' + esc(sv.id) + '">' + esc(T("Address for {name}", { name: sv.name })) + "</label>" +
        '<input id="s-p2addr-' + esc(sv.id) + '" type="text" value="" placeholder="https://" data-p2addr="' + esc(sv.id) + '">' +
        '<div class="help">' + H("An address this NAS can reach: the other site’s public address with Remote Access on, or one over a VPN. Its token comes from your sign-in.") + "</div></div>" +
        '<div class="acts"><button class="btn" type="button" data-act="plex2-address" data-server="' + esc(sv.id) + '">' + H("Use This Address") + "</button></div></div>";
    });
    // looking for mirrors: a sample of each library's files looked for on each of the account's servers' libraries, by path
    out += '<div style="margin-top:10px">' + checkbox("sync", "discover", T("Look for mirrors among my Plex account’s servers"),
      { s: H("Weekly while a library’s mirror isn’t chosen, when this page opens after an hour, and on Look for Mirrors — which also compares each mirror chosen over all of the library’s files.") }) + "</div>" +
      '<div class="acts"><button class="btn" type="button" data-act="sync-discover"' + (sy.running ? " disabled" : "") + ">" + (sy.running ? H("Looking…") : H("Look for Mirrors")) + "</button>" +
      ' <span class="muted" style="font-size:13px">' + esc(sy.discovered_at ? T("looked {when}", { when: ago(sy.discovered_at) }) : T("Not looked yet")) + "</span></div>";
    if (sy.error) out += '<div class="det bad">' + esc(sy.error) + "</div>";
    (sy.candidates || []).forEach(function (c) {  // (a server in use: its light above says how it is)
      if ((p2.servers || []).some(function (sv) { return sv.id === c.id; })) return;
      if (!c.url) out += '<div class="det muted">' + esc(T("{name}: can’t be reached from here.", { name: c.name })) + "</div>";
      else if (c.error) out += '<div class="det bad">' + esc(c.name) + ": " + esc(c.error) + "</div>";
    });
    if (!libs.length) return out + '<p class="muted">' + (librariesPending() ? H("Reading your libraries from Plex…") : H("No libraries yet.")) + "</p></section>";
    var states = p2.states || {}, found = sy.libraries || {}, pairs = p2.pairs || {};
    out += '<div class="tbl-wrap"><table class="tbl mid"><thead><tr><th scope="col">' + H("Library here") + '</th><th scope="col">' + H("Mirrored by") + '</th><th scope="col"></th></tr></thead><tbody>' +
      libs.map(function (l) {
        var key = String(l.key), st = states[key] || { state: "unset", server: "", key: "", there: "", there_key: "", name: "" }, f = found[key], rate = f ? Math.round(100 * f.rate) : null;
        var sel = '<select data-p2lib="' + esc(key) + '" aria-label="' + esc(T("The mirror of {library}", { library: l.title })) + '">' + mirrorOptions(l, st, p2, sy) + "</select>" +
          (st.state !== "unset" ? ' <button type="button" class="revert" data-p2lib-revert="' + esc(key) + '" title="' + H("Back to the default ({value})", { value: T("not decided") }) + '">↺</button>' : "");
        var status;
        if (st.state === "mirror") {
          // the mirror chosen: Mirrored while its files are this library's (plex2.match_rate, over all of them: the count is in
          // the list) — a warning once they aren't, and what's in the way before that's known
          var rt = (p2.rates || {})[key], sv = (p2.servers || []).filter(function (x) { return x.id === st.server; })[0] || {}, pair = pairs[key], cerr = (p2.compare_errors || {})[key];
          var differ = !!pair && rt != null && rt < (p2.matched || 0.9);
          var where = !sv.reached ? '<span class="bad">' + esc(T("{name} isn’t reached", { name: st.name || st.server })) + "</span>"
            : !sv.known && sv.error ? '<span class="bad">' + esc(T("{name} isn’t answering", { name: sv.name })) + "</span>"
            : !sv.known ? '<span class="muted">' + esc(T("{name}: reading its libraries…", { name: sv.name })) + "</span>"
            : !pair ? '<span class="bad">' + esc(T("no such library on {name} now", { name: sv.name })) + "</span>"
            : differ ? '<span class="bad">' + esc(T("{library} on {name} doesn’t have most of this library’s files: the wrong library, or a folder sync that’s behind?", { library: pair.title, name: pair.name })) + "</span>"
            : cerr ? '<span class="bad">' + esc(T("{library} on {name}: its files couldn’t be compared — {why}", { library: pair.title, name: pair.name, why: cerr })) + "</span>"
            : '<span class="muted">' + esc(T("{library} on {name}", { library: pair.title, name: pair.name })) + (rt == null ? " · " + esc(T("files not compared yet")) : "") + "</span>";
          status = (differ ? badge("warn", T("Files differ")) : badge("pass", T("Mirrored"))) + " " + where;
        } else if (st.state === "none") status = "";
        else {  // not decided: what looking found for it (the list says the state) — nothing said of a library not looked at yet
          var where = f ? esc(f.name) + ": " + esc(f.their_title) + ' <span class="muted">' + esc(T("{found} of {of} files there", { found: fmtNum(f.found), of: fmtNum(f.of) })) + "</span>" : "";
          status = rate >= 90 ? badge("pass", T("Looks like a mirror")) + " " + where : rate >= 20 ? badge("warn", T("Partly the same")) + " " + where
            : '<span class="muted">' + (sy.running ? H("Looking…") : (sy.compared || []).indexOf(key) >= 0 ? H("Nothing found that looks like one") : H("Not looked for yet")) + "</span>";
        }
        return "<tr><td>" + libIcon(key) + esc(l.title) + "</td><td>" + sel + "</td><td>" + status + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    if ((sy.peers || []).length) out += '<div class="note">' + H("Another Plex Cache shares these folders (it marked itself in them): {n} system(s). Each optimizes its own side; claims keep them off the same file.", { n: sy.peers.length }) + "</div>";
    // the sign-in the servers are listed from: this server's, or an account of its own
    var signin = '<button class="btn" type="button" data-act="plex2-signin">' + (p2.own_signin ? H("Sign In Again") : H("Use Another Plex Account")) + "</button>";
    out += '<div class="acts" style="margin-top:10px">' + signin + "</div>" +
      '<div class="det muted">' + (p2.signed_in ? H("Your account’s servers are listed from this server’s sign-in (<a href=\"/system#plex\" data-nav>System → Plex</a>) — or sign in to another account, when the mirror is on one.")
        : H("Sign in with Plex first (<a href=\"/system#plex\" data-nav>System → Plex</a>): your account’s servers are listed from there.")) + "</div>";
    return out + "</section>";
  }
  // Mirror: the server kept in step with this one (sync.py: the servers that may be one), each library's state, Plex's own
  // sync of watched state, and what uses the mirror — Sync Libraries (the tool that brings its libraries in line) among them
  function pageMirrors() {
    var sy = ST.sync || {}, p2 = ST.plex2 || {};
    var out = top(T("Mirror"), H("Other Plex servers with the same files — at another site, kept in step by syncing the folders — chosen per library, from the servers of your Plex account."), "");
    out += '<div class="stack" style="max-width:980px">';
    // ---- each library: its mirror, chosen from the account's servers (what looking found for it), under the lights
    out += mirrorLibrariesCard();
    // ---- what Plex's own sync carries (Sync Libraries leaves it to Plex)
    out += '<section class="card"><h2>' + H("Watched State") + "</h2>" +
      '<p class="hint">' + H("What each person has watched, and their ratings: Plex carries these between your servers itself. Everything else about a library is brought in line by <a href=\"/sync\" data-nav>Sync Libraries</a>.") + "</p>" +
      '<div class="grid">' +
      select("sync", "watched", T("Watched state and ratings"), [["plex", T("Plex’s own sync carries them")], ["off", T("Not synced")]],
        { s: H("Plex syncs watched state and ratings between your own servers when “Sync my watch state and ratings” is on in your Plex account. Leave it to Plex rather than doing it twice.") }) +
      select("sync", "users", T("Whose"), [["home", T("Everyone in my Plex Home")], ["me", T("Just me")]],
        { s: H("For what’s synced per user (a later part): every user of the Plex Home, or only you.") }) +
      "</div></section>";
    // ---- what uses it
    var on = !!p2.any_mirror;
    out += '<section class="card">' + heading(esc(T("What Uses It")), "") + '<div class="row">' + badge(on ? "pass" : "info", on ? T("On") : T("Waiting")) +
      '<div class="grow"><div class="what">' + H("Converting to MKV") + '</div><div class="det">' +
      H("A converted file’s original is deleted only once its library’s mirror has the MKV as a version of the same item too. <a href=\"/convert\" data-nav>Tools → Convert to MKV</a>") + "</div></div></div>" +
      '<div class="row">' + badge(on ? "pass" : "info", on ? T("On") : T("Waiting")) + '<div class="grow"><div class="what">' + H("Sync Libraries") + '</div><div class="det">' +
      H("Brings each paired library in line with the other server’s, one way, from the side whose edits win — when you press Apply. <a href=\"/sync\" data-nav>Tools → Sync Libraries</a>") + "</div></div></div>" +
      '<div class="row">' + badge(on ? "pass" : "info", on ? T("On") : T("Waiting")) + '<div class="grow"><div class="what">' + H("Mirror Checks") + '</div><div class="det">' +
      H("What Sync Libraries can’t bring in line, and what the folder sync or a Plex scan missed. <a href=\"/mirrorchecks\" data-nav>Tools → Mirror Checks</a>") + "</div></div></div>" +
      '<div class="row">' + badge(on ? "pass" : "info", on ? T("On") : T("Waiting")) + '<div class="grow"><div class="what">' + H("Convert Folder to Child Dataset") + '</div><div class="det">' +
      H("Both servers converted one after the other, the folder sync kept off the new dataset and the old folder, and a conversion left partway on either side found. <a href=\"/dataset\" data-nav>Tools → Convert Folder to Child Dataset</a>") + "</div></div></div></section>";
    return out + "</div>";
  }
  function pageSystem() {
    var c = ST.connections || {}, testing = ST.testing;
    var out = top(T("System"), H("Connections and system-wide options. Stored in the app’s config folder, so they survive updates."),
      '<button class="btn" data-act="retest"' + (testing ? " disabled" : "") + ">" + (testing ? H("Testing…") : H("Re-test Settings")) + "</button>");
    out += banner(settingsSummary(c.summary), testing);
    out += '<div class="stack" style="max-width:980px">';

    // ---- Access: who may use the web UI and the API
    var au = ST.auth || {};
    out += '<section class="card" id="access"><h2>' + H("Access") + '</h2><p class="hint">' + H("Who can open Plex Cache and change its settings.") + "</p>" +
      '<div class="det">' + (au.enabled ? badge("pass", T("Sign-in required")) + " " + H("Signed in as {user}.", { user: au.user })
        : badge(S.general.lan_only ? "info" : "warn", T("No sign-in")) + " " +
          (S.general.lan_only ? H("Any device on your local network can change settings and run the optimizer.") : H("Any device that can reach it can change settings and run the optimizer.")) + " " +
          H("To require a sign-in, set PLEXCACHE_USERNAME and PLEXCACHE_PASSWORD in the app’s YAML (Apps → plex-cache → Edit).")) + "</div>" +
      '<div style="margin-top:10px">' + checkbox("general", "lan_only", T("Local network only"),
        { s: H("Devices outside your network — the internet — are refused, signed in or not."),
          more: H("Your network means private addresses (10.x.x.x, 172.16–31.x.x, 192.168.x.x), this NAS, and VPNs into your network that use their own range, such as Tailscale (100.64–127.x.x). " +
            "Behind a reverse proxy, the proxy’s address is what counts. Turning this off from outside your network isn’t possible: change it from a device on your network.") }) + "</div></section>";

    // ---- Plex
    var guessPlex = ST.host ? "http://" + ST.host + ":32400" : "http://192.168.1.10:32400";
    var tokOk = c.plex_token && c.plex_token.status === "ok";
    out += '<section class="card" id="plex"><h2>Plex</h2><p class="hint">' + H("Used to list your libraries and see what’s playing.") + "</p>" +
      '<div class="lights">' + lightFor(c.plex_server, T("Plex server"), testing) + lightFor(c.plex_token, T("Plex sign-in"), testing) + "</div>" +
      '<div class="grid">' +
      textInput("plex", "url", T("Server address"), "s-purl", H("Usually your NAS’s IP. Port 32400 is used if you leave it off."), guessPlex) +
      secretInput("plex", "token", T("Token (filled in when you sign in)"), "s-ptok") +
      select("general", "poll_seconds", T("Check what’s playing every"), [[3, P(3, "{n} second", "{n} seconds")], [5, P(5, "{n} second", "{n} seconds")], [10, P(10, "{n} second", "{n} seconds")], [30, P(30, "{n} second", "{n} seconds")]],
        { s: H("Drives Now Playing Cache and Prefetch."), more: H("How often the app asks Plex what’s playing. Each check is one small request on your own network, so the load is tiny at any of these. " +
          "What changes is how quickly it reacts: when a stream starts, when someone skips (Now Playing Cache then reads from the new spot), and when a player buffers (L2ARC-only caching pauses for it).<br><br>" +
          "<strong>3 seconds</strong> — a lot of music, or viewers who skip around: reacts a little sooner.<br>" +
          "<strong>5 seconds</strong> — the best fit for most people.<br>" +
          "<strong>10 seconds</strong> — fine for mostly movies and TV watched straight through.<br>" +
          "<strong>30 seconds</strong> — the fewest requests, but streams and skips can be noticed up to half a minute late; short songs may be half over, and brief buffering can be missed.<br><br>" +
          "Faster than 3 seconds gains little: Now Playing Cache waits for Plex’s own start-up buffer anyway.") }) + "</div>" +
      '<div class="acts"><button class="btn' + (tokOk ? "" : " pri") + '" data-act="plex-signin">' + (tokOk ? H("Sign In Again") : H("Sign In with Plex")) + "</button>" +
      '<span class="muted" style="font-size:13px">' + H("Why sign in?") + info(H("Plex only shares your libraries and what’s playing with apps that hold a token for your account, so this step is required. " +
      "“Sign In with Plex” opens plex.tv; once you approve “Plex Cache”, Plex gives this app a token. It’s kept only in this app’s config folder and is used to read from your server — " +
      "it can’t see your password. If you already have a token, you can paste it instead."), T("signing in")) + "</span></div></section>";

    // ---- TrueNAS
    var guessTn = ST.host ? "https://" + ST.host : "https://192.168.1.10";
    var pf = ST.platform || {};
    out += '<section class="card" id="truenas"><h2>TrueNAS <span class="muted" style="font-weight:400;font-size:14px">· ' + H("optional, recommended") + "</span></h2>" +
      // where this is running: TrueNAS for certain (the API, or the host's version file), or another Linux host
      '<div class="lights">' + light(pf.truenas ? "ok" : pf.truenas === false ? "off" : "warn", T("Platform"), esc(msg(pf.label || "")) +
        (pf.truenas === null || pf.truenas === undefined ? " · " + esc(T("Connect the API, or mount the host’s /etc/version at /host/etc/version in the app’s YAML, to be sure")) : pf.how === "kernel" ? " · " + esc(T("told by the kernel; the API would add the version")) : ""),
        pf.truenas == null && (ST.pending || ST.checking || testing)) + "</div>" +
      // not TrueNAS: the fields are there to see, greyed out, with the pitch
      (pf.truenas === false ? '<div class="note pitch">' + H("Not running on TrueNAS, so there’s no API to connect here. Upgrade your server to TrueNAS to supercharge it with caching: SSD speeds from regular HDDs!") + "</div>" : "") +
      '<fieldset class="plain' + (pf.truenas === false ? " greyed" : "") + '"' + (pf.truenas === false ? " disabled" : "") + ">" +
      '<p class="hint">' + H("Read-only: checks cache devices and dataset settings.") + info(H("It’s also how the app sizes the L2ARC, pauses for scrubs and resilvers, and checks the optimizer’s service. It never changes anything."), "TrueNAS") + "</p>" +
      '<div class="lights">' + lightFor(c.truenas_server, "TrueNAS", testing) + lightFor(c.truenas_key, T("API key"), testing) +
      lightInfo(c.truenas_api, T("API compatibility"), testing, info(H("TrueNAS renames API methods between releases (<code>zfs.snapshot.*</code> became <code>pool.snapshot.*</code> in 25.04). This light checks every call Plex Cache makes — signing in, the version, pools, disks, dataset settings, apps, updates, snapshots — against the list of methods this TrueNAS offers, and is green when each has a name there. " +
        "<strong>If it’s orange, please report it on GitHub</strong> with your TrueNAS version and the calls it names: the app then learns the new name, and the features that need it work again. A weekly check against the next TrueNAS release runs on GitHub too, so most renames are caught before they reach you."), T("API compatibility"))) + "</div>" +
      '<div class="grid">' +
      textInput("truenas", "url", T("TrueNAS address"), "s-turl", H("https:// is used automatically — TrueNAS revokes API keys sent over plain http."), guessTn) +
      secretInput("truenas", "api_key", T("API key"), "s-tkey") + "</div>" +
      '<div class="chk" style="margin-top:12px">' + infoOrGap("", "") + '<label><input type="checkbox" data-check="truenas.verify_tls"' + (S.truenas.verify_tls ? " checked" : "") +
      ">" + H("Verify the TLS certificate (leave off for TrueNAS’s self-signed certificate)") + "</label>" + revertBtn("truenas", "verify_tls", "") + "</div>" +
      details("apikey", H("How to create an API key"),
        "<ol style=\"margin:8px 0 0;padding-left:20px\"><li>" + H("In TrueNAS, open your user menu (top right) → <strong>API Keys</strong>.") + "</li>" +
        "<li>" + H("Choose <strong>Add</strong>, name it “plex-cache”, and save.") + "</li>" +
        "<li>" + H("Copy the key it shows (it’s only shown once) and paste it above.") + "</li></ol>",
        !(c.truenas_key && c.truenas_key.status === "ok")) +
      "</fieldset></section>";

    // ---- the Plex Cache at the mirror's site
    out += peerCard();

    // ---- Media Mappings
    out += mappingsCard();

    // ---- Preferences: lists, and what stands in for a snapshot before files change
    var g = S.general;
    out += '<section class="card" id="prefs"><h2>' + H("Preferences") + '</h2><div class="grid">' +
      number("general", "page_size", T("Entries per Library page"), 25, 1000, H("On the Library views, a tool’s rows, a job’s files: a long list a page at a time, with the pages told by what’s on them.")) +
      number("general", "tool_page_size", T("Entries per Tools page"), 5, 200, H("On every tool’s page, the Optimizer’s and the check pages: the cards a page at a time. All still ticks every page’s.")) +
      select("general", "backup", T("Backup before changes"), [["snapshot", T("A snapshot, taken through TrueNAS")], ["manual", T("A backup I make myself")], ["copies", T("The copies the action keeps")]],
        { s: H("Before any action rewrites, moves or deletes files — the file tools, Optimize Selected, Child Dataset — a recent snapshot of the library’s dataset must be in reach, or this stands in for it.") + " " +
            H("Before Plex’s records change — Merge Duplicates, Sync Libraries, the merge Organize Folders makes — the app backs up each record it changes, under a snapshot or the copies an action keeps; with a backup you make yourself, it doesn’t."),
          more: H("<strong>A snapshot</strong> is the whole dataset as it is this minute, taken through TrueNAS when you press Take a Snapshot on the action’s page, named <code>plexcache-&lt;tool&gt;-&lt;library&gt;-&lt;time&gt;</code> and deleted by the app after the days set below. Needs TrueNAS connected with a key allowed to make snapshots (TrueNAS 24.10 and later).<br><br>" +
          "<strong>A backup I make myself</strong>: you make one, and say so on the action’s page; it counts for half an hour.<br><br>" +
          "<strong>The copies the action keeps</strong>: where an action sets files aside rather than deleting them (Duplicate Versions, Child Dataset), those are the backup. An action that keeps none asks for a snapshot or a backup instead.") }) +
      number("general", "backup_days", T("Keep backups for (days)"), 1, 365, H("How long a backup made before files change is kept: a snapshot the app takes is deleted after this long, by the app itself (TrueNAS keeps a snapshot for ever otherwise); files set aside or copied as a backup are deleted after it too. Until then a backup holds the space the changed files took, and a change can be reverted from it.") + " " + H("A backup of Plex’s records is kept as long, then deleted by the app.")) +
      "</div></section>";
    // ---- Updates
    out += '<section class="card" id="updates"><h2>' + H("Updates") + '</h2>' +
      '<div class="chk" style="margin-top:8px">' + info(H("Every 6 hours the app asks GitHub for the branch’s newest finished build — a small download, nothing about your setup is sent — " +
        "and, when there’s a newer one, says so under the app’s name and in Home Assistant’s update entity. Check Now asks straight away; Update Now has TrueNAS pull it."), T("update check")) +
      '<label><input type="checkbox" data-check="general.update_check"' + (g.update_check ? " checked" : "") + ">" + H("Check GitHub for new versions") + "</label>" + revertBtn("general", "update_check", "") + updateLine() + "</div></section>";
    out += diskSpeedCard();
    out += mqttCard();
    out += "</div>";
    return out;
  }

  var MQTT_STEPS = [["broker", T("Broker found")], ["tls", T("Encrypted (TLS)")], ["login", T("Login accepted")], ["publish", T("Publishing")]];
  var MQTT_WAITING = { broker: T("Looking for the broker…"), tls: T("Starting TLS…"), login: T("Signing in…"), publish: T("Sending…") };
  var UPDATING = false;  // Update Now pressed on this page: said as updating until the page comes back on the new build
  function updateStage(u) {
    if (UPDATING || u.updating) return "updating";
    if (!u.checked && !u.error) return "checking";  // (just started: GitHub asked in a moment)
    if (!u.available) return "";
    if (!(ST.platform || {}).truenas) return "link";
    return ((ST.connections || {}).truenas_key || {}).status === "ok" ? "button" : "needkey";  // (TrueNAS could do it, with a key)
  }
  function updateLine() {
    var u = ST.updates || {};
    if (!S.general.update_check) return "";
    var stage = updateStage(u);
    var build = '<span class="muted" style="font-size:13px">' + (u.channel === "dev" ? H("Dev build v{version}{commit}.", { version: ST.version, commit: u.commit ? " (" + u.commit + ")" : "" }) : H("Stable build v{version}.", { version: ST.version })) + "</span> ";
    if (stage === "updating") return build + '<span class="muted" style="font-size:13px">' + H("Updating: TrueNAS is pulling the newer image and restarting the app. This page comes back by itself.") + "</span>";
    var btn = ' <button class="btn sm" type="button" data-act="updates-check"' + (u.checking || stage === "checking" ? " disabled" : "") + ">" + (u.checking || stage === "checking" ? H("Checking…") : H("Check Now")) + "</button>" +
      (stage === "button" ? ' <button class="btn sm pri" type="button" data-act="updates-apply">' + H("Update Now") + "</button>" : "");
    var line = "";
    if (stage === "checking") line = H("Checking GitHub…");
    else if (u.available) line = (u.latest && u.latest !== ST.version.replace(/-dev$/, "") ? H("v{version} is available.", { version: u.latest }) : H("A newer dev build is available (dev is at {head}).", { head: u.latest_commit })) +
      (stage === "needkey" ? " " + H("To install it from here, add a TrueNAS API key in <a href=\"/system#truenas\" data-nav>System → TrueNAS</a>; TrueNAS will pull the image and restart the app.") : "");
    else if (u.checked) line = H("Up to date (checked {when}).", { when: ago(u.checked) });
    else if (u.error) line = H("Couldn’t check: {why}", { why: u.error });
    return build + '<span class="muted" style="font-size:13px">' + line + "</span>" + btn;
  }
  function mqttLights() {
    var m = ST.mqtt || {}, M = S.mqtt, steps = m.steps || {};
    if (!M.enabled) return light("off", T("Off"), T("Tick “Publish over MQTT” below to start."));
    return MQTT_STEPS.map(function (st) {
      var r = steps[st[0]] || { status: "off", text: "" }, detail = msg(r.text);
      if (st[0] === "publish" && r.status === "ok") {
        detail = detail + (m.last_publish ? " · " + T("last update {when}", { when: ago(m.last_publish) }) : "") +
          (m.ha_seen ? " · " + T("Home Assistant last said hello {when}", { when: ago(m.ha_seen) }) : "");
      }
      if (r.status === "testing") return light("testing", st[1], MQTT_WAITING[st[0]]);
      if (r.status === "off") return light("off", st[1], detail || (m.state === "error" ? T("Waiting for the step before") : ""));
      return light(r.status, st[1], detail);
    }).join("");
  }
  function mqttRetryLine() {
    var m = ST.mqtt || {};
    if (!S.mqtt.enabled || !S.mqtt.host) return "";
    var txt = "";
    if (m.state === "error" && m.next_retry) {
      var secs = Math.max(0, Math.round(m.next_retry - Date.now() / 1000));
      txt = secs ? H("Trying again in {n} s.", { n: secs }) : H("Trying again…");
    } else if (m.state === "connecting") txt = H("Connecting…");
    return '<div class="acts"><button class="btn" data-act="mqtt-retry"' + (m.state === "connecting" ? " disabled" : "") + ">" +
      (m.state === "connected" ? H("Reconnect") : H("Reconnect Now")) + "</button>" +
      (txt ? '<span class="muted" style="font-size:13px">' + txt + "</span>" : "") + "</div>";
  }
  function mqttCard() {
    var M = S.mqtt, on = M.enabled;
    var out = '<section class="card" id="mqtt"><h2>MQTT <span class="muted" style="font-weight:400;font-size:14px">· ' + H("Home Assistant, Node-RED, openHAB and more · optional") + "</span></h2>" +
      '<p class="hint">' + H("Publishes status and lots of metrics, and takes commands to switch features and run jobs.") +
      info(H("MQTT is a common way for home automation and monitoring tools to share data, through a <strong>broker</strong> that everything connects to. " +
        "Plex Cache publishes everything as one JSON message on <code>{base}/state</code> (every {n} s, and straight away after a change), " +
        "says <code>online</code>/<code>offline</code> on <code>{base}/availability</code>, and takes commands on <code>{base}/set/…</code>. " +
        "Node-RED, openHAB, ioBroker, Telegraf and others can read those directly.<br><br>" +
        "<strong>Home Assistant:</strong> with discovery on, a “Plex Cache” device appears with sensors for health, streams, RAM, ARC/L2ARC, warm-up, " +
        "the Metrics figures and lifetime totals, plus switches, buttons and selects. Install the <strong>Mosquitto broker</strong> add-on and the " +
        "<strong>MQTT</strong> integration, add a Home Assistant user for this app (Settings → People → Users), and enter its name and password here.<br><br>" +
        "<strong>Security:</strong> the broker login is the lock. Use a separate login per device, use TLS if your broker offers it, and switch off " +
        "“Allow control” for a watch-only connection. Broker ACLs can limit this login to the <code>{base}/#</code> and <code>{prefix}/#</code> topics.",
        { base: M.base_topic, n: M.interval, prefix: M.discovery_prefix }), "MQTT") + "</p>" +
      '<div class="lights">' + mqttLights() + "</div>" + mqttRetryLine() +
      '<div class="chk" style="margin-top:12px">' + infoOrGap("", "") + '<label><input type="checkbox" data-check="mqtt.enabled"' + (on ? " checked" : "") + ">" + H("Publish over MQTT") + "</label></div>";
    if (!on) return out + "</section>";
    out += '<div class="grid">' +
      textInput("mqtt", "host", T("Broker address"), "s-mhost", H("For Home Assistant’s Mosquitto add-on, Home Assistant’s IP."), "192.168.1.20") +
      number("mqtt", "port", T("Port"), 1, 65535, { s: H("1883, or 8883 with TLS."), more: H("1883 is the usual port without TLS and 8883 with it. " +
        "In Home Assistant’s Mosquitto add-on, 8883 only opens when the add-on has certificate files (its log says “SSL not enabled - No valid certs found!” otherwise). " +
        "Its websocket ports (1884, 8884) are for browsers and won’t work here.") }) +
      textInput("mqtt", "username", T("Username"), "s-muser", "", "plexcache") +
      secretInput("mqtt", "password", T("Password"), "s-mpass") +
      select("mqtt", "language", T("Language in Home Assistant"), PCI18N.LANGUAGES.map(function (l) { return [l[0], l[0] === "en" ? T("{name} — original", { name: l[1] }) : l[1]]; }),
        { s: H("Names, descriptions and status text."), more: H("Home Assistant doesn’t tell MQTT devices which language it uses, so pick it here. " +
          "Entity IDs stay the same in every language, so automations keep working when you change it. This page follows your browser instead (menu at the top right).") }) + "</div>" +
      '<div class="chk" style="margin-top:12px">' + infoOrGap("", "") + '<label><input type="checkbox" data-check="mqtt.tls"' + (M.tls ? " checked" : "") + ">" + H("Use TLS") + "</label>" + revertBtn("mqtt", "tls", "") +
      (M.tls ? info(H("Leave off for a self-signed certificate, or when connecting by IP to a broker whose certificate names a domain (such as a Let’s Encrypt one). " +
          "The connection is still encrypted; on, it also proves it’s really your broker."), T("Verify")) +
        '<label><input type="checkbox" data-check="mqtt.verify_tls"' + (M.verify_tls ? " checked" : "") + ">" + H("Verify the broker’s certificate") + "</label>" + revertBtn("mqtt", "verify_tls", "") : "") +
      '<label><input type="checkbox" data-check="mqtt.discovery"' + (M.discovery ? " checked" : "") + ">" + H("Home Assistant discovery") +
      info(H("Publishes Home Assistant’s discovery messages, so the “Plex Cache” device and its entities appear on their own. openHAB and Domoticz can read them too. " +
        "Off: only the state, availability and command topics, for tools that don’t use discovery. Turning it off removes the entities from Home Assistant."), T("Home Assistant discovery")) + "</label>" + revertBtn("mqtt", "discovery", "") +
      '<label><input type="checkbox" data-check="mqtt.allow_control"' + (M.allow_control ? " checked" : "") + ">" + H("Allow control") +
      info(H("Accepts commands on <code>{base}/set/…</code>: switch the three features on or off, run or stop warm-up, re-run health checks, " +
        "benchmark disks, change RAM limits and pacing, and pick each library’s Prefetch choices. In Home Assistant these are switches, buttons, sliders and selects. " +
        "Off: status only.", { base: M.base_topic }), T("Allow control")) + "</label>" + revertBtn("mqtt", "allow_control", "") + "</div>" +
      details("mqtt-more", H("More options"), '<div class="grid">' +
        number("mqtt", "interval", T("Send an update every (seconds)"), 5, 600, H("Changes are sent straight away too.")) +
        select("mqtt", "report_period", T("Metrics figures cover"), [["today", T("Today")], ["week", T("Last 7 days")], ["month", T("Last 30 days")]],
          H("The skips, Prefetch and Now Playing Cache figures.")) +
        textInput("mqtt", "base_topic", T("Topic"), "s-mbase", H("Status goes to &lt;topic&gt;/state; commands come in on &lt;topic&gt;/set/…."), "plexcache") +
        (M.discovery ? textInput("mqtt", "discovery_prefix", T("Discovery prefix"), "s-mdisc", H("Leave as homeassistant unless you changed it in Home Assistant."), "homeassistant") : "") +
        "</div>", false) +
      "</section>";
    return out;
  }

  // While a benchmark runs: which dataset it's reading now, and which are still to come.
  function benchState(name) {
    var p = ST.benchmark.running && ST.benchmark.progress;
    if (!p || p.finished.indexOf(name) >= 0) return "";
    return p.dataset === name ? badge("running", T("Measuring…")) : badge("info", T("Waiting"));
  }
  function diskSpeedCard() {
    var g = S.general, b = ST.benchmark, speeds = g.disk_speeds || {};
    var ds = (ST.zfs.datasets || []).filter(function (d) { return d.keys.length; });
    var out = '<section class="card" id="disk-speed"><div class="head"><h2>' + H("Disk Speed") + '</h2><span style="flex:1"></span>' +
      '<button class="btn" data-act="benchmark"' + (b.running || !ds.length ? " disabled" : "") + ">" +
      (b.running ? (b.progress ? H("Measuring {n} of {total}…", { n: b.progress.done + 1, total: b.progress.total }) : H("Measuring…")) : H("Benchmark Now")) + "</button></div>" +
      '<p class="hint">' + H("How fast each dataset reads when nothing is cached.") + info(H("The app uses it to tell a cache hit from a disk read " +
      "(a read at least 4× faster came from cache) — that’s how warm-up reports what was already cached — to time Now Playing Cache’s automatic start, and to work out how hard background " +
      "warm-up is working your disks. Datasets on different disks (HDD, SSD, mixed) get their own speed.<br><br>" +
      "<strong>Benchmark Now</strong> reads up to 64 MB from five random media files on each dataset and times it. The middle result becomes that dataset’s speed, " +
      "so one file that happened to be cached already doesn’t skew it. It takes a few seconds per dataset, only reads, and is harmless. " +
      "Run it again after adding disks or SSDs, or type a speed in by hand."), T("disk speed")) + "</p>";
    if (!ds.length) {
      out += '<p class="muted" style="margin:0">' + H("Datasets appear here once a library’s files are readable (see Media Mappings above).") + "</p>";
    } else {
      out += '<div class="tbl-wrap"><table class="tbl mid"><thead><tr><th scope="col">' + H("Dataset") + '</th><th scope="col">' + H("Libraries") + '</th><th scope="col">' + H("Uncached speed") +
        '</th><th scope="col">' + H("Measured") + "</th></tr></thead><tbody>";
      ds.forEach(function (d) {
        var name = d.dataset || d.mountpoint, row = speeds[name];
        out += '<tr><td class="mono nw">' + esc(name) + "</td><td>" + esc(d.libraries.join(", ")) + '</td><td class="nw"><input type="number" min="10" max="20000" aria-label="' + H("Speed of {name} in MB/s", { name: name }) +
          '" data-speed="' + esc(name) + '" value="' + esc(row ? row.mb_s : g.hdd_mb_per_sec) + '"> MB/s</td><td class="muted">' +
          (benchState(name) || (row && row.measured_at ? esc(fmtIso(row.measured_at)) : (row ? H("Set by hand") : H("Not yet — using the speed below")))) + "</td></tr>";
      });
      out += "</tbody></table></div>";
    }
    out += '<div class="grid">' + number("general", "hdd_mb_per_sec", T("Speed assumed for datasets not measured yet (MB/s)"), 10, 5000, H("A typical hard drive pool reads 150–250 MB/s.")) + "</div>";
    if (b.last && b.last.error) out += '<div class="note">' + esc(b.last.error) + "</div>";
    if (b.last && b.last.rows && b.last.rows.length) {
      var rowsHtml = "";
      b.last.rows.forEach(function (r) {
        rowsHtml += '<tr><td class="mono">' + esc(r.dataset) + "</td><td>" + esc(KIND_NAME[r.kind]) + "</td><td>" + esc(r.mb_s) + " MB/s</td><td>" + esc(r.mb) + ' MB</td><td class="mono">' + esc(r.file) + "</td></tr>";
      });
      out += details("bench", H("Last benchmark details"), '<div class="tbl-wrap"><table class="tbl"><thead><tr>' + [N("Dataset"), N("Type"), N("Speed"), N("Read"), N("File")].map(function (c) {
        return "<th>" + H(c) + "</th>"; }).join("") + "</tr></thead><tbody>" + rowsHtml + "</tbody></table></div>");
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
    var names = Object.keys(bad).map(function (k) { return bad[k]; });
    if (sum.status === "ok") return { status: "warn", text: P(n, "Connected, but {n} library isn’t being cached: {list} — see Media Mappings below.", "Connected, but {n} libraries aren’t being cached: {list} — see Media Mappings below.", { list: names }) };
    return { status: sum.status, text: msg(sum.text) + " " + P(n, "Also, {n} library isn’t being cached: {list} — see Media Mappings below.", "Also, {n} libraries aren’t being cached: {list} — see Media Mappings below.", { list: names }) };
  }

  function lightFor(r, title, testing) {
    if (!r) return light("off", title, T("Not tested yet"), testing);
    return light(r.status, r.text, r.detail, testing);
  }
  // a light with an ⓘ after its title: what the check looks at
  function lightInfo(r, title, testing, infoHtml) {
    var st = testing ? "testing" : r ? r.status : "off", text = testing ? T("Checking…") : r ? r.text : title, detail = r ? r.detail : T("Not tested yet");
    return '<div class="lightrow"><span class="light l-' + esc(st) + '" aria-hidden="true"></span><div class="grow"><div class="what">' + esc(text) + infoHtml + "</div>" +
      (detail && !testing ? '<div class="det">' + links(detail) + "</div>" : "") + "</div></div>";
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
    var out = '<section class="card" id="mappings"><h2>' + H("Media Mappings") + "</h2>" +
      '<p class="hint">' + H("Tick the libraries to cache.") + info(H("Unticked libraries are skipped by every feature and by the health checks. " +
      "Plex lists each library’s folders as it sees them inside its own app; if this app has a folder mounted somewhere else, choose where. " +
      "Folders marked ✓ contain that library’s files. A library is only cached once all its folders are found."), T("media mappings")) + "</p>";
    if (!locs.length) return out + '<p class="muted" style="margin:0">' + (librariesPending() || ST.checking ? H("Reading your libraries from Plex…") : H("Your library folders appear here once Plex is connected.")) + "</p></section>";
    var healthAt = ST.health ? ST.health.at : 0;
    out += '<div class="tbl-wrap"><table class="tbl"><thead><tr>' + [N("Cache"), N("Library"), N("Plex folder"), N("In this app")].map(function (c) {
      return '<th scope="col">' + H(c) + "</th>"; }).join("") + "</tr></thead><tbody>";
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
        tick = '<label class="tick" title="' + (excluded ? H("Not cached — tick to cache it") : (libOk ? H("Cached") : H("Wanted, but its folder isn’t found yet"))) +
          '"><input type="checkbox" data-lib="' + esc(l.key) + '"' + (excluded ? "" : " checked") + '><span class="sr">' + H("Cache {name}", { name: l.library }) + "</span></label>";
      }
      var cur = currentMapping(l.plex_path);
      var pending = mapPending[l.plex_path] && mapPending[l.plex_path] >= healthAt;
      var st = { ok: "ok", missing: "fail", empty: "fail", unreadable: "fail" }[l.status];
      var found = l.found_at || [];
      var opts = '<option value=""' + (cur === "" ? " selected" : "") + ">" + H("Same path as Plex ({path})", { path: l.plex_path }) + "</option>";
      var seen = {};
      seen[l.plex_path] = 1;
      found.forEach(function (f) { seen[f] = 1; opts += '<option value="' + esc(f) + '"' + (cur === f ? " selected" : "") + ">✓ " + esc(f) + "</option>"; });
      if (cur && !seen[cur]) { seen[cur] = 1; opts += '<option value="' + esc(cur) + '" selected>' + esc(cur) + "</option>"; }
      (ST.mapping_choices || []).forEach(function (f) {
        var insideMatch = found.some(function (m) { return f.indexOf(m.replace(/\/$/, "") + "/") === 0; });
        if (!seen[f] && !insideMatch && !inUse(f, l.plex_path)) opts += '<option value="' + esc(f) + '">' + esc(f) + "</option>";
      });
      opts += '<option value="__other__"' + (otherOpen[l.plex_path] ? " selected" : "") + ">" + H("Other path…") + "</option>";
      var lightText = pending ? "" : (excluded ? T("Not cached") : { ok: T("Files readable"), missing: T("Not found"), empty: T("Folder is empty"), unreadable: T("Files not found here") }[l.status]);
      if (excluded && !pending) st = "off";
      var detail = pending ? "" : (l.status === "ok" ? "" : (found.length ? T("Found at {path} — choose it below.", { path: found[0] }) : T("{why}. Mount it, or untick it to stop caching it.", { why: l.detail })));
      if (excluded && !pending) {
        detail = l.status === "ok" ? T("Unticked, so it isn’t cached or checked.") :
          T("Unticked, so it isn’t cached or checked. ({why} — tick it again once it’s mounted.)", { why: l.detail });
      }
      // Which dataset the folder is on, and whether that dataset can cache at all.
      var ds = (ST.zfs.datasets || []).filter(function (d) { return d.dataset === l.dataset || d.mountpoint === l.dataset; })[0];
      if (!pending && !excluded && l.status === "ok" && ds) {
        var pc = ds.props && ds.props.primarycache;
        detail = pc && pc !== "all" ? T("On {dataset} — primarycache={value}, so it can’t be cached until that’s fixed in [Health](/health#datasets).", { dataset: ds.dataset, value: pc }) : T("On {dataset}", { dataset: ds.dataset });
        if (pc && pc !== "all") st = "warn";
      }
      // One tick box and name per library, spanning all of its folders.
      var span = libRows.length > 1 ? ' rowspan="' + libRows.length + '"' : "";
      var libCells = tick ? "<td" + span + ">" + tick + "</td><td" + span + ">" + esc(l.library) +
        (libRows.length > 1 ? '<div class="muted" style="font-size:12px">' + PH(libRows.length, "{n} folder", "{n} folders") + "</div>" : "") + "</td>" : "";
      out += '<tr class="' + (excluded ? "excl" : "") + (tick ? "" : " cont") + '">' + libCells + '<td class="mono">' + esc(l.plex_path) + "</td><td>" +
        light(st, lightText, detail, pending) +
        '<select aria-label="' + H("Where {path} is mounted in this app", { path: l.plex_path }) + '" data-map="' + esc(l.plex_path) + '">' + opts + "</select>" +
        (otherOpen[l.plex_path] ? '<input type="text" class="mono" placeholder="/path/in/this/app" aria-label="' + H("Other path") + '" data-map-other="' + esc(l.plex_path) + '" value="' + esc(cur) + '">' : "") +
        "</td></tr>";
    });
    out += "</tbody></table></div>";
    out += details("mounting", H("Mounting media in this app"),
      "<p style=\"margin:8px 0 0\">" + H("Each library folder needs to be mounted (read-only) into this app. You can mount one shared folder that holds everything, " +
      "or each library separately. If you use the same container paths as your Plex app, nothing needs choosing here. " +
      "Mounts are set in TrueNAS: Apps → this app → Edit.") + "</p>");
    return out + "</section>";
  }

  // --------------------------------------------------------------- render
  var PAGES = { health: pageHealth, caching: pageCaching, ssd: pageSsd, library: pageLibrary, tools: pageTools, optimizer: pageOptimizer, convert: pageConvert, dataset: pageDataset,
    merge: pageMerge, versions: pageVersions, folders: pageFolders, unlink: pageUnlink, sync: pageSync, filechecks: pageFilechecks, plexchecks: pagePlexchecks, mirrorchecks: pageMirrorchecks,
    processes: pageProcesses, mirrors: pageMirrors, activity: pageActivity, system: pageSystem };
  // the tools, under the Tools entry in the menu: [page, name, what it does]
  var TOOLS = [
    ["optimizer", N("Optimizer"), N("Rewrites media files so they suit caching: the dataset’s record size and compression, and MKV files laid out with their metadata at the end.")],
    ["convert", N("Convert to MKV"), N("Converts other containers (AVI, MPEG-TS, VOB…) to MKV when nothing in them would be lost — backed up first, revertable after — and sees the new file into Plex on every server before the original goes.")],
    ["dataset", N("Convert Folder to Child Dataset"), N("Turns a library folder into a dataset of its own, at the same path, with the record size that suits it."), N("Child Dataset")],
    ["merge", N("Merge Duplicates"), N("Finds the same film on several Plex records and makes them versions of one, keeping its edits, poster and watched state. No file is touched.")],
    ["versions", N("Duplicate Versions"), N("Where one Plex item holds the same file twice, removes the extra copy — the file and Plex’s part — after a hold.")],
    ["folders", N("Organize Folders"), N("Moves loose files into a folder of their own, and renames folders to Title (Year) {edition}, without Plex losing anything.")],
    ["unlink", N("Unlink Files"), N("Replaces hard links and symlinks with files of their own — the same bytes — so synced folders match everywhere and the duplicate tools can tidy them safely.")],
    ["sync", N("Sync Libraries"), N("Makes a library on its mirror the same as here, or here the same as there — the files it doesn’t list, versions, matches, edited fields, posters, collections and editions — one way, from the side whose edits win.")],
    ["filechecks", N("File Checks"), N("What’s in each file, as inspected: its tracks — audio, subtitles, their languages and codecs — checked against what a good file has. Each check can be turned off.")],
    ["plexchecks", N("Plex Checks"), N("How Plex has each item: its versions, their names and lengths, the fields you locked. Each check can be turned off.")],
    ["mirrorchecks", N("Mirror Checks"), N("Compares the two sites of a mirror: the files on each one’s disk, and what each Plex lists. Finds what the folder sync or a Plex scan missed, and has Plex look again.")]];
  var TOOL_PAGES = { optimizer: 1, convert: 1, dataset: 1, merge: 1, versions: 1, folders: 1, unlink: 1, sync: 1, filechecks: 1, plexchecks: 1, mirrorchecks: 1 };
  function toolsNav(current) {
    return '<div class="lctl" style="margin-bottom:14px"><div class="seg small" role="tablist" aria-label="' + H("Tools") + '">' +
      '<a role="tab" href="/tools" data-nav aria-selected="' + (current === "tools") + '">' + H("All tools") + "</a>" +
      TOOLS.map(function (t) { return '<a role="tab" href="/' + t[0] + '" data-nav aria-selected="' + (current === t[0]) + '">' + esc(T(t[3] || t[1])) + "</a>"; }).join("") + "</div></div>";
  }
  // ---- Counts: every number a page shows of what's been found, one standard (evaluate.py). The state's "counts" give every
  // page of the Tools menu — and the Library page — its count for each library and over them all ("tools": the menu's),
  // each in one of four states: done {s, n}; pending {s, why, progress?, partial?}: not in yet — a placeholder, never a
  // number, not even 0 — but for a count that grows as it's read (partial: File Checks while the files are inspected),
  // what's been found so far with the turning wheel beside it — or, worked out again ("last": a check page after a
  // restart, Mirror Checks scanning again), what was found last time, its findings shown meanwhile; na {s, why}: the page doesn't apply there (not set up) —
  // a grey dash, information rather than a result; failed {s, why}: couldn't be worked out — red. Drawn
  // by countChip (the tabs, the Tools page's cards, the menu, the Library page's lines) and countBox (in place of what a
  // page found while its count isn't done); never worked out here (test_every_count_is_evaluated_before_it_shows).
  function countOf(page, key) {
    var c = (ST.counts || {})[page];
    c = c && (key == null ? c.total : (c.libraries || {})[String(key)]);
    return c && c.s ? c : { s: "pending" };  // (not in the state yet: still to come)
  }
  function countDone(c) { return !!c && c.s === "done"; }
  // what a count says of itself, as plain text (a title): why it isn't a number, and how far it's got
  function countWhy(c) {
    var w = c.why ? msg(c.why).replace(/\[([^\]]+)\]\([^)]*\)/g, "$1") : c.s === "pending" ? T("Counting…") : "", p = c.progress;
    if (c.partial != null) w = (c.last ? P(c.partial, "{n} found last time, checking again.", "{n} found last time, checking again.", { n: fmtNum(c.partial) })
      : P(c.partial, "{n} found so far, still counting.", "{n} found so far, still counting.", { n: fmtNum(c.partial) })) + " " + w;
    return p && p.total ? w + " " + T("{done} of {total} · {pct}%", { done: fmtNum(p.done || 0), total: fmtNum(p.total), pct: Math.round(100 * (p.done || 0) / p.total) }) : w;
  }
  // A count as a chip, one way everywhere: a turning wheel while it's still to come — beside what's been found so far, for
  // a count that grows as it's read (partial); a grey dash where the page doesn't apply; a red ! where it couldn't be
  // worked out; else the number — red (bad: toolBad) while something red on the page stops part of it, whatever the
  // number; yellow with something to do; green at 0. What's been found so far takes the colour of what it holds the same
  // way (partialTone) — red over yellow — but grey, never green, while it's 0: nothing found yet isn't a result
  // @testable: partialTone
  function partialTone(c, bad) {
    return c.s !== "pending" || c.partial == null ? "" : bad ? "fail" : c.partial ? "warn" : "";
  }
  // @end
  function countChip(c, bad) {
    c = c || { s: "pending" };
    var why = esc(countWhy(c)), tone = partialTone(c, bad);
    if (c.s === "pending") return ' <span class="count busy' + (c.partial != null ? " partial" : "") + (tone ? " " + tone : "") + '" role="status" title="' + why + '" aria-label="' + why + '">' +
      (c.partial != null ? fmtNum(c.partial) + " " : "") + '<span class="spin" aria-hidden="true"></span></span>';
    if (c.s === "na") return ' <span class="count na" title="' + why + '" aria-label="' + why + '">–</span>';
    if (c.s === "failed") return ' <span class="count fail" title="' + why + '" aria-label="' + why + '">!</span>';
    if (bad) return ' <span class="count fail" title="' + H("Something red on its page stops part of it, or couldn’t be checked") + '">' + fmtNum(c.n || 0) + "</span>";
    return ' <span class="count ' + (c.n ? "warn" : "ok") + '" title="' + H("Found to do") + '">' + fmtNum(c.n || 0) + "</span>";
  }
  // What a page found, while its count isn't done: in place of its stats and its rows, what it waits for (and how far it's
  // got; with a partial count, what's been found so far — the rows found so far drawn under it) — or, where the page doesn't
  // apply or couldn't be worked out, why
  function countBox(c) {
    c = c || { s: "pending" };
    var why = c.why ? links(msg(c.why)) : esc(T("Counting…"));
    if (c.s === "na") return '<p class="muted toolempty countbox">' + badge("off", T("Doesn’t apply")) + " " + why + "</p>";
    if (c.s === "failed") return '<p class="muted toolempty countbox">' + badge("fail", T("Couldn’t be worked out")) + " " + why + "</p>";
    var p = c.progress, pct = p && p.total ? Math.round(100 * (p.done || 0) / p.total) : null;
    return '<div class="migprog countbox" role="status"><div class="what">' + (c.partial != null ? (c.last ? PH(c.partial, "{n} found last time — checking again", "{n} found last time — checking again", { n: fmtNum(c.partial) })
      : PH(c.partial, "{n} found so far — still counting", "{n} found so far — still counting", { n: fmtNum(c.partial) }))
      : H("Not in yet")) + '</div><div class="det">' + why +
      (pct != null ? " " + H("{done} of {total} · {pct}%", { done: fmtNum(p.done || 0), total: fmtNum(p.total), pct: pct }) : "") + "</div>" +
      (pct != null ? '<div class="meter thin" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '"><div style="width:' + pct + '%"></div></div>'
        : '<div class="meter thin busy" role="progressbar" aria-busy="true"><div></div></div>') + "</div>";
  }
  // whether something red on a tool's page stops part of it (or a check page's checks couldn't all run), in library ``key``
  // or in any: the server's word for every tool alike (the state's tools_red, App._tools_red), so the red on a page reaches
  // its library tab (libTabs), its card on the Tools page and the menu — never worked out here, nor tool by tool
  function toolBad(name, key) {
    var libs = (ST.tools_red || {})[name] || [];
    return key == null ? libs.length > 0 : libs.indexOf(String(key)) >= 0;
  }
  function pageTools() {
    var out = top(T("Tools"), H("What Plex Cache can do to your library beyond caching it. Each tool has a page of its own; none changes anything until you ask it to. The number on a card is what the tool has found to do, across your libraries."), "");
    out += '<div class="stack">' + toolsNav("tools") + '<div class="grid">' + TOOLS.map(function (t) {
      return '<section class="card toolcard"><h2><a href="/' + t[0] + '" data-nav>' + esc(T(t[1])) + " →</a>" +
        countChip(countOf(t[0]), toolBad(t[0])) + "</h2><p>" + esc(T(t[2])) + "</p></section>";
    }).join("") + "</div></div>";
    return out;
  }
  // ---- the tools that scan a library and do the rows you tick (tools/): Merge Duplicates, Duplicate Versions, Organize Folders…
  var TOOL = { merge: { key: null, sel: {}, confirm: false, keep: {} }, versions: { key: null, sel: {}, confirm: false, keep: {} }, folders: { key: null, sel: {}, confirm: false }, unlink: { key: null, sel: {}, confirm: false }, sync: { key: null, sel: {}, confirm: false }, convert: { key: null, sel: {}, confirm: false },
    optimizer: { key: null, sel: {}, confirm: false, libOf: {} } };  // (the optimizer's: a file's ticks by its path, over its libraries)
  function toolState(name) { return ((ST.tools || {})[name]) || { results: [], extra: {} }; }
  // the library tabs: the ones the tool can work on
  function toolLibs(name, types) {
    var u = TOOL[name], libs = ((ST.library || {}).libraries || []).filter(function (l) { return l.kind && (!types || types.indexOf(l.type) >= 0); });
    if (!u.key || !libs.some(function (l) { return String(l.key) === String(u.key); })) u.key = libs.length ? String(libs[0].key) : null;
    if (!libs.length) return '<p class="muted">' + (librariesPending() ? H("Reading your libraries from Plex…") : H("No library of the kind this tool works on.")) + "</p>";
    return libTabs({ tool: name, libs: libs, key: u.key, attrs: function (l) { return 'data-act="tool-lib" data-tool="' + name + '" data-key="' + esc(l.key) + '"'; } });
  }
  // ---- The library tabs, drawn one way on every tool's page and the check pages: a tab per library — its icon, its name,
  // and the page's count there (countOf, drawn by countChip: the number, a turning wheel while it's still to come, a grey
  // dash where the page doesn't apply; red while something red on the page stops part of it there: toolBad).
  // o: {tool (the page), libs, key (the library open), attrs(l) (the tab's data-act and its own)}
  function libTabs(o) {
    return '<div class="seg small strong" role="tablist" aria-label="' + H("Library") + '">' + o.libs.map(function (l) {
      return '<button role="tab" type="button" aria-selected="' + (String(l.key) === String(o.key)) + '" ' + o.attrs(l) + ">" + libIcon(l.key) + esc(l.title) + countChip(countOf(o.tool, l.key), toolBad(o.tool, l.key)) + "</button>";
    }).join("") + "</div>";
  }
  // whether the tool is scanning this library: said by the server, or asked for from this page and not yet answered
  // (a scan is kicked off, then the state read: the page says Scanning… across that gap too)
  function toolScanning(name, t) {
    var u = TOOL[name];
    if (t.running && t.progress && t.progress.what === "scan") return true;
    if (!u.scanAt) return false;
    if ((t.error_at || 0) >= u.scanAt || (t.scan && String(t.scan.library) === String(u.key) && (t.scan.at || 0) >= u.scanAt)) { u.scanAt = 0; return false; }
    return Date.now() / 1000 - u.scanAt < 120;
  }
  // what's running: the scan (how far, where the tool counts), or the rows being done (which one, which step)
  function toolRun(t) {
    var pg = t.progress;
    if (!t.running || !pg) return "";
    if (pg.what === "scan") {
      var spct = pg.total ? Math.round(100 * (pg.done || 0) / pg.total) : null;
      return '<div class="migprog"><div class="what">' + H("Scanning…") + '</div><div class="det">' + H("Reading the library from Plex (and its mirror’s, if it has one).") +
        (spct != null ? " " + H("{done} of {total} · {pct}%", { done: fmtNum(pg.done || 0), total: fmtNum(pg.total), pct: spct }) : "") + "</div>" +
        (spct != null ? '<div class="meter thin" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + spct + '"><div style="width:' + spct + '%"></div></div>'
          : '<div class="meter thin busy" role="progressbar" aria-busy="true"><div></div></div>') + "</div>";
    }
    if (pg.queued && !(ST.job && ST.job.kind === "tool" && ST.job.running)) return '<div class="migprog"><div class="what">' + H("Queued") + '</div><div class="det">' + H("Starts once the file job before it is done (<a href=\"/processes\" data-nav>Processes</a>).") + "</div></div>";
    return "";  // (the process's own progress is shown with Apply: toolApply)
  }
  // ---- The standard tool page. Every tool's page is one (test_tool_pages_follow_the_standard): the title with a one-line
  // blurb; the card's heading with the long explanation behind its ⓘ — never a paragraph on the page; the library tabs; the
  // options and Scan / Scan Again, with the scanning state; one line on what the last scan found; the rows — a poster and the
  // item's name first, the tool's own columns after — or, with none, a sentence saying so; Apply, with the backup plan; and
  // whatever the tool adds under it. What's in a row is fact (names, sizes, badges); an explanation goes behind a chip.
  // Every row is a comparison card (cmpRow), the tool passing what it shows (card(r, sc): see cmpRow); which rows show, and
  // which can be ticked, is the cards' checks' doing (rowSeverity) — the same in every tool.
  // spec: {name, title, help, types?, options?, read(sc) -> {n, text} (what the scan read: toolStats), units?(r) (how many of
  //        those a row is: 1), extra?(sc) -> [chips], empty, card(r, sc) -> cmpRow's spec, confirm(n), applies(sc),
  //        stale?(sc), choiceOff?(id, value, sc), after?(t, sc)}. What the scan waits for, or why it doesn't apply (Sync
  //        Libraries without a mirror), is the library's count's to say (countBox), as on every page.
  function toolPage(spec) {
    var name = spec.name, t = toolState(name), out = top(T(spec.title), toolBlurb(name), "");
    // the library's count (countOf): until it's done, what the tool found there isn't drawn — not its stats, its options'
    // counts nor its rows — and the placeholder says what it waits for (countBox)
    var tabs = toolLibs(name, spec.types), key = TOOL[name].key, cnt = key ? countOf(name, key) : null, ready = !key || countDone(cnt);  // (the tabs first: they pick the library)
    var sc = toolRows(name, t), stale = !!(sc && spec.stale && spec.stale(sc));
    if (ready) toolAutoScan(name, stale);  // (the rows of a count that's done: instant, the evaluator's analysis)
    if (stale || !ready) sc = null;  // (a scan from an older version: not shown, and scanned again by itself)
    // the rows: only those with a yellow chip (something to do) or a red one (what stops it) — a row whose issue is
    // switched off above has neither, and isn't drawn (the Library page shows every item); worked out here, from the rows
    // as found, so a switch takes effect at once
    var cards = sc ? toolCards(name, sc, spec) : null, read = sc && spec.read ? spec.read(sc) : null, units = spec.units || function () { return 1; };
    var sum = function (rows) { return rows.reduce(function (a, r) { return a + units(r); }, 0); };
    out += '<div class="stack">' + toolsNav(name) + '<section class="card">' + toolHead(name, T(spec.title), spec.help) +
      toolTop({ tabs: tabs, options: toolOptions(name, t, sc, spec), scan: toolScanBar(name, t) + toolRun(t),
        stats: sc && { last: toolScanning(name, t), read: read && read.n, readText: read && read.text, issues: cards.shown.length,
          passNow: read && read.n - sum(cards.rows), passAll: read && read.n - sum(sc.rows), extra: spec.extra ? spec.extra(sc) : [] } });
    if (!ready) out += countBox(cnt);  // (what it waits for — the mirror not read yet, say — or why it doesn't apply: countBox, as every page's)
    else if (sc) {
      if (!sc.rows.length) out += '<p class="muted toolempty">' + badge("pass", T("Nothing found")) + " " + spec.empty + "</p>";
      else if (!cards.shown.length) out += '<p class="muted toolempty">' + badge("info", T("Nothing to do")) + " " + H("What was found is about an issue switched off above.") + "</p>";
      else {
        // a page of cards at a time (cardsPer), the pages told by what's on them; All ticks every row on every page
        var u = TOOL[name], off = Math.max(0, Math.min(u.offset || 0, cards.shown.length - 1));
        var per = cardsPer();
        off -= off % per;
        var onPage = cards.shown.slice(off, off + per), canIds = {};
        cards.can.forEach(function (r) { canIds[r.id] = true; });
        u.pageCan = onPage.filter(function (c) { return canIds[c.id]; }).map(function (c) { return c.id; });
        var pg = pager({ total: cards.shown.length, pages: pageRanges(cards.shown.map(function (c) { return c.title; }), per) }, off, per, 'data-act="tool-page" data-tool="' + name + '"');
        out += pg + onPage.map(cmpRow).join("") + pg;
        out += toolApply(name, t, cards.can, spec.confirm, spec.applies(sc));
      }
    }
    return out + "</section>" + (spec.after ? spec.after(t, sc) : "") + toolProcess(name) + toolHistory(name) + "</div>";
  }
  // The tools' options are per library (the tools settings section): a library's kind decides which it has, and each is
  // its default unless set for that library. A switch says whether rows about its issue are live; a choice is a way to do it.
  var KIND_OF_TYPE = { movie: "movie", show: "tv", artist: "audio" };
  function toolKind(key) {
    var lib = ((ST.library || {}).libraries || []).filter(function (l) { return String(l.key) === String(key); })[0];
    return lib ? (lib.kind || KIND_OF_TYPE[lib.type] || "") : "";
  }
  function toolEnabled(name, key) {
    var t = toolState(name), on = (t.enabled || {})[key];
    if (on) return on;
    on = {};
    (t.options || []).forEach(function (o) { on[o.id] = o.default; });
    return on;
  }
  function toolLive(name, key, row) {
    var on = toolEnabled(name, key);
    return (row.issues || []).some(function (i) { return on[i] === true; });
  }
  // A tool's rows as cards (spec.card), each with its severity, and only the ones to show — those with something to do
  // (a yellow chip) or a red chip that stops it: the ones that can be done first, then the ones stopped, each in the scan's
  // order. ``can``: the rows that can be ticked (a yellow chip and no red one: Apply, and All — every page's — TOOL[name].can).
  function toolCards(name, sc, spec) {
    var todo = [], stopped = [], can = [], hidden = 0, rows = [];
    sc.rows.forEach(function (r) {
      var c = Object.assign({ tool: name, id: r.id, title: r.title }, spec.card(r, sc));
      cmpBlocks(name, sc.library, r, c);
      c.sev = rowSeverity(cmpStatuses(c));
      if (!c.sev.shown) { hidden++; return; }
      rows.push(r);
      if (c.sev.live && !c.sev.blocked) { todo.push(c); can.push(r); } else stopped.push(c);
    });
    TOOL[name].can = can.map(function (r) { return r.id; });
    return { shown: todo.concat(stopped), can: can, hidden: hidden, rows: rows };
  }
  function toolOptions(name, t, sc, spec) {
    var u = TOOL[name], key = u.key;
    if (!key) return "";
    var kind = toolKind(key), opts = (t.options || []).filter(function (o) { return !kind || o.kinds.indexOf(kind) >= 0; });
    if (!opts.length) return "";
    var on = toolEnabled(name, key), help = TOOL_OPTION_HELP[name] || {}, last = toolRows(name, t);  // (sc: null while the count isn't done — no counts; a choice the last scan rules out stays out)
    return optionGrid(opts.map(function (o) {
      var v = on[o.id], rv = v === o.default ? "" : ' <button type="button" class="revert" data-act="tool-switch-revert" data-tool="' + name + '" data-opt="' + esc(o.id) + '" data-key="' + esc(key) + '" title="' + H("Back to the default ({value})", { value: o.choices ? esc(msg((o.choices.filter(function (c) { return c[0] === o.default; })[0] || [0, o.default])[1])) : o.default ? T("on") : T("off") }) + '">↺</button>';
      var hlp = help[o.id] || richText(msg(o.help || ""));
      if (o.choices) {
        return optionRow({ help: hlp, title: msg(o.name), label: esc(msg(o.name)) + ' <select class="sel sm" data-change="tool-switch" data-tool="' + name + '" data-opt="' + esc(o.id) + '" data-key="' + esc(key) + '">' +
          o.choices.map(function (c) { return '<option value="' + esc(c[0]) + '"' + ((spec.choiceOff && last && spec.choiceOff(o.id, c[0], last)) ? " disabled" : "") + (v === c[0] ? " selected" : "") + ">" + esc(msg(c[1])) + "</option>"; }).join("") + "</select>", revert: rv });
      }
      // what the switch finds in the library, as last scanned: its rows (grey while it's off: what turning it on would show)
      var n = sc ? sc.rows.filter(function (r) { return (r.issues || []).indexOf(o.id) >= 0; }).length : null;
      return optionRow({ help: hlp, title: msg(o.name), label: '<input type="checkbox" data-act="tool-switch" data-tool="' + name + '" data-opt="' + esc(o.id) + '" data-key="' + esc(key) + '"' + (v ? " checked" : "") + "> " + esc(msg(o.name)), revert: rv, count: n, on: !!v });
    }));
  }
  // ---- Options, drawn one way everywhere — the File Checks' way: a row each, its ⓘ first, its box (or its choice) and its
  // name on one line, its ↺ back to the default, and on the right what it finds in the library (yellow; green 0; grey while
  // it's off) — the rows in two columns where there's room. o: {help, title, label (html: the box or choice, the name),
  // revert (html), count (n, or null: none to say), on}
  function optionRow(o) {
    var n = o.count, b = n == null ? "" : !o.on ? (n ? badge("off", fmtNum(n)) : "") : n ? badge("warn", fmtNum(n)) : badge("pass", "0");
    return '<div class="chk lhchk optrow">' + info(o.help, o.title) + "<label>" + o.label + "</label>" + (o.revert || "") + b + "</div>";
  }
  function optionGrid(rows) { return '<div class="optgrid">' + rows.join("") + "</div>"; }
  // ---- The top of every tool's page and check page, one layout: the library tabs, and on their right what the last scan
  // found (toolStats); under them the options (optionGrid), anything the page adds there (more), and Scan / Scan Again.
  // o: {tabs, stats (toolStats'), options, more, scan} — each html but stats, any of them left out
  function toolTop(o) {
    return '<div class="lctl tooltop">' + (o.tabs || "") + toolStats(o.stats) + "</div>" + (o.options || "") + (o.more || "") + (o.scan || "");
  }
  // What the last scan found, the same chips on every page: what was read (green), the issues found — each item once,
  // however many it has — (yellow; green when none), how many pass the checks as they're set (green), how many would pass
  // every check at its strictest (grey), then what the page adds (extra: chips, statChip). Each left out when not known.
  // st: {read (n), readText, issues, passNow, passAll, extra: [html], last (a scan running: these are the last one's)}
  function toolStats(st) {
    if (!st) return "";
    var ok = function (n) { return n != null && !isNaN(n); }, out = [];
    if (ok(st.read)) out.push(statChip("pass", st.readText));
    if (ok(st.issues)) out.push(st.issues ? statChip("warn", P(st.issues, "{n} issue found", "{n} issues found", { n: fmtNum(st.issues) }), T("Items with something to do or something in the way, each counted once."))
      : statChip("pass", T("No issues found")));
    if (ok(st.read) && ok(st.passNow)) out.push(statChip("pass", P(Math.max(0, st.passNow), "{n} passes current tests", "{n} pass current tests", { n: fmtNum(Math.max(0, st.passNow)) }), T("Items with nothing flagged by the checks as they’re set above.")));
    if (ok(st.read) && ok(st.passAll)) out.push(statChip("grey", P(Math.max(0, st.passAll), "{n} passes all tests", "{n} pass all tests", { n: fmtNum(Math.max(0, st.passAll)) }), T("Items with nothing flagged with every check on, at its strictest.")));
    out = out.concat(st.extra || []);
    if (!out.length) return "";
    return '<div class="toolstats">' + (st.last ? '<span class="det">' + H("From the last scan:") + "</span>" : "") + out.join("") + "</div>";
  }
  // the Library page's views (Health, Caching): what they found as the tools' pages say it, chips on a row of their own
  function libStats(st) { var h = toolStats(st); return h ? '<div class="libstats">' + h + "</div>" : ""; }
  function statChip(status, text, tip) { return '<span class="badge b-' + esc(status) + '"' + (tip ? ' title="' + esc(tip) + '"' : "") + ">" + esc(text) + "</span>"; }
  // @testable: placeBlocks — pure: the standard way a check from elsewhere (another tool, Library Health) that stops a row
  // goes on its card: ``blocks`` [{check, rk?}] each on the side of its record (side.rk), else the side that stays (kind
  // keep), else the card's own chips — first among them; a side with a red one that stays is greyed (dim), as the sides
  // that go: it can't go as it is
  function placeBlocks(c, blocks) {
    var sides = (c.left || []).concat(c.right || []), kept = sides.filter(function (sd) { return sd.kind === "keep"; })[0];
    blocks.forEach(function (b) {
      var sd = (b.rk != null && sides.filter(function (x) { return x.rk != null && String(x.rk) === String(b.rk); })[0]) || kept;
      if (!sd) { c.checks = [b.check].concat(c.checks || []); return; }
      sd.checks = [b.check].concat(sd.checks || []);
      if (b.check.status === "fail" && sd.kind === "keep") sd.dim = true;
    });
    return c;
  }
  // @end
  // What stops a tool's rows, put on every tool's cards by toolCards (placeBlocks) — the server refusing the same rows
  // (Tool.mark_stops) — and only on a row with something to do: its blocks, each a red chip on its record's side
  function cmpBlocks(name, key, r, c) {
    return placeBlocks(c, toolLive(name, key, r) ? recordBlocks(name, key, r) : []);
  }
  // ---- What stops a row, said one way for every block on every tool's page (blockText). A block (row.blocks: Tool.mark_stops,
  // every one of Tool.block_ids) is a finding on one of the row's records — a Library Health check the tool is stopped by
  // (Tool.blocking_checks), a source's finding on its files (Mirror Checks' on Sync Libraries' rows: Tool.blocking_sources),
  // its record one of a film's several (Merge Duplicates': tool_merge) — gone while its check is switched off. Its red chip
  // opens to why the row is stopped (what was found, and by which page), what could go wrong if it went ahead anyway, and
  // the two ways out: put right what was found (an example of how), or Ignore it on the record — going ahead is still
  // possible, and yours to choose. Ignored (here, or on the page that found it), the chip is grey, says so, and can be
  // brought back. Every block has its words here, what could go wrong said for each tool it stops; the tests hold every
  // tool's blocks to it (test_every_block_says_why_and_both_ways_out).
  // {page (the one that found it), label, found(x) (html), risk: html, or {tool: html}, fix (html), ok (html: when it's right
  // as it is), ignored (html: what it means once ignored)}
  var BLOCK_TEXT = {
    versions_named_differently: { page: "plexchecks", label: N("Dissimilar file versions"),
      found: function () { return H("A file looks attached to the wrong record: its name is dissimilar to the record’s other files."); },
      risk: {
        merge: N("Merging would join another record to one that may hold a file of a different film: the film’s metadata over the wrong file, put back only by splitting the record apart again."),
        sync: N("Its files pair it with an item on the other side: with one of them another film’s, syncing could bring the wrong item in line, and carry the wrong grouping across.") },
      fix: N("If these are different titles: split the record apart in Plex (its menu: Split Apart), then scan again."),
      ok: N("If the files are right — one film named in two languages, say — ignore it."),
      ignored: N("Its files are named dissimilarly, but that’s ignored: they’re taken as right.") },
    // Mirror Checks' findings on an item's files (mirrorhealth.BLOCKS: they aren't the same at the two sites, so the items
    // paired by them may not be one), as the last compare found them
    mirror_only_here: mirrorBlock(N("Only on disk here"), N("Let the folder sync copy the file to the other site, or delete it here if it shouldn’t be there, then press Scan Again on Mirror Checks.")),
    mirror_only_there: mirrorBlock(N("Only on disk at the other site"), N("Let the folder sync copy the file here, or delete it at the other site if it shouldn’t be there, then press Scan Again on Mirror Checks.")),
    mirror_size_differs: mirrorBlock(N("A different size at each site"), N("Let the folder sync finish copying it, or copy the right one over the other, then press Scan Again on Mirror Checks.")),
    mirror_missing_here: mirrorBlock(N("In Plex here, not on disk"), N("Put the file back, or have Plex here look at its folder (Scan Folder on Mirror Checks) so it drops what’s gone, then press Scan Again there.")),
    // the row's record one of a film's several, as Merge Duplicates last found them in this library (Tool.merge_first)
    tool_merge: { page: "merge", label: N("More than 1 Plex record"),
      found: function () { return H("This item is on more than one Plex record here."); },
      risk: N("What’s done here would be done to one record of the film only: the others stay as they were, and the film’s records drift further apart."),
      fix: N("Merge the records with <a href=\"/merge\" data-nav>Merge Duplicates</a> first: this row is free once they’re one."),
      ok: N("If they’re meant to be separate records — two films Plex matched alike, say — ignore it: this record is then done on its own."),
      ignored: N("It’s one of several Plex records, but that’s ignored: this record is done on its own.") } };
  function mirrorBlock(label, fix) {
    return { page: "mirrorchecks", label: label,
      found: function (x) { return H("This item’s files aren’t the same at the two sites: {what}", { what: msg(x.text) }); },
      risk: N("Its two sides are paired by their files, so syncing it now could bring the wrong item in line: the match, edits and posters of one film set on another."),
      fix: fix,
      ok: N("If it’s right as it is — a different cut kept at each site, say — ignore it."),
      ignored: N("Its files aren’t the same at the two sites, but that’s ignored: it can be synced.") };
  }
  // what every red chip drawn says, by tool and block: read by the tests (tests/render.js), as PCINFO
  var BLOCKS_SAID = {};
  window.PCBLOCKS = BLOCKS_SAID;
  // a block's words, the one way: why it's stopped, what could go wrong, fix it, or ignore it (with its button); ignored,
  // what that means and Bring Back. The page that found it linked to the record there (recordLink), or to the page.
  function blockText(name, key, x) {
    var bt = BLOCK_TEXT[x.id], risk = H(typeof bt.risk === "string" ? bt.risk : bt.risk[name]);
    var onChecks = Object.keys(CHECK_PAGES).some(function (c) { return CHECK_PAGES[c][0] === bt.page; });  // (its record there: a link straight to it)
    var where = '<a href="' + esc(onChecks ? recordLink(bt.page, key, x.key) : "/" + bt.page) + '" data-nav>' + esc(toolTitle(bt.page)) + "</a>";
    var btn = function (act, label, tip) {
      return ' <button class="btn sm" type="button" data-act="' + act + '" data-key="' + esc(key) + '" data-item="' + esc(x.key) + '" data-ids="' + esc(x.id) + '"' + busyAttr(scanBusy(key, [x.id]), tip) + ">" + label + "</button>";
    };
    var out = x.ignored ?
      "<strong>" + H("Ignored:") + "</strong> " + H(bt.ignored) + " " + H("The row can go ahead. Ignored on this record, as on {page}.", { page: "\u0001" }).replace("\u0001", where) +
        btn("lh-restore", H("Bring Back"), H("Stops this row again until it’s put right or ignored.")) :
      "<strong>" + H("Why it’s stopped:") + "</strong> " + bt.found(x) + " " + H("Found by {page}.", { page: "\u0001" }).replace("\u0001", where) +
      "<br><br><strong>" + H("What could go wrong:") + "</strong> " + risk +
      "<br><br><strong>" + H("To fix it:") + "</strong> " + H(bt.fix) +
      "<br><br><strong>" + H("Or ignore it:") + "</strong> " + H(bt.ok) + " " + H("The row can then go ahead; it shows as ignored on {page} too.", { page: "\u0001" }).replace("\u0001", where) +
        btn("lh-ignore", H("Ignore"), H("Lets this row go ahead as it is. It can be brought back."));
    BLOCKS_SAID[name + ":" + x.id + (x.ignored ? ":ignored" : "")] = out;
    return out;
  }
  // a row's blocks as red chips (grey once ignored) on the side of their record
  function recordBlocks(name, key, r) {
    return (r.blocks || []).filter(function (x) { return x.on !== false && BLOCK_TEXT[x.id]; }).map(function (x) {
      return { rk: x.key, check: { id: "block:" + x.id, status: x.ignored ? "grey" : "fail", label: T(BLOCK_TEXT[x.id].label), text: blockText(name, key, x) } };
    });
  }
  // What a tool's row says about its item: chips (the Health view's kind), each a badge that opens on a click to its
  // explanation and details underneath — the one way a row carries a check (the standard the tests hold the tool pages to).
  // items: [{id, issue?, status?, label, text (html), lines: [html]}]. The colours mean the same everywhere (rowSeverity):
  // yellow (warn) is what there is to do — only the row's issue is yellow, so it's given as ``issue`` (an option id), never
  // as a status; grey (info, grey, off) says something and changes nothing; red (fail) stops the row; green (pass) is good.
  // One chip open on a card at a time ("tool:card" -> {id, pair, group}): opening another closes it. A chip may name a
  // ``pair`` (its file, the mirror, a setting): opened, the first chip of the same pair on each of the card's other sides
  // opens with it, so the two read side by side — File 1 with the file on the other side, Record destroyed on the mirror
  // with Listed there too.
  var TOOL_OPEN = {};
  // a chip's colour: an item with ``issue`` is the issue the row is about — yellow while its switch is on for the library,
  // grey (off) while it isn't; the rest keep their own status
  function chipStatus(name, c) {
    if (!c.issue) return c.status;
    return TOOL[name] && toolEnabled(name, TOOL[name].key)[c.issue] === true ? "warn" : "off";
  }
  // @testable: chipsOpen — pure: which of a group's chips (one side's, or the card's own) are open, the card's open one
  // being ``at`` ({id, pair, group}, or none): the one itself, or on another side the first chip of its pair
  function chipsOpen(at, rowId, items) {
    var paired = false;
    return items.map(function (c) {
      var id = rowId + "/" + c.id, open = !!at && (at.id === id || (!paired && !!c.pair && at.pair === c.pair && at.group !== rowId));
      if (open && at.id !== id) paired = true;  // (one of the pair to a side)
      return open;
    });
  }
  // @end
  function chips(name, rowId, items, cardId) {
    var p = chipParts(name, rowId, items, cardId);
    return '<div class="sschips">' + p.buttons + "</div>" + p.shown;
  }
  // the chips' buttons, and what the open one says (to go under them): {buttons, shown}
  function chipParts(name, rowId, items, cardId) {
    cardId = cardId == null ? rowId : cardId;
    var out = "", shown = "", opens = chipsOpen(TOOL_OPEN[name + ":" + cardId], rowId, items);
    items.forEach(function (c, k) {
      var id = rowId + "/" + c.id, open = opens[k];
      var status = chipStatus(name, c);
      out += '<button type="button" class="badge b-' + esc(status) + " sschip" + (open ? " on" : "") + '" aria-expanded="' + open + '" data-act="tool-chip" data-tool="' + name + '" data-id="' + esc(id) +
        '" data-card="' + esc(cardId) + '" data-group="' + esc(rowId) + '"' + (c.pair ? ' data-pair="' + esc(c.pair) + '"' : "") + ">" + esc(c.label) + "</button>";
      if (open) shown += '<div class="ssval">' + (c.text || "") + (c.lines || []).map(function (l) { return '<div class="det">' + l + "</div>"; }).join("") + "</div>";
    });
    return { buttons: out, shown: shown };
  }
  // @testable: rowSeverity — pure: what a row's checks (the statuses of its chips and boxes) make of it, the one rule for
  // every tool. A yellow one (warn) makes it a row to do: it can be ticked; a red one (fail) stops it: it can't, whatever
  // else it has; grey (info, grey, off) and green (pass) change nothing. A row is shown on a tool's page only with a
  // yellow or a red one. {live, blocked, shown}
  function rowSeverity(statuses) {
    var live = statuses.indexOf("warn") >= 0, blocked = statuses.indexOf("fail") >= 0;
    return { live: live, blocked: blocked, shown: live || blocked };
  }
  // @end
  // every check a card holds — its own chips, each side's, its boxes — by colour (chipStatus)
  function cmpStatuses(c) {
    var out = [], add = function (items) { (items || []).forEach(function (x) { out.push(chipStatus(c.tool, x)); }); };
    add(c.checks);
    (c.left || []).concat(c.right || []).forEach(function (sd) { add(sd.checks); });
    (c.notes || []).forEach(function (n) { out.push(n.status); });
    return out;
  }
  // ---- The comparison card: every tool's row is one (test_tool_rows_use_the_comparison_card), and the optimizer's
  // libraries. One item, what the tool does to it side by side: what goes framed in red and labelled (its insides greyed),
  // what stays or comes framed in green and labelled, the arrow between pointing at the green side — a button that turns
  // it where the tool lets you choose which side stays (swap), else the same box, fixed — and under them the row's boxes
  // (red: what stops it; grey: worth knowing) and its files, numbered. Above: the tick, the item's name, its checks.
  // What's inside a side is fact, never a colour of its own: only the checks (chips, boxes) carry one, so a yellow or red
  // is always one that counts (rowSeverity), and a card's insides are greyed of any other.
  // c: {tool, id, title, sev (rowSeverity), tick (false: none — the optimizer's), checks: [chip items],
  //     left: [side], right: [side] (one, or several stacked), swap: null | 'data-act=…' (the arrow's button), swapTitle,
  //     notes: [{status: "fail" | "info", html, cls?}], files: [paths, numbered as the File n chips]}
  // side: {kind: "keep" | "drop", label, body (html: a card of the tool's own drawing) — or poster, what (html), lines: [html]
  //     — and checks: [chip items] (with a body: under it), dim (false: a red side not greyed inside — the optimizer's file
  //     now, its warnings to read; true: a green side greyed — one that can't go as it is: a red check from elsewhere on it, placeBlocks)}
  function cmpRow(c) {
    var n = 0;
    var side = function (sd) {
      n += 1;
      // (a side drawn by the tool, ``body``, carries its checks among its own chips — in its slots, never greyed — or, with
      // none, under it)
      var parts = sd.body != null && sd.checks && sd.checks.length ? chipParts(c.tool, c.id + "/s" + n, sd.checks, c.id) : { buttons: "", shown: "" }, own = "";
      var inner = sd.body != null ? cmpQuiet(sd.body) : '<div class="sscard"><div class="ssbody">' +
        (sd.poster ? '<img class="ssposter" src="/api/poster?path=' + encodeURIComponent(sd.poster) + '" alt="" loading="lazy">' : '<div class="ssposter none"' + (sd.noPoster ? ' title="' + esc(sd.noPoster) + '"' : "") + "></div>") +
        '<div class="ssinfo"><div class="what">' + (sd.what || "") + "</div>" + (sd.lines || []).map(function (l) { return '<div class="det">' + cmpQuiet(l) + "</div>"; }).join("") +
        (sd.checks && sd.checks.length ? chips(c.tool, c.id + "/s" + n, sd.checks, c.id) : "") + "</div></div></div>";
      if (sd.body != null) {
        if (inner.indexOf(CHIP_SLOT) >= 0) inner = inner.replace(CHIP_SLOT, parts.buttons).replace(SHOWN_SLOT, parts.shown);
        else if (parts.buttons) own = '<div class="mgchecks"><div class="sschips">' + parts.buttons + "</div>" + parts.shown + "</div>";
      }
      return sd.kind === "keep" ? '<div class="mgkeep"><div class="mgkeeplbl">' + esc(sd.label) + "</div>" + (sd.dim === true ? '<div class="mgdim">' + inner + "</div>" : inner) + own + "</div>"
        : '<div class="mgdrop"><div class="mgdroplbl">' + esc(sd.label) + "</div>" + (sd.dim === false ? inner : '<div class="mgdim">' + inner + "</div>") + own + "</div>";
    };
    var stack = function (list) { var h = list.map(side); return h.length > 1 ? '<div class="ssstack">' + h.join("") + "</div>" : h[0] || ""; };
    var keepRight = (c.right || []).some(function (sd) { return sd.kind === "keep"; }), arrow = keepRight ? "→" : "←";
    var left = stack(c.left || []), right = stack(c.right || []);
    var sev = c.sev || rowSeverity(cmpStatuses(c));
    var tick = c.tick === false ? "" : toolCheck(c.tool, { id: c.id, title: c.title }, !sev.live || sev.blocked) + " ";
    var files = pathsShown(c.files || []);
    return '<div class="ssitem cmprow' + (sev.blocked ? " blocked" : "") + '" data-cmp="' + esc(c.tool) + '"><div class="sstop"><span class="what">' + tick + esc(c.title) + '</span><span class="sswarn">' +
      (c.checks && c.checks.length ? chips(c.tool, c.id, c.checks, c.id) : "") + "</span></div>" + left +
      (c.swap ? '<button type="button" class="ssarrow mgarrow" ' + c.swap + ' title="' + esc(c.swapTitle || "") + '"><span>' + arrow + "</span></button>"
        : '<span class="ssarrow mgarrow fixed" aria-hidden="true"><span>' + arrow + "</span></span>") + right +
      (c.notes || []).map(function (x) { return '<div class="note ' + (x.status === "fail" ? "badbox" : "greybox") + (x.cls ? " " + x.cls : "") + '">' + x.html + "</div>"; }).join("") +
      (files.length ? '<div class="ssnotes ssfilelist"><div class="ssfiles many">' + files.map(function (f, k) { return '<div><span class="ssfn">' + (k + 1) + "</span> " + esc(f) + "</div>"; }).join("") + "</div></div>" : "") + "</div>";
  }
  // @testable: pathsShown — a card's files as listed under it: one file, its whole path; several, their paths without the
  // folders they all share (/data/Movies/ on each), so what tells them apart is what's shown
  function pathsShown(paths) {
    var parts = paths.map(function (p) { return String(p).split("/"); }), k = 0;
    if (parts.length < 2) return paths.map(String);
    while (parts.every(function (ps) { return ps.length - 1 > k && ps[k] === parts[0][k]; })) k++;
    return parts.map(function (ps) { return ps.slice(k).join("/"); });
  }
  // @end
  // a side's insides drawn by the tool's own code (two Plex records side by side: syncSide): its colours greyed,
  // so the only yellow or red on a card is a check's
  function cmpQuiet(html) { return String(html).replace(/\bb-(warn|fail)\b/g, "b-grey"); }
  // the Scan button (with a tool's own options before it), and what the last scan found
  function toolScanBar(name, t) {
    var sc = t.scan, u = TOOL[name], mine = sc && String(sc.library) === String(u.key), scanning = toolScanning(name, t);
    return '<div class="row" style="align-items:center;gap:12px;flex-wrap:wrap">' +
      '<button class="btn" type="button" data-act="tool-scan" data-tool="' + name + '"' + (t.running || scanning ? " disabled" : "") + ">" + (scanning ? H("Scanning…") : mine ? H("Scan Again") : H("Scan")) + "</button>" +
      (mine && !scanning ? '<span class="det">' + H("Scanned {when}", { when: ago(sc.at) }) + "</span>" : "") + "</div>" +
      (scanning && !(t.running && t.progress) ? '<div class="migprog"><div class="meter thin busy" role="progressbar" aria-busy="true"><div></div></div></div>' : "") +
      (t.error && !scanning ? '<p class="bad">' + esc(msg(t.error)) + "</p>" : "");
  }
  // a tool scans the library opened by itself, once (Scan Again for more): what toolAutoPlan says
  function toolAutoScan(name, stale) {
    var u = TOOL[name], t = toolState(name);
    if (!u.key) return;
    u.tried = u.tried || {};
    if (toolAutoPlan(u.key, t, u.tried, stale) === "scan") {
      u.tried[u.key] = true; u.scanAt = Date.now() / 1000;
      api("/api/tools/" + name, { action: "scan", library: u.key, refresh: false }).then(function () { load(); }).catch(function () { u.scanAt = 0; });  // (the data as gathered: instant)
    }
  }
  // @testable: toolAutoPlan — pure: what a tool page does by itself for library `key`. "scan" when the
  // library has no scan yet (or an older library's is shown, or its own is `stale` — from an older version, which the
  // page can't draw), nothing is running, and it wasn't tried already.
  function toolAutoPlan(key, t, tried, stale) {
    if (!key || t.running || tried[key]) return null;
    if (t.scan && String(t.scan.library) === String(key) && !stale) return null;
    return "scan";
  }
  // @end
  // the rows to tick, and the Apply button: a confirmation first, since the tool then acts on your library
  // Before files change: two lights — room for what's written, and a recent snapshot (or a backup you made) — both
  // green (or grey) before Apply opens. The tools that change files (peers.py FILE_TOOLS) and what each writes.
  // the tools that change only Plex's records (no files): their Apply backs the records up first, per the Backup preference
  var PLEX_TOOLS = { merge: true, sync: true };
  // Before an Apply that changes Plex's records (Merge Duplicates, Sync Libraries): what's backed up first — the records,
  // or none where you back up yourself (plexbackup.py, ST.plex_backup) — as one line, for the box above the Apply button
  function plexBackupText() { var pb = ST.plex_backup || {}; return pb.text ? links(msg(pb.text)) : ""; }
  function plexBackupNote() {
    var pb = ST.plex_backup || {};
    if (!pb.kind) return "";
    return '<div class="det plexbackup">' + badge(pb.kind === "records" ? "pass" : "info", pb.kind === "records" ? T("Backup first") : T("No backup")) + " " + plexBackupText() + "</div>";
  }
  var FILE_TOOLS = { versions: function (rows) { return 0; }, folders: function (rows) { return 0; }, unlink: function (rows) { return rows.reduce(function (a, r) { return a + (r.size || 0); }, 0); }, convert: function (rows) { return rows.reduce(function (a, r) { return a + (r.size || 0); }, 0); } };
  var READY = {};  // "key/needed" -> {at, data}
  // The lights before anything changes files, the same everywhere: room for what's written (and what a snapshot keeps),
  // and the backup plan — what the process does first, per the Backup preference (System → Preferences): a snapshot
  // named for the action, the copies the action keeps, or none where you back up yourself. `keeps`: the action keeps
  // copies of its own (a tool's text for them); `retained`: what a snapshot keeps of the files changed; `tool` names
  // the snapshot. A snapshot that couldn't be taken stays said until the next one is, or it's dismissed. {ready, html, label}.
  function readinessPanel(key, needed, tool, keeps, retained) {
    var id = key + "/" + needed + "/" + (keeps ? 1 : 0) + "/" + (retained || 0) + "/" + (tool || ""), got = READY[id];
    if (!got || Date.now() - got.at > 20000) {
      READY[id] = got = { at: Date.now(), data: got ? got.data : null };
      api("/api/tools/readiness?key=" + encodeURIComponent(key) + "&needed=" + encodeURIComponent(needed) + (keeps ? "&keeps=1" : "") +
        (retained ? "&retained=" + encodeURIComponent(retained) : "") + "&tool=" + encodeURIComponent(tool || "")).then(function (d) { READY[id].data = d; render(); }).catch(function () {});
    }
    var d = got.data;
    if (!d) return { ready: false, label: "backup", html: loadingBar(T("Checking room and the backup plan…")) };
    var bp = d.backup || {};
    var change = "";
    var about = info(H("<strong>A snapshot</strong> is the whole dataset as it is this minute, taken through TrueNAS as the process’s first step and named for what it’s for, so you can tell it apart in TrueNAS → Datasets → Snapshots. The app deletes it after <strong>{n} days</strong> (the days are yours to set in <a href=\"/system#prefs\" data-nav>System → Preferences</a>); until then Revert on the <a href=\"/processes\" data-nav>Processes page</a> puts the changed files back from it, and it holds the space those files took.<br><br>" +
      "<strong>The copies an action keeps</strong> are its own: files it sets aside rather than deletes, kept for the same days and put back from its page. An action that keeps none takes a snapshot instead.<br><br>" +
      "<strong>A backup you make yourself</strong>: the process goes ahead without one, and Apply says so.<br><br>" +
      "Which it is, is set once, in <a href=\"/system#prefs\" data-nav>System → Preferences</a>, for every action that changes files.", { n: d.life_days }), T("Backup"));
    var err = d.error ? '<div class="det bad">' + links(msg(d.error.text)) + ' <button class="link" type="button" data-act="tool-backup" data-how="dismiss" data-key="' + esc(key) + '">' + H("Dismiss") + "</button></div>" : "";
    var html = '<div class="lights ready">' + light(d.space.status, T("Room for it"), msg(d.space.text), false) +
      lightInfo({ status: bp.status || "warn", text: T("Backup"), detail: msg(bp.text || "") + change + (d.snapshot && d.snapshot.status === "ok" ? " " + T("A snapshot from the last half hour is there too: {name}.", { name: d.snapshot.name }) : "") }, T("Backup"), false, about) + err + "</div>";
    return { ready: !!d.ready, label: bp.label || "backup", backup: bp.text ? links(msg(bp.text)) : "", html: html };
  }
  function readiness(name, key, rows) {
    return readinessPanel(key, FILE_TOOLS[name](rows), name, (toolState(name) || {}).keeps, 0);
  }
  // a tool card's heading: its name, and How it works opening the long explanation under it (the Caching cards' way);
  // the short one, always shown, is the page's description above (toolBlurb)
  function toolHead(name, title, help) {
    var sec = "tool-" + name;
    return '<div class="head"><h2>' + esc(title) + '</h2><span style="flex:1"></span>' + guideBtn(sec) + "</div>" +
      (guides[sec] ? '<div class="guide toolguide"><div><p>' + help + "</p></div></div>" : "");
  }
  // a tool's title, from the Tools page's list
  function toolTitle(name) { var t = TOOLS.filter(function (x) { return x[0] === name; })[0]; return t ? T(t[1]) : name || ""; }
  // what a tool does, in a sentence or two: its card's text on the Tools page, and the description atop its own page
  // (test_tool_pages_open_with_their_card_text) — one text, so the two can't drift apart
  function toolBlurb(name) { var t = TOOLS.filter(function (x) { return x[0] === name; })[0]; return t ? esc(T(t[2])) : ""; }
  // the process a tool's Apply became (jobs.py ToolJob): running or queued for this tool, or the last one that ended
  function toolJob(name) {
    var j = ST.job;
    if (j && j.kind === "tool" && j.tool === name) return j;
    return null;
  }
  var ROLLBACK_CONFIRM = null;  // the run whose whole-snapshot rollback waits for a yes
  function undoButtons(jobId, bk) {
    bk = bk || {};
    var records = bk.kind === "records";  // (a run that changed only Plex's records: put back from the backup of them)
    var out = '<div class="det undo"><button class="btn sm" type="button" data-act="job-revert" data-job="' + esc(jobId) + '">' + (records ? H("Revert Plex Changes") : H("Revert Changed Files")) + "</button> " +
      '<span class="muted">' + (records ? H("Puts back the Plex records this run changed, from the backup: a merge split apart, each record with its own metadata again.")
        : bk.kind === "snapshot" ? H("Puts back only the files this run changed, from the backup.") : H("Puts back only what this run changed, by the tool’s own way back.")) + "</span></div>";
    if (bk.kind === "snapshot") {
      out += ROLLBACK_CONFIRM === jobId
        ? '<div class="det undo bad">' + H("Roll the whole dataset back to the snapshot? Everything written to it since — by anything, not only this run — is lost.") + " " +
          '<button class="btn sm" type="button" data-act="job-rollback" data-job="' + esc(jobId) + '">' + H("Yes, Roll Back") + "</button> " + '<button class="link" type="button" data-act="job-rollback-no">' + H("Cancel") + "</button></div>"
        : '<div class="det undo"><button class="btn sm" type="button" data-act="job-rollback-ask" data-job="' + esc(jobId) + '">' + H("Roll Back Whole Snapshot") + "</button> " +
          '<span class="muted">' + H("The dataset as it was at the snapshot: every change since, by anything, is lost.") + "</span></div>";
    }
    return out;
  }
  // What Apply will do, said in a box right before it — every tool's (the rule: a tool page passes applies()): what it
  // does, what it changes (and where, with a mirror), and the backup made first — the Backup preference's plan for a tool
  // that changes files, else the tool's own word. ``a``: {does, changes, backup, more, warn}, HTML.
  function applyBox(a, gate) {
    var backup = gate && gate.backup || a.backup;
    return '<div class="note applybox' + (a.warn ? " warnbox" : "") + '">' +
      '<div class="applydoes"><strong>' + H("What Apply does") + "</strong> " + a.does + "</div>" +
      '<div class="applychanges"><strong>' + H("What it changes") + "</strong> " + a.changes + "</div>" +
      (backup ? '<div class="applybackup"><strong>' + H("Backup") + "</strong> " + backup + "</div>" : "") +
      (a.more ? '<div class="applymore">' + a.more + "</div>" : "") + "</div>";
  }
  function toolApply(name, t, rows, what, box) {  // rows: those that can be ticked (toolCards: a yellow chip, no red one)
    var u = TOOL[name], n = rows.filter(function (r) { return u.sel[r.id]; }).length;
    if (!rows.length) return "";
    var all = rows.length && rows.every(function (r) { return u.sel[r.id]; });
    var job = toolJob(name), busy = !!(job && job.running) || (t.running && (t.progress || {}).what === "apply") || (ST.queue || []).some(function (q) { return q.kind === "tool" && (q.args || {}).tool === name; });
    var picked = rows.filter(function (r) { return u.sel[r.id]; });
    var gate = FILE_TOOLS[name] ? readiness(name, u.key, picked.length ? picked : rows)  // (nothing ticked yet: the room all of them would take)
      : PLEX_TOOLS[name] ? { ready: true, html: "", label: (ST.plex_backup || {}).label || "backup", backup: plexBackupText() } : { ready: true, html: "", label: "backup" };
    var out = gate.html;
    // the process: its steps while it runs (the backup first), and how it ended until dismissed
    if (job && !job.running && (job.errors || []).length && (job.done || 0) === 0 && !busy) {
      out += '<div class="det bad">' + H("Not done — the backup couldn’t be made: {why}", { why: msg(job.errors[0].reason || "") }) + ' <button class="link" type="button" data-act="job" data-job="dismiss">' + H("Dismiss") + "</button></div>";
    }
    out += applyBox(box, FILE_TOOLS[name] || PLEX_TOOLS[name] ? gate : null);  // (right before Apply)
    var page = u.pageCan || [], pageAll = page.length && page.every(function (id) { return u.sel[id]; });
    out += '<div class="row" style="align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px"><label class="det"><input type="checkbox" data-act="tool-all" data-tool="' + name + '"' + (all ? " checked" : "") + "> " +
      (rows.length > page.length ? H("All {n} (every page)", { n: fmtNum(rows.length) }) : H("All")) + "</label>" +
      (rows.length > page.length ? '<label class="det"><input type="checkbox" data-act="tool-pagesel" data-tool="' + name + '"' + (pageAll ? " checked" : "") + "> " + H("This page") + "</label>" : "");
    if (!gate.ready) u.confirm = false;
    var label = gate.label === "none" ? H("Apply to {n} Selected Without Backing Up", { n: fmtNum(n) }) : FILE_TOOLS[name] || PLEX_TOOLS[name] ? H("Backup and Apply to {n} Selected", { n: fmtNum(n) }) : H("Apply to {n} Selected", { n: fmtNum(n) });
    if (u.confirm && n) {
      out += '<span>' + esc(what(n)) + "</span>" +
        '<button class="btn pri" type="button" data-act="tool-apply-yes" data-tool="' + name + '">' + H("Yes, Do It") + "</button>" +
        '<button class="btn" type="button" data-act="tool-apply-no" data-tool="' + name + '">' + H("Cancel") + "</button>";
    } else {
      out += '<button class="btn pri" type="button" data-act="tool-apply" data-tool="' + name + '"' + (!n || busy || !gate.ready ? " disabled" : "") + ">" + label + "</button>" +
        (!gate.ready && n ? ' <span class="det">' + H("Apply opens once both lights above are green or grey.") + "</span>" : "");
    }
    return out + "</div>";
  }
  function toolCheck(name, r, off) {
    return '<input type="checkbox" data-act="tool-sel" data-tool="' + name + '" data-id="' + esc(r.id) + '"' + (TOOL[name].sel[r.id] && !off ? " checked" : "") + (off ? " disabled" : "") + ' aria-label="' + esc(r.title) + '">';
  }
  // what was done, last first: each row's result and the steps it took
  function toolResults(t) {
    var rs = t.results || [];
    if (!rs.length) return "";
    return '<section class="card"><h2>' + H("Done") + '</h2><div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">' + H("Item") + '</th><th scope="col">' + H("Result") + "</th></tr></thead><tbody>" +
      rs.slice(0, 30).map(function (r) {
        return "<tr><td>" + esc(r.title || r.id) + '<div class="det">' + fmtWhen(r.at) + "</div></td><td>" +
          badge(r.result === "done" ? "pass" : "warn", r.result === "done" ? T("Done") : T("Not done")) + (r.reason ? ' <span class="bad">' + esc(msg(r.reason)) + "</span>" : "") +
          (r.steps || []).map(function (x) { return '<div class="det">' + esc(msg(x)) + "</div>"; }).join("") + "</td></tr>";
      }).join("") + "</tbody></table></div></section>";
  }
  function toolRows(name, t) {
    var sc = t.scan;
    return sc && String(sc.library) === String(TOOL[name].key) ? sc : null;
  }
  var MERGE_FIELD = null;  // the one chip opened: {row, id}
  // the records of one film side by side, on the comparison card: the one kept and the ones folded into it, as Sync
  // Libraries shows two servers' records — the poster, the fields each has, its files. The folded records' edits are lost.
  // The records keep their places (the tool's pick on the left); the frame and the arrow say which is kept — the tool's
  // pick, or the one chosen with the arrow (sent with Apply as keep_key)
  var SWAP_KEYS = {  // what the arrow goes round, per tool that has one: the keys of a row's sides, the tool's pick first
    merge: function (r) { return [r.keep.key].concat((r.others || []).map(function (o) { return o.key; })); },
    versions: function (r) { return [r.keep.path].concat((r.extras || []).map(function (x) { return x.path; })); } };
  // what merging does to each file, on its chip: the kept record's own files stay, the others' come over as its versions
  function mergeFileKept(f, how) {
    return how === "gains" ? H("This file isn’t attached to this item yet: it will be transferred over, as a version of the record kept.") : H("This is the existing file of the record kept: it will be kept.");
  }
  function mergeFileLost(f) { return H("This file will be transferred over to the record kept, as a version of it. The file itself isn’t touched; this record’s own metadata goes."); }
  function mergeCard(r) {
    var all = [r.keep].concat(r.others || []), chosen = TOOL.merge.keep[r.id] || r.keep.key;
    // the files by name — or by their whole path where two records' files share a name (a link or a copy in another folder)
    var names = all.reduce(function (a, x) { return a.concat(x.files || []); }, []), clash = names.some(function (f, k) { return names.indexOf(f) !== k; });
    if (clash) all = all.map(function (x) { return Object.assign({}, x, { files: x.paths || x.files }); });
    var keep = all.filter(function (x) { return String(x.key) === String(chosen); })[0] || all[0], others = all.filter(function (x) { return x !== keep; }), kinds = r.kinds || [];
    var files = [keep].concat(others).reduce(function (a, x) { return a.concat(x.files || []); }, []).filter(function (f, k, a) { return a.indexOf(f) === k; }), fidx = {};
    files.forEach(function (f, k) { fidx[f] = k + 1; });
    var act = 'data-act="merge-field" data-id="' + esc(r.id) + '"', openId = MERGE_FIELD && MERGE_FIELD.row === r.id ? MERGE_FIELD.id : null;
    var otherFiles = others.reduce(function (a, x) { return a.concat(x.files || []); }, []);
    // the one kept shows the files it gains (greyed); one folded in is greyed inside (its files become the kept one's
    // versions; its own edits, poster and collections go)
    var side = function (x) {
      if (x === keep) return { rk: x.key, kind: "keep", label: T("Metadata kept"), body: syncSide(Object.assign({}, x, { own: true, gains: otherFiles.filter(function (f) { return (x.files || []).indexOf(f) < 0; }) }), others[0] || null, "", true, kinds, act, openId, null, fidx, otherFiles, mergeFileKept, true) };
      return { rk: x.key, kind: "drop", label: T("Metadata lost"), body: syncSide(x, keep, "", false, kinds, act, openId, null, fidx, keep.files || [], mergeFileLost, true) };
    };
    var checks = [{ id: "why", issue: r.why, label: r.why === "file" ? T("The same file") : r.why === "matched" ? T("Same match") : T("Named alike"),
      text: r.why === "file" ? H("The two records hold the very same file — a link, or a copy with the same size and running time.") : r.why === "matched" ? H("Plex matched both records to the same film or show; their running times agree, and they’re the same edition.") : H("Unmatched, their files are named for the same title and year, and their running times agree.") }];
    if (kinds.filter(function (k) { return k !== "match"; }).length) checks.push({ id: "lost", status: "info", label: T("Edits lost"), text: H("A folded record has edits, a poster or collections of its own: Plex keeps only the kept record’s. Open the chips to see what goes.") });
    if ((r.linked || []).length) checks.push({ id: "linked", status: "info", label: T("Linked file"), text: H("{files}: a link (a hard link or symlink). Merging records whose files are links is risky where folders are synced — the other site holds copies, not links, and a merge there would go differently. Make each a file of its own first (Unlink Files) and let every synced library catch up.", { files: r.linked.map(function (l) { return l.name; }).join(", ") }) });
    var pathOf = {};  // (a file's whole path, for the list under the card)
    all.forEach(function (x) { (x.files || []).forEach(function (f, i) { pathOf[f] = (x.paths || [])[i] || f; }); });
    return { checks: checks, left: [side(all[0])], right: all.slice(1).map(side), files: files.map(function (f) { return pathOf[f] || f; }),
      swap: 'data-act="tool-swap" data-tool="merge" data-id="' + esc(r.id) + '"', swapTitle: T("Keep the other record’s metadata instead") };
  }
  function recLine(r) {  // a Plex record, briefly: its name, how many fields were edited, its files
    if (!r) return '<span class="muted">' + H("—") + "</span>";
    return '<div>' + esc(r.label) + (r.locked ? " " + badge("info", P(r.locked, "{n} edited field", "{n} edited fields", { n: r.locked })) : "") + (r.matched === false ? " " + badge("warn", T("Unmatched")) : "") + "</div>" +
      '<div class="det mono">' + esc((r.files || []).join(", ")) + "</div>";
  }
  var MERGE_HELP = N("Plex can hold the same film on two records — one added before the other was matched, a second copy in another folder — each with its own poster, edits and watched state. Plex’s own Merge turns them into versions of one record, keeping the record’s metadata; this finds them for you. Records are the same film when they hold the very same file (a link or a copy in another folder: the same size and running time), whatever each is matched to; or when they share a match or, unmatched, when their files are named for the same title and year — and their running times agree (4% or 30 seconds, whichever is more: a different frame rate or cut of the credits; two films that merely share a title and year differ by more), and they’re the same edition (a Director’s Cut and the theatrical cut are two records on purpose), unless their files are the very same file. The record kept is the one with the most edited fields, then the most files, then the oldest. No file is touched, so a mirror isn’t affected.");
  function pageMerge() {
    return toolPage({ name: "merge", title: "Merge Duplicate Plex Records", types: ["movie", "show"],
      help: MERGE_HELP + "<br><br>" + H("Why: a film added twice — a copy in another folder, a file re-added, one matched after the other — is two records in Plex: twice in every list, two posters, and watched on one but not the other. Merging makes them one record with two versions, as Plex offers for a single pair; this finds them all. Only this server’s records: no file is touched, so a mirror isn’t affected — its own duplicates are merged there."),
      stale: function (sc) { return !("mirror" in sc) || sc.rows.some(function (r) { return !r.keep || !r.keep.fields; }); },  // (no mirror said: from before the box before Apply)
      read: function (sc) { return sc.records == null ? null : { n: sc.records, text: P(sc.records, "{n} record read", "{n} records read", { n: fmtNum(sc.records) }) }; },
      units: function (r) { return 1 + (r.others || []).length; },  // (a film's every record: none of them passes)
      empty: H("Every film is on one record."),
      card: function (r) { return mergeCard(r); },
      confirm: function (n) { return P(n, "Merge the records of {n} film in Plex? Their metadata becomes the kept record’s.", "Merge the records of {n} films in Plex? Their metadata becomes the kept record’s.", { n: n }); },
      applies: function (sc) { return mergeApplies(sc); } });
  }
  // Merging is this server's Plex only: no file moves, so the folder sync carries nothing and a mirror keeps its own records.
  // Sync Libraries is how a mirror's records follow — when it can: only from the side whose edits win to the other, and only
  // with Versions grouped differently on. Said for the mirror there is (scan: tools mirrored()) and Sync Libraries' options
  // for this library.
  function mergeApplies(sc) {
    var m = sc.mirror, on = toolEnabled("sync", sc.library), there = "";
    var warn = false;
    if (m && m.how === "watched") {
      if (on.source === "mirror") {
        warn = true;
        there = H("<strong>{name}’s edits win</strong> in Sync Libraries, so it can’t carry a merge from here: the film stays a difference until it’s merged there too. Merge it on {name} instead (its own Merge Duplicates, or Plex), and <a href=\"/sync\" data-nav>Sync Libraries</a> then merges it here.", { name: m.name });
      } else if (on.grouping !== false) {
        there = H("{name}’s Plex keeps its own records. To merge them there too, use <a href=\"/sync\" data-nav>Sync Libraries</a>: scanned again, each merged film shows as one item here and several there, and applying it merges them on {name}.", { name: m.name });
      } else {
        there = H("{name}’s Plex keeps its own records. <a href=\"/sync\" data-nav>Sync Libraries</a> can merge them there too once “Versions grouped differently” is on for this library.", { name: m.name });
      }
    } else if (m) {
      there = H("{name} looks like a mirror of these files, but it isn’t chosen as this library’s mirror: its Plex keeps its own records. Run Merge Duplicates there too, or choose it on the <a href=\"/mirrors\" data-nav>Mirror page</a>.", { name: m.name });
    }
    return { warn: warn,
      does: H("Each ticked film’s records become one, with Plex’s own Merge: the kept record’s metadata stays, and the others’ files become its versions."),
      changes: H("Only this server’s Plex records. No file is touched, so the folder sync carries nothing.") + (there ? " " + there : "") };  // (the backup: the Plex records', from the gate)
  }
  var VERSIONS_HELP = N("One Plex record can hold the same file twice — a copy that slipped in, or a second path to one file (a link). This finds them: versions of one record with the same running time and size. It also finds the copies Plex doesn’t list at all — a second path to a listed file, or an identical copy of one (the same size and bytes at both ends): Plex’s scanner skips a file it already has under another path, so these only show on disk, taking space and getting cached and optimized twice. The copy kept is the one in a folder of its own, then the newest, then the first Plex lists — or the one you choose with the arrow between them; the others are set aside in the library’s own trash folder (on the same dataset: no copying) and Plex asked to scan. They’re deleted for good only once Plex here no longer lists them — and, with a mirror, once its Plex no longer does either (the synced folder carries the change there) — then after the hold below. Until then, each can be put back.<br><br><strong>Run <a href=\"/merge\" data-nav>Merge Duplicates</a> first.</strong> A film on two records isn’t one item holding its file twice yet: merging makes its files versions of one record, and only then can this tell an extra copy for sure. Rows whose record is still to be merged are marked in red. A copy that’s the only file of a Plex record here would take the record with it (its metadata and watched state): it’s marked Record destroyed, in red, and can’t be ticked until Merge Duplicates has made it a version of the record kept. One that’s the only file of a record on the mirror — where the synced folder carries the deletion — is marked Record destroyed there, in grey: it can still be done, and that record goes; merge the records there first to keep it.");
  // Duplicate Versions' rows, on the comparison card: the file kept and the files deleted, as Merge Duplicates shows a
  // film's records. Which is kept is the tool's pick (a folder of its own, then the newest) or the one chosen with the
  // arrow (sent with Apply as keep_key: its path). A file on a Plex record shows the record's poster; one on none (a copy
  // Plex doesn't list), a grey one. Each deleted file says what deleting it does to a Plex record: red when it destroys
  // one here (its record's only file — merge the records first; the row can't be ticked), grey when it destroys one on
  // the mirror (the synced folder carries the deletion there: worth knowing, and it doesn't stop the row).
  function versionsCard(r, sc) {
    var mname = (sc.mirror || {}).name || "";
    var all = [r.keep].concat(r.extras || []), chosen = TOOL.versions.keep[r.id] || r.keep.path;
    var keep = all.filter(function (x) { return x.path === chosen; })[0] || r.keep;
    // what deleting each file does with this one kept: as the scan found it for each choice (ending), else the row's own
    var ending = (r.ending || {})[keep.path], endsHere = function (x) { return ending ? (ending.here || []).indexOf(x.path) >= 0 : !!x.ends_record; };
    var endsThere = function (x) { return ending ? (ending.there || []).indexOf(x.path) >= 0 : !!x.ends_record_there; };
    var linked = function (x) { return x !== keep && (keep.ino ? keep.ino === x.ino : !!x.linked); };
    var gone = all.filter(function (x) { return x !== keep; }), here = gone.filter(endsHere), there = gone.filter(endsThere);
    var side = function (f, k) {
      var kept = f === keep, onRecord = !f.unlisted;
      var checks = [{ id: "path", pair: "file", status: "grey", label: T("File {n}", { n: k + 1 }), lines: ['<span class="mono">' + esc(f.path || f.name) + "</span>"],
        text: kept ? H("This is the existing file: it will be kept, where it is.") : H("This copy will be set aside in the library’s trash folder, and deleted for good after the hold; it can be put back until then.") }];
      if (!kept && endsHere(f)) checks.push({ id: "ends", status: "fail", label: T("Record destroyed"), text: H("This is the only file of its Plex record here: deleting it would remove the record, with its metadata and watched state. Merge the records first, so it’s a version of the record kept.") });
      // the mirror, on every side, paired: Record destroyed there in place of Listed there too, where it would be
      if (!kept && endsThere(f)) checks.push({ id: "there", pair: "mirror", status: "info", label: T("Record destroyed on {name}", { name: mname }), text: H("On {name}, this is the only file of its Plex record: the synced folder carries the deletion there, and the record goes with its metadata and watched state.", { name: mname }) });
      else if (f.there !== undefined && f.there !== null) checks.push(f.there
        ? { id: "there", pair: "mirror", status: "info", label: T("Listed on {name} too", { name: mname }), text: kept ? H("Its Plex lists this file too: it stays there, as here.") : H("Its synced folder carries the change: the copy goes there as well, and is deleted here only once its Plex no longer lists it.") }
        : { id: "there", pair: "mirror", status: "pass", label: T("Not listed on {name}", { name: mname }), text: kept ? H("Its Plex doesn’t list this file.") : H("Its Plex doesn’t list this copy: the deletion needn’t wait for it.") });
      if (!kept && linked(f)) checks.push({ id: "linked", status: "info", label: T("A link to the kept file"), text: H("A second path to the very same file: setting it aside frees nothing, but leaves the file where Plex has it and nowhere else.") });
      if (f.unlisted) checks.push({ id: "unlisted", status: "info", label: T("Not in Plex"), text: H("Plex skips a file it already has under another path: this one is only on disk, taking space and getting cached and optimized twice.") });
      return { kind: kept ? "keep" : "drop", label: kept ? T("This file kept") : T("This file deleted"), poster: onRecord ? r.poster : "", noPoster: T("Not on a Plex record"),
        what: onRecord ? esc(r.title) : '<span class="muted">' + H("No Plex record") + "</span>", lines: [esc(f.folder === "." ? T("the library folder") : f.folder) + " · " + fmtBytes(f.size)], checks: checks };
    };
    var checks = [r.unlisted
      ? { id: "issue", issue: "unlisted", label: T("Copy on disk"), text: H("A file in the library folder Plex doesn’t list, holding the same bytes as one it does: set aside with the record untouched.") }
      : { id: "issue", issue: "versions", label: T("Same file twice"), text: H("The record holds this file as two versions — the same size and running time, the same bytes at both ends: the extra copy set aside, and Plex asked to let go of it.") }];
    if (r.mirror_error) checks.push({ id: "mirror", status: "info", label: T("{name} not read", { name: mname }), text: esc(r.mirror_error) });
    var notes = [];
    if (here.length) notes.push({ status: "fail", cls: "vsblocked", html: H("Record destroyed: deleting it would destroy its Plex record here, whose only file it is. Merge the records first with <a href=\"/merge\" data-nav>Merge Duplicates</a>, so it’s a version of the record kept; then it can be deleted.") });
    if (there.length) notes.push({ status: "info", cls: "vsthere", html: H("Record destroyed on {name}: there it’s the only file of its Plex record, and the synced folder carries the deletion, so that record goes with its metadata and watched state. To keep it, merge the records there first (<a href=\"/merge\" data-nav>Merge Duplicates</a> on {name}, or Sync Libraries).", { name: mname }) });
    return { checks: checks, left: [side(all[0], 0)], right: all.slice(1).map(function (f, k) { return side(f, k + 1); }), notes: notes,
      files: all.map(function (f) { return f.path || f.name; }),
      swap: 'data-act="tool-swap" data-tool="versions" data-id="' + esc(r.id) + '"', swapTitle: T("Keep the other file instead") };
  }
  function trashList() {
    var tr = ST.trash || { items: [] }, items = tr.items || [];
    if (!items.length) return "";
    var state = function (e) {
      var mname = mirrorName(e.library);
      var here = e.here ? badge("pass", T("Gone from Plex")) : badge("info", T("Plex still lists it"));
      var there = e.there === null || e.there === undefined ? "" : " " + (e.there ? badge("pass", T("Gone from {name}", { name: mname })) : badge("info", T("{name} still lists it", { name: mname })));
      var when = e.clear_at ? '<div class="det">' + H("Deleted for good {when}", { when: fmtWhen(e.clear_at + tr.hold_days * 86400) }) + "</div>" : "";
      return here + there + (e.stuck ? " " + badge("warn", T("Held: not deleted after {n} days", { n: 14 })) : "") + when + (e.error ? '<div class="det bad">' + esc(e.error) + "</div>" : "") + (e.note ? '<div class="det muted">' + esc(e.note) + "</div>" : "");
    };
    var act = function (e, a, label, pri) { return '<button class="btn sm' + (pri ? " pri" : "") + '" type="button" data-act="trash" data-trash="' + a + '" data-path="' + esc(e.path) + '">' + label + "</button> "; };
    return '<section class="card">' + heading(esc(T("Set Aside")), N("Files the tools set aside, in the library’s .plexcache-trash folder, waiting until Plex no longer lists them (and the mirror’s Plex, where the library has one), then the hold. A file that never gets there is held and said so on Health: delete it now, put it back, or keep it set aside and stop following it.")) +
      '<p class="help">' + H("Kept {n} days once every Plex has let go of them (the backup length in <a href=\"/system#prefs\" data-nav>System → Preferences</a>).", { n: tr.hold_days }) + "</p>" +
      '<div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">' + H("File") + '</th><th scope="col">' + H("Now") + '</th><th scope="col"></th></tr></thead><tbody>' +
      items.map(function (e) {
        return "<tr><td>" + esc(e.title) + '<div class="det mono">' + esc(e.path.split("/").pop()) + '</div><div class="det">' + esc(msg(e.why)) + " · " + fmtWhen(e.at) + "</div></td><td>" + state(e) + "</td><td>" +
          act(e, "purge", H("Delete Now")) + act(e, "restore", H("Put Back")) + act(e, "keep", H("Keep Set Aside")) + "</td></tr>";
      }).join("") + "</tbody></table></div></section>";
  }
  function pageVersions() {
    return toolPage({ name: "versions", title: "Duplicate Versions",
      help: VERSIONS_HELP + "<br><br>" + H("Why: a second copy of the same file on one record — a copy that slipped in, a file linked twice — takes space, is cached and optimized twice, and shows as two versions that are one. This sets the extra copy aside, and deletes it once every Plex has let go of it; nothing goes for good until then, and it can be put back. With a mirror, the synced folder carries the deletion there, so each copy says whether its Plex lists it."),
      read: function (sc) { return sc.records == null ? null : { n: sc.records, text: P(sc.records, "{n} record read", "{n} records read", { n: fmtNum(sc.records) }) }; },
      extra: function (sc) { return sc.frees ? [statChip("pass", T("{size} freed", { size: fmtBytes(sc.frees) }), T("What deleting every extra copy found frees."))] : []; },
      empty: H("No file is held twice."),
      card: function (r, sc) { return versionsCard(r, sc); },
      confirm: function (n) { return P(n, "Set aside the extra copies of {n} item? Plex is asked to scan; they’re deleted for good after the hold, and can be put back until then.", "Set aside the extra copies of {n} items? Plex is asked to scan; they’re deleted for good after the hold, and can be put back until then.", { n: n }); },
      applies: function (sc) {
        return { does: H("The extra copies of each ticked item are set aside in the library’s trash folder, and Plex asked to scan; each is deleted for good after the hold, and can be put back until then."),
          changes: H("Files in this library’s folders, and this server’s Plex records of them.") + (sc.mirror ? " " + H("{name} is watched: a file goes for good only once its Plex no longer lists it either.", { name: sc.mirror.name }) : "") };
      },
      after: function () { return trashList(); } });
  }
  var FOLDERS_HELP = N("Plex recommends each film in a folder of its own, named <code>Title (Year)</code> — with <code>{edition-…}</code> for an edition, and <code>{tmdb-…}</code> only when two films share a title and year. This moves loose files (in the library folder itself) into such folders, with their subtitles and artwork; with Clean Up, it also renames a film’s folder whose name doesn’t carry the title and year (a folder you named, with both in it, is left as it is).<br><br>Plex loses nothing: each file is first given its new path (a hard link — the same file, nothing copied), Plex asked to scan the new folder, the record it makes for it merged into the original (edits, poster and watched state kept), and only then the old path removed. With a mirror, the synced folder carries the change there and the same merge is done there once its Plex has the new path.");
  var UNLINK_HELP = N("A hard link is one file under two paths; a symlink is a path that points at another. Plex lists only one of two paths to the same bytes, so a linked copy never shows there. Syncthing can’t carry a hard link at all: the other site gets two whole, separate copies, and once either changes the two sites no longer hold the same thing. A symlink is carried as a link, and dangles on the other side when its target isn’t in the synced folder. This tool makes each linked file a file of its own under the same path — the bytes copied beside it and swapped in, the dates kept, so Plex and the folder sync see the same file. Once every site holds plain files, Merge Duplicates and Duplicate Versions can tidy the copies safely.");
  function pageFolders() {
    return toolPage({ name: "folders", title: "Organize Folders", types: ["movie"],
      help: FOLDERS_HELP + "<br><br>" + H("Why: Plex matches a film by its folder as much as by its file name, and a folder of its own is where subtitles, artwork and a second version belong. Films loose in the library folder are matched less reliably and tangle with each other; this gives each its own folder, named as Plex recommends, without Plex losing anything about it."),
      read: function (sc) { return sc.records == null ? null : { n: sc.records, text: P(sc.records, "{n} record read", "{n} records read", { n: fmtNum(sc.records) }) }; },
      empty: H("Every film is in a folder of its own."),
      // the comparison card, its direction fixed: the folder the film is in now, and the one it moves to, its files under
      card: function (r) {
        var media = r.moves.filter(function (m) { return m.media; }).length, side = r.moves.length - media;
        var from = r.kind === "loose" ? T("the library folder") : r.from_folder + "/";
        var tags = [{ id: "media", status: "grey", label: P(media, "{n} file", "{n} files", { n: media }), text: H("The film’s own files: moved with their dates, as hard links first — the same file, nothing copied.") }];
        if (side) tags.push({ id: "side", status: "grey", label: P(side, "{n} sidecar", "{n} sidecars", { n: side }), text: H("Its subtitles and artwork: they move with it.") });
        var issue = r.kind === "loose"
          ? { id: "kind", issue: "loose", label: T("Loose file"), text: H("A film in the library folder itself rather than in a folder of its own: Plex matches it less reliably, and its subtitles and artwork have nowhere to belong. It’s moved into a folder named as Plex recommends; its sidecars (subtitles, artwork) go with it.") }
          : { id: "kind", issue: "rename", label: T("Rename folder"), text: H("The film’s folder isn’t named for its title and year; it’s renamed as Plex recommends, everything in it moving along.") };
        return { checks: [],
          left: [{ kind: "drop", label: T("Original folder"), poster: r.poster, what: esc(r.title), lines: ['<span class="mono">' + esc(from) + "</span>"],
            checks: [issue].concat(tags).concat(r.moves.map(function (m, k) { return { id: "f" + (k + 1), pair: "f" + (k + 1), status: "grey", label: T("File {n}", { n: k + 1 }), lines: ['<span class="mono">' + esc(m.from) + "</span>"],
              text: H("This file is here now: it will be moved to the new folder with its dates — linked there first (the same file, nothing copied), then this path removed once Plex has the new one.") }; })) }],
          right: [{ kind: "keep", label: T("Folder moved here"), poster: r.poster, what: esc(r.title), lines: ['<span class="mono">' + esc(r.folder + "/") + "</span>"],
            checks: r.moves.map(function (m, k) { return { id: "f" + (k + 1), pair: "f" + (k + 1), status: "grey", label: T("File {n}", { n: k + 1 }), lines: ['<span class="mono">' + esc(m.to || r.folder + "/" + m.from.split("/").pop()) + "</span>"],
              text: H("This file will be here after the move: the same file, under its new path, and Plex’s record of it kept (edits, poster and watched state).") }; }) }],
          files: r.moves.map(function (m) { return m.from; }) };
      },
      confirm: function (n) { return P(n, "Move {n} film into its own folder? Each is linked first, scanned into Plex, merged if need be, then the old path removed.", "Move {n} films into their own folders? Each is linked first, scanned into Plex, merged if need be, then the old path removed.", { n: n }); },
      applies: function (sc) {
        return { does: H("Each ticked film is moved into a folder of its own, named as Plex recommends: linked first, scanned into Plex, merged into its record if need be, then the old path removed."),
          changes: H("Files and folders in this library, and this server’s Plex records of them (merged: edits, poster and watched state kept).") + (sc.mirror ? " " + H("{name} is watched: the synced folder carries the move there, and the same merge is made in its Plex once it lists the new folder.", { name: sc.mirror.name }) : "") };
      },
      after: function (t) {
        var pend = (t.extra || {}).pending || [];
        if (!pend.length) return "";
        var mname = mirrorName((pend[0] || {}).library);
        return '<section class="card"><h3>' + H("Waiting for {name}", { name: mname }) + "</h3>" + info(H("Its Plex hasn’t listed the new folder yet; the synced folder brings the files, and the new record there is merged into the old once it has."), T("Waiting for {name}", { name: mname })) +
          pend.map(function (e) { return '<div class="det">' + esc(e.title) + " → " + esc(e.folder) + "/ · " + fmtWhen(e.at) + (e.stuck ? " " + badge("warn", T("Not picked up after {n} days", { n: 3 })) : "") + (e.note ? ' <span class="muted">' + esc(e.note) + "</span>" : "") + "</div>"; }).join("") + "</section>";
      } });
  }
  function pageUnlink() {
    var u = TOOL.unlink;
    return toolPage({ name: "unlink", title: "Unlink Files",
      help: UNLINK_HELP + "<br><br>" + H("Why: a link looks like a second file to you but not to Plex, which lists one path and ignores the other — and not to the folder sync, which turns it into separate copies at the other site that drift apart. Making each a file of its own puts every site on the same footing; then the duplicate tools can remove the extra copies the same way everywhere."),
      read: function (sc) { return sc.files == null ? null : { n: sc.files, text: P(sc.files, "{n} file read", "{n} files read", { n: fmtNum(sc.files) }) }; },
      empty: H("Every file is a file of its own."),
      // the comparison card, its direction fixed, labelled with what Apply does: the file kept (the arrow points at it),
      // and the link — deleted (the remove option: a path Plex doesn't list), or replaced with a file of its own
      card: function (r, sc) {
        var sym = r.kind === "symlink", removes = unlinkRemoves(sc) && !sym && (r.removable || []).length;
        var others = r.others || [], gone = removes ? others.filter(function (o) { return r.removable.indexOf(o.path) >= 0; }) : [];
        var files = [r.path].concat(others.map(function (o) { return o.path; })).concat(sym && r.target ? [r.target] : []), fidx = {};
        files.forEach(function (f, k) { fidx[f] = k + 1; });
        var chip = function (path, text) { return { id: "f" + fidx[path], pair: "file", status: "grey", label: T("File {n}", { n: fidx[path] }), text: text, lines: ['<span class="mono">' + esc(path) + "</span>"] }; };
        var listed = function (o) { return o.listed ? { id: "l" + fidx[o.path], pair: "plex", status: "info", label: T("In Plex"), text: H("Another path to the same file that Plex lists as an item of its own: two records of one file. Made a file of its own, it’s a copy for Merge Duplicates.") }
          : { id: "l" + fidx[o.path], pair: "plex", status: "info", label: T("Not in Plex"), text: H("Another path to the same file that Plex doesn’t list: it looks like a file to you, not to Plex.") }; };
        var side = function (kind, label, path, checks) {
          return { kind: kind, label: label, poster: r.poster, what: esc(r.title), lines: ['<span class="mono">' + esc(String(path).split("/").pop()) + "</span> · " + fmtBytes(r.size)], checks: checks };
        };
        var keep, drop;
        if (removes) {  // the path Plex has (and any other it lists) kept; each path it doesn't list deleted
          keep = [side("keep", T("File kept"), r.path, [chip(r.path, H("This is the existing file, where Plex has it: it will be kept, as it is."))].concat(
            others.filter(function (o) { return gone.indexOf(o) < 0; }).map(function (o) { return chip(o.path, H("Another path to the file that Plex lists: it will be kept, as it is.")); })))];
          drop = gone.map(function (o) { return side("drop", T("This hardlink deleted"), o.path, [chip(o.path, H("This path to the file will be deleted: the file stays, under the path Plex has. Nothing is copied.")), listed(o)]); });
        } else if (sym) {  // the file the symlink points at kept; the symlink replaced with a copy of it
          keep = [side("keep", T("File kept"), r.target || r.path, r.target ? [chip(r.target, H("This is the existing file the symlink points at: it will be kept, as it is."))] : [])];
          drop = [side("drop", T("Symlink replaced with file"), r.path, [chip(r.path, H("This symlink will be replaced with a file of its own: the bytes it points at copied under the same path, the dates kept, so Plex and the folder sync see a file."))])];
        } else {  // the other paths keep the file; this one becomes a file of its own
          keep = [side("keep", T("File kept"), (others[0] || {}).path || r.path, others.map(function (o) { return chip(o.path, H("This is the existing file: it will be kept, as it is, under this path.")); }).concat(others.map(listed)))];
          drop = [side("drop", T("Hardlink replaced with file"), r.path, [chip(r.path, H("This hard link will be replaced with a file of its own: the same bytes copied under the same path, the dates kept, so Plex and the folder sync see the same file."))])];
        }
        return { checks: [sym
            ? { id: "link", issue: "symlink", label: T("Symlink"), text: H("A path that points at another file: Plex and the folder sync see the link, not the file it points at."), lines: ['<span class="mono">' + esc(r.target) + "</span>"] }
            : { id: "link", issue: "hardlink", label: T("Hard link"), text: H("One file under {n} paths. Plex lists one and ignores the others; a folder sync can’t carry the link, and makes whole copies at the other site.", { n: r.links }) }],
          left: keep, right: drop, files: files };
      },
      confirm: function (n) {
        var sc = toolRows("unlink", toolState("unlink")) || {};
        return unlinkRemoves(sc) ? P(n, "Untangle {n} linked file? The paths Plex doesn’t list are removed; a link Plex lists under every path is made a file of its own.", "Untangle {n} linked files? The paths Plex doesn’t list are removed; a link Plex lists under every path is made a file of its own.", { n: n })
          : P(n, "Make {n} linked file a file of its own? Its bytes are copied under the same path; nothing else changes.", "Make {n} linked files files of their own? Their bytes are copied under the same paths; nothing else changes.", { n: n });
      },
      applies: function (sc) {
        // the warning (the way to do it is the choice among the options above), with what Apply does
        var how = { watched: T("the mirror chosen on the Mirror page"), suspected: T("a server of your account whose files are here too — a mirror until you say otherwise on the Mirror page"), peer: T("another Plex Cache sharing these folders") }[sc.mirror && sc.mirror.how];
        var out = sc.mirror
          ? H("<strong>These files look mirrored on {name}</strong>{how}. Syncing files — Syncthing and the like — doesn’t carry a hard link: at the other site each path is a whole copy of its own. So here each link is made a file of its own, which copies the original into a duplicate; the sync then carries the same copies across. Afterwards, make sure your file sync and both Plex servers have caught up with the changes, then run <a href=\"/merge\" data-nav>Merge Duplicates</a> on each side. Removing the extra paths instead would delete the other site’s only copy, and Plex there would lose the item’s metadata.", { name: sc.mirror.name, how: how ? " (" + how + ")" : "" })
          : H("No mirror is known: <strong>removing the extra paths</strong> is the clean way — the file stays where Plex has it and nowhere else. Making each link a file of its own, then merging, is the safer way if these files are mirrored somewhere the app can’t see: a duplicate survives a sync, a removed path doesn’t.");
        return { warn: !!sc.mirror, more: out,
          does: unlinkRemoves(sc) ? H("For each ticked file, the paths Plex doesn’t list are removed; a link whose every path Plex lists is made a file of its own instead.") : H("Each ticked link is made a file of its own: its bytes copied under the same path, the dates kept."),
          changes: H("Linked files in this library’s folders. Plex records are left as they are.") };
      },
      choiceOff: function (id, value, sc) { return id === "remove" && value === "remove" && !!sc.mirror; } });
  }
  function unlinkRemoves(sc) { return toolEnabled("unlink", TOOL.unlink.key).remove === "remove" && !(sc && sc.mirror); }
  // ---- Sync Libraries (tools/sync.py): one item on this server and on its mirror, on the comparison card — the side brought
  // in line as it is now (framed red, greyed) and as it will be (green: the winning side's item, as it's made there), the
  // arrow fixed. Each side is a Plex record as syncSide draws it: the fields that differ a chip each, opening to the value
  // with the other side's beside it. Its files are chips of their own (File n, the same file opening on both sides); the
  // row's checks name each kind of difference — its option's issue — and what's worth knowing before it's applied.
  var SYNC_FIELD = null;  // the one field chip opened: {row, id}
  var SYNC_ISSUE = { missing: "scan", grouping: "grouping", match: "matches", edits: "edits", art: "art", collections: "collections", labels: "collections", editions: "editions" };
  var SYNC_HELP = N("Makes a library on this server and on its mirror the same, one way: from the side whose edits win to the other, through the other’s Plex. A file the other side’s Plex doesn’t list has its folder scanned there; versions that are one item on the winning side and several on the other are merged into one there; then the match, the fields you edited (set and locked as on the winning side — a field edited only on the other side is unlocked and given the winning side’s value), posters and backgrounds chosen by hand, collections and labels, and a film’s edition are set as on the winning side. Watched state and ratings aren’t: Plex’s own sync carries them (the <a href=\"/mirrors\" data-nav>Mirror page</a>).<br><br>Items are paired across the two servers by their files’ place in the library folder, so another title or another match doesn’t lose the pair. Nothing is deleted on either side: a file or an item only on the other side stays. What this can’t bring in line — several items on the winning side that are one on the other, files grouped another way on each side, a file that isn’t on the other site’s disk — is on <a href=\"/mirrorchecks\" data-nav>Mirror Checks</a>.<br><br>Nothing changes until you press Apply. The records it will change are backed up first, per the Backup preference, and Revert on the run puts them back.");
  function syncNames(sc) {  // {src, dst}: the side whose edits win, and the side brought in line
    var n = sc.names || {};
    return sc.source === "mirror" ? { src: n.mirror, dst: n.this } : { src: n.this, dst: n.mirror };
  }
  function syncCard(r, sc) {
    var nm = syncNames({ names: sc.names, source: r.source }), mirrorWins = r.source === "mirror", kinds = r.kinds || [];
    var mid = (((ST.plex2 || {}).pairs || {})[r.library] || {}).server || "";  // (the mirror's posters come through this app from its server)
    var srcs = r.src_items || [], dsts = r.dst_items || [], src = srcs[0] || r.src || {}, dst = dsts[0] || {};
    var act = 'data-act="sync-field" data-id="' + esc(r.id) + '"', openId = SYNC_FIELD && SYNC_FIELD.row === r.id ? SYNC_FIELD.id : null;
    // the files, numbered over both sides (the winning side's first) by name; each side's path is its own server's
    var fidx = {}, order = [], pathOf = {};
    srcs.concat(dsts).forEach(function (x) {
      (x.files || []).forEach(function (f, i) { if (!fidx[f]) { order.push(f); fidx[f] = order.length; pathOf[f] = (x.paths || [])[i] || f; } });
    });
    var filesOf = function (list) { return [].concat.apply([], list.map(function (x) { return x.files || []; })); };
    var srcFiles = filesOf(srcs), dstFiles = filesOf(dsts);
    var base = function (p) { return String(p).split("/").pop(); };
    var scanned = (r.scan_files || []).map(base), unknown = (r.unknown || []).map(base);
    var fileChip = function (x, f, i, text) {
      return { id: "f" + fidx[f], pair: "f" + fidx[f], status: "grey", label: T("File {n}", { n: fidx[f] }), text: text, lines: ['<span class="mono">' + esc((x.paths || [])[i] || f) + "</span>"] };
    };
    var keepFile = function (f) {
      if (r.shape === "split") return H("An item of its own on {name} now: merged there into one item, as one of its versions.", { name: nm.dst });
      if (dstFiles.indexOf(f) >= 0) return H("Listed on {name} too: it stays as it is.", { name: nm.dst });
      if (scanned.indexOf(f) >= 0) return H("Not listed on {name}: its folder is scanned there, and Plex there adds it.", { name: nm.dst }) +
        (unknown.indexOf(f) >= 0 ? " " + H("Whether it’s on disk there can’t be told: no Plex Cache answers at that site.") : "");
      return H("Not on {name}’s disk, so a scan there can’t find it: see <a href=\"/mirrorchecks\" data-nav>Mirror Checks</a>.", { name: nm.dst });
    };
    var dropFile = function (f) {
      if (r.shape === "split") return H("An item of its own on {name} now: merged into one item with the others, as a version.", { name: nm.dst });
      return srcFiles.indexOf(f) >= 0 ? H("Listed on {name} now: it stays.", { name: nm.dst }) : H("Only on {name}: it stays as it is. Nothing is deleted.", { name: nm.dst });
    };
    // the side brought in line as it is now (each of its items, where the winning side's one is several there) — or, not
    // listed there at all, a side saying so; and as it will be: the winning side's item
    var left = dsts.length ? dsts.map(function (x) {
      return { kind: "drop", label: T("{name} now", { name: nm.dst }), body: syncSide(Object.assign({}, x, { files: [] }), src, mirrorWins ? "" : mid, false, kinds, act, openId, null, null, null, null, true),
        checks: (x.files || []).map(function (f, i) { return fileChip(x, f, i, dropFile(f)); }) };
    }) : [{ kind: "drop", label: T("{name} now", { name: nm.dst }), poster: "", noPoster: T("Not listed there"), what: '<span class="muted">' + H("Not listed on {name}", { name: nm.dst }) + "</span>", lines: [], checks: [] }];
    var right = [{ kind: "keep", label: T("{name} after syncing", { name: nm.dst }), body: syncSide(Object.assign({}, src, { files: [] }), dsts[0] || null, mirrorWins ? mid : "", true, kinds, act, openId, null, null, null, null, true),
      checks: (src.files || []).map(function (f, i) { return fileChip(src, f, i, keepFile(f)); }) }];
    // the row's checks: each kind of difference, yellow while its option is on — then what's worth knowing first (grey)
    var both = function (a, b) { return [esc(nm.src) + ": " + a, esc(nm.dst) + ": " + b]; };
    var srcLocked = src.locked || [], dstLocked = dst.locked || [];
    var setNow = srcLocked.filter(function (f) { return dstLocked.indexOf(f) < 0 || fieldValue(src, f) !== fieldValue(dst, f); });
    var undone = dstLocked.filter(function (f) { return srcLocked.indexOf(f) < 0; });
    var checks = [];
    kinds.forEach(function (k) {
      var issue = SYNC_ISSUE[k];
      if (!issue) return;
      if (k === "missing") {
        var nf = (r.scan_files || []).length;
        checks.push({ id: "k-missing", issue: issue, label: P(nf, "{n} file not listed there", "{n} files not listed there", { n: nf }),
          text: H("{name}’s Plex doesn’t list it: the folder is scanned there, and Plex adds what it finds.", { name: nm.dst }), lines: (r.scan || []).map(function (f) { return '<span class="mono">' + esc(f) + "</span>"; }) });
      } else if (k === "grouping") {
        checks.push({ id: "k-grouping", issue: issue, label: T("Versions grouped"), text: H("One item on {src}, {n} items on {dst}: merged into one item there with Plex’s own Merge, every file a version.", { src: nm.src, dst: nm.dst, n: dsts.length }) });
      } else if (k === "match") {
        checks.push({ id: "k-match", issue: issue, label: T("Match"), text: H("Matched to another entry on {dst}, or unmatched there: matched as on {src}.", { src: nm.src, dst: nm.dst }),
          lines: both(src.matched && src.guid ? guidLink(src.guid) : esc(T("Unmatched")), dst.matched && dst.guid ? guidLink(dst.guid) : esc(T("Unmatched"))) });
      } else if (k === "edits") {
        checks.push({ id: "k-edits", issue: issue, label: T("Edited fields"),
          text: (setNow.length ? H("Edited on {src}: set and locked the same on {dst}.", { src: nm.src, dst: nm.dst }) : "") + (setNow.length && undone.length ? " " : "") +
            (undone.length ? H("Edited only on {dst}: unlocked there, and given {src}’s value.", { src: nm.src, dst: nm.dst }) : ""),
          lines: setNow.concat(undone).map(function (f) { return "<strong>" + esc(fieldName(f)) + "</strong> " + esc(fieldValue(src, f) || "—"); }) });
      } else if (k === "art") {
        checks.push({ id: "k-art", issue: issue, label: T("Poster chosen by hand"), text: H("The poster or background chosen by hand on {src} is uploaded to {dst} and chosen there.", { src: nm.src, dst: nm.dst }) });
      } else if (k === "collections" || k === "labels") {
        checks.push({ id: "k-" + k, issue: issue, label: k === "collections" ? T("Collections") : T("Labels"), text: H("Made the same as on {src}.", { src: nm.src }),
          lines: both(esc((src[k] || []).join(", ") || "—"), esc((dst[k] || []).join(", ") || "—")) });
      } else if (k === "editions") {
        checks.push({ id: "k-editions", issue: issue, label: T("Edition"), text: H("Set as on {src}: Plex doesn’t carry editions between servers.", { src: nm.src }), lines: both(esc(src.edition || "—"), esc(dst.edition || "—")) });
      }
    });
    if (r.warn) checks.push({ id: "empty", status: "info", label: T("Would lose metadata"), text: H("{src}’s item is unmatched with no description, while {dst}’s is filled in: check whose edits should win before applying.", { src: nm.src, dst: nm.dst }) });
    if (kinds.indexOf("edits") >= 0 && undone.length) checks.push({ id: "undone", status: "info", label: T("Edits undone there"), text: H("Edited on {dst} only, so unlocked there and given {src}’s value: {fields}.", { src: nm.src, dst: nm.dst, fields: undone.map(fieldName).join(", ") }) });
    var flagged = [].concat.apply([], ((r.tags || {}).src || []).concat((r.tags || {}).dst || [])).filter(function (t) { return t.id !== "locked_empty"; });
    if (flagged.length) checks.push({ id: "flagged", status: "info", label: T("Looks wrong"), text: H("A Plex check flags a record here: its data may be wrong on that side. Check it before applying."), lines: flagged.map(function (t) { return esc(msg(t.text)); }) });
    return { checks: checks, left: left, right: right, files: order.map(function (f) { return pathOf[f]; }) };
  }
  function syncApplies(sc) {
    var nm = syncNames(sc), mirrorWins = sc.source === "mirror";
    return { warn: mirrorWins,
      does: H("Each ticked item on {dst} is brought in line with {src}’s, through its Plex: the folders of files it doesn’t list scanned, versions merged, then the match, edited fields, posters, collections and the edition set as on {src}.", { src: nm.src, dst: nm.dst }),
      changes: (mirrorWins ? H("<strong>This server’s own Plex records</strong>: {src}’s edits win.", { src: nm.src }) : H("Only {dst}’s Plex records.", { dst: nm.dst })) + " " + H("No file is touched, and nothing is deleted on either server.") };
  }
  function pageSync() {
    var sc0 = toolRows("sync", toolState("sync")) || {};
    return toolPage({ name: "sync", title: "Sync Libraries",
      help: SYNC_HELP + "<br><br>" + H("Why: the folder sync carries the files, but each Plex keeps its own records — its matches, your edits, the posters you chose, collections — so the two drift apart. This brings the other side in line, a row at a time, once you’ve seen what changes."),
      stale: function (sc) { return !sc.names || sc.rows.some(function (r) { return !r.kinds || !r.src_items; }); },
      read: function (sc) { return sc.records == null ? null : { n: sc.records, text: P(sc.records, "{n} record read", "{n} records read", { n: fmtNum(sc.records) }) }; },
      extra: function (sc) {
        var nm = syncNames(sc), out = [statChip("grey", T("{n} on {name} too", { n: fmtNum(sc.matched || 0), name: nm.dst }), T("Items found on both servers, by their files’ place in the library folder."))];
        if (sc.disk === false) out.push(statChip("grey", T("{name}’s disk not read", { name: nm.dst }), T("No Plex Cache answers at that site: a file its Plex doesn’t list has its folder scanned there, on the disk or not.")));
        return out;
      },
      empty: H("Nothing to bring in line: the two servers have this library the same."),
      card: function (r, sc) { return syncCard(r, sc); },
      confirm: function (n) { return P(n, "Bring {n} item on {name} in line? Its Plex records are changed to match the other side’s.", "Bring {n} items on {name} in line? Their Plex records are changed to match the other side’s.", { n: n, name: syncNames(sc0).dst }); },
      applies: function (sc) { return syncApplies(sc); } });
  }
  // the two check pages: Library Health's view, one category of checks at a time, over the library chosen (the same tabs)
  function checksPage(name, only, title, help) {
    var libs = ((ST.library || {}).libraries || []).filter(function (l) { return l.kind; });
    if (!LIB.key || !libs.some(function (l) { return String(l.key) === String(LIB.key); })) LIB.key = libs.length ? String(libs[0].key) : null;
    var out = top(T(title), toolBlurb(name), "") + '<div class="stack">' + toolsNav(name) + '<section class="card">' + toolHead(name, T(title), help);
    if (!libs.length) return out + '<p class="muted">' + (librariesPending() ? H("Reading your libraries from Plex…") : H("No libraries yet.")) + "</p></section></div>";
    // the tabs, as every tool's: each with the page's count there (what its checks found, ignored items left out)
    var tabs = libTabs({ tool: name, libs: libs, key: LIB.key, attrs: function (l) { return 'data-act="lib-tab" data-key="' + esc(l.key) + '"'; } });
    return out + libHealthView(only, tabs) + "</section></div>";
  }
  function pageFilechecks() {
    return checksPage("filechecks", "file", "File Checks", H("Each check looks at a file’s tracks as the inspector read them: whether it has audio and subtitles, whether their languages are tagged and agree, what codecs the audio is in and whether FLAC would hold it better or play more widely. Only the items a check flags are listed (every item is on the Library page), each with a chip per check; a chip opens to what was found. Ignore greys an item’s findings out for good. The counts beside the switches leave ignored items out."));
  }
  function pagePlexchecks() {
    return checksPage("plexchecks", "plex", "Plex Checks", H("Each check looks at an item as Plex has it: several files on one item named for different titles or years (a version added to the wrong item), versions whose running times differ by more than a little, a field you locked that holds nothing. The locked-field check reads each item’s own page, which the data layer gathers after the records. Only the items a check flags are listed (every item is on the Library page), each with a chip per check."));
  }
  function pageMirrorchecks() {
    return checksPage("mirrorchecks", "mirror", "Mirror Checks", H("Each library paired with one on its mirror (the Mirror page) is compared file by file, by each file’s place in the library folder. " +
      "<strong>The disks</strong>: through the Plex Cache at the other site, its files against these — on one side only, or a different size. A file changed in the last hour is left out: it may still be syncing. " +
      "<strong>The Plexes</strong>: what the mirror’s Plex lists against what Plex here lists, and the files here that Plex doesn’t list. Scan Folder, under such a finding, asks the Plex that’s missing the file to look at its folder. " +
      "<strong>The folder sync</strong>: whether the markers and claims in the hidden .plexcache folder reach the other site. While they don’t, the two Plex Caches could optimize the same file, so optimizing and the file tools are held in that library until the sync carries them, until you ignore the finding, or while its check is off.<br><br>" +
      "It only reads, every 6 hours and when you press Scan Again. Each finding is a chip on its item — a file Plex doesn’t list, or one only at the other site, is an item of its own. Ignore greys an item’s findings out for good."));
  }

  function pageConvert() {
    var kindOf = {};
    CONVERT_TYPES.forEach(function (t) { kindOf[t[0]] = t; });
    return toolPage({ name: "convert", title: "Convert to MKV", types: ["movie", "show"],
      help: CONVERT_HELP + "<br><br>" + H("Why: MKV is the container the optimizer can lay out for caching — the index and metadata after the video, so the start of a file is all content — and it keeps every stream, chapter and subtitle the old containers hold. A converted file plays as before; it just caches better. Which kinds of file convert is ticked above, per library; a kind unticked still lists its files, grey."),
      read: function (sc) { return sc.files == null ? null : { n: sc.files, text: P(sc.files, "{n} file read", "{n} files read", { n: fmtNum(sc.files) }) }; },
      extra: function (sc) { return sc.waiting ? [statChip("grey", P(sc.waiting, "{n} on its way into Plex", "{n} on their way into Plex", { n: fmtNum(sc.waiting) }), T("Converted: the MKV is next to the original, waiting for Plex to list it."))] : []; },
      empty: H("Every video file is MKV already, or its MKV is on its way into Plex."),
      // the comparison card, its direction fixed: the original, deleted once Plex has the MKV, and the MKV written beside it
      card: function (r) {
        // the MKV's name as it will be written: "<name> MKV-<EXT>.mkv", or the next free one, no two rows the same (r.new_name, from the scan)
        var t = kindOf[r.kind] || [r.kind, (r.kind || "").toUpperCase(), "", [], ""], mkv = r.new_name || r.name.replace(/\.([^./]*)$/, function (m, ext) { return " MKV-" + ext.toUpperCase() + ".mkv"; }), mkvPath = r.path.replace(/[^/]*$/, "") + mkv;
        var checks = [{ id: "kind", issue: r.kind, label: T(t[1]), text: H(t[2]) + (t[3].length ? "<br><strong>" + H("What MKV wouldn’t keep") + "</strong>" : ""), lines: t[3].map(function (x) { return H(x); }).concat(t[4] ? ["<strong>" + H("How converting handles it") + "</strong> " + H(t[4])] : []) }];
        if (r.skipped) checks.push({ id: "skip", status: "info", label: T("Left as it is"), text: H("Converting would lose something, so the file was left as it is last time: {why}. It isn’t tried again while it stays as it is — untick it, or leave it ticked to try once more.", { why: msg(r.skipped) }) });
        return { checks: checks,
          left: [{ kind: "drop", label: T("Original file deleted"), poster: r.poster, what: esc(r.title), lines: ['<span class="mono">' + esc(r.name) + "</span>"],
            checks: [{ id: "file", pair: "file", status: "grey", label: T("File {n}", { n: 1 }), text: H("This is the original file: it will be deleted once Plex has the new MKV as a version of the same item."), lines: ['<span class="mono">' + esc(r.path) + "</span>"] },
              { id: "box", pair: "box", status: "grey", label: T(t[1]), text: H(t[2]) }, { id: "size", status: "grey", label: fmtBytes(r.size), text: H("Its size now: the MKV comes out about the same.") }] }],
          right: [{ kind: "keep", label: T("New MKV created"), poster: r.poster, what: esc(r.title), lines: ['<span class="mono">' + esc(mkv) + "</span>"],
            checks: [{ id: "file", pair: "file", status: "grey", label: T("File {n}", { n: 2 }), text: H("This MKV will be written next to the original, and Plex adds it as a version of the same item: the item keeps its metadata and watched state."), lines: ['<span class="mono">' + esc(mkvPath) + "</span>"] },
              { id: "box", pair: "box", status: "grey", label: "MKV", text: H("Every stream, chapter and subtitle kept, checked against the original, then optimized like any MKV.") }] }],
          files: [r.path, mkvPath] };
      },
      confirm: function (n) { return P(n, "Convert {n} file to MKV? The MKV is written next to it and seen into Plex; the original goes once Plex has the MKV.", "Convert {n} files to MKV? Each MKV is written next to its original and seen into Plex; the originals go once Plex has the MKVs.", { n: n }); },
      applies: function (sc) {
        return { does: H("Each ticked file is converted to MKV, written next to it and seen into Plex; the original goes once Plex has the MKV."),
          changes: H("Files in this library’s folders, and this server’s Plex records of them.") + (sc.mirror ? " " + H("{name} is watched: an original goes only once its Plex has the MKV too.", { name: sc.mirror.name }) : "") };
      },
      after: function (t) { return convertWaiting(t); } });
  }
  function pageDataset() {
    return top(T("Convert Folder to Child Dataset"), toolBlurb("dataset"), "") +
      '<div class="stack">' + toolsNav("dataset") + migrationCard() + "</div>";
  }
  var ALIAS = { features: "caching", settings: "system", mirror: "mirrorchecks" };  // pages before 0.15 (and Mirror Checks, once Mirror Health at /mirror)

  // The side menu folds down to its icons (remembered in this browser); folded, each item's
  // name shows on hover instead.
  function setSideCollapsed(on) {
    var shell = document.querySelector(".shell"), btn = document.getElementById("sidetoggle");
    shell.classList.toggle("collapsed", on);
    btn.setAttribute("aria-expanded", String(!on));
    var label = on ? T("Expand menu") : T("Collapse menu");
    btn.title = label;
    btn.querySelector(".lbl").textContent = label;
    document.querySelectorAll(".side .nav").forEach(function (a) {
      if (on) a.title = a.querySelector(".lbl").textContent; else a.removeAttribute("title");
    });
  }
  document.getElementById("version").addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-act=updates-apply]");
    if (!b) return;
    b.disabled = true;
    api("/api/updates/apply", {}).then(function () { UPDATING = true; render(); })
      .catch(function (err) { toast(err.message); b.disabled = false; });
  });
  document.getElementById("sidetoggle").addEventListener("click", function () {
    var on = !document.querySelector(".shell").classList.contains("collapsed");
    setSideCollapsed(on);
    try { localStorage.setItem("pc-side", on ? "collapsed" : "open"); } catch (e) { /* storage unavailable */ }
  });
  try {
    if (localStorage.getItem("pc-side") === "collapsed") {
      setSideCollapsed(true);
      PCI18N.ready.then(function () { setSideCollapsed(true); });  // again once the names are translated
    }
  } catch (e) { /* storage unavailable */ }

  // A tall poster's width from its height (2:3): browsers don't take it from a height that's stretched
  function fitPosters() {
    document.querySelectorAll(".posterbig").forEach(function (el) { el.style.width = Math.round(el.offsetHeight * 2 / 3) + "px"; });
  }
  window.addEventListener("resize", fitPosters);
  // The count beside Tools in the menu: what the tools have found to do, over every page (the state's "tools" count, as
  // countChip draws one) — a turning wheel while any page's is still to come (beside what's been found so far, coloured as
  // countChip colours it: partialTone), a red ! where one couldn't be worked out, red
  // while something red on a tool's page stops part of it (shown even at 0: bad), nothing at 0
  function menuCount(el, c, bad) {
    if (!el) return;
    var busy = c.s === "pending", html = busy ? (c.partial ? fmtNum(c.partial) : "") + '<span class="spin" aria-hidden="true"></span>' : c.s === "failed" ? "!" : c.s === "done" && (c.n || bad) ? fmtNum(c.n) : "";
    if (el.dataset.html !== html) { el.dataset.html = html; el.innerHTML = html; }  // (the wheel not started again with every state)
    el.hidden = !html;
    var tone = partialTone(c, bad);
    el.classList.toggle("busy", busy);
    el.classList.toggle("warn", tone === "warn");
    el.classList.toggle("fail", tone === "fail" || (!busy && (bad || c.s === "failed")));
    var why = busy || c.s === "failed" ? countWhy(c) : "";
    if (why) { el.setAttribute("title", why); el.setAttribute("aria-label", why); } else { el.removeAttribute("title"); el.removeAttribute("aria-label"); }
  }
  // a redraw that leaves the page where it is: what opens or closes under a click doesn't move what's on the screen
  function renderStill() { var y = window.scrollY; render(); window.scrollTo(window.scrollX, y); }
  function render() {
    if (!ST) return;
    document.querySelectorAll(".nav").forEach(function (a) {
      var on = a.dataset.page === page || (a.dataset.page === "tools" && !!TOOL_PAGES[page]);
      if (a.classList.contains("on") !== on) a.classList.toggle("on", on);
      if (on && a.getAttribute("aria-current") !== "page") a.setAttribute("aria-current", "page");
      else if (!on && a.hasAttribute("aria-current")) a.removeAttribute("aria-current");
    });
    // the tools, under Tools in the menu while one of their pages is open
    var sub = document.getElementById("toolsub");
    if (sub) {
      var onTools = page === "tools" || !!TOOL_PAGES[page];
      if (sub.hidden !== !onTools) sub.hidden = !onTools;
      if (onTools && sub.dataset.for !== page) {
        sub.dataset.for = page;
        sub.innerHTML = TOOLS.map(function (t) { return '<a class="subnav' + (page === t[0] ? " on" : "") + '" href="/' + t[0] + '" data-nav' + (page === t[0] ? ' aria-current="page"' : "") + ">" + esc(T(t[3] || t[1])) + "</a>"; }).join("");
      }
    }
    var dot = document.getElementById("dot-health");
    var c = ST.health ? ST.health.counts : null;
    dot.className = "dot " + (!c ? "" : c.fail ? "fail" : c.warn ? "warn" : "ok");
    menuCount(document.getElementById("count-tools"), countOf("tools"), TOOLS.some(function (t) { return toolBad(t[0]); }));
    var ver = document.getElementById("version"), up = ST.updates || {};
    // the stages: updating (asked of TrueNAS, until this page comes back on the new build), checking (just started, GitHub not
    // asked yet), an update to have (a button when TrueNAS can do it, else a link), or nothing to say
    var stage = updateStage(up);
    ver.innerHTML = '<a class="verlink" href="' + esc(up.url || up.repo || "#") + '" target="_blank" rel="noopener" title="' + H("This build on GitHub") + '">v' + esc(ST.version) + "</a>" +
      (stage === "updating" ? '<span class="update quiet">' + H("Updating…") + "</span>"
        : stage === "checking" ? '<span class="update quiet">' + H("checking…") + "</span>"
        : stage === "button" ? '<button type="button" class="update" data-act="updates-apply">' + H("Update now") + "</button>"
        : stage === "needkey" ? '<a class="update" href="/system#truenas" data-nav>' + H("Update available · add a TrueNAS API key to install it") + "</a>"
        : stage === "link" ? '<a class="update" href="' + esc(up.repo) + (up.channel === "dev" ? "/commits/dev" : "/releases") + '" target="_blank" rel="noopener">' +
          (up.latest && up.latest !== ST.version.replace(/-dev$/, "") ? H("v{version} available ↗", { version: up.latest }) : H("newer dev build ↗")) + "</a>" : "");
    var bar = document.getElementById("userbar");
    if (!bar) {
      // Top right: the language menu and the sign-out button.
      bar = document.createElement("div");
      bar.id = "userbar";
      bar.className = "userbar";
      bar.appendChild(PCI18N.picker());
      if (ST.auth && ST.auth.enabled) {
        var who = document.createElement("span");
        // The user name only shows on hover, so the bar stays short.
        who.innerHTML = '<button type="button" class="signout" title="' + H("Sign out {user}", { user: ST.auth.user }) + '" aria-label="' + H("Sign out {user}", { user: ST.auth.user }) + '">' +
          '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg></button>';
        who.className = "whobox";
        who.querySelector("button").addEventListener("click", function () { api("/logout", {}).then(function () { location.reload(); }); });
        bar.appendChild(who);
      }
      document.querySelector(".shell").appendChild(bar);
    }
    main.innerHTML = PAGES[page]();
    fitBars();
    fitPosters();
    reattachInfo();
    stickHeights();
  }
  // Library Health's legend stays at the top of the window, and the table's column names just under it
  function stickHeights() {
    var ls = main.querySelector(".lstick");
    if (ls) main.style.setProperty("--lstick", ls.offsetHeight + "px");
  }
  window.addEventListener("resize", stickHeights);

  // Scroll to a part of a page — once it's there: a page whose data is still loading draws it a little later
  // @testable: recordId — the anchor of a Plex record on any page that lists records (Plex Checks, File Checks, the Library's
  // Health view): rec-<library>-<rating key>. recordLink(page, library, key) is the link to it — /plexchecks#rec-1-190: the
  // page opens on that library, on the page of its list that holds the record, scrolled to it and marked a moment (REVEAL)
  function recordId(lib, rk) { return "rec-" + String(lib).replace(/[^\w]/g, "_") + "-" + String(rk).replace(/[^\w]/g, "_"); }
  function recordLink(pageName, lib, rk) { return "/" + pageName + "#" + recordId(lib, rk); }
  // @end
  var REVEAL = null;  // the record a link asked for: {lib, rk, id}, until it's been scrolled to
  function reveal(hash) {
    var m = /^rec-(\w+?)-(\w+)$/.exec(hash || "");
    if (!m) return false;
    REVEAL = { lib: m[1], rk: m[2], id: hash };
    LIB.key = m[1]; LIB.healthOffset = 0;
    return true;
  }
  function scrollToSoon(id, tries) {
    var target = document.getElementById(id);
    if (target) { if (REVEAL && REVEAL.id === id) setTimeout(function () { REVEAL = null; }, 2500); return target.scrollIntoView({ block: "start" }); }
    if ((tries || 0) < 30) setTimeout(function () { scrollToSoon(id, (tries || 0) + 1); }, 100);
  }
  // A click under way (the button pressed, not yet released — and a moment after, while its click lands): redrawing
  // then would swap the element out from under the pointer and lose the click, so the refresh waits
  var POINTER_AT = 0;
  document.addEventListener("pointerdown", function () { POINTER_AT = Date.now() + 1e9; }, true);
  document.addEventListener("pointerup", function () { POINTER_AT = Date.now(); }, true);
  document.addEventListener("pointercancel", function () { POINTER_AT = Date.now(); }, true);
  function interacting() { return Date.now() - POINTER_AT < 400; }
  // Text selected on the page (to copy): redrawing would lose it, so the refresh waits until it's gone
  function selecting() {
    var sel = window.getSelection && window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
    var node = sel.getRangeAt(0).commonAncestorContainer;
    return main.contains(node);
  }
  function editing() {
    if (main.querySelector(".pick.open")) return true;  // a dropdown is open
    var a = document.activeElement;
    // Typing or picking in a field holds re-drawing off; a ticked box doesn't.
    return a && main.contains(a) && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.type !== "checkbox" && a.type !== "radio";
  }

  var build = null;  // the server's fingerprint of these files (server.py BUILD)
  // the page open, and the library open on it, told with every state the page asks for: the server works out what they
  // show first (evaluate.py: Evaluator.focus) — and the inspector reads that library's files first
  function focusQuery() {
    if (!TOOL_PAGES[page] && !HEALTH_PAGES[page]) return "";
    var key = TOOL[page] ? TOOL[page].key : HEALTH_PAGES[page] ? LIB.key : "";
    return "?focus=" + encodeURIComponent(page + ":" + (key || ""));
  }
  function load() {
    return api("/api/state" + focusQuery()).then(function (st) {
      // A new version of the web UI (after an update or tnsync): reload so this tab runs it.
      // Unsaved changes are sent first; the next refresh reloads.
      if (build && st.build && st.build !== build && !Object.keys(pending).length) { location.reload(); return; }
      build = build || st.build;
      ST = st;
      retriesChanged(st);
      if (!Object.keys(pending).length) S = st.settings;
      if (!editing() && !selecting() && !interacting()) render();
      lookIfDue();
    }).catch(function () {
      if (!ST) main.innerHTML = '<p class="muted">' + H("Can’t reach the Plex Cache service. Retrying…") + "</p>";
      else if (UPDATING || (ST.updates || {}).updating) render();  // (away while it restarts: still "Updating…")
    });
  }

  function go(p, push) {
    p = ALIAS[p] || p;
    page = PAGES[p] ? p : "health";
    if (page === "ssd") EST = {};  // the what-if sliders start from your settings every time
    if (push) history.pushState({}, "", "/" + page);
    else if (location.pathname !== "/" + page && location.pathname !== "/") history.replaceState({}, "", "/" + page + location.hash);
    document.title = "Plex Cache – " + ({ health: T("Health"), caching: T("Caching"), ssd: T("SSD Cache"), library: T("Library"), tools: T("Tools"), processes: T("Processes"), mirrors: T("Mirror"), activity: T("Metrics"), system: T("System") }[page] ||
      (TOOLS.filter(function (t) { return t[0] === page; })[0] || [0, page])[1]);
    render();
    window.scrollTo(0, 0);
    main.focus({ preventScroll: true });
    lookIfDue();
  }
  // The Mirror page open: a look for mirrors by itself (sync.py discover, which also compares each mirror chosen) — when
  // there's never been one, a library the last one didn't score, or an hour since; at most every 10 minutes from this tab,
  // and only while looking by itself is on (sync.discover) and there's a sign-in to list the servers from.
  var LOOKED = 0;
  function lookIfDue() {
    if (!ST || page !== "mirrors") return;
    var sy = ST.sync || {}, p2 = ST.plex2 || {}, libs = ((ST.library || {}).libraries || []).filter(function (l) { return l.kind; });
    if (sy.running || !p2.signed_in || !(S.sync || {}).discover || Date.now() - LOOKED < 600000) return;
    var compared = sy.compared || [], now = Date.now() / 1000;
    if (sy.discovered_at && now - sy.discovered_at < 3600 && !libs.some(function (l) { return compared.indexOf(String(l.key)) < 0; })) return;
    LOOKED = Date.now();
    try { api("/api/sync/discover", {}).then(function () { load(); }).catch(function () {}); } catch (e) { /* (the page drawn offline: nothing to ask) */ }
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
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") hideInfo();
    // Tiles that open a detailed view work from the keyboard too
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches("[data-goto]")) { e.preventDefault(); e.target.click(); }
  });
  window.addEventListener("resize", function () { if (popFor) reattachInfo(); });

  document.addEventListener("click", function (e) {
    // ↺ on the Optimizer page and the second server's pairs: back to the default
    var orv = e.target.closest("[data-optlib-revert], [data-p2lib-revert], [data-peerurl-revert], [data-lhcheck-revert], [data-lhlang-revert], [data-lhquality-revert]");
    if (orv) {
      e.preventDefault();
      if (orv.dataset.lhlangRevert !== undefined) {
        queueSave("library_health", "subtitle_language", (ST.library_health_defaults || {}).subtitle_language || "en", true);
        LIB.health = {};
      } else if (orv.dataset.lhqualityRevert !== undefined) {
        queueSave("library_health", "audio_quality", (ST.library_health_defaults || {}).audio_quality || "poor", true);
        LIB.health = {};
      } else if (orv.dataset.optlibRevert !== undefined) {
        var libs = JSON.parse(JSON.stringify((S.optimizer || {}).libraries || {}));
        var lib = libs[orv.dataset.optlibRevert] = optChoices(orv.dataset.optlibRevert);
        lib[orv.dataset.field] = ((ST.optimizer_defaults || {}).library || {})[orv.dataset.field];
        queueSave("optimizer", "libraries", libs, true);
      } else if (orv.dataset.lhcheckRevert !== undefined) {
        var lh = JSON.parse(JSON.stringify((S.library_health || {}).checks || {}));
        delete lh[orv.dataset.lhcheckRevert];
        queueSave("library_health", "checks", lh, true);
      } else if (orv.dataset.peerurlRevert !== undefined) {  // (the other Plex Cache's address: found by itself again)
        var svs = JSON.parse(JSON.stringify((S.peer || {}).servers || {}));
        delete svs[orv.dataset.peerurlRevert];
        queueSave("peer", "servers", svs, true);
      } else {
        mirrorChoose(orv.dataset.p2libRevert, "unset");  // (the Mirror page's row: back to not decided)
      }
      render();
      return;
    }
    var sr = e.target.closest("[data-libsec-revert]");
    if (sr) {
      e.preventDefault();
      saveLibSeconds(sr.dataset.libsecRevert, null);
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
    var tile = e.target.closest("[data-goto]");  // (not "go": that's the page switcher, used below)
    if (tile && !e.target.closest(".info")) {
      var where = tile.dataset.goto.split(":");
      if (where[0] === "lasts" && where[1]) {
        lastsView = where[1];
        try { localStorage.setItem("pc-lasts-view", lastsView); } catch (err) { /* storage unavailable */ }
        render();
      }
      var el = document.getElementById(where[0]);
      if (el) el.scrollIntoView({ block: "start" });  // (a smooth scroll would be cut short by the next refresh)
      return;
    }
    var cv = e.target.closest("[data-cov-lib], [data-cov-mode], [data-cov-by]");
    if (cv) {
      if (cv.dataset.covLib) covLib = cv.dataset.covLib;
      else if (cv.dataset.covMode) covMode = cv.dataset.covMode;
      else covBy = Number(cv.dataset.covBy);
      try { localStorage.setItem("pc-cov-mode", covMode); localStorage.setItem("pc-cov-by", String(covBy)); } catch (err) { /* storage unavailable */ }
      covHover = null;
      render();
      return;
    }
    var sl = e.target.closest("[data-skips-lib]");
    if (sl) { skipsLib = sl.dataset.skipsLib; render(); return; }
    var lr = e.target.closest("[data-lasts-range]");
    if (lr) {
      lastsRange = lr.dataset.lastsRange;
      try { localStorage.setItem("pc-lasts-range", lastsRange); } catch (err) { /* storage unavailable */ }
      render();
      return;
    }
    var lf = e.target.closest("[data-lasts-first]");
    if (lf) {
      lastsFirst = !lastsFirst;
      try { localStorage.setItem("pc-lasts-first", lastsFirst ? "show" : "hide"); } catch (err) { /* storage unavailable */ }
      lastsFreshUpdate(null);
      return;
    }
    var lb = e.target.closest("[data-lasts-view], [data-lasts-tier]");
    if (lb) {
      if (lb.dataset.lastsView) lastsView = lb.dataset.lastsView; else lastsTier = lb.dataset.lastsTier;
      try { localStorage.setItem("pc-lasts-view", lastsView); localStorage.setItem("pc-lasts-tier", lastsTier); } catch (err) { /* storage unavailable */ }
      render();
      return;
    }
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
      var toRecord = reveal(parts[1]);
      go(parts[0].replace("/", ""), true);
      if (parts[1]) { if (toRecord) history.replaceState({}, "", location.pathname + "#" + parts[1]); scrollToSoon(parts[1], toRecord ? -70 : 0); }  // (a record: up to 10 s, for its list to come in)
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
    if (act === "recheck") { api("/api/checks", { rescan: true }).then(function () { toast(T("Checking…")); ST.checking = true; render(); }); }
    else if (act === "optimize-sel") {
      // the files ticked in one library — every one of them (All): the whole library, its conflict copies too
      var ok = el.dataset.key, ou = TOOL.optimizer, opaths = (optState(ok).data || {}).paths || [];
      var picked = opaths.filter(function (p) { return ou.sel[p]; });
      if (!picked.length) return;
      // ZFS settings not right yet: ask first (each file would be written now and again after the change)
      var zs = ((((ST.optimizer || {}).libraries || {})[ok] || {}).zfs || {}).status;
      if (zs === "warn" && optChoices(ok).sync && !el.dataset.confirmed) { OPT_CONFIRM = ok; render(); return; }
      OPT_CONFIRM = null;
      el.disabled = true;
      api("/api/optimize/library", picked.length === opaths.length ? { library: ok } : { library: ok, files: picked }).then(function (r) {
        picked.forEach(function (p) { delete ou.sel[p]; });
        toast(r.started ? T("Optimizing…") : r.queued ? T("Added to the queue") : T("Already queued, or the optimizer isn’t set up"));
        optState(ok).at = 0; load();
      }).catch(function (e) { toast(e.message, true); render(); });
    }
    else if (act === "opt-all" || act === "opt-page") {  // every file of the library to optimize, on every page — or the page open
      var ak = el.dataset.key, ad = optState(ak).data || {}, asel = TOOL.optimizer.sel;
      (act === "opt-all" ? ad.paths || [] : (ad.files || []).map(function (f) { return f.path; })).forEach(function (p) { if (el.checked) asel[p] = true; else delete asel[p]; });
      OPT_CONFIRM = null; render();
    }
    else if (act === "optimizer-survey") { el.disabled = true; api("/api/optimizer/survey", {}).then(function () { ST.optimizer.surveying = true; render(); setTimeout(load, 1500); }).catch(function (e) { toast(e.message, true); }); }
    else if (act === "opt-retry") { var ro = optState(el.dataset.key); ro.error = null; ro.asked = null; render(); }
    else if (act === "opt-page-go") { optState(el.dataset.key).offset = Number(el.dataset.offset) || 0; render(); }
    else if (act === "optimize-cancel") { OPT_CONFIRM = null; render(); }
    else if (act === "optimizer-check") {
      el.disabled = true;
      api("/api/optimizer/check", {}).then(function () { load(); }).catch(function (e) { toast(e.message, true); load(); });
    }
    else if (act === "updates-apply") {
      el.disabled = true;
      api("/api/updates/apply", {}).then(function () { UPDATING = true; render(); })
        .catch(function (e) { toast(e.message); el.disabled = false; });
    }
    else if (act === "updates-check") { el.disabled = true; api("/api/updates/check", {}).then(function () { setTimeout(load, 1500); }).catch(function (e) { toast(e.message); }); }
    else if (act === "tool-lib") { OPT_CONFIRM = null; TOOL[el.dataset.tool].key = el.dataset.key; TOOL[el.dataset.tool].sel = {}; TOOL[el.dataset.tool].confirm = false; TOOL[el.dataset.tool].offset = 0; render(); load(); }  // (load: the library opened is worked out first)
    else if (act === "tool-swap") {  // the comparison card's arrow: the next side of the row kept instead (round and round)
      var wt = el.dataset.tool, wrow = (((toolState(wt).scan || {}).rows) || []).filter(function (r) { return r.id === el.dataset.id; })[0];
      if (wrow) {
        var keys = SWAP_KEYS[wt](wrow), cur = TOOL[wt].keep[el.dataset.id] || keys[0];
        TOOL[wt].keep[el.dataset.id] = keys[(keys.indexOf(cur) + 1) % keys.length];
        TOOL[wt].confirm = false;
        render();
      }
    }
    else if (act === "merge-field") { MERGE_FIELD = MERGE_FIELD && MERGE_FIELD.row === el.dataset.id && MERGE_FIELD.id === el.dataset.f ? null : { row: el.dataset.id, id: el.dataset.f }; delete TOOL_OPEN["merge:" + el.dataset.id]; render(); }
    else if (act === "sync-field") { SYNC_FIELD = SYNC_FIELD && SYNC_FIELD.row === el.dataset.id && SYNC_FIELD.id === el.dataset.f ? null : { row: el.dataset.id, id: el.dataset.f }; delete TOOL_OPEN["sync:" + el.dataset.id]; render(); }
    else if (act === "tool-switch" || act === "tool-switch-revert") {
      // a tool's option for this library: saved under tools.<tool>.<library>, and the page redrawn at once from the rows it has
      var sname = el.dataset.tool, skey = el.dataset.key, sopt = el.dataset.opt, st2 = toolState(sname);
      var decl = (st2.options || []).filter(function (o) { return o.id === sopt; })[0] || {};
      var sval = act === "tool-switch-revert" ? decl.default : el.tagName === "SELECT" ? el.value : el.checked;
      var all = JSON.parse(JSON.stringify((S.tools || {})[sname] || {}));
      all[skey] = Object.assign({}, all[skey] || {});
      if (act === "tool-switch-revert") delete all[skey][sopt]; else all[skey][sopt] = sval;
      if (!Object.keys(all[skey]).length) delete all[skey];
      queueSave("tools", sname, all, true);
      st2.enabled = st2.enabled || {}; st2.enabled[skey] = Object.assign({}, st2.enabled[skey] || {}); st2.enabled[skey][sopt] = sval;
      TOOL[sname].sel = {}; TOOL[sname].confirm = false;
      renderStill();
    }
    else if (act === "tool-sel") { TOOL[el.dataset.tool].sel[el.dataset.id] = el.checked; TOOL[el.dataset.tool].confirm = false; render(); }
    else if (act === "lh-retry") { delete LIB.health[LIB.key]; render(); }
    else if (act === "lh-scan") {  // Scan Again on a check page whose findings are worked out in the background (Mirror Checks)
      el.disabled = true;
      api("/api/library/health/scan", { library: el.dataset.key, category: el.dataset.cat }).then(function (r) { if (!r.started) toast(T("Already running")); load(); })
        .catch(function (e) { toast(e.message, true); load(); });
    }
    else if (act === "lh-action") {  // a finding's action, under its chip (Mirror Checks' Scan Folder)
      el.disabled = true;
      api("/api/library/health/action", { library: el.dataset.key, item: el.dataset.item, check: el.dataset.check }).then(function (r) { toast(msg(r.said)); load(); })
        .catch(function (e) { toast(e.message, true); render(); });
    }
    else if (act === "lh-show") {  // a tool's chips on the Library page shown or hidden: the view only, kept as the tools' switches are
      var shown = JSON.parse(JSON.stringify((S.library_health || {}).checks || {}));
      shown[el.dataset.id] = el.dataset.on === "1";
      queueSave("library_health", "checks", shown, true);
      render();
    }
    else if (act === "tool-page") { TOOL[el.dataset.tool].offset = Number(el.dataset.offset) || 0; render(); }
    else if (act === "tool-pagesel") {  // the rows on the page open that can be ticked
      var pu = TOOL[el.dataset.tool];
      (pu.pageCan || []).forEach(function (id) { if (el.checked) pu.sel[id] = true; else delete pu.sel[id]; });
      pu.confirm = false; render();
    }
    else if (act === "tool-all") {
      var tu = TOOL[el.dataset.tool];  // (the rows that can be ticked, as last drawn: toolCards)
      tu.sel = {}; if (el.checked) (tu.can || []).forEach(function (id) { tu.sel[id] = true; });
      tu.confirm = false; render();
    }
    else if (act === "tool-scan") {
      el.disabled = true;
      var su = TOOL[el.dataset.tool];
      su.sel = {}; su.confirm = false; (su.tried = su.tried || {})[su.key] = true; su.scanAt = Date.now() / 1000;
      render();
      api("/api/tools/" + el.dataset.tool, { action: "scan", library: su.key, refresh: true }).then(function () { load(); }).catch(function (e) { su.scanAt = 0; toast(e.message, true); load(); });
    }
    else if (act === "tool-apply") { TOOL[el.dataset.tool].confirm = true; render(); }
    else if (act === "tool-backup") {  // a snapshot taken, a backup you say you've made, or the last error dismissed
      var how = el.dataset.how;
      el.disabled = true;
      if (how === "snapshot") el.textContent = T("Taking the snapshot…");
      api("/api/tools/snapshot", { library: el.dataset.key, action: how === "snapshot" ? "take" : how === "dismiss" ? "dismiss" : "ack", how: how, tool: el.dataset.tool })
        .then(function () { READY = {}; render(); }).catch(function (e) { READY = {}; toast(e.message, true); render(); });
    }
    else if (act === "job-revert") {  // a tool's run undone from its backup: the files it changed put back
      el.disabled = true; el.textContent = T("Reverting…");
      api("/api/job", { action: "revert", job: el.dataset.job }).then(function (r) { toast(T("Reverted: {ok} put back, {failed} not", { ok: r.reverted, failed: r.failed })); PROC = null; load(); })
        .catch(function (e) { toast(e.message, true); PROC = null; load(); });
    }
    else if (act === "plex-revert") {  // Plex's records put back from a backup of them (plexbackup.py)
      el.disabled = true; el.textContent = T("Reverting…");
      api("/api/job", { action: "plex_revert", id: el.dataset.id }).then(function (r) { toast(T("Put back: {ok} records, {failed} not", { ok: r.restored, failed: r.failed })); PROC = null; load(); })
        .catch(function (e) { toast(e.message, true); PROC = null; load(); });
    }
    else if (act === "job-rollback-ask") { ROLLBACK_CONFIRM = el.dataset.job; render(); }
    else if (act === "job-rollback-no") { ROLLBACK_CONFIRM = null; render(); }
    else if (act === "job-rollback") {  // the whole dataset rolled back to the run's snapshot
      el.disabled = true; el.textContent = T("Rolling back…"); ROLLBACK_CONFIRM = null;
      api("/api/job", { action: "rollback", job: el.dataset.job }).then(function (r) { toast(T("Rolled back to {names}", { names: (r.rolled_back || []).join(", ") })); PROC = null; load(); })
        .catch(function (e) { toast(e.message, true); PROC = null; load(); });
    }
    else if (act === "tool-chip") {  // the one chip open on its card (and its pair on the other sides): another closes it
      var ck = el.dataset.tool + ":" + el.dataset.card, cur = TOOL_OPEN[ck];
      TOOL_OPEN[ck] = cur && cur.id === el.dataset.id ? null : { id: el.dataset.id, pair: el.dataset.pair || "", group: el.dataset.group };
      if (MERGE_FIELD && MERGE_FIELD.row === el.dataset.card) MERGE_FIELD = null;
      if (SYNC_FIELD && SYNC_FIELD.row === el.dataset.card) SYNC_FIELD = null;
      renderStill();
    }
    else if (act === "tool-history") { var th = TOOL[el.dataset.tool], open = th.history !== undefined ? th.history : true; th.history = !open; renderStill(); }
    else if (act === "tool-apply-no") { TOOL[el.dataset.tool].confirm = false; render(); }
    else if (act === "tool-apply-yes") {
      el.disabled = true;
      var au = TOOL[el.dataset.tool], ids = Object.keys(au.sel).filter(function (k) { return au.sel[k]; }), choices = {};
      ids.forEach(function (id) { if (au.keep && au.keep[id]) choices[id] = { keep_key: au.keep[id] }; });
      au.sel = {}; au.confirm = false; au.history = true; au.openLatest = true;
      api("/api/tools/" + el.dataset.tool, { action: "apply", ids: ids, choices: choices }).then(function () { PROC = null; load(); }).catch(function (e) { toast(e.message, true); load(); });
    }
    else if (act === "trash") {
      // a file a tool set aside: deleted now, put back, or kept set aside (trash.py)
      el.disabled = true;
      api("/api/trash", { path: el.dataset.path, action: el.dataset.trash }).then(function () { load(); }).catch(function (e) { toast(e.message, true); load(); });
    }
    else if (act === "conv") {
      // a converted file's original: delete now, keep both, or ask Plex again (converting.py)
      el.disabled = true;
      api("/api/conversions", { original: el.dataset.orig, action: el.dataset.conv }).then(function () { load(); })
        .catch(function (e) { toast(e.message, true); load(); });
    }
    else if (act === "optimize-covers") {
      api("/api/optimize/library", { library: el.dataset.key, mode: "covers" }).then(function (r) {
        toast(r.started ? T("Syncing cover art…") : r.queued ? T("Added to the queue") : T("Already queued, or the optimizer isn’t set up")); load();
      });
    }
    else if (act === "mig") {
      // the folder-to-dataset steps (migrating.py)
      var m = el.dataset.mig;
      if (m === "ask_delete") { MIG_CONFIRM_DELETE = true; render(); return; }
      MIG_CONFIRM_DELETE = false;
      el.disabled = true;
      api("/api/migration", { action: m, library: el.dataset.lib, folder: el.dataset.folder, scope: el.dataset.scope })
        .then(function () { load(); }).catch(function (e) { toast(e.message, true); load(); });
    }
    else if (act === "queue-remove") {
      el.disabled = true;
      api("/api/job", { action: "remove", id: el.dataset.id }).then(function () { toast(T("Removed from the queue")); load(); procFetch(true); });
    }
    else if (act === "mig-nodelete") { MIG_CONFIRM_DELETE = false; render(); }
    else if (act === "proc-open") {
      var pj = el.dataset.job;
      procOpen = procOpen === pj ? null : pj;
      if (procOpen) procFilesFetch(pj, 0);
      renderStill();
      if (!procOpen) { var pr = document.getElementById("proc-" + pj); if (pr && pr.getBoundingClientRect().top < 0) pr.scrollIntoView({ block: "start" }); }
    }
    else if (act === "proc-failed") {
      // from a job's failures (Optimizer, Health, Processes): its files on the Processes page, failed first
      var fj = el.dataset.job;
      procOpen = fj; procSort[fj] = "result"; procFiles[fj] = null;
      procFilesFetch(fj, 0); procFetch(true);
      go("processes", true);
      setTimeout(function () { var x = document.getElementById("proc-" + fj); if (x) x.scrollIntoView({ block: "start" }); }, 600);
    }
    else if (act === "proc-retry") {
      el.disabled = true;
      api("/api/job", { action: "retry", job: el.dataset.job, file: el.dataset.file }).then(function (r) {
        toast(r.ok ? T("It’s tried again after the file in hand") : T("Already waiting to be tried again")); load(); procFetch(true);
      }).catch(function (e) { toast(e.message); el.disabled = false; });
    }
    else if (act === "proc-chart") { procChart[el.dataset.key] = !procChart[el.dataset.key]; render(); }
    else if (act === "file-toggle") { SHOWFILE[el.dataset.file] = !SHOWFILE[el.dataset.file]; render(); }
    else if (act === "lib-view") { LIB.view = el.dataset.view; LIB_DRIVE.at = 0; if (LIB.view === "caching") libFetch(true); render(); }
    else if (act === "lh-open") { var lk = el.dataset.key + ":" + el.dataset.id; LH_OPEN[lk] = !LH_OPEN[lk]; renderStill(); }
    else if (act === "lh-ignore" || act === "lh-restore") {
      el.disabled = true;
      api("/api/library/health/ignore", { library: el.dataset.key, item: el.dataset.item, action: act === "lh-ignore" ? "ignore" : "restore", ids: (el.dataset.ids || "").split(",").filter(Boolean) })
        .then(function () { LIB.health = {}; render(); load(); }).catch(function (e) { toast(e.message); });  // (a tool's rows freed or stopped at once: the state again)
    }
    else if (act === "sync-discover") {
      el.disabled = true;
      api("/api/sync/discover", {}).then(function () { load(); }).catch(function (e) { toast(e.message, true); load(); });
    }
    else if (act === "mirror-set") {
      // a library's mirror (the dataset page's buttons): a library of a server, or none — plex2.choose, as the Mirror page's list
      el.disabled = true;
      mirrorChoose(el.dataset.key, el.dataset.state, el.dataset.server || "", el.dataset.their || "");
    }
    else if (act === "lib-tab") {
      // the library opened is worked out first — its counts, and the inspector reads its files first (focusQuery: load)
      LIB.key = el.dataset.key; LIB.offset = 0; LIB.healthOffset = 0; LIB.open = null; LIB.data = null; LIB.hold.inspect[LIB.key] = false;
      libFetch(true); render(); load();
    }
    else if (act === "lib-page") { LIB.offset = Number(el.dataset.offset) || 0; LIB.open = null; libFetch(true); }
    else if (act === "lh-page") { LIB.healthOffset = Number(el.dataset.offset) || 0; render(); window.scrollTo(0, 0); }
    else if (act === "lib-block") {
      var bn = el.dataset.n === "video" ? "video" : Number(el.dataset.n);
      LIB.block[el.dataset.file] = LIB.block[el.dataset.file] === bn ? null : bn;
      render();
    }
    else if (act === "lib-open") {
      var lf = el.dataset.file, before = el.getBoundingClientRect().top;
      LIB.open = LIB.open === lf ? null : lf;
      if (LIB.open) libDetailFetch(lf);
      render();
      var row = Array.prototype.find.call(main.querySelectorAll("tr.lrow"), function (r) { return r.dataset.file === lf; });
      if (row) window.scrollBy(0, row.getBoundingClientRect().top - before);  // (one above closing doesn't move it)
    }
    else if (act === "lib-survey") {
      el.disabled = true;
      var how = el.dataset.how;
      LIB.hold.inspect[LIB.key] = how === "pause";  // (paused by hand: not started again by itself until Resume)
      api("/api/library/survey", how === "pause" ? { library: LIB.key, pause: true } : { library: LIB.key, force: how === "again" }).then(function () { load(); });
    }
    else if (act === "proc-sort") { var sj = el.dataset.job, sk = el.dataset.key; procSort[sj] = procSort[sj] === sk ? null : sk; procFilesFetch(sj, 0); }
    else if (act === "proc-page") { procFilesFetch(el.dataset.job, Number(el.dataset.offset) || 0); }
    else if (act === "job") {
      // the file job running now: pause, resume or cancel (jobs.py)
      var ja = el.dataset.job;
      api("/api/job", { action: ja }).then(function () {
        if (ja !== "dismiss") toast({ pause: T("Pausing"), resume: T("Resuming"), cancel: T("Cancelling") }[ja]); load();
      });
    }
    else if (act === "benchmark") { api("/api/benchmark", {}).then(function () { toast(T("Measuring disk speed…")); ST.benchmark.running = true; render(); }); }
    else if (act === "warmup-run") { api("/api/warmup/run", {}).then(function (r) { toast(r.started ? T("Warm-up started") : T("Already running")); load(); }); }
    else if (act === "lasts-reset") { api("/api/cachelog/reset", {}).then(function () { toast(T("Started again")); load(); }); }
    else if (act === "warmup-stop") { api("/api/warmup/stop", {}).then(function () { toast(T("Stopping…")); }); }
    else if (act === "mqtt-retry") {
      flush().then(function () { return api("/api/mqtt/reconnect", {}); }).then(function () {
        ST.mqtt = Object.assign({}, ST.mqtt, { state: "connecting", next_retry: null }); render(); setTimeout(load, 1500);
      });
    }
    else if (act === "retest") { flush().then(function () { return api("/api/test", {}); }).then(function () { ST.testing = true; render(); load(); }); }
    else if (act === "plex-signin") { signIn(); }
    else if (act === "plex2-signin") { signIn("plex2"); }
    else if (act === "plex2-address") {  // an address for a mirror this NAS can reach, by hand
      var inp = document.querySelector('[data-p2addr="' + el.dataset.server + '"]');
      el.disabled = true;
      api("/api/plex2/address", { server: el.dataset.server, url: inp ? inp.value.trim() : "" }).then(function () { load(); }).catch(function (e) { toast(e.message, true); load(); });
    }
    else if (act === "peer-pair" || act === "peer-unpair" || act === "peer-test") {  // System → Mirror Plex Cache, per mirror's server
      el.disabled = true;
      flush().then(function () { return api("/api/peer/" + act.slice(5), { server: el.dataset.server || "" }); }).then(function (got) { ST.peer = got; render(); setTimeout(load, 2000); })
        .catch(function (e) { toast(e.message, true); load(); });
    }
  }

  // navigator.clipboard only works on https:// or localhost; this app is usually
  // opened as http://<nas-ip>, so fall back to the older copy command.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { toast(T("Copied")); }, function () { legacyCopy(text); });
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
    toast(ok ? T("Copied") : T("Couldn’t copy — select the command and copy it by hand"));
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

  function signIn(target) {
    var base = target === "plex2" ? "/api/plex2/pin" : "/api/plex/pin";  // (the second server's sign-in keeps its own token)
    var win = window.open("about:blank", "plexauth", "width=800,height=720");
    api(base, {}).then(function (pin) {
      if (win) win.location = pin.auth_url; else window.open(pin.auth_url, "_blank");
      var tries = 0;
      var timer = setInterval(function () {
        if (++tries > 90) { clearInterval(timer); toast(T("Sign-in timed out")); return; }
        api(base + "/" + pin.id, {}).then(function (r) {
          if (r.done) {
            clearInterval(timer);
            if (win) try { win.close(); } catch (e) { /* ignore */ }
            toast(T("Signed in with Plex")); load();
          }
        }).catch(function () { /* keep polling */ });
      }, 2000);
    }).catch(function (e) { if (win) win.close(); toast(T("Couldn’t start sign-in: {why}", { why: e.message })); });
  }

  function parseVal(sec, key, el) {
    var cur = S[sec][key];
    if (typeof cur === "number") return Number(el.value);
    return el.value;
  }

  // Library cached over time: hover (or touch) a point in time to read that hour
  main.addEventListener("pointermove", function (e) {
    var hit = e.target.closest && e.target.closest(".covhit");
    if (!hit) return;
    var box = hit.getBoundingClientRect(), n = Number(hit.dataset.covN);
    var i = Math.max(0, Math.min(n - 1, Math.round((e.clientX - box.left) / box.width * (n - 1))));
    if (i === covHover) return;
    covHover = i;
    var body = main.querySelector("#cov-body");
    if (body) body.innerHTML = covBody();
  });
  main.addEventListener("pointerleave", function (e) {
    if (covHover == null) return;
    covHover = null;
    var body = main.querySelector("#cov-body");
    if (body) body.innerHTML = covBody();
  }, true);

  main.addEventListener("input", function (e) {
    var el = e.target;
    if (el.dataset.lastsStale !== undefined) { lastsFreshUpdate(Number(el.value)); return; }
    if (el.dataset.covRange !== undefined) {
      covRange = Number(el.value);
      covHover = null;
      var cc = main.querySelector("#coverage");
      if (cc) cc.outerHTML = coverageCard();
      var sl2 = main.querySelector("#cov-range");
      if (sl2) sl2.focus();
      return;
    }
    if (el.type === "number" && el.dataset.set && el.value !== "") {
      var np = el.dataset.set.split("."), n = Number(el.value), lo = Number(el.min), hi = Number(el.max);
      if (n >= lo && n <= hi) {
        setLocal(np[0], np[1], n);
        refreshRevert(np[0], np[1]);
        if (np[0] === "warmup") {  // the sizing table follows as you type
          var siz2 = main.querySelector(".sizing");
          if (siz2) siz2.outerHTML = sizingHtml(true);
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
      refreshRamLines();
      if (parts[0] === "warmup") {
        var est = main.querySelector(".est");
        if (est) est.outerHTML = estimateHtml();
        var siz = main.querySelector(".sizing");
        if (siz) siz.outerHTML = sizingHtml(true);
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
        el.placeholder = T("Saved — paste to replace");
      }
    } else if (el.dataset.check) {
      var c = el.dataset.check.split(".");
      queueSave(c[0], c[1], el.checked, true);
      refreshRevert(c[0], c[1]);
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
      if (!(v >= 10 && v <= 20000)) { toast(T("Speed must be between {min} and {max} MB/s", { min: 10, max: 20000 })); return; }
      sp[el.dataset.speed] = { mb_s: v, measured_at: null };
      queueSave("general", "disk_speeds", sp, true);
    } else if (el.dataset.mapOther !== undefined) {
      var v = el.value.trim();
      if (v && v.charAt(0) !== "/") { toast(T("Enter a full path starting with /")); return; }
      saveMapping(el.dataset.mapOther, v);
    }
  });

  // A library's seconds (null: back to its kind's default).
  function saveLibSeconds(key, value) {
    var libs = JSON.parse(JSON.stringify(S.warmup.libraries || {}));
    libs[key] = libs[key] || {};
    if (value == null) delete libs[key].seconds; else libs[key].seconds = value;
    if (!Object.keys(libs[key]).length) delete libs[key];
    queueSave("warmup", "libraries", libs, false);
  }
  document.addEventListener("input", function (e) {
    var el = e.target;
    if (!el.dataset || el.dataset.libsec === undefined) return;
    var v = document.querySelector('[data-val="' + el.id + '"]');
    if (v) v.textContent = secText(el.value, el.dataset.unit);
  });
  // Mirror: a library's mirror chosen from its row's list — not decided, no mirror, or "<server>:<its library there>"
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el.dataset || el.dataset.p2lib === undefined) return;
    var v = el.value, i = v.indexOf(":");
    el.disabled = true;
    if (i < 0) mirrorChoose(el.dataset.p2lib, v); else mirrorChoose(el.dataset.p2lib, "mirror", v.slice(0, i), v.slice(i + 1));
  });
  // System → Mirror Plex Cache: the other Plex Cache's address by hand, per mirror's server (peer.servers)
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el.dataset || el.dataset.peerurl === undefined) return;
    var svs = JSON.parse(JSON.stringify((S.peer || {}).servers || {}));
    svs[el.dataset.peerurl] = { url: el.value.trim() };
    queueSave("peer", "servers", svs, true);
  });
  // Library → Health: each check's switch
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el.dataset) return;
    if (el.dataset.lhlang !== undefined) {  // the subtitle language wanted: the check runs again with it
      queueSave("library_health", "subtitle_language", el.value, true);
      LIB.health = {};
      return;
    }
    if (el.dataset.change && el.tagName === "SELECT") { action(el.dataset.change, el); return; }  // (a choice acts when changed — data-change, never data-act: a click only opens it)
    if (el.dataset.lhquality !== undefined) {  // the audio quality level: the floors move, so the checks run again
      queueSave("library_health", "audio_quality", el.value, true);
      LIB.health = {};
      return;
    }
    if (el.dataset.lhcheck === undefined) return;
    var checks = JSON.parse(JSON.stringify((S.library_health || {}).checks || {}));
    checks[el.dataset.lhcheck] = el.checked;
    queueSave("library_health", "checks", checks, true);
    render();  // (the findings are all there: the view follows the switch at once)
  });
  // The Optimizer page: each library's choices (sync, mkv: checkboxes; images: the cover art select)
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el.dataset || el.dataset.optlib === undefined) return;
    var libs = JSON.parse(JSON.stringify((S.optimizer || {}).libraries || {}));
    var lib = libs[el.dataset.optlib] = optChoices(el.dataset.optlib);
    lib[el.dataset.optkey] = el.type === "checkbox" ? el.checked : el.value;
    queueSave("optimizer", "libraries", libs, true);
  });
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el.dataset || el.dataset.libsec === undefined) return;
    saveLibSeconds(el.dataset.libsec, Number(el.value));
    render();  // shows (or hides) its ↺ straight away
  });

  // ---- the mark-carrying dropdown (pickHtml): open, move, choose, close
  var PICK_ACTIONS = {
    set: function (arg, value) { var sk = arg.split("."); queueSave(sk[0], sk[1], value, true); },
    warm: function (key, value) {  // a library's warm-up schedule
      var libs = JSON.parse(JSON.stringify(S.warmup.libraries || {}));
      libs[key] = libs[key] || {};
      libs[key].schedule = value;
      queueSave("warmup", "libraries", libs, true);
    },
    lib: function (arg, value) {  // "<library key>.<start|end|end_at>"
      var at = arg.lastIndexOf("."), key = arg.slice(0, at), field = arg.slice(at + 1);
      var libs = JSON.parse(JSON.stringify(S.nextepisode.libraries || {}));
      libs[key] = libs[key] || {};
      libs[key][field] = value;
      queueSave("nextepisode", "libraries", libs, true);
    }
  };
  function pickOpen(pk) {
    document.querySelectorAll(".pick.open").forEach(function (o) { if (o !== pk) pickClose(o, false); });
    pk.classList.add("open");
    pk.querySelector(".pick-btn").setAttribute("aria-expanded", "true");
    var list = pk.querySelector(".pick-list");
    list.hidden = false;
    (list.querySelector(".sel") || list.querySelector("li")).focus();
  }
  function pickClose(pk, focusBtn) {
    pk.classList.remove("open");
    pk.querySelector(".pick-btn").setAttribute("aria-expanded", "false");
    pk.querySelector(".pick-list").hidden = true;
    if (focusBtn) pk.querySelector(".pick-btn").focus();
  }
  function pickChoose(li) {
    var pk = li.closest(".pick"), on = pk.dataset.on, at = on.indexOf(":");
    pickClose(pk, true);
    if (li.getAttribute("aria-selected") === "true") return;
    PICK_ACTIONS[on.slice(0, at)](on.slice(at + 1), li.dataset.value);
    render();
  }
  document.addEventListener("click", function (e) {
    var back = e.target.closest && e.target.closest("[data-pick-revert]");
    if (back) {
      var on = back.dataset.pickRevert, at = on.indexOf(":");
      PICK_ACTIONS[on.slice(0, at)](on.slice(at + 1), back.dataset.pickDefault);
      render();
      return;
    }
    var btn = e.target.closest && e.target.closest(".pick-btn");
    if (btn) { var pk = btn.closest(".pick"); if (pk.classList.contains("open")) pickClose(pk, true); else pickOpen(pk); return; }
    var li = e.target.closest && e.target.closest(".pick-list li");
    if (li) { pickChoose(li); return; }
    document.querySelectorAll(".pick.open").forEach(function (o) { pickClose(o, false); });
  });
  document.addEventListener("keydown", function (e) {
    var btn = e.target.closest && e.target.closest(".pick-btn");
    if (btn && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); pickOpen(btn.closest(".pick")); return; }
    var li = e.target.closest && e.target.closest(".pick-list li");
    if (!li) return;
    var items = Array.prototype.slice.call(li.parentNode.children), i = items.indexOf(li);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); items[Math.max(0, Math.min(items.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))].focus(); }
    else if (e.key === "Home" || e.key === "End") { e.preventDefault(); items[e.key === "Home" ? 0 : items.length - 1].focus(); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickChoose(li); }
    else if (e.key === "Escape") { e.preventDefault(); pickClose(li.closest(".pick"), true); }
    else if (e.key === "Tab") pickClose(li.closest(".pick"), false);
  });

  window.addEventListener("popstate", function () { go(location.pathname.replace("/", "") || "health", false); });

  page = location.pathname.replace("/", "") || "health";
  load().then(function () {
    go(page, false);
    // A link straight to a card (/ssd#l2arc-settings)
    if (reveal(location.hash.slice(1))) { render(); scrollToSoon(location.hash.slice(1), -70); }  // (a record: once its list is in)
    var target = location.hash && document.getElementById(location.hash.slice(1));
    if (target) target.scrollIntoView({ block: "start" });
  });
  setInterval(function () { if (!document.hidden) load(); }, 4000);
});
