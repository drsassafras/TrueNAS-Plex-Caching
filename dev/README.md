# Developing Dionysus

This folder holds what you need to try changes on a real TrueNAS before pushing them:

| File | What it is |
|------|------------|
| `compose.dev.yaml` | A **test copy** of the app (`plex-cache-dev`, port 8643) that runs the published image but with its code replaced by whatever you last copied to the NAS. It has its own settings, so it never touches the real `plex-cache` app. |
| `tnsync` | A small script that **copies your code to the NAS, restarts the test app and runs the tests and checks** — in one command, from your computer, all at once on a live board (`board.py`). |
| `future.py` | Builds the image on the **next** Python's prerelease (3.16 while the image runs 3.15) and each tool's newest pre-release where one is ahead of its stable release (Node's nightlies, ffmpeg's git snapshot, libjpeg-turbo's and oxipng's pre-releases, the web's npm packages' `next`/`beta`/`rc` tags; MKVToolNix has releases only), and runs the suite inside it, every warning an error and the web's checks a year ahead (four years of browsers); `--stable` keeps the tools at their pins, `--local` uses an interpreter on your computer instead. The "future" track, local only (below). |
| `upkeep.py` | The dependency check: everything the app depends on against upstream's newest release, pins a minor behind moved (only with `dev` checked out), a major behind a prompt. tnsync runs it on every run (below), with a progress bar for each look-up and npm install; the suite's test reads its last answers, never upstream or npm. |
| `progress.py` | The progress bar the dev tools share: `tests/run.py`'s and `upkeep.py`'s. |
| `typecheck.py` | **Python Validation**: pyright's type check (`pyrightconfig.json`) and Ruff's lint (`ruff.toml`), each at its version in `tools.toml`, as tnsync and GitHub run them; `--fix` makes Ruff's safe fixes first. |
| `checks.py`, `tools.toml` | **What every check tool shares**: `tools.toml` holds each check tool's version (and, for a release binary, where it comes from and its checksum for each platform); `checks.py` fetches each at it the first time (into `~/.cache/plex-cache/tools/`), says what each finds one way (`file:line:col - severity: message (Tool CODE)`, annotated on GitHub), draws the board's bar and says the result line. A check tool is a section of the board run through it, never one of its own (`CheckToolTests`). |
| `versions.py` | Prints what an image carries (its OS, Python, MKVToolNix, ffmpeg…); each build logs it. |
| `coverage.py` | Runs the tests and shows how much of the code they reach, per file. No installs needed (Python 3.12+). |
| `codecs.py` | Codec names as MKVToolNix shows them, for Library Health (`plexcache/codec_names.py`). The image build runs it for the MKVToolNix release it installs; run it by hand to refresh the committed copy. |
| `web.js`, `package.json` | What checks and builds the web app's CSS, scripts and pages: **Stylelint**, **ESLint** and **html-validate** on the source (each with a check that no feature it uses is missing from a target browser), then **postcss-preset-env** (the newer CSS its targets lack, rewritten as CSS they read), **Autoprefixer** (the browser prefixes they need), **cssnano**, **Terser** and **html-minifier-terser** (minified) for the image. The settings, the browser floor and the pinned packages are in `package.json`; see below. |
| `screenshots.js` | Makes the README screenshots from the demo (see below). |
| `icons.js` | Draws the app's icons in Chromium, as a browser draws them, each from the logo the app shows at that size and centred on what it draws: `favicon.ico` from the tab icon (`plexcache/web/theatre-tab.svg`: Joined D at 16px, Wide bar at 32 and 48px; for browsers without SVG favicons), and from the full theatre playing (`theatre-playing.svg`, the menu's logo with its arrow lit) on the menu's colour the iPhone's `apple-touch-icon.png` and the installed app's `icon-192.png`, `icon-512.png` and `icon-maskable-512.png` (`manifest.webmanifest`). Run `NODE_PATH=$(npm root -g) node dev/icons.js` after changing either logo, then the style sample; `IconTests` fails when one is out of date (`node dev/icons.js check`). Needs Playwright, like the screenshots. |
| `style.py`, `style-guide.json`, `style-sample.js`, `decor-draws.js` | **The style guide**: every look the app may wear, each approved by the maintainer from a sample. `style.py check` lists what's new, changed or gone (the suite fails on it), `sample` photographs those in each theme and mode, `approve` (the maintainer only, at a terminal) adds them. See below. |
| `make_fixtures.py` | Makes the optimizer's test files (`tests/fixtures/*.mkv`: many real-world features, a few hundred KB) and the snapshot of what optimizing them gives. Run it with `--snapshot` after checking a change the tests caught (needs ffmpeg and MKVToolNix). |

The loop is: edit on your computer → `tnsync local -y` → refresh `http://<nas-ip>:8643`. Nothing reaches GitHub (or the real app) until you commit and push.

## What tnsync does

```
tnsync local      your folder as it is now (uncommitted changes included)
tnsync master     the latest code on GitHub's default branch (main or master)
tnsync dev        the latest code on GitHub's dev branch
tnsync release    the newest release tag, e.g. v1.2.0
tnsync beta       the newest tag including pre-releases, e.g. v1.3.0-beta.1
tnsync init       set up tnsync for a project

  -n  dry run: list what would be copied and run the checks, change nothing
  -y  restart the app once the files are copied (without it, it isn't: nothing is asked)
  -N  don't restart (the default)
  --tests-first  copy nothing until the checks have passed
  --skip-tests   don't run the tests and checks
  --skip-future  run the current tests only, not the future ones
  --release      the release checks too, and the tests as for a release (before dev is released)
  --no-sample    Style Approval only checks: no sample of the looks waiting is made (r makes it first then)
```

Everything runs **at the same time**, each step in a section of its own on a board about half a screen tall (`dev/board.py`), redrawn as it goes: a block that's blue while the step is under way, then green (passed), orange (a warning: stopped with Enter, dependencies behind), red (failed) or grey (for the record: skipped, not run, already in sync): a header, its progress bar (the one it moved last) (`dev/progress.py`'s, the same bar each tool draws on its own, sent to the board: `PLEXCACHE_PROGRESS=feed`) and the last line it printed. 

On top, the board's header: what's checked (`Local Copy on Dev`; `Committed Copy on Main` or `on Dev` for `master`/`main` and `dev`, `Release Copy of v1.2.0`, `Beta Copy of v1.3.0-beta.1`), the run's modes (`· Release`, `· Tests first`, `· Dry run`), the commit (`a1b2c3d`, `+ uncommitted` when your folder has changes; the first thing cut on a narrow terminal) and the time since the board started, which stops when the last block ends. Under it, the keys (`[Enter] Cancel`, and `[r] Review Style Changes` while the style review is offered) and, on the right, how many blocks are done (`6/8 done · 1 failed`, the failed and warnings only when there are).

Every block reads the same way: its title, its status in capitals and its time; its bar (`done/total` and the item under way or what it did, with how many workers do it on the right, under the time); and one result line (`dev/progress.py`'s `result`: facts joined with ` · `, no time, no workers and no count the bar already shows, so a passing suite's says what it ran on — `Python 3.15.2 · Node 26.12.0 · …`, a nightly's build stamp left off — and a failing one `3 failed · …`).

1. **Sync to the NAS** — copies only what changed with rsync over SSH (a bar of the files as they go), and deletes files on the NAS that you deleted locally. Installed packages (`node_modules`, from `npm ci`) never go: the NAS runs the source as it is. With `-y`, it then **restarts the test app** with TrueNAS's own `midclt call app.redeploy plex-cache-dev`, and waits until TrueNAS says it's done: you can refresh the page and try your change while the checks run.
2. **Current Tests** (`TN_TEST=python3 tests/run.py --exclude WebBuildTests --exclude StyleGuideTests`), on exactly the code it copies (for `master`, `dev` and the rest, a copy from git, with your folder's installed packages linked in for the web tests; it installs those itself first, `npm ci --prefix dev`, when they're missing or `dev/package-lock.json` has changed).
3. **Future Tests** (`TN_FUTURE=python3 dev/future.py --exclude StyleGuideTests`: the same suite on the next Python, see below: a bar for the image build, then one for the tests), stopped as soon as a current test fails (the current tests run on to their end, so every failure is still listed). `--skip-future` leaves them out when you're in a hurry.
4. **HTML/JS/CSS Validation** (`TN_VALIDATE`: `WebBuildTests`, Stylelint, ESLint, html-validate and the build), the web's checks from the suite, in a section of their own.
5. **Python Validation** (`TN_TYPES=python3 dev/typecheck.py`, as GitHub runs it; its bar the checks done): pyright's type check (`pyrightconfig.json`, `--warnings`) and Ruff's lint (`ruff.toml`: unused imports and variables, likely bugs, older syntax Python 3.15 says more simply, the import order, the security rules), at once, each at its version in `dev/tools.toml`, through the check tools' shared code (`dev/checks.py`). Its result line says what each found (`0 errors · 0 warnings · 0 Ruff findings`). Ruff is installed the first time, at its pin, into `~/.cache/plex-cache/tools/ruff-<version>` (nothing goes into your Python); `python3 dev/typecheck.py --fix` makes Ruff's safe fixes (the import order, unused imports) in your folder, then checks. A rule that's wrong for a line is said so there (`# noqa: <code> — why`).
6. **Security Audit** (`TN_AUDIT=python3 dev/audit.py`): which app code no security audit has seen, from each audit's record and git — green when every rule is met, orange when unaudited code is past its grace window (or sensitive code waits at all) — a warning, like the looks to approve, that neither stops the copy nor fails the run. Once a release candidate is tagged (`vX.Y.Z-rc.N`) it also says which release rules aren't met yet, and with `--release` those are a check like the others: red. It reads the records, not the tests, so it starts straight away. See **Security audits** below.
7. **Release checks**, only with `--release` (`TN_RELEASE=python3 dev/release.py`): what has to be true before `dev` is released — the version a new `X.Y.0` above what `main` and the release tags have (dev's builds raise the last number, 0.16.22; a release starts the next, 0.17.0), every translation catalog in step with the code and complete (`dev/i18n.py check --complete`), every setting placed for MQTT (a control in Home Assistant, or listed in `NOT_OVER_MQTT` in `plexcache/homeassistant.py` with why it isn't one, so a new setting can't be forgotten), and the README's MQTT topics and commands the ones the code has. A check like the tests: red when it fails, what it found printed above the board, and with `--tests-first` nothing is copied. With `--release` the checks also get `TNSYNC_RELEASE=1` and `PLEXCACHE_I18N_RELEASE=1`, so the suite checks the catalogs as GitHub does for `main`. Without `--release` none of this runs, so a normal `dev` run never fails on translations or the version. Use it as `tnsync dev --release` (or `local --release`) when getting dev ready to release; `tnsync release` is something else, the newest release tag synced.
8. **Dependencies** (below).
9. **Style Approval** (`TN_STYLE=python3 dev/style.py check`: the looks waiting for your approval, see "The style guide" below), on its own: green when every look is approved, orange when some wait, its block saying how many and the command to approve them (`python3 dev/style.py review`; or `sample, then review` when the page is out of date). When looks wait, the block then makes their sample there and then (`python3 dev/style.py sample`, its bar on the block: `LOOKS WAITING: MAKING SAMPLE`; the pages photographed several at once, its workers beside the bar), in your folder, so the review opens at once; one that can't be made leaves the block `LOOKS WAITING: NO SAMPLE`, what it said above the board, never a failure. Only one sample is made at a time: a review or sample started meanwhile waits for it. `--no-sample` leaves it to the check alone, as it was: no sample, no review at the end. Once it's made, the header offers **r**, and the review page opens in your browser. And once every other block is done (none failed, on a terminal, not a dry run) with looks still waiting, the review is the block's last step on the board: blue `IN REVIEW`, the page's address its line, the board kept up until you close the page (Enter skips it: `LOOKS WAITING` again); then the check again, and the block ends green `ALL APPROVED` (`6 approved just now · commit dev/style-guide.json and dev/style-approved.zip`) or yellow with what's left. Once they're done, the block says the command instead. Nothing it found is printed above the board; it doesn't hold up the copy (`--tests-first` included) or the future tests, and its warning isn't a failure. It's the last block on the board, and runs every time, even on code that already passed, since it's quick.

Each section keeps the same three lines from the start, its whole block in its colour, so nothing moves as it fills in; when a step ends, its result is in its own block, and nothing is said again after it. What a failed step printed (the tests' failures in full), and the dependencies' box when they aren't all current, go *above* the board as the step ends, so the board stays put at the bottom of the screen: scroll up to read them. Enter stops the checks (a copy under way finishes). The test app is already running that code (unless `--tests-first`), so fix it and sync again.

The **dependencies** (`TN_UPKEEP=python3 dev/upkeep.py --tnsync`) are looked up in your folder rather than the copy while the rest runs; once the checks are done it runs again (from the day's answers) to move what it moves, so a pin never changes under the tests, and shows one last box: green when everything is at upstream's newest release; yellow with each package that has a new version and what to do about it (a major release is yours to move; a minor or patch one moves by itself on `dev`); grey when upstream couldn't be reached. With `dev` checked out, the minor and patch releases are moved there and then, in your folder: the box lists them, to test (the next `tnsync`) and commit. With `main` checked out, only minor and patch releases count: they're moved there the same way, and a major release isn't reported at all (majors are only ever taken on `dev`, and reach `main` when dev is released). On any other branch it only reports.

**If the NAS already has exactly these files** (compared by content, so the same code from another branch counts too), nothing is copied and the app isn't restarted. If the last tests tnsync ran on that code passed, the Sync block says it's already in sync and each check's block that it already passed (grey), and nothing runs but the dependencies. If they failed (or there's no result yet, `TN_TEST`, `TN_VALIDATE`, `TN_TYPES` or `TN_FUTURE` changed, or the last run was a different command such as `tnsync master`), the Sync block says the files are in sync (grey) and the checks run again. The last result is kept in `~/.local/state/tnsync/`.

With `--tests-first`, the copy waits for the checks, and if any fail (from the first failing test, not once the suite ends) **nothing is copied and the app isn't restarted**, so the NAS keeps running the last good code. Use it when the test app needs to stay working (for example, before syncing `master` or a release).

`master`, `dev`, `release` and `beta` copy exactly what's in git at that point; they never touch your working folder.

tnsync isn't specific to this project — any project with a `.tnsync` file in its root can use it.

## Setting it up (once)

### 1. On TrueNAS

1. **SSH:** System → Services → SSH: turn it on (and tick "Start automatically").
2. **Your key:** Credentials → Users → `truenas_admin` → Edit → paste your computer's public key (`cat ~/.ssh/id_ed25519.pub`; make one with `ssh-keygen -t ed25519` if you don't have it) into **Authorized Keys**. Check it works: `ssh truenas_admin@<nas-ip>`.
3. **Permission to restart apps:** on the same user, add `/usr/bin/midclt` to **Allowed sudo commands with no password**. (tnsync tries `midclt` directly first and falls back to `sudo -n midclt`.)
4. **Folders for the test app** — in the TrueNAS shell (change the paths if your apps live elsewhere):
   ```sh
   sudo mkdir -p /mnt/ssd/AppConfigs/plex-cache-dev/src /mnt/ssd/AppConfigs/plex-cache-dev/config
   sudo chown truenas_admin /mnt/ssd/AppConfigs/plex-cache-dev/src
   sudo chown 568:568 /mnt/ssd/AppConfigs/plex-cache-dev/config
   ```

### 2. On your computer

1. **Put tnsync on your PATH:**
   ```sh
   chmod +x ~/Code/TrueNAS-Plex-Caching/dev/tnsync
   mkdir -p ~/bin
   ln -sf ~/Code/TrueNAS-Plex-Caching/dev/tnsync ~/bin/tnsync   # ~/bin must be on your PATH
   ```
   (If `tnsync` then says "permission denied", the `chmod +x` was missed.)
2. **Your NAS's address**, shared by every project — `~/.config/tnsync/config`:
   ```
   TN_HOST=192.168.1.10
   TN_USER=truenas_admin
   TN_PORT=22
   ```
3. **This project's settings** — `.tnsync` in the project root (it's in `.gitignore`, so each person keeps their own):
   ```
   TN_APP=plex-cache-dev
   TN_DEST=/mnt/ssd/AppConfigs/plex-cache-dev/src
   TN_PATHS=plexcache
   TN_EXCLUDE=.git .DS_Store __pycache__ *.pyc
   TN_TEST=python3 tests/run.py --exclude WebBuildTests --exclude StyleGuideTests
   TN_VALIDATE=python3 tests/run.py tests.test_plexcache.WebBuildTests
   TN_TYPES=python3 dev/typecheck.py
   TN_AUDIT=python3 dev/audit.py
   TN_FUTURE=python3 dev/future.py --exclude StyleGuideTests
   TN_STYLE=python3 dev/style.py check
   TN_RELEASE=python3 dev/release.py
   TN_UPKEEP=python3 dev/upkeep.py --tnsync
   ```
   `tnsync init` asks for these and writes both files for you. Settings are read as `KEY=value` lines, not run — except `TN_TEST`, `TN_VALIDATE`, `TN_TYPES`, `TN_AUDIT`, `TN_FUTURE`, `TN_STYLE`, `TN_RELEASE` and `TN_UPKEEP`, which are run as commands, so only put commands there you'd type yourself.

### 3. The test app

1. Run `tnsync local` once **before** installing the test app, so its code folder exists (otherwise Docker creates an empty one and the app won't start).
2. Open `compose.dev.yaml`, make the media lines match your real `plex-cache` app (and Plex), and set your NAS's IP in `x-portals`.
3. TrueNAS: Apps → Discover Apps → ⋮ → **Install via YAML**, name it `plex-cache-dev`, paste the file.

After that, `tnsync local -y` is all you need.

## Working without a NAS

```sh
npm ci --prefix dev     # once (and when dev/package-lock.json changes): what the CSS tests need
python3 tests/run.py    # the test suite, with fake Plex, TrueNAS and MQTT servers; two workers per core (-j 1: one test at a time)
python3 dev/coverage.py # the same, showing how much of the code the tests reach (Python 3.12+; add --missing for line numbers)
python3 tests/demo.py   # the UI with sample data at http://127.0.0.1:8642
DEMO_NO_L2ARC=1 python3 tests/demo.py 8651   # the same, on a pool without an SSD cache
```

## Screenshots for the README

`docs/screenshots/` is made from the demo, with an invented library (no real data), by `dev/screenshots.js`. It needs Node and Playwright (`npm i -g playwright && npx playwright install chromium`). Start the two demos it reads from, then run it:

```sh
sudo mkdir -p /mnt/tank/plexfiles && sudo chown "$USER" /mnt/tank/plexfiles   # or any folder: it only shows in the paths
DEMO_SHOWCASE=1 DEMO_ROOT=/mnt/tank/plexfiles python3 tests/demo.py 8650 &
DEMO_SHOWCASE=1 DEMO_NO_L2ARC=1 python3 tests/demo.py 8651 &
NODE_PATH=$(npm root -g) node dev/screenshots.js
```

`DEMO_SHOWCASE=1` sets the demo up as a mostly healthy system with one warning left to show. The script swaps the demo's 127.0.0.1 addresses for a typical home one (192.168.1.10) in the pictures.

## The CSS, the scripts and the pages

`plexcache/web`'s CSS, scripts and pages are written as they're read, checked against one floor of target browsers and built for the image by `dev/web.js`. The floor is an age: every browser version out in the last five years (`"browserslist"` in `dev/package.json`, `last 5 years`; in November 2026 that's Safari and iOS 15.2, Chrome and Edge 96, Firefox 95 — about 97% of use). One age holds every browser to the same date, so the CSS, the scripts and the HTML can't disagree on what's supported, and the window moves on by itself: each build is checked against the browsers of its own day, so what an old browser lacks stops counting once it falls out of the window, and the pages may then use it. QQ Browser is left out (caniuse's data for it: an old engine without `min()`).

- **The CSS** is written **without browser prefixes** (`-webkit-`, `-moz-`, `-ms-`, `-o-`): write the standard property and leave the prefixes to Autoprefixer. It may use newer standard CSS that **postcss-preset-env** rewrites for the targets (nesting, the media range syntax `(width >= 600px)`, `lab()` and `oklch()` colours and the rest of its stage 2 features): the build writes them out as CSS the oldest targets read, keeping the newer form beside it where the plugin does. What it can't rewrite (container queries, subgrid, `:has()`) is held to the floor as before. Only `app.css` is built: the pages' and scripts' own styles and the themes' CSS reach the browser as written, so they keep to CSS the targets read. An old Safari throws out a whole rule when one of its selectors is new to it, so a newer selector (`:focus-visible`, `:has()`) gets a rule of its own rather than sharing one (`.x:hover{…}.x:focus-visible{…}`, not `.x:hover,.x:focus-visible{…}`); cssnano, told the targets, keeps them apart.
- **The checks**: Stylelint on the CSS, ESLint on the scripts and html-validate on the pages and on every page as the app draws it — each with its recommended checks (html-validate's `standard`), none turned off — and each with a check that nothing it uses is missing from a target: stylelint-no-unsupported-browser-features (caniuse's data), eslint-plugin-compat, and for the HTML a check of every element, attribute and input type against MDN's data (`dev/web.js`). The CSS is checked twice: as written (Stylelint's checks, no prefixes), and as the oldest targets get it — what postcss-preset-env makes of it, the newer forms left out — for what a target lacks, each finding told by the line it's written on. So a feature the build rewrites passes, and one it can't is found. A finding is fixed in the code.
- **What the pages can do without** (`"graceful"` in `dev/package.json`): the few features a target lacks that only do nothing there — a focus ring an old Safari draws its own way, a cursor on a phone, `loading="lazy"` on posters — each named with why. The checks pass over these only; a new one is a choice, and `WebBuildTests` lists them. When the window has passed one (the checks no longer find it with the entry taken out), it comes out.

The image build (the Dockerfile's `web` stage, on Node's LTS) runs `dev/web.js build` on each: the CSS gets its newer features rewritten for its targets and the prefixes they still need and is minified with cssnano, the scripts are minified with Terser and the pages with html-minifier-terser; those copies replace the source's in the image. `tnsync` copies the source as it is, so the test app serves it unprefixed and unminified — fine for a current browser.

```sh
node dev/web.js lint                         # Stylelint, ESLint and html-validate on plexcache/web
node dev/web.js build plexcache/web/app.css  # what the image ships, to stdout (--no-minify: prefixed only); a .js or .html the same
```

The suite checks it (`WebBuildTests`): no prefix written by hand in the CSS, the pages' and scripts' own styles or the themes' CSS; every check clean on every stylesheet, script and page, on the server's `/theme.js` and on every page as drawn before and after the first checks; the floor and the graceful list as they're meant to be; no newer selector sharing a rule; the CSS build clean, only adding prefixes and fallbacks (every declaration written kept, in order); the CSS check passing what postcss-preset-env rewrites and finding, by its line, what it can't; the built scripts drawing every page as the source does, and the built pages keeping every tag and word. They need `npm ci --prefix dev` (GitHub's test job runs it); without node they skip, like the page tests.

## The style guide

Every look the app wears is one the maintainer approved, and `dev/style-guide.json` lists them all: each rule of `plexcache/web/app.css` (by its selector and media query, with its declarations), each `style="..."` a page or script writes, each style property a script sets, each drawing (an icon, the grape arrow, a chart's marks), what `plexcache/web/decor.js` draws (run on a fixed page by `dev/decor-draws.js`, so a change to its code that draws the same needs no approval), each part of the themes' CSS (`themes.py`'s `css()` and `script()`), every colour, font and decoration of the built-in themes, and every image and font the app ships. `StyleGuideTests` fails on any look that isn't in the guide, isn't as the guide has it, or has gone from the code, and on a guide edited by hand (its seal won't match).

A new or changed look goes in only like this:

```sh
python3 dev/style.py check     # what differs from the guide
python3 dev/style.py sample    # the review page: each of those Before (as approved) and After (now), side by side
open dev/style-sample/index.html
python3 dev/style.py review    # the maintainer, at a terminal: the page with Approve all and an Approve per change
python3 dev/style.py approve   # or, at a terminal, type "approve" to add them all
```

`sample` needs Playwright, like the screenshots (`npm i -g playwright && npx playwright install chromium`); it shows the dev tools' progress bar while it works. It starts two demos with the same data, today's app and the app as approved (today's with the approved look files put back: the copy `approve` keeps in `dev/style-approved.zip`, or a folder given with `sample --before <folder>`), photographs each look on both in Classic and Dionysus, light and dark, cropped to the same spot, and compares them pixel for pixel. The page shows only what looks different, Before and After side by side (with a button that marks the pixels that changed); a look drawn on no demo page as its code, Before and After; and, folded away, the looks whose code changed but that drew exactly the same, which need no looking at (approving still covers them). A look seen on whole pages (a theme's colour, a shipped image) is judged against its own file put back alone, so another change on the page isn't taken for it; `decor.js` can't run with another day's styles, so its looks are judged against the whole Before. When the Before itself isn't exactly as the guide has it, the page says which looks. Words aren't a look: what the app says is content, so rewording a title, a label or a tooltip needs no approval (in a look, any text with a letter in it is `…`: between tags, in the attributes people read, in a stylesheet's `content`), and the Before takes today's pages, scripts and translations for a stylesheet's rules and the whole-page looks, so a reworded title doesn't show as a difference either. Each pair has "Highlight the difference" and a slider view (full width: click or drag on the picture, or the slider under it, to move between Before and After). `review` serves that page to this computer only (127.0.0.1, with a token the page's buttons send) while it runs, and only when started at a terminal: "Approve all", or "Approve" on one change, puts those looks in the guide (each only if it still differs exactly as the sample showed it), and an approved change leaves the page; it closes once everything is approved, or after an hour unused (`review --board` is tnsync's r: what it says goes to `dev/style-sample/review.log`); once everything is approved it keeps `dev/style-approved.zip` too. `check` fails with how many looks wait and, on tnsync's board, a line linking the page (or saying it's out of date). `approve` only takes a sample of exactly the looks that differ now (its fingerprint; change the code after and it asks for a new sample), keeps the sample's fingerprint and the date in the guide, keeps the approved look files in `dev/style-approved.zip` (the next sample's Before; commit it with the guide), and won't run without a terminal. Nobody but the maintainer approves, edits the guide or loosens the test (`CLAUDE.md`). A theme someone uploads on System → Themes is their own: the guide covers the code.

## Security audits

Security audits are done by hand (a model, or a person), and `dev/audit.py` keeps track of what each one saw, line by line, so nothing is forgotten and you can see where auditing is thin.

**Each audit has two halves** with the same id (`2026-11-10-1`: the day and a count, never who did it):

- **public**, here in `security/audits/<id>.toml`: the day, the dev commit audited, `full` or `partial`, and a partial's scope (whole files, or whole functions as `path:function`);
- **private**, in the private audits repo (checked out beside this one, `~/Code/TrueNAS-Plex-Caching-audits`; `private` in `dev/audit.toml`, or `PLEXCACHE_AUDITS`): the **auditor** (`fable-5.1`, `opus-5.5`, or a person as `human:name`), its **thoroughness** (1 the most thorough … 5; for a model, its effort: 1 max, 2 xhigh, 3 high, 4 medium, 5 low), and its findings. Its `rules.toml` holds the release rules, since they name auditors; its `FORMAT.md` every field.

**A line counts as seen** by an audit when it's in the audit's scope and hasn't changed since the audit's commit: `git blame` says which commit last changed it, and that commit is the audit's or one before it. So a line goes stale by itself when it changes; nobody marks anything. `dev/audit.toml` says what counts as app code (`plexcache/`, the web's scripts, pages and CSS, the `Dockerfile`, `compose.yaml`, the workflows, `dev/tnsync`; not the tests, translations or docs).

**The rules:**

- on dev (from `dev/audit.toml`, any auditor): code no audit has seen waits no longer than `grace_days` (7), nor adds up to more than `grace_share` (10%) of the app code — so audits come in batches, about one a week, rather than line by line. Sensitive code (the private `rules.toml`'s `[sensitive]`) doesn't wait: committed and unaudited, it's orange at once;
- at release (`--release`, from the private `rules.toml`): a full audit by an auditor `accept` takes (`opus>=5.5 thoroughness<=3`: Opus 5.5 or a later Opus, at high or more thorough), no older than `max_age_days`, and every line since seen by one it takes; the release candidate's tag (annotated: `git tag -a`) at least `soak_days` old; no finding open at `block_open` or above; and every finding's answer for main.

**A public declaration, private requirements.** With only the public half (GitHub, anyone's clone) the block says what's covered, full or partial, and what's waiting, and adds a notice that the auditor and thoroughness requirements weren't checked: a notice, never a failure, so anyone can run it. With the private repo too, it also lists the open findings and holds the release rules; and if its `rules.toml` has no `[release] accept`, or an entry it can't read, that's orange (red at release), so a requirement that isn't set doesn't go unnoticed.

**Doing one:**

```sh
python3 dev/audit.py next                   # what most needs an audit, file by file: sensitive first, then the longest waiting
python3 dev/audit.py start                  # the standard prompt (the private PROMPT.md) for a full audit of this commit
python3 dev/audit.py start plexcache/peerlink.py plexcache/app.py:login    # …or a partial one
# run the audit, then fill in a copy of the private repo's templates/audit.toml
python3 dev/audit.py record filled.toml     # checked, split: security/audits/<id>.toml here, audits/<id>.toml there
```

`record` also fills in each finding's `introduced_in` (the commit that brought it in, from `git blame`) and `missed_by` (the earlier audits that had those lines in scope after that, and didn't report it), so you can see, over time, what each auditor and thoroughness finds and misses. Then commit both halves, each in its own repo.

## Python and the other dependencies

The code needs Python 3.15 or later. The image is built on `python:3.15-slim-trixie` (`PYTHON_TAG` in the Dockerfile), whatever 3.15.x is newest at the build, and the build workflow rebuilds the release weekly so a new patch release or Debian package reaches the image within a week. MKVToolNix comes from its own repository (mkvtoolnix.download, which is why the image is Debian), ffmpeg built from its release with no external libraries (the optimizer only copies streams, hashes them and reads cover art; the static builds around carry every codec library, and Debian's package 500 MB of dependencies), oxipng from its releases and jpegtran built from libjpeg-turbo's, each pinned in the Dockerfile. GitHub runs the tests on 3.15 with every deprecation warning an error, and the Python validation after them (`dev/typecheck.py`: `pyright`, whose `pyrightconfig.json` makes an error a fault that would fail at run time and keeps the annotation warnings at zero, and Ruff's lint, `ruff.toml`, kept at zero too).

**Kept current by a check, not by memory.** `python3 dev/upkeep.py` (and tnsync's last box, which runs it): once a day it asks upstream for the newest release of everything the app depends on — the Python and the Debian the image is built on, MKVToolNix, ffmpeg, oxipng, libjpeg-turbo, pyright and Ruff (a Ruff minor release is a prompt, since its minors change what its rules find), and the Node LTS and packages that check and build the CSS, scripts and pages (`dev/package.json`, moved with npm so its lock moves too) — against what the project pins. Home Assistant is one too: for each new release, every key the MQTT discovery messages use (`DISCOVERY_KEYS` in `plexcache/homeassistant.py`, held to what's published by `test_discovery_keys_are_listed`) is looked for in that release's MQTT sources, still taken and not deprecated or removed, and `HA_CHECKED` moves to it; a key it no longer takes is a prompt naming it. A pin a patch or minor release behind is moved there and then — in a checkout of `dev`, which carries the newest software, or of `main`, which takes only minor and patch releases (a major is never looked at there: majors are only taken on `dev`, and reach `main` when it's released); on any other branch, or in a copy, it's reported — the Dockerfile or workflow edited, said in the output (MKVToolNix's every release counts as minor, since each carries fixes); a major release, or a new Debian or Python, fails the test naming it, until it's moved or held by choice. `--report` moves nothing, `--fresh` asks again. `test_dependencies_are_current` (by hand, not on GitHub) reads only the last look's answers, however old, so the suite never waits on upstream or npm: it fails on what's left to you (on `dev`, a major; on `main`, nothing, since its minor releases move by themselves; elsewhere, anything behind), and skips until there's been a look. The GitHub Actions and the image's base are Dependabot's, on `main` (with its security updates) and on `dev`. `python3 dev/versions.py` prints what an image carries; each build logs it.

**The future track.** `python3 dev/future.py` builds the image on the next Python's newest prerelease (`python:3.16.0a1-slim-trixie`, say, while the image runs 3.15) and runs the whole suite inside it with every warning an error. The tools come the same way: for each, upstream's newest pre-release when it's ahead of the stable release, else that stable release — Python's alphas, betas and rcs as Docker Hub tags them; Node's nightlies and rcs from nodejs.org (the page tests need node; the image serves no pages through it, so the build is fetched once, kept in `~/.cache/plex-cache/node/`, and mounted into the container); ffmpeg's git snapshot, built from source once a week (`FFMPEG_SOURCE` and `FFMPEG_STAMP` in the Dockerfile); libjpeg-turbo's and oxipng's GitHub pre-releases; every package `dev/package.json` pins (both sections) at the newest npm dist-tag ahead of `latest` (`next`, `beta`, `rc`…), else `latest`, installed in a copy of `dev/package.json` kept in `~/.cache/plex-cache/web-future/` and mounted over the checkout's `node_modules` (yours stay at the pins; a pre-release outside another package's peer range is installed with `--legacy-peer-deps`); MKVToolNix has releases only. The web's checks run a year ahead too: the software tried is about a year ahead of the image's, so `dev/web.js` takes `"futureBrowserslist"` (`last 4 years`) in place of `"browserslist"` (`PLEXCACHE_WEB_TARGETS=future`). The run starts with a table of what it chose, kept for a day in `~/.cache/plex-cache/future.json` (`--fresh` asks again; `python3 dev/upkeep.py` reports it too); upstream not reached means the pin. `--stable` keeps every tool at the Dockerfile's pin, with Debian's node; `--tag 3.15-slim-forky` will try the next Debian the same way. The build shows as one line that moves (its step, what it's doing, the time); its full output goes to a log, shown when the build fails, or on the terminal with `-v`. The suite then runs through `tests/run.py`, with its progress bar. It needs Docker with a running engine, and uses BuildKit when Docker has it (`docker buildx`; on a Mac, `brew install docker-buildx`, then the `cliPluginsExtraDirs` line brew prints in `~/.docker/config.json`), else Docker's legacy builder. `--local` uses a `python3.16` of your own (or `uv`, which fetches prerelease builds). It is local on purpose: GitHub shows how the app runs; this shows where it is going. When the next Python is released and the suite is clean on it, moving the image is `PYTHON_TAG` in the Dockerfile and `python-version` in `build.yml`.

## Troubleshooting

- **"can't SSH to …"** — SSH isn't running on TrueNAS, or your key isn't on `truenas_admin` (step 1.2).
- **Copied, but the restart failed** — `/usr/bin/midclt` isn't in the user's no-password sudo commands (step 1.3).
- **The test app won't start after installing** — the code folder was empty: run `tnsync local -y`.
- **Tests fail only on your computer** — the suite has to pass on your own Python as well as the 3.15 in the image (the code needs 3.15 or later; `dev/future.py` tries the next Python's prerelease), and a system other than Linux (macOS, say) has no `/proc` and lacks some Linux-only calls (such as `os.posix_fadvise`): tests that touch those need to fake them or work without them.
