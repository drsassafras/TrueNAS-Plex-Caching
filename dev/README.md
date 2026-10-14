# Developing Plex Cache

This folder holds what you need to try changes on a real TrueNAS before pushing them:

| File | What it is |
|------|------------|
| `compose.dev.yaml` | A **test copy** of the app (`plex-cache-dev`, port 8643) that runs the published image but with its code replaced by whatever you last copied to the NAS. It has its own settings, so it never touches the real `plex-cache` app. |
| `tnsync` | A small script that **runs the tests, copies your code to the NAS and restarts the test app** — in one command, from your Mac. |
| `coverage.py` | Runs the tests and shows how much of the code they reach, per file. No installs needed (Python 3.12+). |
| `screenshots.js` | Makes the README screenshots from the demo (see below). |

The loop is: edit on your Mac → `tnsync local -y` → refresh `http://<nas-ip>:8643`. Nothing reaches GitHub (or the real app) until you commit and push.

## What tnsync does

```
tnsync local      your folder as it is now (uncommitted changes included)
tnsync master     the latest code on GitHub's default branch (main or master)
tnsync release    the newest release tag, e.g. v1.2.0
tnsync beta       the newest tag including pre-releases, e.g. v1.3.0-beta.1
tnsync init       set up tnsync for a project

  -n  dry run: run the tests and list what would be copied, change nothing
  -y  restart the app afterwards without asking
  -N  don't restart and don't ask
  --skip-tests  copy without running the tests (not recommended)
```

1. **Tests first.** It runs this project's tests (`python3 tests/run.py`) on exactly the code it's about to copy. If any fail, it stops with a big red banner: **nothing is copied and the app isn't restarted**, so the NAS keeps running the last good code.
2. **Copies only what changed** with rsync over SSH, and deletes files on the NAS that you deleted locally.
3. **Restarts the test app** with TrueNAS's own `midclt call app.redeploy plex-cache-dev`, and waits until TrueNAS says it's done.

`master`, `release` and `beta` copy exactly what's in git at that point; they never touch your working folder.

tnsync isn't specific to this project — any project with a `.tnsync` file in its root can use it.

## Setting it up (once)

### 1. On TrueNAS

1. **SSH:** System → Services → SSH: turn it on (and tick "Start automatically").
2. **Your key:** Credentials → Users → `truenas_admin` → Edit → paste your Mac's public key (`cat ~/.ssh/id_ed25519.pub`; make one with `ssh-keygen -t ed25519` if you don't have it) into **Authorized Keys**. Check it works: `ssh truenas_admin@<nas-ip>`.
3. **Permission to restart apps:** on the same user, add `/usr/bin/midclt` to **Allowed sudo commands with no password**. (tnsync tries `midclt` directly first and falls back to `sudo -n midclt`.)
4. **Folders for the test app** — in the TrueNAS shell (change the paths if your apps live elsewhere):
   ```sh
   sudo mkdir -p /mnt/ssd/AppConfigs/plex-cache-dev/src /mnt/ssd/AppConfigs/plex-cache-dev/config
   sudo chown truenas_admin /mnt/ssd/AppConfigs/plex-cache-dev/src
   sudo chown 568:568 /mnt/ssd/AppConfigs/plex-cache-dev/config
   ```

### 2. On your Mac

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
   ```
   `tnsync init` asks for these and writes both files for you. Settings are read as `KEY=value` lines, not run — except `TN_TEST`, which is run as a command, so only put commands there you'd type yourself.

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

## Troubleshooting

- **"can't SSH to …"** — SSH isn't running on TrueNAS, or your key isn't on `truenas_admin` (step 1.2).
- **Copied, but the restart failed** — `/usr/bin/midclt` isn't in the user's no-password sudo commands (step 1.3).
- **The test app won't start after installing** — the code folder was empty: run `tnsync local -y`.
- **Tests fail only on the Mac** — the suite has to pass on the Mac's Python 3.9 as well as the 3.13 in the image, and macOS has no `/proc` and lacks some Linux-only calls (such as `os.posix_fadvise`): tests that touch those need to fake them or work without them.
