// What plexcache/web/decor.js draws, for the style guide (dev/style.py): decor.js run in node on a fixed page of known
// sizes (cards, a row of tiles, the Kept and Lost boxes and their labels, progress bars, the menu and a page title), in
// a computer's layout and a phone's, with every decoration on and with every one off. Out comes every drawing it puts
// on the page, where it puts it, and every style it sets: so the guide holds what decor.js draws, not how it's written,
// and a change to its code that draws the same needs no approval. A small stand-in for the DOM is enough: decor.js only
// reads sizes and puts drawings in.
//   node dev/decor-draws.js [plexcache/web/decor.js]  → {"<where>": "<what's drawn there>", ...} on stdout
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");

const SRC = process.argv[2] || path.join(__dirname, "..", "plexcache", "web", "decor.js");

// ---- selectors: lists of compounds (tag, .class, :not(.class), :scope) joined by " " or " > "
function compound(s) {
  const c = { tag: null, cls: [], not: [], scope: false };
  s.replace(/:not\(\.([\w-]+)\)/g, (_, n) => { c.not.push(n); return ""; })
    .replace(/:scope/g, () => { c.scope = true; return ""; })
    .replace(/\.([\w-]+)/g, (_, n) => { c.cls.push(n); return ""; })
    .replace(/^([a-z]+)/, (_, t) => { c.tag = t; return ""; });
  return c;
}
function parse(sel) {
  return sel.split(",").map((one) => {
    const parts = one.trim().replace(/\s*>\s*/g, " > ").split(/\s+/), steps = [];
    let comb = " ";
    for (const p of parts) { if (p === ">") { comb = ">"; continue; } steps.push({ comb, c: compound(p) }); comb = " "; }
    return steps;
  });
}
function fits(el, c, scope) {
  if (c.scope) return el === scope;
  if (c.tag && el.tagName.toLowerCase() !== c.tag) return false;
  return c.cls.every((n) => el.classList.contains(n)) && !c.not.some((n) => el.classList.contains(n));
}
function matchSteps(el, steps, i, scope) {
  if (!fits(el, steps[i].c, scope)) return false;
  if (i === 0) return true;
  if (steps[i].comb === ">") return !!el.parentNode && matchSteps(el.parentNode, steps, i - 1, scope);
  for (let p = el.parentNode; p; p = p.parentNode) if (matchSteps(p, steps, i - 1, scope)) return true;
  return false;
}
const matches = (el, sel, scope) => parse(sel).some((steps) => matchSteps(el, steps, steps.length - 1, scope));

// ---- elements
function element(tag, opts) {
  const o = opts || {};
  const el = {
    tagName: String(tag).toUpperCase(), nodeType: 1, parentNode: null, children: [], attrs: {}, style: {}, _html: "",
    w: o.w || 0, h: o.h || 0, top: o.top || 0, radius: o.radius, border: o.border, drawn: !!o.drawn,
    get offsetWidth() { return this.w; }, get offsetHeight() { return this.h; }, get clientWidth() { return this.w; },
    get offsetTop() { return this.top; }, get isConnected() { let p = this; while (p.parentNode) p = p.parentNode; return p === html; },
    get className() { return this.attrs.class || ""; }, set className(v) { this.attrs.class = String(v); },
    get id() { return this.attrs.id || ""; },
    classList: null,
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.children = []; const m = /^<(\w+)([^>]*)>/.exec(this._html);
      if (m) { const k = element(m[1], { drawn: true }); const c = /class="([^"]*)"/.exec(m[2]); if (c) k.attrs.class = c[1]; k._markup = this._html; k.parentNode = this; this.children.push(k); } },
    get firstChild() { return this.children[0] || null; },
    get nextSibling() { const s = this.parentNode && this.parentNode.children; return s ? s[s.indexOf(this) + 1] || null : null; },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }, setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { return this.insertBefore(c, null); },
    insertBefore(c, ref) { if (c.parentNode) c.remove(); c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; },
    insertAdjacentElement(where, c) { if (where !== "afterend") throw new Error("decor-draws.js: insertAdjacentElement " + where); this.parentNode.insertBefore(c, this.nextSibling); return c; },
    remove() { if (this.parentNode) { const s = this.parentNode.children; s.splice(s.indexOf(this), 1); this.parentNode = null; } },
    replaceWith(c) { const p = this.parentNode, n = this.nextSibling; this.remove(); p.insertBefore(c, n); },
    matches(sel) { return matches(this, sel, null); },
    closest(sel) { for (let p = this; p; p = p.parentNode) if (p.nodeType === 1 && matches(p, sel, null)) return p; return null; },
    querySelectorAll(sel) { const out = []; const walk = (e) => { for (const k of e.children) { if (matches(k, sel, this)) out.push(k); walk(k); } }; walk(this); return out; },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    addEventListener() {}, removeEventListener() {},
  };
  el.classList = { contains: (c) => (" " + el.className + " ").indexOf(" " + c + " ") >= 0 };
  el.style.setProperty = (k, v) => { el.style[k] = String(v); };
  el.style.removeProperty = (k) => { delete el.style[k]; };
  if (o.cls) el.className = o.cls;
  if (o.id) el.attrs.id = o.id;
  for (const k of o.kids || []) el.appendChild(k);
  for (const [k, v] of Object.entries(o.attrs || {})) el.attrs[k] = v;
  return el;
}
let html = null;

// ---- the page: a computer's layout (the menu down the side) or a phone's (across the top)
function page(phone) {
  const W = phone ? 360 : 980, card = (w, h, extra) => element("div", Object.assign({ cls: "card", w, h, radius: 12 }, extra || {}));
  const meter = (cls, w, pct) => element("div", { cls, w, attrs: pct == null ? {} : { "aria-valuenow": String(pct) }, kids: [element("div")] });
  const box = (cls) => element("div", { cls, w: phone ? 340 : 420, h: 120, radius: 10, border: 2 });
  const main = element("div", { cls: "main", top: phone ? 60 : 0, w: W, h: 1400, kids: [
    element("div", { cls: "top", w: W, h: 80 }),
    element("div", { cls: "tiles", kids: [card(phone ? 360 : 317, 102, { cls: "card tile" }), card(phone ? 360 : 317, 102, { cls: "card tile" }), card(phone ? 360 : 317, 140, { cls: "card tile" })] }),
    card(W, 400, { id: "c1" }),
    card(W, 260, { kids: [element("span", { cls: "mgkeeplbl", w: 120, h: 20, radius: 10, border: 1 }), box("mgkeep"), element("span", { cls: "mgdroplbl", w: 120, h: 20, radius: 10, border: 1 }), box("mgdrop")] }),
    card(W, 180, { kids: [meter("meter", 300, 37), meter("meter thin", 200, 100), meter("meter busy", 250, null), meter("meter", 280, 0), meter("meter usage", 300, 50)] }),
  ] });
  const side = element("nav", { cls: "side", w: phone ? 400 : 221, h: phone ? 60 : 900 });
  const shell = element("div", { cls: "shell", w: phone ? 400 : 1280, h: 1400, kids: [side, main] });
  return { shell, main };
}

function run(phone, decor) {
  html = element("html", { attrs: { "data-theme": "dionysus", "data-mode": "light" } });
  for (const k of ["spots", "vines", "film", "grapes"]) html.attrs["data-" + k] = decor ? "on" : "off";
  const body = element("body");
  html.appendChild(body);
  const { shell, main } = page(phone);
  body.appendChild(shell);
  const vars = { "--spots-a": decor ? "0.05" : "0", "--spots": "#B07A00", "--menu-spots": "#F0AE1F" };
  const frames = [];
  const document = {
    documentElement: html, body, readyState: "complete",
    createElement: (t) => element(t), querySelector: (s) => html.querySelector(s), querySelectorAll: (s) => html.querySelectorAll(s),
    addEventListener() {}, removeEventListener() {},
  };
  const window = {
    document, location: { pathname: "/tools" }, requestAnimationFrame: (f) => { frames.push(f); return frames.length; },
    getComputedStyle: (el) => ({ borderTopLeftRadius: (el.radius == null ? "" : el.radius + "px"), borderTopWidth: (el.border == null ? "0px" : el.border + "px"),
      getPropertyValue: (n) => vars[n] || "" }),
    addEventListener() {}, removeEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
    MutationObserver: function () { return { observe() {}, disconnect() {} }; },
    ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
    WeakMap, Math, JSON, Object, Array, String, Number, parseFloat, parseInt, isNaN, encodeURIComponent, Error,
  };
  window.window = window;
  vm.createContext(window);
  vm.runInContext(fs.readFileSync(SRC, "utf-8"), window, { filename: SRC });
  if (!window.PCDECOR) throw new Error("decor-draws.js: decor.js set no PCDECOR");
  window.PCDECOR.paint(main);
  for (let i = 0; i < 20 && frames.length; i++) frames.splice(0).forEach((f) => f(0));
  return html;
}

// what's drawn where: each element decor.js put in, by the fixture element it's in and its place there; each style it set
function where(el) {
  const names = [];
  for (let p = el; p && p !== html; p = p.parentNode) {
    const sibs = p.parentNode ? p.parentNode.children.filter((s) => !s.drawn) : [];
    names.unshift(p.tagName.toLowerCase() + (p.className ? "." + p.className.split(" ").join(".") : "") + (sibs.length > 1 ? "[" + sibs.indexOf(p) + "]" : ""));
  }
  return names.join(" > ");
}
const out = {};
for (const [layout, phone] of [["computer", false], ["phone", true]]) {
  for (const [state, on] of [["on", true], ["off", false]]) {
    const root = run(phone, on), tag = `${layout}, decorations ${state}: `;
    const walk = (el) => {
      el.children.forEach((k) => {
        if (k.drawn || k.attrs["data-k"] !== undefined) {
          const kids = el.children.filter((s) => !s.drawn), before = kids.filter((s) => el.children.indexOf(s) < el.children.indexOf(k)).length;
          out[tag + where(el) + " | " + (k.className || k.tagName.toLowerCase()) + " at " + before] = k._markup || ("<" + k.tagName.toLowerCase() + ' class="' + k.className + '">' + k._html);
        } else walk(k);
      });
      const st = Object.entries(el.style).filter(([n, v]) => typeof v !== "function" && v !== "");
      if (st.length) out[tag + where(el) + " | style"] = st.map(([n, v]) => n + ":" + v).sort().join(";");
    };
    walk(root);
  }
}
process.stdout.write(JSON.stringify(out));
