// Renders a page of the web UI in node, from a state given on stdin — for the tests (test_plexcache.py,
// PageStructureTests): the page as the browser would draw it from one /api/state answer, with no
// browser. A small stand-in for the DOM is enough: app.js writes the page as HTML into #main and reads
// little back. Usage: node tests/render.js <page> < state.json  → {"html": …, "text": …} on stdout,
// where the state is the server's /api/state (settings included) and <page> a path such as "system".
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");

process.on("unhandledRejection", (e) => { console.error("render.js: " + (e && e.stack || e)); process.exit(1); });
process.on("uncaughtException", (e) => { console.error("render.js: " + (e && e.stack || e)); process.exit(1); });
const state = JSON.parse(fs.readFileSync(0, "utf-8"));
const pageName = process.argv[2] || "health";

function element(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(), children: [], attrs: {}, dataset: {}, style: {}, hidden: false, disabled: false,
    _html: "", textContent: "", className: "", value: "", checked: false,
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = String(v); },
    classList: { _s: new Set(), contains(c) { return this._s.has(c); }, add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) on = !this._s.has(c); if (on) this._s.add(c); else this._s.delete(c); return on; } },
    addEventListener() {}, removeEventListener() {}, focus() {}, blur() {}, scrollIntoView() {}, click() {},
    appendChild(c) { this.children.push(c); return c; }, removeChild(c) { this.children = this.children.filter(x => x !== c); return c; },
    insertBefore(c) { this.children.unshift(c); return c; }, remove() {}, contains() { return false; }, closest() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; }, getBoundingClientRect() { return { top: 0, left: 0, width: 100, height: 20, bottom: 20, right: 100 }; },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }, setAttribute(k, v) { this.attrs[k] = String(v); },
    hasAttribute(k) { return k in this.attrs; }, removeAttribute(k) { delete this.attrs[k]; },
    select() {}, setSelectionRange() {}, matches() { return false; },
  };
  return el;
}
const byId = {};
const body = element("body");
const document = {
  body, documentElement: element("html"), activeElement: body, hidden: false, title: "", currentScript: null,
  getElementById(id) { return byId[id] || (byId[id] = element("div")); },
  createElement(tag) { return element(tag); }, createTextNode(t) { return { textContent: t }; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, removeEventListener() {},
};
const storage = { _m: {}, getItem(k) { return k in this._m ? this._m[k] : null; }, setItem(k, v) { this._m[k] = String(v); }, removeItem(k) { delete this._m[k]; } };
const never = new Promise(() => {});
const sandbox = {
  document, localStorage: storage, sessionStorage: storage, console,
  location: { pathname: "/" + pageName, hash: "", origin: "http://localhost", href: "http://localhost/" + pageName, reload() {} },
  history: { pushState() {}, replaceState() {} },
  navigator: { language: "en-CA", languages: ["en-CA"], clipboard: { writeText() { return Promise.resolve(); } } },
  // one answer: the state given; every other request stays pending (nothing else is read for a first draw)
  fetch(url) {
    if (process.env.RENDER_DEBUG) console.error("render.js: fetch " + url);
    if (String(url).startsWith("/api/state")) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(state) });
    if (String(url).startsWith("/api/processes") && state._proc) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(state._proc) });
    if (String(url).startsWith("/api/library/health") && state._health) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(state._health) });
    if (String(url).startsWith("/api/optimizer/files") && state._optfiles) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(state._optfiles) });
    if (String(url).startsWith("/api/tools/readiness") && state._ready) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(state._ready) });
    return never;
  },
  setTimeout, clearTimeout, setInterval() { return 0; }, clearInterval() {}, requestAnimationFrame(f) { return setTimeout(f, 0); },
  Intl, Date, Math, JSON, Promise, Object, Array, String, Number, Boolean, RegExp, Error, TypeError, Map, Set, encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isFinite, isNaN,
  URL, URLSearchParams, TextEncoder, TextDecoder, structuredClone,
  matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
  getSelection() { return { toString() { return ""; }, rangeCount: 0 }; },
  scrollTo() {}, scrollBy() {}, scrollX: 0, scrollY: 0, innerWidth: 1280, innerHeight: 800, isSecureContext: true, open() {},
  addEventListener() {}, removeEventListener() {},
  MutationObserver: function () { return { observe() {}, disconnect() {} }; },
  ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
const web = path.join(__dirname, "..", "plexcache", "web");
vm.runInContext(fs.readFileSync(path.join(web, "i18n.js"), "utf-8"), sandbox, { filename: "i18n.js" });
vm.runInContext(fs.readFileSync(path.join(web, "app.js"), "utf-8"), sandbox, { filename: "app.js" });
// the first draw comes after PCI18N.ready and the state's promise: a few turns of the event loop
setTimeout(() => {
  if (process.env.RENDER_DEBUG) console.error("render.js: locale " + (sandbox.PCI18N && sandbox.PCI18N.locale) + ", main drawn: " + !!(byId.main && byId.main.innerHTML));
  const html = byId.main ? byId.main.innerHTML : "";
  const text = html.replace(/<style[\s\S]*?<\/style>/g, " ").replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+/g, " ").trim();
  process.stdout.write(JSON.stringify({ html, text, title: document.title, info: sandbox.PCINFO || {} }));
}, 300);
