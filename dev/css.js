#!/usr/bin/env node
// The app's CSS: checked as written, and built as browsers get it. The settings are dev/package.json's: "browserslist" (the
// browsers it's built for) and "stylelint" (the checks).
//
//     npm ci --prefix dev                                  # once, and again when dev/package-lock.json changes
//     node dev/css.js lint [<file.css>...]                 # Stylelint on plexcache/web/*.css (or the files named)
//     node dev/css.js build [<in.css> [<out.css>]]         # plexcache/web/app.css built (to stdout, or out; out may be in)
//     node dev/css.js build --no-minify ...                # prefixed only, not minified
//
// The CSS in plexcache/web is written without browser prefixes (-webkit-, -moz-...): Stylelint rejects them, and the suite
// checks the pages' and scripts' own styles too (CssBuildTests). The build adds the ones the targets still need
// (Autoprefixer) and minifies (cssnano); the image build runs it (the Dockerfile's css stage), so what ships is built.
//
// A warning from the build (a property Autoprefixer can't prefix as written, an old syntax) is an error: nothing is written,
// the warnings are listed and the exit code is 1. The lint lists what it found and exits 1. 2: the packages aren't installed.
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.dirname(__dirname);
const WEB = path.join(ROOT, "plexcache", "web");
const settings = require("./package.json");

function need(name) {
  try {
    return require(name);
  } catch (e) {
    console.error(`${name} isn't installed: npm ci --prefix dev`);
    process.exit(2);
  }
}

function rel(p) {
  const r = path.relative(ROOT, p);
  return r && !r.startsWith("..") ? r : p;
}

async function build(args) {
  const minify = !args.includes("--no-minify");
  const [src = path.join(WEB, "app.css"), out] = args.filter((a) => a !== "--no-minify");
  const postcss = need("postcss");
  const plugins = [need("autoprefixer")({ overrideBrowserslist: settings.browserslist })];
  if (minify) plugins.push(need("cssnano")({ preset: "default" }));
  const css = fs.readFileSync(src, "utf8");
  const result = await postcss(plugins).process(css, { from: src, map: false });
  const warnings = result.warnings();
  if (warnings.length) {
    for (const w of warnings) console.error(`${rel(src)}:${w.line || "?"}: ${w.text} (${w.plugin})`);
    return 1;
  }
  if (out) fs.writeFileSync(out, result.css);
  else process.stdout.write(result.css);
  const kb = (n) => `${(Buffer.byteLength(n) / 1024).toFixed(1)} KB`;
  console.error(`${rel(src)}: built for ${settings.browserslist.join(", ")}${minify ? `, minified ${kb(css)} → ${kb(result.css)}` : ""}`);
  return 0;
}

async function lint(args) {
  const stylelint = need("stylelint");
  const files = args.length ? args : fs.readdirSync(WEB).filter((n) => n.endsWith(".css")).map((n) => path.join(WEB, n));
  const { results } = await stylelint.lint({ files, config: settings.stylelint, configBasedir: __dirname });
  let found = 0;
  for (const r of results) {
    for (const w of r.warnings) {
      found++;
      console.error(`${rel(r.source)}:${w.line}:${w.column}: ${w.text}`);
    }
    for (const w of r.invalidOptionWarnings || []) {
      found++;
      console.error(`stylelint settings (dev/package.json): ${w.text}`);
    }
  }
  console.error(found ? `${found} found by Stylelint` : `${files.length} file(s) pass Stylelint`);
  return found ? 1 : 0;
}

const COMMANDS = { build, lint };
if (require.main === module) {
  const [cmd, ...args] = process.argv.slice(2);
  if (!COMMANDS[cmd]) {
    console.error("usage: node dev/css.js lint [<file.css>...] | build [--no-minify] [<in.css> [<out.css>]]");
    process.exitCode = 2;
  } else {
    COMMANDS[cmd](args).then((code) => { process.exitCode = code; }, (e) => { console.error(e.message || e); process.exitCode = 1; });
  }
}
