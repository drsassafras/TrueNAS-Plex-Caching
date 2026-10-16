// Screenshots for the README, taken from the demo app (invented library, no real data).
//
// Start two demos first (from the project folder):
//   DEMO_SHOWCASE=1 DEMO_ROOT=/mnt/tank/plexfiles python3 tests/demo.py 8650
//   DEMO_SHOWCASE=1 DEMO_NO_L2ARC=1 python3 tests/demo.py 8651     # a pool without an SSD cache
// then:
//   NODE_PATH=$(npm root -g) node dev/screenshots.js      # needs: npm i -g playwright
//
// The demo runs its fake Plex and TrueNAS on 127.0.0.1; the pictures show a typical home
// address instead (192.168.1.10), like a real install would.
const { chromium } = require("playwright");
const path = require("path");

const OUT = path.join(__dirname, "..", "docs", "screenshots");
const MAIN = process.env.MAIN || "http://127.0.0.1:8650";
const NO_L2 = process.env.NO_L2 || "http://127.0.0.1:8651";

async function tidy(page) {
  await page.evaluate(async () => {
    const st = await (await fetch("/api/state")).json();
    const port = (u) => (u.match(/:(\d+)/) || [])[1];
    const swaps = [
      [new RegExp("https?://127\\.0\\.0\\.1:" + port(st.settings.plex.url), "g"), "http://192.168.1.10:32400"],
      [new RegExp("https://127\\.0\\.0\\.1:" + port(st.settings.truenas.url), "g"), "https://192.168.1.10"],
      [new RegExp("http://127\\.0\\.0\\.1:" + location.port, "g"), "http://192.168.1.10:8642"],
      [/Test Server/g, "Home NAS"],
    ];
    const fix = (s) => swaps.reduce((t, [re, to]) => t.replace(re, to), s);
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n; (n = walk.nextNode());) n.nodeValue = fix(n.nodeValue);
    document.querySelectorAll("input").forEach((i) => { i.value = fix(i.value); });
  });
}

async function shot(browser, { url, name, scheme = "light", height = 1000, full = false, element, before }) {
  const page = await browser.newPage({ viewport: { width: 1280, height }, deviceScaleFactor: 2, colorScheme: scheme });
  await page.goto(url);
  await page.waitForTimeout(2500);
  if (before) await before(page);
  await tidy(page);
  const file = path.join(OUT, name + ".png");
  if (element) {
    const el = await element(page);
    await el.screenshot({ path: file });
  } else {
    await page.screenshot({ path: file, fullPage: full });
  }
  console.log("saved", file);
  await page.close();
}

const card = (title) => async (page) => {
  for (const s of await page.$$("section.card")) if ((await s.innerText()).startsWith(title)) return s;
  throw new Error("no card " + title);
};

(async () => {
  const b = await chromium.launch();
  await shot(b, { url: MAIN + "/activity", name: "activity", height: 1060 });
  await shot(b, { url: MAIN + "/activity", name: "activity-dark", scheme: "dark", height: 1060 });
  await shot(b, { url: MAIN + "/health", name: "health", height: 1400 });
  await shot(b, { url: MAIN + "/caching", name: "features", full: true });
  await shot(b, { url: MAIN + "/system", name: "media-mappings", element: card("Media mappings") });
  await shot(b, { url: NO_L2 + "/ssd", name: "l2arc-sizing", element: async (p) => (await p.$$("section.card"))[1] });
  await shot(b, {
    url: MAIN + "/activity", name: "activity-chart", element: card("Over time"),
    before: async (p) => { await p.click('[data-period="month"]'); await p.waitForTimeout(400); },
  });
  await b.close();
})();
