# Developing Plex Cache

This folder holds what you need to try changes on a real TrueNAS before pushing them:

| File | What it is |
|------|------------|
| `compose.dev.yaml` | A **test copy** of the app (`plex-cache-dev`, port 8643) that runs the published image but with its code replaced by whatever you last copied to the NAS. It has its own settings, so it never touches the real `plex-cache` app. |
| `tnsync` | A small script that **copies your code to the NAS, restarts the test app and runs the tests** — in one command, from your computer. |
| `future.py` | Builds the image on the **next** Python's prerelease (3.16 while the image runs 3.15) and each tool's newest pre-release where one is ahead of its stable release (Node's nightlies, ffmpeg's git snapshot, libjpeg-turbo's and oxipng's pre-releases; MKVToolNix has releases only), and runs the suite inside it, every warning an error; `--stable` keeps the tools at their pins, `--local` uses an interpreter on your computer instead. The "future" track, local only (below). |
| `upkeep.py` | The dependency check: everything the app depends on against upstream's newest release, pins a minor behind moved, a major behind a prompt. The suite runs it by hand (below). |
| `versions.py` | Prints what an image carries (its OS, Python, MKVToolNix, ffmpeg…); each build logs it. |
| `coverage.py` | Runs the tests and shows how much of the code they reach, per file. No installs needed (Python 3.12+). |
| `codecs.py` | Codec names as MKVToolNix shows them, for Library Health (`plexcache/codec_names.py`). The image build runs it for the MKVToolNix release it installs; run it by hand to refresh the committed copy. |
| `screenshots.js` | Makes the README screenshots from the demo (see below). |
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

  -n  dry run: list what would be copied and run the tests, change nothing
  -y  restart the app afterwards without asking
  -N  don't restart and don't ask
  --tests-first  run the tests before copying, and copy nothing unless they pass
  --skip-tests   don't run the tests
  --skip-future  run the current tests only, not the future ones
```

1. **Copies only what changed** with rsync over SSH, and deletes files on the NAS that you deleted locally.
2. **Restarts the test app** with TrueNAS's own `midclt call app.redeploy plex-cache-dev`, and waits until TrueNAS says it's done. You can refresh the page and try your change from here.
3. **Runs the tests** (`python3 tests/run.py`) on exactly the code it copied, and once they pass, **the future tests** (`python3 dev/future.py`: the same suite on the next Python, see below). The run ends in one big box, green when both passed, red when one failed, with a line for each — current and future — so one glance says which is which. The test app is already running that code, so fix it and sync again. The future tests only run when the current ones pass; `--skip-future` leaves them out when you're in a hurry.

**If the NAS already has exactly these files** (compared by content, so the same code from another branch counts too), nothing is copied and the app isn't restarted. If the last tests tnsync ran on that code passed, a green banner says you're already in sync and done. If they failed (or there's no result yet, `TN_TEST` or `TN_FUTURE` changed, or the last run was a different command such as `tnsync master`), a yellow banner says the files are in sync and the tests run again. The last result is kept in `~/.local/state/tnsync/`.

With `--tests-first`, the tests run before anything is copied instead, and if any fail **nothing is copied and the app isn't restarted**, so the NAS keeps running the last good code. Use it when the test app needs to stay working (for example, before syncing `master` or a release).

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
   TN_TEST=python3 tests/run.py
   TN_FUTURE=python3 dev/future.py
   ```
   `tnsync init` asks for these and writes both files for you. Settings are read as `KEY=value` lines, not run — except `TN_TEST` and `TN_FUTURE`, which are run as commands, so only put commands there you'd type yourself.

### 3. The test app

1. Run `tnsync local -N` once **before** installing the test app, so its code folder exists (otherwise Docker creates an empty one and the app won't start).
2. Open `compose.dev.yaml`, make the media lines match your real `plex-cache` app (and Plex), and set your NAS's IP in `x-portals`.
3. TrueNAS: Apps → Discover Apps → ⋮ → **Install via YAML**, name it `plex-cache-dev`, paste the file.

After that, `tnsync local -y` is all you need.

## Working without a NAS

```sh
python3 tests/run.py    # the test suite, with fake Plex, TrueNAS and MQTT servers
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

## Python and the other dependencies

The code needs Python 3.15 or later. The image is built on `python:3.15-slim-trixie` (`PYTHON_TAG` in the Dockerfile), whatever 3.15.x is newest at the build, and the build workflow rebuilds the release weekly so a new patch release or Debian package reaches the image within a week. MKVToolNix comes from its own repository (mkvtoolnix.download, which is why the image is Debian), ffmpeg built from its release with no external libraries (the optimizer only copies streams, hashes them and reads cover art; the static builds around carry every codec library, and Debian's package 500 MB of dependencies), oxipng from its releases and jpegtran built from libjpeg-turbo's, each pinned in the Dockerfile. GitHub runs the tests on 3.15 with every deprecation warning an error, and `pyright` after them (`pyrightconfig.json`: an error is a fault that would fail at run time; the annotation warnings are kept at zero).

**Kept current by a check, not by memory.** `test_dependencies_are_current` runs when the suite runs by hand (not on GitHub): once a day it asks upstream for the newest release of everything the app depends on — the Python and the Debian the image is built on, MKVToolNix, ffmpeg, oxipng, libjpeg-turbo, pyright — against what the project pins. A pin a patch or minor release behind is moved there and then (the Dockerfile or workflow edited, said in the output; MKVToolNix's every release counts as minor, since each carries fixes); a major release, or a new Debian or Python, fails the test naming it, until it's moved or held by choice. `python3 dev/upkeep.py` is the same check as a report (`--report` moves nothing, `--fresh` asks again). The GitHub Actions are Dependabot's. `python3 dev/versions.py` prints what an image carries; each build logs it.

**The future track.** `python3 dev/future.py` builds the image on the next Python's newest prerelease (`python:3.16.0a1-slim-trixie`, say, while the image runs 3.15) and runs the whole suite inside it with every warning an error. The tools come the same way: for each, upstream's newest pre-release when it's ahead of the stable release, else that stable release — Python's alphas, betas and rcs as Docker Hub tags them; Node's nightlies and rcs from nodejs.org (the page tests need node; the image serves no pages through it, so the build is fetched once, kept in `~/.cache/plex-cache/node/`, and mounted into the container); ffmpeg's git snapshot, built from source once a week (`FFMPEG_SOURCE` and `FFMPEG_STAMP` in the Dockerfile); libjpeg-turbo's and oxipng's GitHub pre-releases; MKVToolNix has releases only. The run starts with a table of what it chose, kept for a day in `~/.cache/plex-cache/future.json` (`--fresh` asks again; `python3 dev/upkeep.py` reports it too); upstream not reached means the pin. `--stable` keeps every tool at the Dockerfile's pin, with Debian's node; `--tag 3.15-slim-forky` will try the next Debian the same way. The build shows as one line that moves (its step, what it's doing, the time); its full output goes to a log, shown when the build fails, or on the terminal with `-v`. The suite then runs through `tests/run.py`, with its progress bar. It needs Docker with a running engine, and uses BuildKit when Docker has it (`docker buildx`; on a Mac, `brew install docker-buildx`, then the `cliPluginsExtraDirs` line brew prints in `~/.docker/config.json`), else Docker's legacy builder. `--local` uses a `python3.16` of your own (or `uv`, which fetches prerelease builds). It is local on purpose: GitHub shows how the app runs; this shows where it is going. When the next Python is released and the suite is clean on it, moving the image is `PYTHON_TAG` in the Dockerfile and `python-version` in `build.yml`.

## Troubleshooting

- **"can't SSH to …"** — SSH isn't running on TrueNAS, or your key isn't on `truenas_admin` (step 1.2).
- **Copied, but the restart failed** — `/usr/bin/midclt` isn't in the user's no-password sudo commands (step 1.3).
- **The test app won't start after installing** — the code folder was empty: run `tnsync local -y`.
- **Tests fail only on your computer** — the suite has to pass on your own Python as well as the 3.15 in the image (the code needs 3.15 or later; `dev/future.py` tries the next Python's prerelease), and a system other than Linux (macOS, say) has no `/proc` and lacks some Linux-only calls (such as `os.posix_fadvise`): tests that touch those need to fake them or work without them.
