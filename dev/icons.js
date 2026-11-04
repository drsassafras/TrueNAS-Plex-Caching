// The app's icons, each drawn by Chromium from the logo the app already shows at that size, exactly as a browser draws
// it: the tab icon (plexcache/web/theatre-tab.svg: its Joined D at 16px, its Wide bar from 24px up) for favicon.ico, and
// the full theatre playing (plexcache/web/theatre-playing.svg: the menu's logo with its arrow lit, drawn for a dark
// ground) for the large icons. Each mark
// is centred on what it draws, not on its box (the theatre's dots sit right of its middle).
//
//     NODE_PATH=$(npm root -g) node dev/icons.js          # writes them into plexcache/web/
//     NODE_PATH=$(npm root -g) node dev/icons.js check    # exit 1 if one isn't what the tab icon draws now (IconTests)
//
// Run it after changing either logo, then show the maintainer the style sample (dev/style.py sample): the icons are
// in the style guide like every image the app ships. Needs Playwright, as dev/screenshots.js does
// (npm ci --prefix dev && npx --prefix dev playwright install chromium).
//
//   favicon.ico            the tab icon at 16, 32 and 48px, light colours, see-through: browsers without SVG favicons
//                          ask for /favicon.ico
//   apple-touch-icon.png   the theatre playing at 180px, on the menu's colour: iOS fills see-through with black, and rounds the
//                          corners itself
//   icon-192.png, icon-512.png   "Install app" (manifest.webmanifest, purpose "any"): the theatre on a rounded tile in the
//                          menu's colour
//   icon-maskable-512.png  the same for Android's shaped icons (purpose "maskable"): full bleed, the theatre inside the
//                          middle 80% circle every mask keeps
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const WEB = path.join(__dirname, "..", "plexcache", "web");
const TAB = path.join(WEB, "theatre-tab.svg");
const THEATRE = path.join(WEB, "theatre-playing.svg");
const TILE = "#1B1F1D";   // Dionysus's menu (themes.py "side"), and the manifest's theme_color

// name -> how it's drawn: the logo, its size, the share of it the mark's drawn shape fills, the colours it wears, the
// tile behind it (none: see-through)
const ICONS = {
  "apple-touch-icon.png": { source: THEATRE, size: 180, mark: 0.74, scheme: "dark", tile: TILE, radius: 0 },
  "icon-192.png": { source: THEATRE, size: 192, mark: 0.72, scheme: "dark", tile: TILE, radius: 0.22 },
  "icon-512.png": { source: THEATRE, size: 512, mark: 0.72, scheme: "dark", tile: TILE, radius: 0.22 },
  "icon-maskable-512.png": { source: THEATRE, size: 512, mark: 0.56, scheme: "dark", tile: TILE, radius: 0 },
};
const ICO = { name: "favicon.ico", source: TAB, sizes: [16, 32, 48], scheme: "light" };

// The logo drawn at `px` in a size x size square at (x, y), on the tile; with `bounds`, the box of what it drew instead.
async function render(browser, svg, size, px, x, y, { scheme, tile, radius }, bounds) {
  const page = await browser.newPage({ viewport: { width: size, height: size }, deviceScaleFactor: 1, colorScheme: scheme });
  const back = tile && !bounds ? `background:${tile};border-radius:${Math.round(size * radius)}px;` : "";
  await page.setContent(`<body style="margin:0;background:transparent"><div id="i" style="position:relative;overflow:hidden;width:${size}px;height:${size}px;${back}">`
    + `<img style="position:absolute;left:${x}px;top:${y}px" width="${px}" height="${px}" src="data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}"></div></body>`);
  await page.waitForFunction(() => document.querySelector("img").complete);
  const png = await page.locator("#i").screenshot({ omitBackground: true });
  const out = bounds ? await page.evaluate(async (b64) => {
    const im = await createImageBitmap(await (await fetch("data:image/png;base64," + b64)).blob());
    const c = new OffscreenCanvas(im.width, im.height), g = c.getContext("2d");
    g.drawImage(im, 0, 0);
    const d = g.getImageData(0, 0, im.width, im.height).data;
    let l = im.width, t = im.height, r = -1, btm = -1;
    for (let j = 0; j < im.height; j++) for (let i = 0; i < im.width; i++) {
      if (d[(j * im.width + i) * 4 + 3] > 8) { l = Math.min(l, i); r = Math.max(r, i); t = Math.min(t, j); btm = Math.max(btm, j); }
    }
    return { l, t, w: r - l + 1, h: btm - t + 1 };
  }, png.toString("base64")) : png;
  await page.close();
  return out;
}

// The mark centred on what it draws (its drawn shape's box, not the SVG's), that box filling `mark` of the square.
async function draw(browser, svg, how) {
  const { size, mark = 1 } = how;
  // where its shape falls when the logo fills the square (a tab icon picks its design by the size it's drawn at: measured
  // at the size it'll be drawn)
  const guess = Math.round(size * mark);
  const box = await render(browser, svg, size, guess, 0, 0, how, true);
  const scale = Math.min(size * mark / box.w, size * mark / box.h);
  const px = mark === 1 ? guess : Math.round(guess * scale);
  const k = px / guess;
  const x = Math.round((size - box.w * k) / 2 - box.l * k), y = Math.round((size - box.h * k) / 2 - box.t * k);
  return render(browser, svg, size, px, x, y, how, false);
}

// An .ico of PNGs (every browser that reads .ico reads PNG in it): the header, a directory entry each, the images.
function ico(pngs) {
  const head = Buffer.alloc(6 + 16 * pngs.length);
  head.writeUInt16LE(0, 0); head.writeUInt16LE(1, 2); head.writeUInt16LE(pngs.length, 4);
  let offset = head.length;
  pngs.forEach(({ size, png }, i) => {
    const e = 6 + 16 * i;
    head.writeUInt8(size % 256, e); head.writeUInt8(size % 256, e + 1);
    head.writeUInt16LE(1, e + 4); head.writeUInt16LE(32, e + 6);
    head.writeUInt32LE(png.length, e + 8); head.writeUInt32LE(offset, e + 12);
    offset += png.length;
  });
  return Buffer.concat([head, ...pngs.map((p) => p.png)]);
}

function icoImages(buf) {
  const n = buf.readUInt16LE(4);
  return Array.from({ length: n }, (_, i) => buf.subarray(buf.readUInt32LE(6 + 16 * i + 12), buf.readUInt32LE(6 + 16 * i + 12) + buf.readUInt32LE(6 + 16 * i + 8)));
}

// The same picture, give or take a step of antialiasing (another Chromium may round an edge pixel differently).
async function alike(browser, a, b) {
  const page = await browser.newPage();
  const same = await page.evaluate(async ([a, b]) => {
    const px = async (b64) => {
      const im = await createImageBitmap(await (await fetch("data:image/png;base64," + b64)).blob());
      const c = new OffscreenCanvas(im.width, im.height), x = c.getContext("2d");
      x.drawImage(im, 0, 0);
      return { w: im.width, h: im.height, d: x.getImageData(0, 0, im.width, im.height).data };
    };
    const [p, q] = [await px(a), await px(b)];
    if (p.w !== q.w || p.h !== q.h) return false;
    let off = 0;
    for (let i = 0; i < p.d.length; i++) if (Math.abs(p.d[i] - q.d[i]) > 24) off++;
    return off <= p.d.length * 0.002;
  }, [a.toString("base64"), b.toString("base64")]);
  await page.close();
  return same;
}

(async () => {
  const checking = process.argv[2] === "check";
  const read = (f) => fs.readFileSync(f, "utf-8");
  const browser = await chromium.launch();
  const made = {};
  for (const [name, how] of Object.entries(ICONS)) made[name] = await draw(browser, read(how.source), how);
  const frames = [];
  for (const size of ICO.sizes) frames.push({ size, png: await draw(browser, read(ICO.source), { size, scheme: ICO.scheme }) });
  made[ICO.name] = ico(frames);
  const off = [];
  for (const [name, buf] of Object.entries(made)) {
    const file = path.join(WEB, name);
    if (!checking) { fs.writeFileSync(file, buf); continue; }
    if (!fs.existsSync(file)) { off.push(`${name}: missing`); continue; }
    const now = fs.readFileSync(file);
    const pairs = name === ICO.name ? icoImages(now).map((p, i) => [p, frames[i] && frames[i].png]) : [[now, buf]];
    if (pairs.length !== (name === ICO.name ? frames.length : 1) || !pairs.every((p) => p[1])) { off.push(`${name}: not the sizes it should be`); continue; }
    for (const [a, b] of pairs) if (!(await alike(browser, a, b))) { off.push(`${name}: not what its logo draws now`); break; }
  }
  await browser.close();
  if (off.length) {
    process.stdout.write(off.join("\n") + "\nRemake them: NODE_PATH=$(npm root -g) node dev/icons.js\n");
    process.exit(1);
  }
  if (!checking) process.stdout.write(Object.keys(made).map((n) => "plexcache/web/" + n).join("\n") + "\n");
})();
