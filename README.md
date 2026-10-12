# Plex Cache for TrueNAS

**Superspeed your HDD-based Plex server.**

Plex Cache is a small app for TrueNAS that keeps the parts of your Plex library you're about to watch in ZFS's caches — RAM (ARC) and SSD (L2ARC) — so playback starts, seeks and moves to the next episode at cache speed instead of waiting for hard drives.

Everything is set up in a web UI: what to cache, how much, and when. Health checks tell you whether your system is set up to benefit, and which features to turn on.

It replaces the old scripts in [`legacy/`](legacy/). Tautulli isn't needed any more.

## Features

| Feature | What it does | Needs |
|---|---|---|
| **Library warm-up** | On a schedule, reads the first few seconds of every movie, episode and track so they sit in L2ARC. Press play on anything and it starts from SSD. | An SSD added to the media pool as a cache (L2ARC) device |
| **Now-playing preload** | When someone presses play, reads ahead of them so skipping and scrubbing come from cache. | Works best with `primarycache=all` on the media dataset |
| **Next-episode prefetch** | While an episode plays, reads the start of the next one — using Plex's own episode order, so it crosses seasons and works with any folder layout. | Nothing extra |

The UI also shows:

- **Health**: Plex connection, whether each library's files are actually readable by the app, the media dataset and pool, L2ARC devices and size, dataset cache settings (with the exact command to fix them), and measured disk speed.
- **Activity**: what's playing and what's being cached for it, every warm-up run with its hit rate and how much L2ARC grew, and a live event log.
- **Space estimate**: move the "seconds to cache" sliders and see instantly how much L2ARC that needs. If it won't fit, seconds are reduced automatically.

## Install on TrueNAS

TrueNAS doesn't support third-party app catalogs since 24.10, so this installs as a custom app.

1. **Create a dataset for settings**, e.g. `tank/apps/plex-cache`, and give the `apps` user (568) read/write access.
2. **Find your Plex app's media mounts.** In Apps → Plex → Edit, note each *host path* and the *mount path* inside the container (for example `/mnt/tank/plexfiles` → `/data`).
3. **Apps → Discover Apps → ⋮ → Install via YAML**, name it `plex-cache`, and paste [`compose.yaml`](compose.yaml). Change the lines marked `CHANGE ME`:
   - the settings dataset → `/config`
   - each media host path → **the same mount path Plex uses**, read-only. Add a line per mount if Plex has several.
4. Open `http://<your-nas-ip>:8642`.
5. In **Settings**, enter your Plex address (your NAS IP with port 32400 — not 127.0.0.1) and choose **Sign in with Plex**. Optionally add a TrueNAS API key (user menu → API Keys) so Health can see your cache devices and dataset settings. The app only reads from the API.
6. Check **Health**, then switch features on in **Features**.

Coming from the scripts? **Settings → Import plex-l2arc.conf** carries your values over. Then remove the Tautulli script notifications and the cron job.

## How it works

Plex doesn't read a whole file at once — it buffers a little ahead of playback and reads more as needed. The disk delays you feel are seeks: starting a title, skipping, moving to the next episode. Plex Cache reads those parts *before* Plex asks for them, so ZFS already has them in RAM or on SSD.

- Bitrates come from Plex per file (or are calculated from size and duration when Plex has none), so a 4K remux gets more bytes cached than a 720p episode for the same number of seconds.
- Background work is paced (by default 10% of disk time) and pauses while several streams are playing. "Run now" runs at full speed.
- A read counts as "already cached" when it runs at least 4× faster than your measured disk speed. Unreadable files are reported, never counted as hits.
- L2ARC fills gradually and is evicted over time, which is why warm-up runs on a schedule. Size your L2ARC larger than the estimate so cached data lasts longer — even a modest SSD helps.

## Requirements

- TrueNAS SCALE / Community Edition 24.10 or later (Docker-based apps)
- Plex Media Server reachable from the NAS
- For Library warm-up: at least one cache (L2ARC) device on the media pool, and `secondarycache=all` on the media dataset

The app is pure Python with no third-party dependencies. It runs as the `apps` user with all capabilities dropped and media mounted read-only.

## Development

```sh
python3 -m unittest tests.test_plexcache -v   # tests use fake Plex and TrueNAS servers
python3 tests/demo.py                          # UI demo at http://127.0.0.1:8642 with sample data
docker build -t plex-cache .
```

Configuration lives in `$PLEXCACHE_CONFIG/settings.json` (default `/config`). `PLEX_URL`, `PLEX_TOKEN`, `TRUENAS_URL` and `TRUENAS_API_KEY` environment variables seed it on first start.
