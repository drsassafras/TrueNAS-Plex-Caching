// The style sample's pictures (dev/style.py sample starts the demos and runs this; not run by hand): each look that
// differs from the style guide, found on the demo's pages and photographed Before (the app as approved) and After (now),
// in Classic and Dionysus, light and dark, and the two compared pixel for pixel.
//
//     node dev/style-sample.js <out dir>        # reads <out>/plan.json (dev/style.py writes it)
//
// The Before is the same demo's data with the approved app's pages, scripts, styles and themes: its requests for those
// go to the Before demo (plan.before), everything else to the After one, so both sides draw the same data. Both sides
// see the same moment (plan.time), so "3 min ago" reads alike.
//
// Needs Playwright, as dev/screenshots.js does (npm i -g playwright && npx playwright install chromium; style.py passes
// NODE_PATH). It says how it's going on stdout, a line of JSON each, for style.py's progress bar (dev/progress.py):
// {"total": n} first, {"page": "<what was looked at>"} after each page, and last
// {"shots": {"<id>": [{"label", "before", "after", "same", "diff"}, ...]}}: the pictures of each look, two by two.
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [OUT] = process.argv.slice(2);
const plan = JSON.parse(fs.readFileSync(path.join(OUT, "plan.json"), "utf-8"));
const say = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const lookName = (l) => (l.theme === "classic" ? "Classic" : "Dionysus") + " " + l.mode;
const WAIT = 2000;   // the page's first draw and its decorations (decor.js paints on the next frames)
const SIDES = plan.before ? ["before", "after"] : ["after"];
const AFTER = new URL(plan.after).origin;

// side "before": the approved app's files over today's data (judge.only: just those paths of it; judge.except: all
// but those: "document" the page itself, a path ending in / all under it).
async function open(browser, look, page, width, side, judge) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2, colorScheme: look.mode });
  await ctx.clock.setFixedTime(plan.time);
  await ctx.addInitScript((o) => { try { localStorage.setItem("pc-theme", JSON.stringify(o)); } catch { /* none */ } }, look);
  if (side === "before") {
    await ctx.route("**/*", async (route) => {
      const req = route.request(), u = new URL(req.url());
      const app = u.pathname.startsWith("/static/") || u.pathname === "/theme.css" || u.pathname === "/theme.js";
      const j = judge || {};
      const doc = req.resourceType() === "document";
      const kept = (j.except || []).some((e) => (e === "document" ? doc : e.endsWith("/") ? u.pathname.startsWith(e) : u.pathname === e));
      const mine = j.only ? j.only.includes(u.pathname) : (app || doc) && !kept;
      if (u.origin !== AFTER || !mine) return route.continue();
      try {
        return route.fulfill({ response: await route.fetch({ url: plan.before + u.pathname + u.search }) });
      } catch { return route.abort(); }
    });
  }
  const p = await ctx.newPage();
  await p.goto(plan.after + "/" + page);
  await p.waitForTimeout(WAIT);
  return { ctx, p };
}

// Marks the first element on the page that wears the look, and is drawn; false if there's none.
async function find(p, f) {
  return p.evaluate((f) => {
    const seen = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    let els = [];
    if (f.how === "selector") {
      for (const s of f.selectors) { try { els = els.concat([...document.querySelectorAll(s)]); } catch { /* a selector the browser can't take */ } }
    } else if (f.how === "style") {
      const re = new RegExp(f.pattern);
      els = [...document.querySelectorAll("[style]")].filter((el) => re.test((el.getAttribute("style") || "").replace(/\s*([:;,])\s*/g, "$1")));
    }
    const el = els.find(seen);
    document.querySelectorAll("[data-style-sample]").forEach((x) => x.removeAttribute("data-style-sample"));
    if (!el) return false;
    el.setAttribute("data-style-sample", "");
    return true;
  }, f);
}

// Where the marked element is on the page (page coordinates), or null.
async function box(p) {
  return p.evaluate(() => {
    const el = document.querySelector("[data-style-sample]");
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return r.width && r.height ? { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height } : null;
  });
}

// The same piece of the page on every side, round the look's element wherever it is on either, even on a side it isn't
// on (that spot of the page then, for what was there). Never narrower than MIN_W nor taller than MAX_H (CSS pixels), so
// a tall, thin element (a vine down the page's edge) comes with the page beside it, not stretched into a strip.
const MIN_W = 640, MAX_H = 440, PAD = 24;
async function shoot(sides, file) {
  const boxes = Object.values(sides).map((s) => s.box).filter(Boolean);
  if (!boxes.length) return {};
  const left = Math.min(...boxes.map((b) => b.x)) - PAD, top = Math.min(...boxes.map((b) => b.y)) - PAD;
  const right = Math.max(...boxes.map((b) => b.x + b.w)) + PAD, bottom = Math.max(...boxes.map((b) => b.y + b.h)) + PAD;
  const out = { clip: null };
  for (const [side, s] of Object.entries(sides)) {
    const size = await s.p.evaluate(() => ({ w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight }));
    const width = Math.min(size.w, Math.max(MIN_W, right - left));
    const x = Math.max(0, Math.min(size.w - width, (left + right) / 2 - width / 2));
    const y = Math.max(0, top);
    const clip = { x, y, width, height: Math.max(1, Math.min(size.h - y, bottom - y, MAX_H)) };
    out.clip = out.clip || clip;   // (where on the page it is: dev/style.py puts crops of one spot on one card)
    try { await s.p.screenshot({ path: path.join(OUT, file(side)), clip, fullPage: true, timeout: 15000 }); out[side] = file(side); }
    catch { out[side] = null; }   // (it couldn't draw)
  }
  return out;
}

// Before and After pixel for pixel: the same (no pixel off), or not, with a picture of the After that marks what's
// different (the rest faded).
let judge = null;
async function compare(before, after, diffFile) {
  if (!before || !after) return { same: false };
  const a = fs.readFileSync(path.join(OUT, before)), b = fs.readFileSync(path.join(OUT, after));
  if (a.equals(b)) return { same: true };
  const r = await judge.evaluate(async ([a, b]) => {
    const load = (src) => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = no; i.src = src; });
    const [ia, ib] = await Promise.all([load(a), load(b)]);
    const w = Math.max(ia.width, ib.width), h = Math.max(ia.height, ib.height);
    const px = (img) => { const c = document.createElement("canvas"); c.width = w; c.height = h; const g = c.getContext("2d"); g.drawImage(img, 0, 0); return g.getImageData(0, 0, w, h); };
    const da = px(ia), db = px(ib), out = new ImageData(w, h);
    let off = 0;
    for (let i = 0; i < da.data.length; i += 4) {
      const d = Math.abs(da.data[i] - db.data[i]) + Math.abs(da.data[i + 1] - db.data[i + 1]) + Math.abs(da.data[i + 2] - db.data[i + 2]) + Math.abs(da.data[i + 3] - db.data[i + 3]);
      if (d > 0) { off++; out.data.set([230, 20, 60, 255], i); }
      else { const v = 255 - (255 - (db.data[i] + db.data[i + 1] + db.data[i + 2]) / 3) * 0.25; out.data.set([v, v, v, 255], i); }
    }
    if (!off && ia.width === ib.width && ia.height === ib.height) return { same: true };
    const c = document.createElement("canvas"); c.width = w; c.height = h; c.getContext("2d").putImageData(out, 0, 0);
    return { same: false, png: c.toDataURL("image/png").split(",")[1] };
  }, ["data:image/png;base64," + a.toString("base64"), "data:image/png;base64," + b.toString("base64")]);
  if (r.same) return { same: true };
  fs.writeFileSync(path.join(OUT, diffFile), Buffer.from(r.png, "base64"));
  return { same: false, diff: diffFile };
}

// A shipped image (theatre.svg and the like) as it draws, Before and After, at 2x on a plain card.
async function drawFile(browser, file, out) {
  if (!file) return null;
  const p = await browser.newPage({ viewport: { width: 200, height: 200 }, deviceScaleFactor: 2 });
  const mime = file.endsWith(".svg") ? "image/svg+xml" : "image/" + path.extname(file).slice(1);
  await p.setContent(`<body style="margin:0;display:grid;place-items:center;height:100vh;background:#fff"><img style="max-width:160px;max-height:160px" src="data:${mime};base64,${fs.readFileSync(file).toString("base64")}"></body>`);
  await p.waitForTimeout(100);
  await p.screenshot({ path: path.join(OUT, out) });
  await p.close();
  return out;
}

(async () => {
  const browser = await chromium.launch();
  judge = await browser.newPage();
  const items = plan.items.filter((i) => i.find.how === "selector" || i.find.how === "style");
  const images = plan.items.filter((i) => i.find.how === "image");
  const pages = plan.items.some((i) => i.find.how === "pages");
  const first = plan.looks.find((l) => l.theme === "dionysus" && l.mode === "light") || plan.looks[0];
  // Where each look is: the first page (in Dionysus light) that draws it, after (or, for a look that's gone, before).
  const where = {};
  const groups = {};   // page + width -> the items photographed there
  const sides = [...new Set(items.map((i) => i.side))];
  const variants = new Set(plan.items.filter((i) => i.find.how === "pages").map((i) => JSON.stringify(i.find.judge))).size;
  let total = (items.length ? plan.pages.length * sides.length : 0) + (pages ? plan.fixed.length * plan.looks.length : 0);
  say({ total: total });
  for (const side of sides) {
    for (const pg of plan.pages) {
      const { ctx, p } = await open(browser, first, pg, 1280, side);
      for (const it of items) if (it.side === side && !(it.id in where) && await find(p, it.find)) where[it.id] = pg;
      await ctx.close();
      say({ page: "finding the looks: " + pg + (side === "before" ? " (before)" : "") });
    }
  }
  for (const it of items) {
    if (!(it.id in where)) continue;
    const key = where[it.id] + " " + (it.find.width || 1280) + " " + JSON.stringify(it.find.judge || null);   // (one Before per kind)
    (groups[key] = groups[key] || []).push(it);
  }
  total += Object.keys(groups).length * plan.looks.length;
  say({ total: total, more: true });
  const shots = {};
  const keep = async (id, label, before, after, tag, clip) => {
    const c = await compare(before, after, tag + "-diff.png");
    (shots[id] = shots[id] || []).push({ label, before, after, same: c.same, diff: c.diff || null, clip: clip || null });
  };
  for (const im of images) {
    const tag = "look" + im.id + "-file";
    const before = await drawFile(browser, im.find.files.before, tag + "-before.png");
    const after = await drawFile(browser, im.find.files.after, tag + "-after.png");
    const label = "the file " + im.find.path + (im.find.instead ? " (Before: " + im.find.instead + ", which it stands in for)" : "");
    if (before || after) await keep(im.id, label, before, after, tag);
  }
  for (const look of plan.looks) {
    const tag = look.theme + "-" + look.mode;
    for (const [key, list] of Object.entries(groups)) {
      const [pg, width, ...rest] = key.split(" ");
      const judge = JSON.parse(rest.join(" "));
      const open_ = {};
      for (const side of SIDES) open_[side] = await open(browser, look, pg, Number(width), side, judge);
      for (const it of list) {
        const sides = {};
        for (const side of SIDES) {
          const p = open_[side].p;
          sides[side] = { p, box: (await find(p, it.find)) ? await box(p) : null };
        }
        const got = await shoot(sides, (side) => "look" + it.id + "-" + tag + "-" + side + ".png");
        if (got.before || got.after) await keep(it.id, lookName(look) + " · /" + pg, got.before || null, got.after || null, "look" + it.id + "-" + tag, got.clip);
      }
      for (const side of SIDES) await open_[side].ctx.close();
      say({ page: lookName(look) + ": " + pg });
    }
    if (pages) {
      // Each look seen on whole pages is judged against a Before of its own (item.find.judge: dev/style.py judged_by).
      const variants = {};
      for (const it of plan.items) if (it.find.how === "pages") variants[JSON.stringify(it.find.judge)] = it.find.judge;
      for (const pg of plan.fixed) {
        const file = (side, v) => "page-" + pg + "-" + tag + "-" + side + (v ? "-" + v : "") + ".png";
        const after = file("after");
        let ctx, p;
        ({ ctx, p } = await open(browser, look, pg, 1280, "after"));
        await p.screenshot({ path: path.join(OUT, after) });
        await ctx.close();
        const pair = {};
        let n = 0;
        for (const [key, judge] of Object.entries(variants)) {
          if (!plan.before) { pair[key] = { before: null, same: false, diff: null }; continue; }
          const v = String(++n);
          let before = file("before", v);
          ({ ctx, p } = await open(browser, look, pg, 1280, "before", judge));
          try { await p.screenshot({ path: path.join(OUT, before), timeout: 15000 }); } catch { before = null; }   // (it couldn't draw)
          await ctx.close();
          const c = await compare(before, after, file("diff", v));
          pair[key] = { before, same: c.same, diff: c.diff || null };
        }
        for (const it of plan.items) {
          if (it.find.how !== "pages") continue;
          const m = /^theme (\w+)(?:\/(\w+))? /.exec(it.look);   // a theme's look: only in that theme (and mode)
          if (m && m[1] !== "font" && (m[1] !== look.theme || (m[2] && m[2] !== look.mode))) continue;
          const b = pair[JSON.stringify(it.find.judge)];
          (shots[it.id] = shots[it.id] || []).push({ label: lookName(look) + " · /" + pg, before: b.before, after, same: b.same, diff: b.diff });
        }
        say({ page: lookName(look) + ": " + pg });
      }
    }
  }
  await browser.close();
  say({ shots: shots });
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
