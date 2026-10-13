<img src="plexcache/web/logo.svg" alt="" width="96" align="right">

# Plex Cache for TrueNAS

**Superspeed your HDD-based Plex server.**

Plex Cache is a small app for TrueNAS that keeps the parts of your Plex library you're about to watch in ZFS's caches — RAM (ARC) and SSD (L2ARC) — so playback starts, seeks and moves to the next episode at cache speed instead of waiting for hard drives.

Everything is set up in a web UI: what to cache, how much, and when. Health checks tell you whether your system is set up to benefit, and which features to turn on.

It replaces the old scripts in [`legacy/`](legacy/). Tautulli isn't needed any more.

## Features

| Feature | What it does | Needs |
|---|---|---|
| **Library warm-up** | On a schedule, reads the first few seconds of every movie, episode and track so they sit in L2ARC. Press play on anything and it starts from SSD. | An SSD added to the media pool as a cache (L2ARC) device, plus `primarycache=all` |
| **Now-playing preload** | Once Plex reports a stream playing and its start-up buffer has had time to fill, reads the file into RAM — from the play position to the end, then the beginning — so skipping anywhere comes from cache. The whole file if it fits its RAM limit, otherwise as much as fits ahead of the viewer. | `primarycache=all` on the media dataset |
| **Next-episode prefetch** | While an episode plays, reads the next one from the start, within its own RAM limit — using Plex's own episode order, so it crosses seasons and works with any folder layout. | `primarycache=all` on the media dataset |

The UI also shows:

- **Health**: Plex connection, whether each library's files are actually readable by the app, a table of every dataset your cached libraries live on — which libraries sit on each, its cache settings, record size and the L2ARC on its pool (with the exact command to fix anything), plus any media dataset mounted in the app that no library uses. Libraries on a dataset that can't cache are skipped rather than blocking the rest. Untick a library in Settings → Media mappings to stop caching it; it's left out of the health checks too.
- **Activity**: a report card for today, the last 7 days, the last 30 days or the last 12 months — skips that landed in preloaded data, next episodes ready in time, how much preloaded data is still cached 30 minutes later, and how busy the disks were during streams — with a "Needs attention" list that names the cause and the setting to change. Each number says what it is for, and an "Over time" chart breaks every measure down by hour, day or month (with a table view). Below it: RAM-limit use, what's playing and being cached now, warm-up runs, and a live event log.
- **Space estimate**: move the "seconds to cache" sliders and see instantly how much L2ARC that needs. If it won't fit, seconds are reduced automatically.

## Install on TrueNAS

TrueNAS doesn't support third-party app catalogs since 24.10, so this installs as a custom app.

1. **Create a dataset for settings**, e.g. `tank/apps/plex-cache`, and give the `apps` user (568) read/write access.
2. **Find your Plex app's media mounts.** In Apps → Plex → Edit, note each *host path* and the *mount path* inside the container (for example `/mnt/tank/plexfiles` → `/data`).
3. **Apps → Discover Apps → ⋮ → Install via YAML**, name it `plex-cache`, and paste [`compose.yaml`](compose.yaml). Change the lines marked `CHANGE ME`:
   - the settings dataset → `/config`
   - each media host path → **the same mount path Plex uses**, read-only. Add a line per mount if Plex has several.
4. Open `http://<your-nas-ip>:8642`.
5. Open **Settings**. If Plex and TrueNAS run on the same NAS, their addresses are filled in for you; otherwise enter your NAS's IP (Plex's port 32400 is added automatically). Choose **Sign in with Plex** — Plex only shares library and playback details with apps that hold a token. Optionally add a TrueNAS API key (user menu → API Keys) so Health can see your cache devices and dataset settings. The app only reads from the API. Status lights on the page show what's working.
6. If a library shows as not found, either mount it at the same path Plex uses, or mount it anywhere and pick the folder under **Settings → Media mappings**. Folders that contain the library's files are marked ✓.
7. Check **Health** — it should say “Everything is OK” — then switch features on in **Features**.

## How it works

Plex doesn't read a whole file at once — it buffers a little ahead of playback and reads more as needed. The disk delays you feel are seeks: starting a title, skipping, moving to the next episode. Plex Cache reads those parts *before* Plex asks for them, so ZFS already has them in RAM or on SSD.

- Bitrates come from Plex per file (or are calculated from size and duration when Plex has none), so a 4K remux gets more bytes cached than a 720p episode for the same number of seconds.
- Background work is paced (by default 10% of disk time) and pauses while several streams are playing. "Run now" runs at full speed.
- **Disk speed** (Settings) is measured per dataset by *Benchmark now*, which times reads of a few media files on each one. A read counts as "already cached" when it runs at least 4× faster than its dataset's speed. Unreadable files are reported, never counted as hits.
- L2ARC fills gradually and is evicted over time, which is why warm-up runs on a schedule. Size your L2ARC larger than the estimate so cached data lasts longer — even a modest SSD helps.

## Requirements

- TrueNAS SCALE / Community Edition 24.10 or later (Docker-based apps)
- Plex Media Server reachable from the NAS
- **File indexes**: many MKVs (and MP4s without "fast start") keep their seek index at the end, and players read it before playing. Warm-up, Next-episode and (when a file doesn't fit) Now-playing also cache it — found from the file's own layout, so only those bytes are read — and each warm-up run reports what it found.
- **Library warm-up pauses** (and later carries on from the same file) while a player is buffering, while Now-playing or Next-episode is reading, during a scrub or resilver, when the pool's disks are busy with other work (40/50/60%), and optionally after a finish-by time until the next scheduled start.
- **RAM limits**: Now-playing (60%) and Next-episode (20%) share the ZFS cache that's actually available (what it holds plus free RAM, never more than its maximum), minus a reserve left for the rest of the NAS (4 GB by default, in Settings → Behaviour). Health warns when that leaves too little to be useful.
- `primarycache=all` on each media dataset for any feature to work there: ZFS only fills L2ARC from data that passed through RAM, so `primarycache=metadata` means nothing gets cached (the old scripts recommended that — it doesn't work)
- For Library warm-up: at least one cache (L2ARC) device on the media pool, and `secondarycache=all` on the media dataset

The app is pure Python with no third-party dependencies. It runs as the `apps` user with all capabilities dropped and media mounted read-only.

## Development

```sh
python3 tests/run.py   # progress bar; tests use fake Plex and TrueNAS servers
python3 tests/demo.py                          # UI demo at http://127.0.0.1:8642 with sample data
docker build -t plex-cache .
```

Configuration lives in `$PLEXCACHE_CONFIG/settings.json` (default `/config`). `PLEX_URL`, `PLEX_TOKEN`, `TRUENAS_URL` and `TRUENAS_API_KEY` environment variables seed it on first start.
