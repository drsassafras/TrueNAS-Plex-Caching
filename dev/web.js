#!/usr/bin/env node
// The web app's CSS, scripts and pages: checked as written, and built as browsers get them. Every setting is
// dev/package.json's: "browserslist" (the browsers they're built for: one list, "last 5 years", for all three; dev/future.py's
// run, PLEXCACHE_WEB_TARGETS=future, takes "futureBrowserslist" in its place: "last 4 years", the window a year on),
// "graceful" (what a target lacks but the pages can do without), "stylelint", "eslint" and "htmlvalidate" (the checks).
//
//     npm ci --prefix dev                                  # once, and again when dev/package-lock.json changes
//     node dev/web.js lint [<file>...]                     # plexcache/web's .css, .js and .html (or the files named), each by its kind
//     node dev/web.js build <in> [<out>]                   # a .css, .js or .html built (to stdout, or out; out may be in)
//     node dev/web.js build --no-minify <in> [<out>]       # the CSS prefixed only, a script or page as it is: not minified
//
// The lint: Stylelint on the CSS twice, as written (its recommended checks; no browser prefixes written by hand) and as the
// oldest targets get it (no feature a target lacks: stylelint-no-unsupported-browser-features, caniuse's data, on the CSS
// postcss-preset-env makes for them, so a newer feature it rewrites for them is one the CSS can use), ESLint on the scripts (its recommended checks; no feature a
// target lacks: eslint-plugin-compat), TypeScript on the app's own scripts (checkJs: dev/jsconfig.json), html-validate on the pages and the HTML the app draws (its standard checks; no
// element, attribute or input type a target lacks: MDN's data, below). The build: the CSS with the newer features the
// targets lack rewritten as CSS they read (postcss-preset-env: nesting, newer colour and media syntax and the like, the
// newer form kept beside it for the browsers that have it), the prefixes they still need added (Autoprefixer), then
// minified (cssnano); a script minified (Terser), a page (html-minifier-terser). The
// image build runs it on every stylesheet, script and page (the Dockerfile's web stage), so what ships is built.
//
// A warning from the build (a property Autoprefixer can't prefix as written, an old syntax) is an error: nothing is written,
// the warnings are listed and the exit code is 1. The lint lists what it found and exits 1. 2: the packages aren't installed.
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.dirname(__dirname);
const WEB = path.join(ROOT, "plexcache", "web");
const settings = require("./package.json");
// What a target lacks but the pages can do without (it does nothing there, and nothing else goes with it): by the name the
// check reports it by (caniuse's for the CSS, MDN's for the HTML), with why.
const GRACEFUL = settings.graceful;
// The targets: "browserslist", or "futureBrowserslist" for dev/future.py's run (the software it tries is about a year
// ahead of the image's, so the window is a year shorter: the browsers that will have dropped out by then don't count).
const TARGETS = process.env.PLEXCACHE_WEB_TARGETS === "future" ? settings.futureBrowserslist : settings.browserslist;
// The tools that read the targets themselves (cssnano: it won't merge rules into one a target would drop whole;
// eslint-plugin-compat, doiuse) read the same list, by the variable Browserslist looks at first.
process.env.BROWSERSLIST = TARGETS.join(", ");

function need(name) {
  try {
    return require(name);
  } catch {
    console.error(`${name} isn't installed: npm ci --prefix dev`);
    process.exit(2);
  }
}

function rel(p) {
  const r = path.relative(ROOT, p);
  return r && !r.startsWith("..") ? r : p;
}

const kb = (n) => `${(Buffer.byteLength(n) / 1024).toFixed(1)} KB`;

// What postcss-preset-env does for the targets: the newer CSS features they lack, rewritten as CSS they read (its stage 2
// features: the ones on their way to every browser; the prefixes are Autoprefixer's, below). preserve: false keeps only
// what the targets read (the CSS the check looks at); the build keeps the newer form beside it too.
function presetEnv(preserve) {
  const options = { browsers: TARGETS, autoprefixer: false };
  if (!preserve) options.preserve = false;
  return need("postcss-preset-env")(options);
}

// The CSS: its newer features rewritten for the targets (postcss-preset-env), prefixed (Autoprefixer), then minified
// (cssnano). Warnings: the list of them, nothing built.
async function buildCss(src, text, minify) {
  const postcss = need("postcss");
  const plugins = [presetEnv(true), need("autoprefixer")({ overrideBrowserslist: TARGETS })];
  if (minify) plugins.push(need("cssnano")({ preset: "default" }));
  const result = await postcss(plugins).process(text, { from: src, map: false });
  const warnings = result.warnings();
  if (warnings.length) return { warnings: warnings.map((w) => `${rel(src)}:${w.line || "?"}: ${w.text} (${w.plugin})`) };
  return { out: result.css };
}

// A script: minified (Terser), its names kept where other scripts see them (the page's scripts share window's).
async function buildJs(src, text, minify) {
  if (!minify) return { out: text };
  const result = await need("terser").minify(text, { compress: true, mangle: true, format: { comments: false } });
  return { out: result.code };
}

// A page: minified (html-minifier-terser): comments out, runs of white space down to one (never none: text and inline
// elements keep their gaps).
async function buildHtml(src, text, minify) {
  if (!minify) return { out: text };
  const { minify: html } = need("html-minifier-terser");
  return { out: await html(text, { collapseWhitespace: true, conservativeCollapse: true, removeComments: true }) };
}

const BUILDERS = { ".css": buildCss, ".js": buildJs, ".html": buildHtml };

async function build(args) {
  const minify = !args.includes("--no-minify");
  const [src, out] = args.filter((a) => a !== "--no-minify");
  const builder = src && BUILDERS[path.extname(src)];
  if (!builder) {
    console.error(`build: a .css, .js or .html file to build${src ? `, not ${src}` : ""}`);
    return 2;
  }
  const text = fs.readFileSync(src, "utf8");
  const result = await builder(src, text, minify);
  if (result.warnings) {
    for (const w of result.warnings) console.error(w);
    return 1;
  }
  if (out) fs.writeFileSync(out, result.out);
  else process.stdout.write(result.out);
  const targets = path.extname(src) === ".css" ? `built for ${TARGETS.join(", ")}` : "";  // (a script: as written, for them all)
  const size = minify ? `minified ${kb(text)} → ${kb(result.out)}` : "";
  console.error(`${rel(src)}: ${[targets, size].filter(Boolean).join(", ") || "unchanged"}`);
  return 0;
}

// The checks of the scripts (ESLint): settings.eslint's "extends" (its recommended checks, eslint-plugin-compat's), then
// its own language settings and rules.
function eslintConfig() {
  const own = settings.eslint;
  const presets = { "@eslint/js:recommended": () => need("@eslint/js").configs.recommended,
    "eslint-plugin-compat:flat/recommended": () => need("eslint-plugin-compat").configs["flat/recommended"] };
  const globals = need("globals");
  return [
    ...own.extends.map((name) => {
      if (!presets[name]) throw new Error(`eslint settings (dev/package.json): no such preset: ${name}`);
      return presets[name]();
    }),
    { languageOptions: { ecmaVersion: own.ecmaVersion, sourceType: "script", globals: { ...globals.browser, ...own.globals } },
      settings: { browserslistOpts: { path: __dirname }, lintAllEsApis: true }, rules: own.rules },
  ];
}

// The CSS as the oldest targets get it (postcss-preset-env's rewrite, the newer forms left out), with a map back to the
// lines it was written on: what the check of the targets looks at.
async function forTargets(file) {
  const result = await need("postcss")([presetEnv(false)]).process(fs.readFileSync(file, "utf8"),
    { from: file, map: { inline: false, annotation: false, sourcesContent: false } });
  const map = new (need("source-map-js").SourceMapConsumer)(result.map.toJSON());
  return { css: result.css, at: (line, column) => map.originalPositionFor({ line, column: Math.max(0, column - 1) }) };
}

function stylelintFound(results, where) {
  const found = [];
  for (const r of results) {
    for (const w of r.warnings) found.push(`${where(r, w)}: ${w.text}`);
    for (const w of r.invalidOptionWarnings || []) found.push(`stylelint settings (dev/package.json): ${w.text}`);
  }
  return found;
}

async function lintCss(files) {
  const stylelint = need("stylelint");
  // as written: settings.stylelint's checks (its recommended ones; no browser prefixes written by hand)
  const { results } = await stylelint.lint({ files, config: settings.stylelint, configBasedir: __dirname });
  const found = stylelintFound(results, (r, w) => `${rel(r.source)}:${w.line}:${w.column}`);
  // as the targets get it: stylelint-no-unsupported-browser-features (doiuse, caniuse's data): no CSS feature a target
  // lacks, partly or wholly (partly: what's there is what the pages use), but the ones the pages can do without, in what
  // postcss-preset-env makes of it (a feature it rewrites for them isn't there to find); found, it's told by its own line
  const graceful = Object.keys(GRACEFUL).filter((id) => !id.includes("."));
  const config = { plugins: ["stylelint-no-unsupported-browser-features"],
    rules: { "plugin/no-unsupported-browser-features": [true, { ignorePartialSupport: true, ignore: graceful }] } };
  for (const file of files) {
    const built = await forTargets(file);
    const got = await stylelint.lint({ code: built.css, codeFilename: file, config, configBasedir: __dirname });
    found.push(...stylelintFound(got.results, (r, w) => {
      const at = built.at(w.line, w.column);
      return at.line ? `${rel(file)}:${at.line}:${at.column + 1}` : `${rel(file)} (as the targets get it, line ${w.line})`;
    }));
  }
  return found;
}

// The folder that holds every file named (ESLint passes over a file outside its own folder)
function common(files) {
  let dir = ROOT;
  for (const f of files) {
    while (path.relative(dir, path.resolve(f)).startsWith("..")) dir = path.dirname(dir);
  }
  return dir;
}

async function lintJs(files) {
  const { ESLint } = need("eslint");
  const eslint = new ESLint({ cwd: common(files), overrideConfigFile: true, overrideConfig: eslintConfig(), errorOnUnmatchedPattern: true });
  const found = [];
  for (const r of await eslint.lintFiles(files)) {
    for (const m of r.messages) found.push(`${rel(r.filePath)}:${m.line || "?"}:${m.column || "?"}: ${m.message}${m.ruleId ? ` (${m.ruleId})` : ""}`);
  }
  return found;
}

// The browsers of the targets as MDN's compatibility data names them (the rest, UC Browser and the like, it has no data for).
const BCD_BROWSERS = { chrome: "chrome", edge: "edge", firefox: "firefox", safari: "safari", opera: "opera", ios_saf: "safari_ios",
  and_chr: "chrome_android", and_ff: "firefox_android", samsung: "samsunginternet_android", op_mob: "opera_android", android: "webview_android" };

// The oldest version of each target browser (MDN's names): what a feature has to be in, for the pages to use it.
function oldestTargets() {
  const oldest = {};
  for (const entry of need("browserslist")(TARGETS)) {
    const [name, version] = entry.split(" ");
    const bcd = BCD_BROWSERS[name];
    const v = parseFloat(version.split("-")[0]);
    if (bcd && !Number.isNaN(v) && !(oldest[bcd] <= v)) oldest[bcd] = v;
  }
  return oldest;
}

// Which of the targets lack a feature (an element, an attribute, an input type): from its MDN support statements, a
// version it's in (not behind a flag, not prefixed or under another name, not removed since) no newer than the oldest target.
function lacking(compat, oldest) {
  if (!compat) return [];
  const has = (stmt, v) => {
    if (!stmt || stmt.flags || stmt.prefix || stmt.alternative_name) return false;
    const added = stmt.version_added;
    if (added === true) return true;
    if (!added || added === "preview") return false;
    const removed = typeof stmt.version_removed === "string" ? parseFloat(stmt.version_removed.replace("≤", "")) : null;
    return parseFloat(added.replace("≤", "")) <= v && !(removed !== null && removed <= v);
  };
  return Object.entries(oldest).filter(([b, v]) => {
    const support = compat.support[b];
    if (support === undefined) return false;  // (no data for it)
    return ![].concat(support).some((stmt) => has(stmt, v));
  }).map(([b, v]) => `${need("@mdn/browser-compat-data").browsers[b].name} ${v}`);
}

// html-validate's check of the pages against the targets: each element, attribute and input type in a version of every
// one (MDN's data; the CSS's and scripts' checks are Stylelint's and eslint-plugin-compat's, on the same list).
function compatPlugin() {
  const { Rule } = need("html-validate");
  const bcd = need("@mdn/browser-compat-data");
  const oldest = oldestTargets();
  class Compat extends Rule {
    setup() {
      // a feature: its MDN path (as settings.graceful names it) and data
      const at = (id) => [id, id.split(".").reduce((o, k) => (o ? o[k] : undefined), bcd)];
      const check = (node, what, [id, data], location) => {
        if (!data || GRACEFUL[id]) return;
        const missing = lacking(data.__compat, oldest);
        if (missing.length) this.report(node, `${what} isn't in ${missing.join(", ")} (${id})`, location);
      };
      this.on("tag:start", (event) => {
        const el = event.target;
        if (!el || !el.tagName) return;
        const tag = el.tagName.toLowerCase();
        check(el, `<${tag}>`, at(bcd.html.elements[tag] ? `html.elements.${tag}` : `svg.elements.${tag}`));
      });
      this.on("attr", (event) => {
        const el = event.target, key = String(event.key || "").toLowerCase();
        const tag = el && el.tagName ? el.tagName.toLowerCase() : "";
        const html = bcd.html.elements[tag];
        if (!html || key.includes(".")) return;  // (an SVG's attributes: drawn as the element is)
        let id = html[key] ? `html.elements.${tag}.${key}` : `html.global_attributes.${key}`;
        const type = tag === "input" && key === "type" && event.value ? `type_${String(event.value).toLowerCase()}` : "";
        if (type && html[type]) id = `html.elements.input.${type}`;
        check(el, `${tag}[${key}${type ? `=${event.value}` : ""}]`, at(id), event.keyLocation);
      });
    }
  }
  return { name: "web-compat", rules: { "web-compat/compat": Compat } };
}

async function lintHtml(files) {
  const { HtmlValidate } = need("html-validate");
  const own = settings.htmlvalidate;
  const hv = new HtmlValidate({ ...own, plugins: [compatPlugin()], rules: { ...own.rules, "web-compat/compat": "error" } });
  const found = [];
  for (const file of files) {
    const report = await hv.validateFile(file);
    for (const r of report.results) {
      for (const m of r.messages) found.push(`${rel(file)}:${m.line}:${m.column}: ${m.message} (${m.ruleId})`);
    }
  }
  return found;
}

const LINTERS = { ".css": ["Stylelint", lintCss], ".js": ["ESLint", lintJs], ".html": ["html-validate", lintHtml] };

// TypeScript's check of the app's own scripts (dev/jsconfig.json: checkJs, the types in dev/web.d.ts), run when the lint
// takes one of them: the scripts share their globals, so it checks them together, and what it finds in any of them counts.
const TYPED = path.join(__dirname, "jsconfig.json");
function typedScripts(files) {
  return files.filter((f) => path.extname(f) === ".js" && path.dirname(path.resolve(f)) === WEB);
}
function lintTypes() {
  let tsc;
  try {
    tsc = path.join(path.dirname(require.resolve("typescript/package.json")), "bin", "tsc");
  } catch {
    console.error("typescript isn't installed: npm ci --prefix dev");
    process.exit(2);
  }
  const got = require("child_process").spawnSync(process.execPath, [tsc, "-p", TYPED, "--pretty", "false"], { encoding: "utf8" });
  if (got.error) throw got.error;
  const found = [];
  for (const line of (got.stdout + got.stderr).split("\n")) {
    const m = line.match(/^(.+?)\((\d+),(\d+)\): error (TS\d+): (.*)$/);
    if (m) found.push(`${rel(path.resolve(__dirname, m[1]))}:${m[2]}:${m[3]}: ${m[5]} (${m[4]})`);
    else if (line.trim() && !/^\s/.test(line)) found.push(line);  // (not a finding's own lines: tsc couldn't run as asked)
    else if (line.trim() && found.length) found[found.length - 1] += " " + line.trim();
  }
  if (got.status && !found.length) found.push(`tsc stopped (${got.status})`);
  return found;
}

async function lint(args) {
  const files = args.length ? args : fs.readdirSync(WEB).filter((n) => LINTERS[path.extname(n)]).map((n) => path.join(WEB, n));
  const other = files.filter((f) => !LINTERS[path.extname(f)]);
  if (other.length) {
    console.error(`lint: .css, .js and .html files, not ${other.join(", ")}`);
    return 2;
  }
  let total = 0;
  for (const [ext, [name, run]] of Object.entries(LINTERS)) {
    const mine = files.filter((f) => path.extname(f) === ext);
    if (!mine.length) continue;
    const found = await run(mine);
    for (const f of found) console.error(f);
    console.error(found.length ? `${found.length} found by ${name}` : `${mine.length} file(s) pass ${name}`);
    total += found.length;
  }
  const typed = typedScripts(files);
  if (typed.length) {
    const found = lintTypes();
    for (const f of found) console.error(f);
    console.error(found.length ? `${found.length} found by TypeScript` : `${typed.length} file(s) pass TypeScript`);
    total += found.length;
  }
  return total ? 1 : 0;
}

const COMMANDS = { build, lint };
if (require.main === module) {
  const [cmd, ...args] = process.argv.slice(2);
  if (!COMMANDS[cmd]) {
    console.error("usage: node dev/web.js lint [<file.css|.js|.html>...] | build [--no-minify] <in.css|.js|.html> [<out>]");
    process.exitCode = 2;
  } else {
    COMMANDS[cmd](args).then((code) => { process.exitCode = code; }, (e) => { console.error(e.message || e); process.exitCode = 1; });
  }
}
