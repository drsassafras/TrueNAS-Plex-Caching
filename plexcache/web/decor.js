// Dionysus's decorations: what a theme's "decor" turns on (plexcache/themes.py), drawn on whatever page is open.
//
// theme.js marks <html> with the theme's choices before the page draws: data-spots, data-vines and data-film (on | off).
// This file draws them:
//  * spots round the app and on the menu (a tile made here in the theme's colours);
//  * vines: a wavy line with a leaf now and then round every card, and down the seam between the menu and the page
//    (held beside the menu as the page scrolls). A card's colour stops at its vine (the vine is the card's border), and
//    so do the menu's and the page's at the seam;
//  * film: a reel unspooling a band of film under every page's title, its wave picked from the page's name, so a page
//    always looks the same and no two pages quite match.
// Everything is drawn from the cards and titles the shared code makes (app.js: card markup, top()), so every page and
// tool gets the same, with nothing to add page by page. app.js calls PCDECOR.paint() after it draws a page; changes
// made inside a page later are caught by watching it.
(function () {
  "use strict";
  const d = document.documentElement;
  const on = function (k) { return d.getAttribute("data-" + k) === "on"; };
  const r1 = function (n) { return Math.round(n * 100) / 100; };
  function rng(seed) { return function () { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  function hs(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function ease(a, b, t) { const x = Math.max(0, Math.min(1, (t - a) / (b - a))); return x * x * (3 - 2 * x); }
  function css(name) { return getComputedStyle(d).getPropertyValue(name).trim(); }
  const enc = function (s) { return 'url("data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s) + '")'; };

  // ------------------------------------------------------------------ spots
  // a tile of broken rings and dots, in one colour at a low strength, that repeats without a seam
  function spotTile(color, a, seed) {
    const R = rng(seed), T = 180, n = 5; let o = "";
    for (let gy = 0; gy < n; gy++) for (let gx = 0; gx < n; gx++) {
      const cell = T / n, cx = gx * cell + cell / 2 + (R() - .5) * cell * .45 + (gy % 2 ? cell / 4 : 0), cy = gy * cell + cell / 2 + (R() - .5) * cell * .45;
      const rr = 5.5 + R() * 3, segs = 3 + Math.floor(R() * 2), start = R() * 360, sw = 1.8 + R() * 1.2;
      for (let k = 0; k < segs; k++) {
        const a0 = (start + k * 360 / segs) * Math.PI / 180, a1 = a0 + (360 / segs) * (.45 + R() * .25) * Math.PI / 180, jr = rr * (.85 + R() * .3);
        const x0 = cx + jr * Math.cos(a0), y0 = cy + jr * Math.sin(a0), x1 = cx + jr * Math.cos(a1), y1 = cy + jr * Math.sin(a1);
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
    const a = parseFloat(css("--spots-a")) || 0;
    if (!on("spots") || !a) { d.style.removeProperty("--spots-img"); d.style.removeProperty("--menu-spots-img"); return; }
    d.style.setProperty("--spots-img", spotTile(css("--spots"), a, 31));
    d.style.setProperty("--menu-spots-img", spotTile(css("--menu-spots"), Math.max(a, .06), 11));
  }

  // ------------------------------------------------------------------ vines
  const leafAt = function (x, y, ang, len, g) { return '<path d="M0 0q' + r1(len / 2) + " " + r1(-len * .42) + " " + len + " 0q" + r1(-len / 2) + " " + r1(len * .42) + " " + (-len) + ' 0z" transform="translate(' + r1(x) + " " + r1(y) + ") rotate(" + r1(ang) + ')" fill="' + g + '"/>'; };
  const curlAt = function (x, y, s, g) { return '<path d="M' + r1(x) + " " + r1(y) + "c0 " + 4 * s + " 2 " + 7 * s + " 5 " + 7 * s + "c2.4 0 3 " + (-3 * s) + " 1 " + (-3.2 * s) + '" fill="none" stroke="' + g + '" stroke-width="1.3" stroke-linecap="round"/>'; };
  // a leaf growing out of the vine: a short stalk from the line, then the leaf, so it always joins the line
  function leafOn(x, y, ang, len, c) {
    const a = ang * Math.PI / 180, cx = Math.cos(a), sy = Math.sin(a);
    return '<path d="M' + r1(x - cx * .6) + " " + r1(y - sy * .6) + "L" + r1(x + cx * 2.8) + " " + r1(y + sy * 2.8) + '" stroke="' + c + '" stroke-width="1.2" stroke-linecap="round" fill="none"/>' + leafAt(x + cx * 2.2, y + sy * 2.2, ang, len, c);
  }
  // a wavy line round a rounded box of w x h; inset keeps the whole wave inside the box (for clipping to it)
  function edge(w, h, rad, seed, inset) {
    const x0 = .5, y0 = .5, W = w - 1, H = h - 1, r = Math.min(rad, W / 2, H / 2);
    const Ls = W - 2 * r, Ms = H - 2 * r, A = Math.PI * r / 2, seg = [Ls, A, Ms, A, Ls, A, Ms, A], P = 2 * Ls + 2 * Ms + 4 * A;
    function at(s) {
      s = ((s % P) + P) % P; let i = 0; while (i < 7 && s > seg[i]) { s -= seg[i]; i++; }
      function arc(cx, cy, a0) { const a = a0 + (r ? s / r : 0); return [cx + r * Math.cos(a), cy + r * Math.sin(a), Math.cos(a), Math.sin(a)]; }
      switch (i) { case 0: return [x0 + r + s, y0, 0, -1]; case 1: return arc(x0 + W - r, y0 + r, -Math.PI / 2); case 2: return [x0 + W, y0 + r + s, 1, 0]; case 3: return arc(x0 + W - r, y0 + H - r, 0);
        case 4: return [x0 + W - r - s, y0 + H, 0, 1]; case 5: return arc(x0 + r, y0 + H - r, Math.PI / 2); case 6: return [x0, y0 + H - r - s, -1, 0]; default: return arc(x0 + r, y0 + r, Math.PI); }
    }
    const R = rng(seed), n1 = Math.max(2, Math.round(P / (80 + R() * 30))), n2 = Math.max(5, Math.round(P / (30 + R() * 8))), p1 = R() * 6.3, p2 = R() * 6.3, A1 = 1.1, A2 = .45;
    const sh = inset ? -(A1 + A2 + .7) : 0;
    function pt(s) { const q = at(s), o = sh + A1 * Math.sin(s / P * n1 * 2 * Math.PI + p1) + A2 * Math.sin(s / P * n2 * 2 * Math.PI + p2); return [q[0] + q[2] * o, q[1] + q[3] * o, q[2], q[3]]; }
    const step = P > 6000 ? 4 : 2.5; let path = "";
    for (let s = 0; s < P; s += step) { const q = pt(s); path += (s ? "L" : "M") + r1(q[0]) + " " + r1(q[1]); }
    path += "z";
    // a leaf or a curl every 200 to 620 px along the line, mostly leaning outward
    function deco(c) {
      let o = "";
      for (let s = 60 + R() * 240; s < P - 30; s += 200 + R() * 422) {
        const q = pt(s), tang = Math.atan2(q[2], -q[3]) * 180 / Math.PI, out = R() < .65;
        if (R() < .7) o += leafOn(q[0], q[1], out ? tang - (38 + R() * 20) : tang + (38 + R() * 20), 7 + R() * 2, c);
        else o += '<g transform="rotate(' + r1(tang) + " " + r1(q[0]) + " " + r1(q[1]) + ')">' + curlAt(q[0], q[1], out ? -1 : 1, c) + "</g>";
      }
      return o;
    }
    return { d: path, deco: deco };
  }
  let drawn = {};  // vines already worked out, by size and seed: a page draws again every few seconds, the same each time
  function cached(key, make) { if (!drawn[key]) { if (Object.keys(drawn).length > 400) drawn = {}; drawn[key] = make(); } return drawn[key]; }
  // (``before``: where among el's children it goes, when not at the end)
  function put(el, cls, html, before) {
    const old = el.querySelector(":scope > ." + cls);
    if (!html) { if (old) old.remove(); return; }
    if (old && old.getAttribute("data-k") === html.k) return;
    const t = document.createElement("div"); t.innerHTML = html.svg;
    const svg = /** @type {Element} */ (t.firstChild); svg.setAttribute("data-k", html.k);
    if (old && !before) old.replaceWith(svg);
    else { if (old) { if (before === old) before = old.nextSibling; old.remove(); } el.insertBefore(svg, before || null); }
  }
  // (``only``, in cards(), meters() and boxes(): just these drawn again, the rest left as they are)
  const skip = function (only, el) { return only && only.indexOf(el) < 0; };
  // each card: its colour inside the vine, the vine on its border (its place among the page's cards picks its vine)
  function cards(root, only) {
    const list = Array.prototype.slice.call(root.querySelectorAll(".card"));
    const sizes = list.map(function (c) { return skip(only, c) ? null : [c.offsetWidth, c.offsetHeight, parseFloat(getComputedStyle(c).borderTopLeftRadius) || 12]; });
    const page = location.pathname;
    list.forEach(function (c, i) {
      if (!sizes[i]) return;
      const w = sizes[i][0], h = sizes[i][1], rad = sizes[i][2];
      if (!w || !h) return;
      const seed = hs(page + "#" + (c.id || i)), k = w + "x" + h + "x" + rad + "x" + seed;
      put(c, "vine", { k: k, svg: cached(k, function () {
        const g = edge(w, h, rad, seed, false);
        return '<svg class="vine" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true" focusable="false"><path d="' + g.d +
          '" fill="var(--panel)" stroke="var(--vine)" stroke-width="1.2" stroke-linejoin="round"/>' + g.deco("var(--vine-leaf)") + "</svg>";
      }) });
    });
  }
  // ------------------------------------------------------------------ grapes: progress bars
  // every progress bar (app.js: class "meter", but a usage meter's, "usage") is the leafy, calmer vine of the Decorating
  // Dionysus marks: a gently curving vine with a grape every 24 px, some hanging just off it, loose leaves and curls. The
  // grapes ripen with the bar's per cent, the current one turning from green to wine; with no count ("busy") a ripening
  // wave runs along them. Thin bars are drawn smaller.
  function vineShape(w) {
    const R = rng(41), base = 20, amp = 2.6, n = Math.max(4, Math.floor((w - 36) / 24) + 1), X = function (i) { return 18 + i * 24; };
    const A1 = amp * (.7 + R() * .6), A2 = amp * .45 * (.5 + R()), f1 = .021 + R() * .012, f2 = .05 + R() * .02, p1 = R() * 6.3, p2 = R() * 6.3;
    const vy = function (x) { return base + A1 * Math.sin(x * f1 + p1) + A2 * Math.sin(x * f2 + p2); };
    let dPath = "M4 " + r1(vy(4)), x, i;
    for (x = 7; x < w - 4; x += 3) dPath += "L" + x + " " + r1(vy(x));
    dPath += "L" + (w - 4) + " " + r1(vy(w - 4));
    const xs = [], ys = []; let o = "";
    for (i = 0; i < n; i++) {
      const gx = X(i) + (R() - .5) * 3, v = vy(gx), k = R(), side = k < .2 ? -1 : k < .4 ? 1 : 0, gy = v + side * 9.1;
      xs.push(r1(gx)); ys.push(r1(gy));
      if (side) o += '<path d="M' + r1(gx - side * 1.5) + " " + r1(v) + "Q" + r1(gx) + " " + r1(v + side * 1.5) + " " + r1(gx) + " " + r1(gy - side * 6.5) + '" stroke="var(--leaf)" stroke-width="1.3" fill="none"/>';
    }
    for (i = 0; i < n - 1; i++) {
      if (R() > .7) continue;
      const mx = (xs[i] + xs[i + 1]) / 2, mv = vy(mx), nbr = ys[i] < vy(xs[i]) - 1 || ys[i + 1] < vy(xs[i + 1]) - 1 ? 1 : -1, sd = R() < .5 ? nbr : -nbr;
      o += R() < .85 ? leafAt(mx - 4, mv + sd * 7.5, sd < 0 ? -25 - R() * 30 : 20 + R() * 30, 9, "var(--leaf)") : curlAt(mx - 2, mv, sd, "var(--leaf)");
    }
    return { vine: '<path d="' + dPath + '" stroke="var(--leaf)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>' + o, xs: xs, ys: ys };
  }
  function vineBar(w, pct, busy) {
    const sh = cached("vshape" + w, function () { return vineShape(w); }), n = sh.xs.length, done = pct / 100 * n; let g = "";
    sh.xs.forEach(function (x, i) {
      const y = sh.ys[i];
      if (busy) { g += '<circle cx="' + x + '" cy="' + y + '" r="6.5" fill="var(--unripe)"/><circle class="ripen" cx="' + x + '" cy="' + y + '" r="6.5" style="animation-delay:' + r1((Math.max(0, x - 10) / 200) % 1.6 - 1.6) + 's"/>'; return; }
      const f = Math.max(0, Math.min(1, done - i));
      g += '<circle cx="' + x + '" cy="' + y + '" r="6.5" fill="var(--unripe)"/>' + (f > 0 ? '<circle cx="' + x + '" cy="' + y + '" r="6.5" fill="var(--wine)"' + (f < 1 ? ' opacity="' + r1(f) + '"' : "") + "/>" : "");
    });
    return sh.vine + g;
  }
  function meters(root, only) {
    const list = Array.prototype.slice.call(root.querySelectorAll(".meter:not(.usage)")).filter(function (m) { return !skip(only, m); });
    const widths = list.map(function (m) { return m.clientWidth; });
    list.forEach(function (m, i) {
      const s = m.classList.contains("thin") ? .7 : 1, w = Math.round(widths[i] / s), busy = m.classList.contains("busy");
      if (!w) return;
      const bar = m.querySelector(":scope > div"); let pct = parseFloat(m.getAttribute("aria-valuenow"));
      if (isNaN(pct)) pct = bar ? parseFloat(bar.style.width) || 0 : 0;
      pct = Math.round(Math.max(0, Math.min(100, pct)) * 2) / 2;
      const k = w + "x" + s + (busy ? "b" : "p" + pct);
      put(m, "mvine", { k: k, svg: '<svg class="mvine" width="' + widths[i] + '" height="' + r1(38 * s) + '" viewBox="0 0 ' + w + ' 38" aria-hidden="true" focusable="false">' + vineBar(w, pct, busy) + "</svg>" });
    });
  }

  // the Kept and Lost boxes of a side-by-side card (app.js: cmp card): the cards' own vine line on their border, without
  // leaves, in the box's colour, the box filled out to it (their titles keep their plain line)
  function boxes(root, only) {
    const list = Array.prototype.slice.call(root.querySelectorAll(".mgkeep,.mgdrop")).filter(function (b) { return !skip(only, b); });
    const sizes = list.map(function (b) { const cs = getComputedStyle(b); return [b.offsetWidth, b.offsetHeight, parseFloat(cs.borderTopLeftRadius) || 10, parseFloat(cs.borderTopWidth) || 0]; });
    list.forEach(function (b, i) {
      const w = sizes[i][0], h = sizes[i][1], rad = sizes[i][2], bw = sizes[i][3];
      if (!w || !h) return;
      const k = "b" + w + "x" + h + "x" + rad + "x" + bw;
      put(b, "edgevine", { k: k, svg: cached(k, function () {
        const g = edge(w, h, rad, 3, true);
        return '<svg class="edgevine" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h + '" style="left:' + (-bw) + "px;top:" + (-bw) + 'px" aria-hidden="true" focusable="false"><path d="' + g.d +
          '" fill="var(--panel)" stroke="var(--edge)" stroke-width="1.6" stroke-linejoin="round"/></svg>';
      }) });
    });
  }
  // the app goes to the window's edges (as Classic): its vine is the one down the seam between the menu and the page
  function frame() {
    const shell = document.querySelector(".shell");
    if (!shell) return;
    if (!on("vines")) { put(shell, "seamvine", null); return; }
    seam(shell);
  }
  // down the seam between the menu and the page, held beside the menu so it stays as the page scrolls (along the menu's
  // foot when the menu sits across the top, on a phone)
  function seam(shell) {
    const side = shell.querySelector(".side");
    if (!side) return;
    const across = side.offsetTop + side.offsetHeight <= (shell.querySelector(".main") || side).offsetTop + 1;
    const L = across ? side.offsetWidth : side.offsetHeight, X = across ? side.offsetHeight : 6, k = (across ? "a" : "d") + L + "x" + X;
    put(shell, "seamvine", { k: k, svg: cached("seam" + k, function () {
      const R = rng(19), n1 = Math.max(1, Math.round(L / (80 + R() * 30))), n2 = Math.max(3, Math.round(L / (30 + R() * 8))), p1 = R() * 6.3, p2 = R() * 6.3;
      const off = function (t) { return 1.1 * Math.sin(t / L * n1 * 2 * Math.PI + p1) + .45 * Math.sin(t / L * n2 * 2 * Math.PI + p2); };
      const along = function (t) { return across ? [t, X + off(t)] : [X + off(t), t]; }; let path = "";
      for (let t = 0; t <= L; t += L > 6000 ? 4 : 2.5) { const p = along(t); path += (t ? "L" : "M") + r1(p[0]) + " " + r1(p[1]); }
      // the menu's colour up to the vine and the page's from it, over the straight edge between them, so the vine divides them
      const W = 4, end = across ? "L" + L + " " + (X - W) + "L0 " + (X - W) + "z" : "L" + (X - W) + " " + L + "L" + (X - W) + " 0z";
      let o = (across ? '<rect x="0" y="' + (X - W) + '" width="' + L + '" height="' + 2 * W + '" fill="var(--bg)"/>'
        : '<rect x="' + (X - W) + '" y="0" width="' + 2 * W + '" height="' + L + '" fill="var(--bg)"/>') +
        '<path d="' + path + end + '" fill="var(--side)"/>' +
        '<path d="' + path + '" fill="none" stroke="var(--menu-vine)" stroke-width="1.3" stroke-linecap="round"/>';
      for (let t = 40 + R() * 200; t < L - 24; t += 178 + R() * 378) {
        const q = along(t), sd = R() < .5 ? -1 : 1, base = across ? 0 : 90;
        o += R() < .7 ? leafOn(q[0], q[1], base + sd * (40 + R() * 20) * (across ? -1 : 1), 7 + R() * 2, "var(--menu-vine)")
          : '<g transform="rotate(' + base + " " + r1(q[0]) + " " + r1(q[1]) + ')">' + curlAt(q[0], q[1], sd, "var(--menu-vine)") + "</g>";
      }
      // across: over the top of the page; down: an item of the app's row just after the menu, held as the menu is, 12 wide
      // (a browser doesn't draw an svg of no size) and taking no room: the seam 6 in
      return '<svg class="seamvine ' + (across ? "across" : "down") + '" width="' + (across ? L : 2 * X) + '" height="' + (across ? X + 12 : L) + '" aria-hidden="true" focusable="false">' + o + "</svg>";
    }) }, across ? null : side.nextSibling);
  }

  // ------------------------------------------------------------------ film
  const GLYPH = [-64, -32, 0, 32, 64].map(function (a) { return '<ellipse cx="0" cy="-5" rx="2.3" ry="' + (a === 0 ? 6.2 : 5.4) + '" transform="rotate(' + a + ' 0 6)" fill="var(--film-mark)"/>'; }).join("") +
    '<path d="M-7 9Q0 3 7 9" fill="none" stroke="var(--film-mark)" stroke-width="2" stroke-linecap="round"/>';
  const IONIC = '<path d="M-9-7H9M-4-5V6M0-5V6M4-5V6M-7 8.5H7" fill="none" stroke="var(--hole)" stroke-width="2" stroke-linecap="round"/><circle cx="-8" cy="-4" r="2.2" fill="none" stroke="var(--hole)" stroke-width="1.6"/><circle cx="8" cy="-4" r="2.2" fill="none" stroke="var(--hole)" stroke-width="1.6"/>';
  const WAVES = [[.5, 1.25, .25], [.28, 1.25, .25], [.5, .75, .2], [.36, 2, .25]];
  // a band of film seen in 3D: centreline c(t), width w, tilt k(t) (0: edge on); its sprocket holes and symbols foreshortened
  function ribbon(c, w, tw, n, pitch) {
    const P = [], TH = 1.4; let i, o = "";
    for (i = 0; i <= n; i++) {
      const t = i / n, a = c(Math.max(0, t - .002)), b = c(Math.min(1, t + .002)), p = c(t); let tx = b[0] - a[0], ty = b[1] - a[1]; const L = Math.hypot(tx, ty) || 1;
      tx /= L; ty /= L; P.push({ p: p, T: [tx, ty], N: [-ty, tx], w: w, k: tw(t) });
    }
    const pt = function (q, u, v) { return [q.p[0] + q.N[0] * u + q.T[0] * v, q.p[1] + q.N[1] * u + q.T[1] * v]; };
    const poly = function (arr, fill, extra) { return '<path d="M' + arr.map(function (a) { return r1(a[0]) + " " + r1(a[1]); }).join("L") + 'z" fill="' + fill + '" ' + (extra || "") + "/>"; };
    const hh = function (q) { return Math.max(q.w / 2 * Math.abs(q.k), TH); };
    for (i = 0; i < n; i++) { const A = P[i], B = P[i + 1]; o += poly([pt(A, hh(A), 0), pt(B, hh(B), 0), pt(B, -hh(B), 0), pt(A, -hh(A), 0)], "var(--film)", 'stroke="var(--film)" stroke-width=".6" stroke-linejoin="round"'); }
    let len = 0; const marks = [];
    for (i = 1; i <= n; i++) { len += Math.hypot(P[i].p[0] - P[i - 1].p[0], P[i].p[1] - P[i - 1].p[1]); marks.push(len); }
    const atD = function (dd) { let j = 0; while (j < marks.length && marks[j] < dd) j++; return j < marks.length ? P[j + 1] : null; };
    const hstep = pitch / 4; let dd, Q;
    for (dd = hstep / 2; dd < len; dd += hstep) {
      Q = atD(dd); if (!Q) break; const ak = Math.abs(Q.k); if (ak < .2) continue;
      const hl = Q.w * .05, hw = Math.max(.5, Q.w * .045 * ak);
      [1, -1].forEach(function (sd) { const u = sd * Q.w / 2 * ak * .8; o += poly([pt(Q, u - hw, -hl), pt(Q, u + hw, -hl), pt(Q, u + hw, hl), pt(Q, u - hw, hl)], "var(--hole)"); });
    }
    for (dd = pitch * .5; dd < len - pitch * .3; dd += pitch) {
      Q = atD(dd); if (!Q) break; if (Math.abs(Q.k) < .06) continue; const sc = Q.w / 26;
      o += '<g transform="matrix(' + r1(Q.T[0] * sc) + " " + r1(Q.T[1] * sc) + " " + r1(Q.N[0] * sc * Q.k) + " " + r1(Q.N[1] * sc * Q.k) + " " + r1(Q.p[0]) + " " + r1(Q.p[1]) + ')">' + GLYPH + "</g>";
    }
    return o;
  }
  // the reel: its face in the film's colour, five column cutouts, a cut ring, the hub ring and the peg hole
  function reel(cx, cy, R) {
    const sc = R / 38; let o = '<circle cx="' + cx + '" cy="' + cy + '" r="' + R + '" fill="var(--film)"/><g transform="translate(' + r1(cx - 39 * sc) + " " + r1(cy - 39 * sc) + ") scale(" + r1(sc) + ')">';
    for (let i = 0; i < 5; i++) { const ang = -90 + i * 72, x = r1(39 + 23 * Math.cos(ang * Math.PI / 180)), y = r1(39 + 23 * Math.sin(ang * Math.PI / 180)); o += '<g transform="translate(' + x + " " + y + ") rotate(" + (ang + 90) + ') scale(.8)">' + IONIC + "</g>"; }
    const ring = function (r) { return '<circle cx="39" cy="39" r="' + r + '" fill="none" stroke="var(--hole)" stroke-width="1.6"/>'; };
    return o + ring(33.5) + '<circle cx="39" cy="39" r="4.5" fill="var(--hole)"/>' + ring(8) + "</g>";
  }
  let maskN = 0;
  function band(name, W) {
    const r = 20, cy = r + 4, x0 = r + 4, L = W - x0 - 2, q = (600 - x0 - 2) / L, D = 2 * r, tr = r / L, y0 = cy - r + .8 * D, bw = .2 * D, pitch = 22 * r / 34;
    const R = rng(hs(name)), wv = WAVES[Math.floor(R() * WAVES.length)], j = function () { return .9 + R() * .2; };
    const amp = Math.min(.55, wv[0] * j()), cyc = wv[1] * j() / q, kd = wv[2] * j();
    const X = function (t) { return x0 + t * L; }, open = function (t) { return ease(tr * .5, tr + .1 * q, t); };
    const c = function (t) { return [X(t), y0 - (D * amp) * (.5 - .5 * Math.cos((t - tr) * Math.PI * 2 * cyc)) * ease(tr, tr + .15 * q, t)]; };
    const k = function (t) { return open(t) * (1 - kd + kd * Math.cos((t - tr) * Math.PI * 4 * cyc)); };
    const inner = ribbon(c, bw, k, Math.round(W / 2.3), pitch) + reel(x0, cy, r), H = 2 * r + 8, id = "pcfilm" + (++maskN);
    // the sprocket holes and the reel's cutouts are see-through: a mask with them black
    return '<svg class="filmband" viewBox="0 0 ' + W + " " + H + '" width="' + W + '" height="' + H + '" aria-hidden="true" focusable="false">' +
      '<mask id="' + id + '" maskUnits="userSpaceOnUse" x="-50" y="-50" width="' + (W + 100) + '" height="' + (H + 100) + '"><g style="--film:#fff;--film-mark:#fff;--hole:#000">' + inner + "</g></mask>" +
      '<g mask="url(#' + id + ')" style="--hole:var(--film)">' + inner + "</g></svg>";
  }
  function film(root) {
    const top = root.querySelector(":scope > .top"), old = root.querySelector(":scope > .filmline");
    if (!on("film") || !top) { if (old) old.remove(); return; }
    const W = Math.round(top.offsetWidth), name = location.pathname, k = W + name;
    if (W < 120) return;
    if (old && old.getAttribute("data-k") === k) return;
    const div = document.createElement("div");
    div.className = "filmline"; div.setAttribute("data-k", k);
    div.innerHTML = cached("film" + k, function () { return band(name, W); });
    if (old) old.replaceWith(div); else top.insertAdjacentElement("afterend", div);
  }

  // ------------------------------------------------------------------ when to draw
  let main = null, busy = false;
  function paint(root) {
    root = root || main || document.querySelector(".main") || document.body;
    if (!root) return;
    main = root;
    busy = true;
    try {
      film(root);
      if (on("vines")) { cards(root); boxes(root); }
      else root.querySelectorAll(".card > .vine").forEach(function (v) { v.remove(); });
      if (on("grapes")) meters(root);
      frame();
    } finally { busy = false; }
    watch(root);
  }
  let queued = false;
  function soon() { if (queued) return; queued = true; requestAnimationFrame(function () { queued = false; paint(); }); }
  const DRAWN = ".vine,.edgevine,.mvine,.seamvine,.filmline";
  const ours = function (n) { return n.nodeType === 1 && n.matches(DRAWN); };
  const allOurs = function (nodes) { for (let j = 0; j < nodes.length; j++) if (!ours(nodes[j])) return false; return true; };
  if (window.MutationObserver) {
    new MutationObserver(function (list) {
      if (busy) return;
      // only changes to a page's own content: not the vines and film this file adds (inside them, or put in or taken out)
      for (let i = 0; i < list.length; i++) {
        const r = list[i], t = /** @type {Element} */ (r.target);
        if (t.closest && t.closest(DRAWN)) continue;
        if ((r.addedNodes.length || r.removedNodes.length) && allOurs(r.addedNodes) && allOurs(r.removedNodes)) continue;
        soon(); return;
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
  // A size changed (the window, a card's content wrapping, the menu folded or opened): only what's that size now is
  // drawn again (ResizeObserver), not the whole page. While the menu's width eases, nothing: the end draws it all again.
  let easing = false; const sized = new WeakMap(), ro = window.ResizeObserver ? new ResizeObserver(resized) : null;
  const size = function (el) { return el.offsetWidth + "x" + el.offsetHeight; };
  function watch(root) {
    if (!ro) return;
    ro.disconnect();
    const els = Array.prototype.slice.call(root.querySelectorAll(".card,.mgkeep,.mgdrop,.meter:not(.usage),:scope > .top"));
    const side = document.querySelector(".shell > .side");
    if (side) els.push(side);
    // (its border box: a padding or border that changes moves the vine too; a browser without the choice watches the inside)
    els.forEach(function (el) { sized.set(el, size(el)); ro.observe(el, { box: "border-box" }); });
  }
  function resized(entries) {
    if (busy || easing || !main) return;
    const c = [], b = [], m = []; let rest = false;
    entries.forEach(function (e) {
      const el = e.target, s = size(el);
      if (sized.get(el) === s || !el.isConnected) return;
      sized.set(el, s);
      const cl = el.classList;
      if (cl.contains("card")) c.push(el); else if (cl.contains("meter")) m.push(el); else if (cl.contains("mgkeep") || cl.contains("mgdrop")) b.push(el); else rest = true;
    });
    if (!c.length && !b.length && !m.length && !rest) return;
    busy = true;
    try {
      if (on("vines")) { if (c.length) cards(main, c); if (b.length) boxes(main, b); }
      if (on("grapes") && m.length) meters(main, m);
      if (rest) { film(main); frame(); }
    } finally { busy = false; }
  }
  if (!ro) window.addEventListener("resize", soon);
  const isSide = function (e) { return e.target && e.target.classList && e.target.classList.contains("side"); };
  document.addEventListener("transitionrun", function (e) { if (isSide(e)) easing = true; });
  document.addEventListener("transitioncancel", function (e) { if (isSide(e)) { easing = false; soon(); } });
  // the menu folded or opened (its width eases): the page beside it is another width, its vines drawn again
  document.addEventListener("transitionend", function (e) { if (isSide(e)) { easing = false; soon(); } });
  // after the theme changes (theme.js): the spots' colours, and everything drawn again
  function themed() { drawn = {}; spots(); soon(); }
  window.PCDECOR = { paint: paint, themed: themed };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", themed); else themed();
})();
