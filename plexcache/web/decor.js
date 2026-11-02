// Dionysus's decorations: what a theme's "decor" turns on (plexcache/themes.py), drawn on whatever page is open.
//
// theme.js marks <html> with the theme's choices before the page draws: data-logo (classic | theatre),
// data-spots, data-vines and data-film (on | off). This file draws them:
//  * the logo in the menu, on the sign-in page and on the browser tab;
//  * spots round the app and on the menu (a tile made here in the theme's colours);
//  * vines: a wavy line with a leaf now and then round every card and round the whole app, and down the seam between
//    the menu and the page. A card's colour stops at its vine (the vine is the card's border);
//  * film: a reel unspooling a band of film under every page's title, its wave picked from the page's name, so a page
//    always looks the same and no two pages quite match.
// Everything is drawn from the cards and titles the shared code makes (app.js: card markup, top()), so every page and
// tool gets the same, with nothing to add page by page. app.js calls PCDECOR.paint() after it draws a page; changes
// made inside a page later are caught by watching it.
(function () {
  "use strict";
  var d = document.documentElement;
  var on = function (k) { return d.getAttribute("data-" + k) === "on"; };
  var r1 = function (n) { return Math.round(n * 100) / 100; };
  function rng(seed) { return function () { seed |= 0; seed = seed + 0x6D2B79F5 | 0; var t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  function hs(s) { var h = 2166136261; for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function ease(a, b, t) { var x = Math.max(0, Math.min(1, (t - a) / (b - a))); return x * x * (3 - 2 * x); }
  function css(name) { return getComputedStyle(d).getPropertyValue(name).trim(); }
  var enc = function (s) { return 'url("data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s) + '")'; };

  // ------------------------------------------------------------------ logo
  function logo() {
    var theatre = d.getAttribute("data-logo") === "theatre";
    document.querySelectorAll(".brand img, .brand-row img").forEach(function (img) {
      var src = theatre ? "/static/theatre.svg" : "/static/logo.svg";
      if (img.getAttribute("src").split("?")[0] !== src) img.setAttribute("src", src);
    });
    var icon = document.querySelector('link[rel="icon"]');
    if (icon) { var href = theatre ? "/static/theatre-tab.svg" : "/static/logo.svg"; if (icon.getAttribute("href").split("?")[0] !== href) icon.setAttribute("href", href); }
  }

  // ------------------------------------------------------------------ spots
  // a tile of broken rings and dots, in one colour at a low strength, that repeats without a seam
  function spotTile(color, a, seed) {
    var R = rng(seed), T = 180, n = 5, o = "";
    for (var gy = 0; gy < n; gy++) for (var gx = 0; gx < n; gx++) {
      var cell = T / n, cx = gx * cell + cell / 2 + (R() - .5) * cell * .45 + (gy % 2 ? cell / 4 : 0), cy = gy * cell + cell / 2 + (R() - .5) * cell * .45;
      var rr = 5.5 + R() * 3, segs = 3 + Math.floor(R() * 2), start = R() * 360, sw = 1.8 + R() * 1.2;
      for (var k = 0; k < segs; k++) {
        var a0 = (start + k * 360 / segs) * Math.PI / 180, a1 = a0 + (360 / segs) * (.45 + R() * .25) * Math.PI / 180, jr = rr * (.85 + R() * .3);
        var x0 = cx + jr * Math.cos(a0), y0 = cy + jr * Math.sin(a0), x1 = cx + jr * Math.cos(a1), y1 = cy + jr * Math.sin(a1);
        [-T, 0, T].forEach(function (ox) { [-T, 0, T].forEach(function (oy) {
          if (Math.abs(cx + ox - T / 2) < T / 2 + 12 && Math.abs(cy + oy - T / 2) < T / 2 + 12)
            o += '<path d="M' + r1(x0 + ox) + " " + r1(y0 + oy) + "A" + r1(jr) + " " + r1(jr) + " 0 0 1 " + r1(x1 + ox) + " " + r1(y1 + oy) + '" stroke-width="' + r1(sw) + '"/>';
        }); });
      }
      o += '<circle cx="' + r1(cx) + '" cy="' + r1(cy) + '" r="' + r1(rr * .55) + '" fill="' + color + '" fill-opacity="' + r1(a * .45) + '" stroke="none"/>';
      if (R() < .5) o += '<circle cx="' + r1(cx + cell * .42) + '" cy="' + r1(cy - cell * .3) + '" r="' + r1(1 + R()) + '" fill="' + color + '" fill-opacity="' + a + '" stroke="none"/>';
    }
    return enc('<svg xmlns="http://www.w3.org/2000/svg" width="' + T + '" height="' + T + '"><g fill="none" stroke="' + color + '" stroke-opacity="' + a + '" stroke-linecap="round">' + o + "</g></svg>");
  }
  function spots() {
    var a = parseFloat(css("--spots-a")) || 0;
    if (!on("spots") || !a) { d.style.removeProperty("--spots-img"); d.style.removeProperty("--menu-spots-img"); return; }
    d.style.setProperty("--spots-img", spotTile(css("--spots"), a, 31));
    d.style.setProperty("--menu-spots-img", spotTile(css("--menu-spots"), Math.max(a, .06), 11));
  }

  // ------------------------------------------------------------------ vines
  var leafAt = function (x, y, ang, len, g) { return '<path d="M0 0q' + r1(len / 2) + " " + r1(-len * .42) + " " + len + " 0q" + r1(-len / 2) + " " + r1(len * .42) + " " + (-len) + ' 0z" transform="translate(' + r1(x) + " " + r1(y) + ") rotate(" + r1(ang) + ')" fill="' + g + '"/>'; };
  var curlAt = function (x, y, s, g) { return '<path d="M' + r1(x) + " " + r1(y) + "c0 " + 4 * s + " 2 " + 7 * s + " 5 " + 7 * s + "c2.4 0 3 " + (-3 * s) + " 1 " + (-3.2 * s) + '" fill="none" stroke="' + g + '" stroke-width="1.3" stroke-linecap="round"/>'; };
  // a leaf growing out of the vine: a short stalk from the line, then the leaf, so it always joins the line
  function leafOn(x, y, ang, len, c) {
    var a = ang * Math.PI / 180, cx = Math.cos(a), sy = Math.sin(a);
    return '<path d="M' + r1(x - cx * .6) + " " + r1(y - sy * .6) + "L" + r1(x + cx * 2.8) + " " + r1(y + sy * 2.8) + '" stroke="' + c + '" stroke-width="1.2" stroke-linecap="round" fill="none"/>' + leafAt(x + cx * 2.2, y + sy * 2.2, ang, len, c);
  }
  // a wavy line round a rounded box of w x h; inset keeps the whole wave inside the box (for clipping to it)
  function edge(w, h, rad, seed, inset) {
    var x0 = .5, y0 = .5, W = w - 1, H = h - 1, r = Math.min(rad, W / 2, H / 2);
    var Ls = W - 2 * r, Ms = H - 2 * r, A = Math.PI * r / 2, seg = [Ls, A, Ms, A, Ls, A, Ms, A], P = 2 * Ls + 2 * Ms + 4 * A;
    function at(s) {
      s = ((s % P) + P) % P; var i = 0; while (i < 7 && s > seg[i]) { s -= seg[i]; i++; }
      function arc(cx, cy, a0) { var a = a0 + (r ? s / r : 0); return [cx + r * Math.cos(a), cy + r * Math.sin(a), Math.cos(a), Math.sin(a)]; }
      switch (i) { case 0: return [x0 + r + s, y0, 0, -1]; case 1: return arc(x0 + W - r, y0 + r, -Math.PI / 2); case 2: return [x0 + W, y0 + r + s, 1, 0]; case 3: return arc(x0 + W - r, y0 + H - r, 0);
        case 4: return [x0 + W - r - s, y0 + H, 0, 1]; case 5: return arc(x0 + r, y0 + H - r, Math.PI / 2); case 6: return [x0, y0 + H - r - s, -1, 0]; default: return arc(x0 + r, y0 + r, Math.PI); }
    }
    var R = rng(seed), n1 = Math.max(2, Math.round(P / (80 + R() * 30))), n2 = Math.max(5, Math.round(P / (30 + R() * 8))), p1 = R() * 6.3, p2 = R() * 6.3, A1 = 1.1, A2 = .45;
    var sh = inset ? -(A1 + A2 + .7) : 0;
    function pt(s) { var q = at(s), o = sh + A1 * Math.sin(s / P * n1 * 2 * Math.PI + p1) + A2 * Math.sin(s / P * n2 * 2 * Math.PI + p2); return [q[0] + q[2] * o, q[1] + q[3] * o, q[2], q[3]]; }
    var step = P > 6000 ? 4 : 2.5, path = "";
    for (var s = 0; s < P; s += step) { var q = pt(s); path += (s ? "L" : "M") + r1(q[0]) + " " + r1(q[1]); }
    path += "z";
    // a leaf or a curl every 200 to 620 px along the line, mostly leaning outward
    function deco(c) {
      var o = "";
      for (var s = 60 + R() * 240; s < P - 30; s += 200 + R() * 422) {
        var q = pt(s), tang = Math.atan2(q[2], -q[3]) * 180 / Math.PI, out = R() < .65;
        if (R() < .7) o += leafOn(q[0], q[1], out ? tang - (38 + R() * 20) : tang + (38 + R() * 20), 7 + R() * 2, c);
        else o += '<g transform="rotate(' + r1(tang) + " " + r1(q[0]) + " " + r1(q[1]) + ')">' + curlAt(q[0], q[1], out ? -1 : 1, c) + "</g>";
      }
      return o;
    }
    return { d: path, deco: deco };
  }
  var drawn = {};  // vines already worked out, by size and seed: a page draws again every few seconds, the same each time
  function cached(key, make) { if (!drawn[key]) { if (Object.keys(drawn).length > 400) drawn = {}; drawn[key] = make(); } return drawn[key]; }
  function put(el, cls, html) {
    var old = el.querySelector(":scope > ." + cls);
    if (!html) { if (old) old.remove(); return; }
    if (old && old.getAttribute("data-k") === html.k) return;
    var t = document.createElement("div"); t.innerHTML = html.svg;
    var svg = t.firstChild; svg.setAttribute("data-k", html.k);
    if (old) old.replaceWith(svg); else el.appendChild(svg);
  }
  // each card: its colour inside the vine, the vine on its border
  function cards(root) {
    var list = Array.prototype.slice.call(root.querySelectorAll(".card"));
    var sizes = list.map(function (c) { return [c.offsetWidth, c.offsetHeight, parseFloat(getComputedStyle(c).borderTopLeftRadius) || 12]; });
    var page = location.pathname;
    list.forEach(function (c, i) {
      var w = sizes[i][0], h = sizes[i][1], rad = sizes[i][2];
      if (!w || !h) return;
      var seed = hs(page + "#" + (c.id || i)), k = w + "x" + h + "x" + rad + "x" + seed;
      put(c, "vine", { k: k, svg: cached(k, function () {
        var g = edge(w, h, rad, seed, false);
        return '<svg class="vine" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true" focusable="false"><path d="' + g.d +
          '" fill="var(--panel)" stroke="var(--vine)" stroke-width="1.2" stroke-linejoin="round"/>' + g.deco("var(--vine-leaf)") + "</svg>";
      }) });
    });
  }
  // round the whole app, the spots outside it: the app is cut to the vine, which the vine then covers
  function frame() {
    var shell = document.querySelector(".shell");
    if (!shell) return;
    if (!on("vines")) { shell.style.clipPath = ""; put(shell, "framevine", null); put(shell, "seamvine", null); return; }
    var w = shell.offsetWidth, h = shell.offsetHeight, k = w + "x" + h;
    if (!w || !h) return;
    var g = cached("frame" + k, function () { return edge(w, h, 14, 7, true); });
    shell.style.clipPath = "path('" + g.d + "')";
    put(shell, "framevine", { k: k, svg: cached("framesvg" + k, function () {
      return '<svg class="framevine" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true" focusable="false"><path d="' + g.d +
        '" fill="none" stroke="var(--frame)" stroke-width="1.4" stroke-linejoin="round"/>' + edge(w, h, 14, 7, true).deco("var(--frame-leaf)") + "</svg>";
    }) });
    seam(shell);
  }
  // down the seam between the menu and the page (along its foot when the menu sits across the top, on a phone)
  function seam(shell) {
    var side = shell.querySelector(".side");
    if (!side) return;
    var across = side.offsetTop + side.offsetHeight <= (shell.querySelector(".main") || side).offsetTop + 1;
    var L = across ? side.offsetWidth : shell.offsetHeight, X = across ? side.offsetHeight : side.offsetWidth, k = (across ? "a" : "d") + L + "x" + X;
    put(shell, "seamvine", { k: k, svg: cached("seam" + k, function () {
      var R = rng(19), n1 = Math.max(1, Math.round(L / (80 + R() * 30))), n2 = Math.max(3, Math.round(L / (30 + R() * 8))), p1 = R() * 6.3, p2 = R() * 6.3;
      var off = function (t) { return 1.1 * Math.sin(t / L * n1 * 2 * Math.PI + p1) + .45 * Math.sin(t / L * n2 * 2 * Math.PI + p2); };
      var along = function (t) { return across ? [t, X + off(t)] : [X + off(t), t]; }, path = "";
      for (var t = 0; t <= L; t += L > 6000 ? 4 : 2.5) { var p = along(t); path += (t ? "L" : "M") + r1(p[0]) + " " + r1(p[1]); }
      var o = '<path d="' + path + '" fill="none" stroke="var(--menu-vine)" stroke-width="1.3" stroke-linecap="round"/>';
      for (t = 40 + R() * 200; t < L - 24; t += 178 + R() * 378) {
        var q = along(t), sd = R() < .5 ? -1 : 1, base = across ? 0 : 90;
        o += R() < .7 ? leafOn(q[0], q[1], base + sd * (40 + R() * 20) * (across ? -1 : 1), 7 + R() * 2, "var(--menu-vine)")
          : '<g transform="rotate(' + base + " " + r1(q[0]) + " " + r1(q[1]) + ')">' + curlAt(q[0], q[1], sd, "var(--menu-vine)") + "</g>";
      }
      return '<svg class="seamvine" width="' + (across ? L : X + 12) + '" height="' + (across ? X + 12 : L) + '" aria-hidden="true" focusable="false">' + o + "</svg>";
    }) });
  }

  // ------------------------------------------------------------------ film
  var GLYPH = [-64, -32, 0, 32, 64].map(function (a) { return '<ellipse cx="0" cy="-5" rx="2.3" ry="' + (a === 0 ? 6.2 : 5.4) + '" transform="rotate(' + a + ' 0 6)" fill="var(--film-mark)"/>'; }).join("") +
    '<path d="M-7 9Q0 3 7 9" fill="none" stroke="var(--film-mark)" stroke-width="2" stroke-linecap="round"/>';
  var IONIC = '<path d="M-9-7H9M-4-5V6M0-5V6M4-5V6M-7 8.5H7" fill="none" stroke="var(--hole)" stroke-width="2" stroke-linecap="round"/><circle cx="-8" cy="-4" r="2.2" fill="none" stroke="var(--hole)" stroke-width="1.6"/><circle cx="8" cy="-4" r="2.2" fill="none" stroke="var(--hole)" stroke-width="1.6"/>';
  var WAVES = [[.5, 1.25, .25], [.28, 1.25, .25], [.5, .75, .2], [.36, 2, .25]];
  // a band of film seen in 3D: centreline c(t), width w, tilt k(t) (0: edge on); its sprocket holes and symbols foreshortened
  function ribbon(c, w, tw, n, pitch) {
    var P = [], TH = 1.4, i, o = "";
    for (i = 0; i <= n; i++) {
      var t = i / n, a = c(Math.max(0, t - .002)), b = c(Math.min(1, t + .002)), p = c(t), tx = b[0] - a[0], ty = b[1] - a[1], L = Math.hypot(tx, ty) || 1;
      tx /= L; ty /= L; P.push({ p: p, T: [tx, ty], N: [-ty, tx], w: w, k: tw(t) });
    }
    var pt = function (q, u, v) { return [q.p[0] + q.N[0] * u + q.T[0] * v, q.p[1] + q.N[1] * u + q.T[1] * v]; };
    var poly = function (arr, fill, extra) { return '<path d="M' + arr.map(function (a) { return r1(a[0]) + " " + r1(a[1]); }).join("L") + 'z" fill="' + fill + '" ' + (extra || "") + "/>"; };
    var hh = function (q) { return Math.max(q.w / 2 * Math.abs(q.k), TH); };
    for (i = 0; i < n; i++) { var A = P[i], B = P[i + 1]; o += poly([pt(A, hh(A), 0), pt(B, hh(B), 0), pt(B, -hh(B), 0), pt(A, -hh(A), 0)], "var(--film)", 'stroke="var(--film)" stroke-width=".6" stroke-linejoin="round"'); }
    var len = 0, marks = [];
    for (i = 1; i <= n; i++) { len += Math.hypot(P[i].p[0] - P[i - 1].p[0], P[i].p[1] - P[i - 1].p[1]); marks.push(len); }
    var atD = function (dd) { var j = 0; while (j < marks.length && marks[j] < dd) j++; return j < marks.length ? P[j + 1] : null; };
    var hstep = pitch / 4, dd, Q;
    for (dd = hstep / 2; dd < len; dd += hstep) {
      Q = atD(dd); if (!Q) break; var ak = Math.abs(Q.k); if (ak < .2) continue;
      var hl = Q.w * .05, hw = Math.max(.5, Q.w * .045 * ak);
      [1, -1].forEach(function (sd) { var u = sd * Q.w / 2 * ak * .8; o += poly([pt(Q, u - hw, -hl), pt(Q, u + hw, -hl), pt(Q, u + hw, hl), pt(Q, u - hw, hl)], "var(--hole)"); });
    }
    for (dd = pitch * .5; dd < len - pitch * .3; dd += pitch) {
      Q = atD(dd); if (!Q) break; if (Math.abs(Q.k) < .06) continue; var sc = Q.w / 26;
      o += '<g transform="matrix(' + r1(Q.T[0] * sc) + " " + r1(Q.T[1] * sc) + " " + r1(Q.N[0] * sc * Q.k) + " " + r1(Q.N[1] * sc * Q.k) + " " + r1(Q.p[0]) + " " + r1(Q.p[1]) + ')">' + GLYPH + "</g>";
    }
    return o;
  }
  // the reel: its face in the film's colour, five column cutouts, a cut ring, the hub ring and the peg hole
  function reel(cx, cy, R) {
    var sc = R / 38, o = '<circle cx="' + cx + '" cy="' + cy + '" r="' + R + '" fill="var(--film)"/><g transform="translate(' + r1(cx - 39 * sc) + " " + r1(cy - 39 * sc) + ") scale(" + r1(sc) + ')">';
    for (var i = 0; i < 5; i++) { var ang = -90 + i * 72, x = r1(39 + 23 * Math.cos(ang * Math.PI / 180)), y = r1(39 + 23 * Math.sin(ang * Math.PI / 180)); o += '<g transform="translate(' + x + " " + y + ") rotate(" + (ang + 90) + ') scale(.8)">' + IONIC + "</g>"; }
    var ring = function (r) { return '<circle cx="39" cy="39" r="' + r + '" fill="none" stroke="var(--hole)" stroke-width="1.6"/>'; };
    return o + ring(33.5) + '<circle cx="39" cy="39" r="4.5" fill="var(--hole)"/>' + ring(8) + "</g>";
  }
  var maskN = 0;
  function band(name, W) {
    var r = 20, cy = r + 4, x0 = r + 4, L = W - x0 - 2, q = (600 - x0 - 2) / L, D = 2 * r, tr = r / L, y0 = cy - r + .8 * D, bw = .2 * D, pitch = 22 * r / 34;
    var R = rng(hs(name)), wv = WAVES[Math.floor(R() * WAVES.length)], j = function () { return .9 + R() * .2; };
    var amp = Math.min(.55, wv[0] * j()), cyc = wv[1] * j() / q, kd = wv[2] * j();
    var X = function (t) { return x0 + t * L; }, open = function (t) { return ease(tr * .5, tr + .1 * q, t); };
    var c = function (t) { return [X(t), y0 - (D * amp) * (.5 - .5 * Math.cos((t - tr) * Math.PI * 2 * cyc)) * ease(tr, tr + .15 * q, t)]; };
    var k = function (t) { return open(t) * (1 - kd + kd * Math.cos((t - tr) * Math.PI * 4 * cyc)); };
    var inner = ribbon(c, bw, k, Math.round(W / 2.3), pitch) + reel(x0, cy, r), H = 2 * r + 8, id = "pcfilm" + (++maskN);
    // the sprocket holes and the reel's cutouts are see-through: a mask with them black
    return '<svg class="filmband" viewBox="0 0 ' + W + " " + H + '" width="' + W + '" height="' + H + '" aria-hidden="true" focusable="false">' +
      '<mask id="' + id + '" maskUnits="userSpaceOnUse" x="-50" y="-50" width="' + (W + 100) + '" height="' + (H + 100) + '"><g style="--film:#fff;--film-mark:#fff;--hole:#000">' + inner + "</g></mask>" +
      '<g mask="url(#' + id + ')" style="--hole:var(--film)">' + inner + "</g></svg>";
  }
  function film(root) {
    var top = root.querySelector(":scope > .top"), old = root.querySelector(":scope > .filmline");
    if (!on("film") || !top) { if (old) old.remove(); return; }
    var W = Math.round(top.offsetWidth), name = location.pathname, k = W + name;
    if (W < 120) return;
    if (old && old.getAttribute("data-k") === k) return;
    var div = document.createElement("div");
    div.className = "filmline"; div.setAttribute("data-k", k);
    div.innerHTML = cached("film" + k, function () { return band(name, W); });
    if (old) old.replaceWith(div); else top.insertAdjacentElement("afterend", div);
  }

  // ------------------------------------------------------------------ when to draw
  var main = null, busy = false;
  function paint(root) {
    root = root || main || document.querySelector(".main") || document.body;
    if (!root) return;
    main = root;
    busy = true;
    try {
      film(root);
      if (on("vines")) cards(root);
      else root.querySelectorAll(".card > .vine").forEach(function (v) { v.remove(); });
      frame();
    } finally { busy = false; }
  }
  var queued = false;
  function soon() { if (queued) return; queued = true; requestAnimationFrame(function () { queued = false; paint(); }); }
  if (window.MutationObserver) {
    new MutationObserver(function (list) {
      if (busy) return;
      // only changes to a page's own content: not the vines and film this file adds
      for (var i = 0; i < list.length; i++) {
        var t = list[i].target;
        if (!(t.closest && t.closest(".vine,.framevine,.seamvine,.filmline"))) { soon(); return; }
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
  window.addEventListener("resize", soon);
  // after the theme changes (theme.js): the logo, the spots' colours, and everything drawn again
  function themed() { drawn = {}; spots(); logo(); soon(); }
  window.PCDECOR = { paint: paint, themed: themed };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", themed); else themed();
})();
