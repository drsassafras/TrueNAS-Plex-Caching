<img src="plexcache/web/logo.svg" alt="" width="96" align="right">

# Plex Cache for TrueNAS

**Superspeed your HDD-based Plex server.**

Plex Cache is a small app for TrueNAS that keeps the parts of your Plex library you're about to watch or listen to in ZFS's caches — RAM (ARC) and SSD (L2ARC) — so playback starts, seeks and moves on to the next episode or track at cache speed instead of waiting for hard drives.

Everything is set up in a web UI: what to cache, how much, and when. Health checks tell you whether your system is set up to benefit — with the exact command to fix anything that isn't — and the Metrics page shows whether it's actually working.

<p align="center">
  <img src="docs/screenshots/activity.png" alt="The Metrics page: a weekly report card, four headline numbers and a chart of skips served from cache" width="860">
</p>

## Screenshots

| | |
|---|---|
| [<img src="docs/screenshots/health.png" alt="Health page" width="420">](docs/screenshots/health.png)<br>**Health** — every check with a plain-English fix | [<img src="docs/screenshots/features.png" alt="Features page" width="420">](docs/screenshots/features.png)<br>**Features** — three caching features, each with its own limits |
| [<img src="docs/screenshots/l2arc-sizing.png" alt="L2ARC sizing" width="420">](docs/screenshots/l2arc-sizing.png)<br>**L2ARC sizing** — how big an SSD to buy, from your own library | [<img src="docs/screenshots/media-mappings.png" alt="Media Mappings" width="420">](docs/screenshots/media-mappings.png)<br>**Media Mappings** — finds your library folders for you |
| [<img src="docs/screenshots/activity-chart.png" alt="Metrics chart" width="420">](docs/screenshots/activity-chart.png)<br>**Over time** — every measure by hour, day or month | [<img src="docs/screenshots/activity-dark.png" alt="Activity page in dark mode" width="420">](docs/screenshots/activity-dark.png)<br>**Dark mode** — follows your system |

## Features

The web UI has nine pages: **Health**, **Caching** (what gets cached and how much RAM it may use), **SSD Cache** (your L2ARC and how big it needs to be), **Library Health** (what's where in each file, the blocks it lands on and what caching keeps of it), **Optimizer** and **Processes** (rewriting files so they suit caching, and what each job did, file by file), **Sync** (other Plex servers with the same files, and what's kept in step with them), **Metrics** and **System** (connections, access, media mappings, disk speed, MQTT, updates).

### L2ARC or RAM only

Caching is planned around an SSD cache (L2ARC) on the media pool, and Health warns when there isn't one. To run on RAM only, tick **I won't use an L2ARC** on the SSD Cache page. Dropdowns start with an explicit **Default** choice that names what it means right now (e.g. *Default — At 90%*), and mark the other choices: a green RAM icon on the default used without an L2ARC, a blue SSD icon on the default used with one, a grey SSD icon on L2ARC options (they fall back to the RAM default without an L2ARC), and a grey RAM icon on RAM options (ignored when there's an L2ARC). The same icons sit beside settings that are wholly one kind, and a legend at the top of the Caching page explains them. Defaults are looked up every time caching runs, so they change by themselves when an L2ARC is added or lost. A choice that needs an L2ARC falls back to its RAM-only default when there isn't one, and Health lists any setting that had to fall back.

### Three caches, one page each

The Caching page has a card for each cache, with each library's choices on it, and help behind "How it works".

- **Now Playing Cache** — once a stream is playing and Plex's own buffer has had time to fill, reads the file being played into RAM from the play position onwards, then the beginning, so skips land in cache.
  - Start timing worked out from the file's bitrate and your disks' measured speed (direct play and transcoding buffer differently), or a fixed delay.
  - The whole file if it fits its RAM limit, otherwise as much as fits ahead of the viewer — and it starts again from the new spot after a big skip.
  - Reads a set number of files at a time, so hard drives aren't asked to seek between many streams at once.
- **Lead-in Cache** — a file's *lead-in* is its first seconds plus its seek index: everything a player needs to start it instantly. Per library: the lead-in length, a warm-up schedule, and how far Prefetch reaches.
  - **Warm-up** (needs an SSD cache, L2ARC) puts the lead-in of every movie, episode and track in a library on the SSD: at boot only, or at boot plus twice monthly (the 1st and 15th), weekly, twice weekly (the chosen day and three days later) or daily, at one shared time — or not at all, with "Run Now" for any time. There's no on/off switch: warm-up runs for every library that has a schedule. Default follows the caching mode: at boot only with an L2ARC, no warm-up without one.
  - **Prefetch**, once Now Playing Cache is done, loads the lead-ins of what's likely to play next, in Plex's own order (across seasons and any folder layout): don't prefetch · next episode · rest of the season · whole season · next season · all seasons · the whole library (from "whole season" on, an L2ARC is needed). Music and audiobooks use the same ladder for tracks and chapters, albums and books (music has no "next album"); movies follow their Plex collections.
  - Lead-in length is in seconds of playback (by default 60 for movies, 30 for TV and 10 for audio), scaled by each file's own bitrate, so a 4K remux gets more bytes than a 720p episode. The seek index (MKV Cues, MP4 `moov`) is found from the file's own layout. If your settings don't fit the SSD, lead-ins are shortened automatically.
- **Whole File Cache** — near the end of what's playing (at 80% or 90%), Prefetch loads whole files, per library: nothing · next episode · rest of the season. On RAM only, music's default is nothing: RAM is scarce and tracks are short; with an L2ARC it's the rest of the album, like the others. Episodes already watched aren't offered in full: they'd only push lead-ins off the SSD.
- Each Prefetch step includes the ones before it, read in order, so the next episode always comes first. The season is that of what plays next: at the last episode of season 1, "rest of the season" means season 2.
- **Needed soon → RAM, needed later → SSD.** RAM keeps one-off reads for hours at most, so only the next episode in full and the rest of the season's lead-ins are read straight into it. Everything further ahead — earlier episodes' and the next season's lead-ins, all seasons, the whole library, the rest of the season in full — needs an L2ARC and goes to the SSD in the background, at the L2ARC pacing and outside the RAM limit; without one it falls back to the RAM default. Each choice in the dropdowns says which way it's read. Defaults follow the caching mode (with an L2ARC: lead-ins of the whole library and the rest of the season in full; on RAM only: the rest of the season's lead-ins and the next episode in full).

### Cache Settings and RAM Only Settings

- **RAM limits that respect your NAS** — Now Playing Cache and Prefetch each get a share of the ZFS cache that's actually available (never more than its maximum), minus a reserve left for the rest of the NAS. They cover what's meant to stay in RAM — Now Playing Cache and whole files, and without an L2ARC lead-ins too — with or without an L2ARC. With one, lead-ins only pass through RAM on their way to the SSD (that's how ZFS fills an L2ARC), so they don't count; the L2ARC pacing keeps them gentle.
- **Cooldown** — everything cached is logged, lead-ins in one log and whole files in another, and isn't read again until its cooldown has passed, whichever feature asks. A whole file counts as a lead-in too, so warm-up skips files Prefetch just read, and Now Playing Cache doesn't re-read a file Prefetch loaded near the end of the last one. RAM keeps one-off reads for hours at most, so on RAM only the cooldowns in RAM Only Settings apply (2 hours for lead-ins and 3 for whole files, by default; from none or 10 minutes up); with an L2ARC everything read is taken to be on the SSD, and the L2ARC cooldowns on the SSD Cache page apply instead (1 day for lead-ins and 6 hours for whole files). Timed warm-ups only skip lead-ins Prefetch cached since the last warm-up, so they always re-check their own — what's still on the SSD is a quick hit, and what the L2ARC has since overwritten (it reuses its space oldest first) is read again; "Run now" reads everything.

### L2ARC Cache Settings: big jobs in the background

On the SSD Cache page. Everything that needs an L2ARC — warm-up, and the Prefetch choices with the grey SSD icon (all seasons, the whole library, the rest of a season in full...) — reads in the background at the L2ARC pacing. By default that's just under how fast ZFS copies to the SSD (`l2arc_write_max`): anything read from the disks faster leaves RAM before it's copied, while reads that come from the SSD already aren't held back. It also steps aside, then carries on from the same file, while a player is buffering, while Now Playing Cache or Prefetch is reading for someone, during a scrub or resilver, or when the disks are busy with other work. Caching that works on RAM alone — Now Playing Cache, and Prefetch's smaller steps like the next episode — never waits, so the reads that help most always happen, while large ones can't get in the way. Prefetch's L2ARC-only reads share one queue for every player: the newest request goes to the front (after the file being read finishes), and everything in it is read in the end, even after its player stops.

### Health checks that explain themselves

- **Plex and TrueNAS** — server reachable, signed in, API key accepted. The TrueNAS connection is read-only.
- **Libraries** — opens a real file in every library folder, and suggests the right folder when the app has it mounted somewhere else. Each library also shows the record size of its dataset (always read from the dataset: from TrueNAS, or from ZFS itself) and the one recommended for it from its average bitrate: 512K under 1 Mbps (music, audiobooks), 1M up to 15 Mbps (FLAC, SD to 1080p encodes), 2M up to 40 Mbps (4K encodes, 1080p remuxes), 4M above (4K remuxes), and 128K for files under 1 MB. A second best is named too, the neighbouring size on the side the bitrate leans to. **Ideal** when the dataset uses the recommended size, **Pass** when it uses the second best (fine too, so a library near the line between two sizes doesn't flip between them), **Warning** otherwise; the column's ⓘ says where to change it in TrueNAS.
- **Media Datasets** — for every dataset a library is on, one column per check, each with an ⓘ saying what it checks, why it matters and what to do about a warning:
  - **Cache settings** — `primarycache` and `secondarycache`;
  - **Dataset record size** — that the setting suits every library on it (each library's recommended size or second best). When no one size can, it names the library to move to a dataset of its own, since a record size is set per dataset;
  - **File record size** — that every file is written in that size (**All files match**). A new record size only applies to files written afterwards; every health check looks at every file (one `stat()` each, a fraction of a second for 20,000 files), and the [optimizer](#the-optimizer-files-written-for-caching) writes the others again;
  - **Compression** — ZSTD at levels 3 to 19 passes; ZSTD-3 (plain "ZSTD" in TrueNAS) is recommended. From level 3, ZFS gives up early on what won't compress (most of a video), so higher levels only make marginally smaller files for more CPU when writing — reading is no slower at any level — and on a mostly idle NAS that's your call. ZSTD-1 and 2 (slightly less compression, no faster in practice), LZ4, ZSTD-FAST and anything else get a warning;
  - **L2ARC** — the SSD cache on its pool.

  Also flags datasets mounted in the app that no library uses, and works out which dataset really holds a library when datasets are mounted inside one another.
- **ZFS and RAM** — how much RAM is free for preloading, and whether ZFS settings would stop warm-up from landing on the SSD (for example, warm-up reading faster than ZFS copies to the SSD).
- **Is It Working?** — compares warm-up runs to show how much was already cached, and a day after each run checks a sample of its lead-ins: how many are still on the SSD. The L2ARC reuses its space oldest first, so that's how long lead-ins really last on your NAS.
- Every problem comes with the fix — a setting to change or a command to copy — and features that can't work on your system are switched off with the reason, coming back on by themselves once it's fixed.

### The optimizer: files written for caching

A second container, `plex-cache-optimizer`, rewrites media files so they suit caching. It's optional, and the only part of the app that writes media. It does nothing on its own: on the **Optimizer** page, choose for each library what it should do, then press **Optimize Now**. Until it works, the top of the Optimizer page checks, in order, everything it needs — in the app's YAML, running, answering, holding the app's token, with its tools, with media it may write — and shows only what's left to do, with the lines to paste filled in for your NAS. With a TrueNAS API key that can see apps, it can tell "not in the YAML" from "stopped right after starting". Health → Plex Cache says in one line whether it's ready.

Per library:

- **TrueNAS compression & record size** (on by default) — writes each file again, byte for byte, so it takes its dataset's current record size and compression: ZFS applies both only to files as they're written. Any kind of file. The copy is read back and must have the same SHA-256 as the original before it replaces it. A file's compression can't be read back from it, so Plex Cache keeps a record of its own (below); the record size is read from each file.
- **Optimize MKV files** (off by default) — for each MKV file it:
  - remuxes it with MKVToolNix (`mkvmerge --no-attachments`), keeping every track as it is — image subtitles (PGS, VobSub) get zlib, mkvmerge's default; video, audio and text subtitles aren't compressed;
  - handles cover art (image attachments, which some DLNA players and TVs show) as the library chooses with **Cover art**:
    - **Plex poster, unmatched items only** (the default) — when Plex can't match something to an online entry (home videos, concerts, rare releases), it has nowhere to fetch artwork from again, and the most reliable place to keep it is in the file. Those get Plex's poster (whatever you set in Plex), losslessly compressed as below; matched items have their cover art taken out, since Plex fetches it anyway. Plex marks unmatched items with a `local://` guid;
    - **Strip it** — takes it out of every file;
    - **Keep it, compressed** — keeps it, losslessly compressed: PNG with [oxipng](https://github.com/shssoichiro/oxipng) (`-o 4 --strip safe`, which keeps colour profiles and transparency), JPEG with jpegtran from libjpeg-turbo (`-copy all -optimize`: new Huffman tables, the picture and every marker untouched, still baseline JPEG so every TV shows it). Each result is decoded and compared with the original pixel for pixel, and kept only if it's smaller;
    - **Plex poster, every item** — puts Plex's poster in every file (a movie's own; an episode's season poster, or the show's), as `cover.jpg` or `cover.png`, losslessly compressed the same way. The app downloads the poster as Plex has it (not resized), and rewrites the file again when the poster in Plex changes;
  - handles the file's tags (titles, notes, and the statistics MKVToolNix writes for each track — Plex reads none of them) as the library chooses with **Tags**:
    - **Keep tags** (the default) — nothing is lost: ffprobe, Jellyfin and MediaInfo keep the track durations and bitrates they read from the statistics tags, and home videos Plex can't match keep their titles and notes. Usually a few KB, but some tools write hundreds of KB, cached with every file;
    - **Strip excessive tags** — only files with over 256 KB of tags (the ones Library Health warns of) lose them;
    - **Strip tags** — every file loses its tags, global and track (and no new statistics tags are written): the least metadata to cache, but other players show less. Plex is unaffected.

    The optimizer's own tag is always written back, whichever is chosen;
  - puts the attachments back **after the video** (`mkvpropedit`), least compressible first — images, then fonts, then text — and moves the index and tags after them. The lead-in is then all video, and everything that compresses sits together at the end of the file, next to the padding of its last record;
  - writes it in the dataset's current record size and compression;
  - checks, with ffmpeg, that every video, audio and subtitle stream is exactly the same as before, and every attachment is byte for byte what was meant to go in.

A running job shows on the Optimizer page (and on Health) with a progress bar, the file in hand and an estimate of when it'll be done (from how fast files are really written on your NAS), with **Pause** and **Cancel** (both after the file in hand). It pauses by itself while the pool is resilvering — that pause can't be lifted by hand — and starts paused if a resilver is running. Its progress is saved after every file, so a restart carries on where it was ("Optimization Resumed"); a file the optimizer was in the middle of is simply done again (the original is only ever replaced whole). Only one file activity runs at a time: optimizing, syncing cover art, or moving a folder to its own dataset.

Either way it works one file at a time, skips anything playing and anything written in the last 10 minutes (still being imported), keeps each file's owner, permissions, timestamps and extended attributes (and skips files whose owner it can't keep), and swaps the new file in with an atomic rename.

**Two kinds of record**, because the two kinds of optimizing last differently:

- **The ZFS record** is about this NAS: a file synced elsewhere is written again there. Plex Cache keeps it in its config folder (`zfs-record.json`): each dataset's record size and compression, and since when they've been seen (the date the record began, then the date of each change, with the earlier settings); and each file's compression, which can't be read from the file:
  - **Optimal** — the optimizer wrote it, with the dataset's compression now;
  - **Assumed** — it first showed up at a check when the dataset's settings were the same as at the check before, so it was written with them;
  - **Unknown** — it was there when the record began, or showed up right after the settings changed;
  - **NotOptimal** — it's known to be written with another compression.

  Optimal and Assumed need nothing; Unknown and NotOptimal are written again. A file written with ZSTD 3 to 19 is never NotOptimal, whatever level the dataset uses now, so changing between those levels never rewrites a library. The exact compression each file was written with is kept either way. Datasets and files gone for six months are dropped.
- **The MKV record travels with the file**: the optimizer tags every file it optimizes (`PLEXCACHE_OPTIMIZER` = its version, a global Matroska tag; the file's own tags are kept), so a file copied, migrated or synced to another system isn't done again there. The optimizer has its own version, **0.1.2**: a new minor version (0.2) means files are done again, a new patch version (0.1.1) doesn't, and a file tagged by a newer optimizer is never touched by an older one. Plex Cache remembers each file's tag with its size, modification time and inode, so it reads a file's tag once; after that, telling what's done is one `stat()` per file.

**Converting to MKV** (off until you tick **Convert to MKV** for a library). Which kinds of file are converted is chosen once, on the Optimizer page: AVI, MPEG-TS/M2TS, MPEG-PS (VOB, MPG), FLV, OGM/OGV, RealMedia and 3GP by default; MP4/M4V and MOV only if you add them (Apple's apps and some TVs play them directly, and they can hold iTunes details MKV doesn't keep). Music and audiobooks are never converted.

- **Inspected first.** What MKVToolNix reads of the file (`mkvmerge -J`) is compared with everything ffprobe sees in it. If anything wouldn't be kept — a track MKV can't hold (Teletext, an MP4 closed-caption track), chapters, cover art, details such as an iTunes show and episode — or it's protected by DRM, the file is left as it is, with the reason (Processes and Library Health say it), and isn't tried again unless it changes.
- **Checked after.** The MKV must hold every video and audio track with the same codec and number of packets, every subtitle track (MP4 text subtitles become SRT), and any closed captions inside the video. Then it's optimized like any MKV, and given the original's owner, permissions and timestamps.
- **Plex keeps its metadata.** The MKV is written next to the original (`<name>.mkv`, never over an existing file) and Plex is asked to scan that folder, so it adds the MKV as a second version of the same item, even when libraries are scanned by hand. Once Plex has it, the original is kept for **Keep each original after Plex has its MKV** (7 days by default: time for a second Plex server on a synced copy of the files to pick the MKV up first), then deleted and the folder scanned again. The item keeps its edits, watch history and collections. If Plex makes the MKV an item of its own, nothing is deleted: merge the two in Plex and it carries on, or choose **Keep Both**. The list of converted files is on the Optimizer page, with **Delete Original Now**.
- **A second Plex server** with the same files (another site, synced) can be watched too, on the **Sync** page (Second Plex Server). Choose it from your Plex account's servers — the sign-in this server already has lists them; **Use Another Plex Account** is for a server on another account — (its addresses are tried, direct ones first; at another site it needs Remote Access, or a VPN), and check how each library is paired with one there (by name and kind unless you choose). A file's path there is its place in its library folder, under that library's folders there, and the page shows what share of each library's files were found that way. An original is then deleted only once both servers have the MKV as a version of the same item — or the other doesn't have the file at all, which counts only when the paths line up (90% of the library's files found), never on a guess. Its folder there is scanned too.

**Library Sync** (the Sync page) compares this server's libraries with a mirrored server's — another Plex server with the same files, at another site, kept in step by syncing the folders. This part only reads; nothing on either server is changed.

- **Mirrored server** is a choice to make: *Not chosen yet* (the default: Health asks, and converted files' originals aren't deleted, since a mirror that isn't watched could lose its item), *None*, or *Yes*, the server watched. **Look for mirrors** checks your Plex account's other servers, weekly and when the Sync page opens: a sample of each server's files is looked up here by path, and a server with nearly all of them here "looks like a mirror" — a click watches it. Another Plex Cache sharing the folders is noticed by the marker it leaves there (the optimizer's claims).
- **Comparing** reads each paired library from both servers and matches items by their files' place in the library folder (the same on both), falling back to the online entry; shows, seasons, artists and albums are matched through their episodes and tracks. The differences are listed source → the other server, by kind: a file one server doesn't list (it needs a scan), versions grouped differently, another match (or unmatched on one side), edited fields (Plex locks a field you edit), posters and backgrounds chosen by hand, collections, labels, and editions. Watched state and ratings aren't compared: Plex's own "Sync my watch state and ratings" carries them. The counts are on the Sync page; each library's items on the Library page's **Sync** view. It runs every few hours (**Compare every**), and on **Compare Now**.
- **Whose edits win** (the source) is yours to set: this server, or the mirror.
- **Nothing acts without a review.** Applying the differences is a later part, and will only ever run after you've looked them over and approved. The settings as last reviewed are kept: whenever one changes (the source, what's compared, a library pair), applying pauses until you've seen how the differences change and pressed **Review & Approve** again. Comparing carries on meanwhile. Health says so.

**Cover art is set once**, when a file is first optimized. A Plex poster already in a file is kept (unless the choice is “Strip it”), and a later change of choice, or a new poster in Plex, doesn't touch optimized files: **Sync Cover Art** does, when you ask. Each Plex poster embedded is tagged `PLEXCACHE_COVER` with the SHA-256 of the poster as Plex has it (before it was compressed), so Sync Cover Art only rewrites files whose poster changed.

**Several systems sharing a library** (Syncthing, both ways) can all optimize it:

- **The same bytes everywhere:** the output is deterministic (`mkvmerge --deterministic`, seeded from the file's content; attachments written by the optimizer itself with their original IDs), so two systems optimizing the same file at once write identical files.
- **Claims:** before remuxing a file, a system claims it — a small file of its own in the library folder (`.plexcache/claims/`); the earliest claim wins, and the others leave the file alone. Claims are made a few files ahead, so they've synced by the time each file's turn comes; a system that never sees another one in the folder doesn't wait. Byte-for-byte rewrites (record size, compression) aren't claimed: they're per NAS, and they keep the modification time, so Syncthing doesn't even notice them.
- **Collisions:** if two systems still change a file at once, Syncthing keeps both (`name.sync-conflict-…`). **Optimize Now** deletes such a copy when it's one of ours — both tagged by the optimizer, every stream and attachment the same — and Health → Plex Cache lists any others for you to look at.

The Optimizer page shows, per library, how many files still need something under its choices, or **Complete**. `python -m plexcache.optimize --check FILE…` checks single MKV files, and `python -m plexcache.optimize FILE…` optimizes them.

**Processes.** A page of its own lists every file job — optimizing, syncing cover art, converting a folder to a child dataset — with the one running now (progress, estimate, **Pause**, **Cancel**) and a log kept six months: each job with when it started, where it picked up after each restart (its resume points), its pauses and why, and how it ended; and each file with how long it took, its size before and after, the verification outcome (passed, failed or not checked), its record size and compression before and after, whether its layout was optimized, and what happened to its cover art (removed, compressed, Plex's poster added…). **Export CSV** and **Export JSON** download it all. A file that fails a check is left as it was; the failures stay listed (and Health says so) until dismissed.

**Convert Folder to Child Dataset.** Health suggests it when one record size can't suit every library on a dataset ("Split dataset"); each library folder is marked *Ideal already*, *Sufficient* or *Split dataset*. A folder can't become a dataset in place, so the Optimizer page walks through it — **Convert Folder to Child Dataset** — and the folder ends up a child dataset at the same path, so Plex and every app see the same files where they were, with their metadata. Plex keeps running; it's restarted once, at the end. The page remembers which step it's at, across restarts.

1. **Create** the dataset next to the folder (`<name>-new`), with the record size and compression shown — in the TrueNAS shell (`sudo zfs create -o recordsize=… -o compression=zstd …`) or the web UI (Datasets → Add Dataset; the page lists the steps). Nothing is copied until it's checked: there, with that record size (or the second best) and a ZSTD 3–19 compression (read through the TrueNAS API), mounted and writable in the optimizer, with room for a second copy.
2. **Copy**: the optimizer copies the folder into it, each file checked (SHA-256, read back), with owners, permissions, times and extended attributes. Pause, resume and stop from the page; it pauses by itself while something plays. Stopped or interrupted, it carries on where it was.
3. **Switch**: a last pass copies what changed meanwhile and compares both copies, file by file; the old folder is moved out of the way (`.<name>.plexcache-old`, undoable); you rename the dataset to the folder's name (`sudo zfs rename …`, or Rename in the web UI where TrueNAS offers it). Turn off Plex's "Empty trash automatically after every scan" until the end, so the files' metadata isn't dropped while they're briefly out of sight.
4. **Verify**: restart Plex; the page checks the folder is the dataset with its settings, every file is there, and Plex shows as many items as before. Then **Delete the Old Copy** (the optimizer can only delete a `.…plexcache-old` folder it made).

It needs the optimizer's media volume to use `propagation: rslave` (in `compose.yaml` and the setup lines), so a dataset made while it runs shows up in it without a restart; the page checks that too. Snapshots keep the old copy's space until they expire.

**Testing it.** `tests/fixtures` holds small MKV files with as many real-world features as practical — H.264 and HEVC, AAC, AC-3, Opus and FLAC, SRT, ASS with fonts, PGS (one without compression) and VobSub, chapters, global and track tags, IETF languages, names, flags, HDR colour, JPEG and PNG cover art — made by `dev/make_fixtures.py`, with a snapshot of what optimizing them gives. The tests compare against it, and the image's build runs them inside the image, with its own MKVToolNix and ffmpeg: if an update of the tools places or changes something differently, the build fails before the image is published.

Things to know: snapshots keep the old copy of every rewritten file until they expire; replication sends each one again; Plex re-analyses each changed file.

### Metrics: is it actually helping?

- A report card for today, the last 7 days, 30 days or 12 months:
  - **Skips served from cache** — jumps of more than 30 seconds that landed in cached data: read by Now Playing Cache, a whole file that was cached (Whole File Cache, or an earlier play), or a jump back into the cached lead-in.
  - **Fully cached age** — of everything read again, the age up to which it was all still cached.
  - **Plays started from cache** — of everything played, how much started from a lead-in (or whole file) already cached, by warm-up, Prefetch or an earlier play.
- An "Over Time" chart for each measure (by hour, day or month, with a table view): skips, data cached ahead, disk load, and **still cached 30 min later** — half an hour after each read into RAM, eight 1 MiB reads across it are timed; back within 4 ms means it came from cache (RAM, or the SSD if it moved there), since a hard drive takes longer just to reach the data. Reads on their way to the SSD aren't checked here; the next-day check after each warm-up covers the SSD.
  - **How Long Cached Data Lasts** — every time something cached is read again, the read is timed, so it says whether what was cached that long ago was still there. Added up by age, in RAM and on the SSD apart, that's how long cached data really lasts on your NAS, and the app suggests cooldowns from it. Ages count from the last time the app read that data, as the cooldown does; RAM reads from before a reboot are left out. A short cooldown (down to none) collects data fastest; the write history is kept for 6 months, across restarts.
  - **Data cached ahead** — what Now Playing Cache and Prefetch read into RAM for viewers, and how much of it was in cache already (each 32 MiB is timed); Prefetch's background reads to the SSD are shown separately.
- A "Needs Attention" list that names the cause and the setting to change.
- RAM-limit use, how busy the disks were while people watched, what's playing and being cached now, warm-up runs and a live event log. History is kept for a year.

### Easy to set up and live with

- **Sign In with Plex** — no token hunting. Plex and TrueNAS addresses are filled in when they run on the same NAS.
- **L2ARC Sizing Estimator** — how much cache the lead-in of everything in your libraries takes, in RAM without an L2ARC and on the SSD with one, counted in whole records as ZFS caches them. Four columns per library: *Target minimum* (the first record of each file at the recommended record size, indexes at the front), *Your minimum* (the first record at your record size, plus the index for files keeping it at the end), *Target* (the recommended record size, indexes at the front, at the lengths on the sliders) and *Your library* (the block sizes your files are written with now, at your settings). Below them: the system buffer (room for everything else ZFS caches; the larger it is, the longer cached files stay on the SSD) and the RAM the L2ARC uses to keep track of what it holds. Warm-up, Prefetch and Now Playing Cache read in whole records of each library's dataset, as ZFS does. Warm-up never caches less than a library's minimum: when the L2ARC is too small it shortens every lead-in by the same share, as little as fits, down to one record. A lead-in slider all the way down is one record too, on the Caching page and in the estimator; the estimator's sliders start from the default lengths. Leave room on top for how much your libraries will grow.
- **Disk speed benchmark** per dataset, so cache hits and preload timing are judged against your actual disks.
- Help behind small ⓘ buttons instead of walls of text, a ↺ to put any setting back to its default, changes saved as you go, dark mode, and a layout that works on a phone.
- **MQTT, for Home Assistant and more** (optional) — publishes health, streams, RAM in use, ARC and L2ARC, warm-up progress, the Metrics figures and lifetime totals, and takes commands. In Home Assistant a "Plex Cache" device appears on its own with 60–80 entities, including switches, buttons and sliders; Node-RED, openHAB and other MQTT tools can use the same topics. Status lights show each step of the connection. Follows the MQTT 3.1.1 and Home Assistant discovery standards, checked by its tests ([Standards](#standards)). See [MQTT](#mqtt-home-assistant-node-red-and-more).
- **Sign-in** for the web UI — a username and password set in the app's YAML (on by default; the example password is refused) — and **local network only**: devices outside your network are refused (System → Access).
- **Update notices** — checks GitHub every 6 hours (can be switched off in System) and says when there's a newer version, in the web UI and in Home Assistant.
- **Safe by design** — runs as TrueNAS's unprivileged `apps` user in a read-only container, with media mounted read-only and all extra permissions dropped; it writes only to its settings folder. Pure Python with no third-party dependencies. The only part that writes media is the optional [optimizer](#the-optimizer-files-written-for-caching), a second container that acts only when you ask it to. If its settings folder isn't writable it says how to fix it, rather than restarting endlessly.

## Install on TrueNAS

TrueNAS doesn't support third-party app catalogs since 24.10, so this installs as a custom app.

1. **Create a dataset for settings**, e.g. `tank/apps/plex-cache`, and give the `apps` user (568) read/write access.
2. **Find your Plex app's media mounts.** In Apps → Plex → Edit, note each *host path* and the *mount path* inside the container (for example `/mnt/tank/plexfiles` → `/data`).
3. **Apps → Discover Apps → ⋮ → Install via YAML**, name it `plex-cache`, and paste [`compose.yaml`](compose.yaml). Change the lines marked `CHANGE ME`:
   - the settings dataset → `/config`
   - each media host path → **the same mount path Plex uses**, read-only. Add a line per mount if Plex has several.
   - for the optimizer (the second service, optional): the same settings folder, read-only, and the media it may rewrite, writable. That's all: it finds the app's token in the settings folder, and it can only change files under the media folders you mount into it — leave out any you don't want changed (or the whole service). It runs as user 568; files it can't write, or whose owner it can't keep, are skipped (see the comments in the YAML for media owned by another user). If you add it later, the Optimizer page shows the lines to paste, filled in for your NAS.
   - `PLEXCACHE_PASSWORD` (and the username, if you like) → your own; the example password is refused. See [the web UI login](#the-web-ui-login).
4. Open `http://<your-nas-ip>:8642` and sign in.
5. Open **System**. If Plex and TrueNAS run on the same NAS, their addresses are filled in for you; otherwise enter your NAS's IP (Plex's port 32400 is added automatically). Choose **Sign In with Plex** — Plex only shares library and playback details with apps that hold a token. Optionally add a TrueNAS API key (user menu → API Keys) so Health can see your cache devices and dataset settings. The app only reads from the API. Status lights on the page show what's working.
6. If a library shows as not found, either mount it at the same path Plex uses, or mount it anywhere and pick the folder under **System → Media Mappings**. Folders that contain the library's files are marked ✓.
7. Check **Health** — it should say “Everything is OK” — then switch features on in **Caching**. If the media pool has no SSD cache (L2ARC), see **SSD Cache**.

### The web UI login

The web UI asks for a username and password, set under `environment:` in the app's YAML (Apps → plex-cache → Edit):

```yaml
      PLEXCACHE_USERNAME: plex-cache
      PLEXCACHE_PASSWORD: "something-long"
```

The example password in `compose.yaml` (`change-me`) is never accepted — anyone can read it here — so set your own when you install. Signing in with it explains what to change. The sign-in page's ⓘ button says where to find and change these settings.

To turn sign-in off, put a `#` in front of both lines (or remove them). The web UI then opens without a password, for anyone on your network; Health notes it, and warns if **Local network only** is off too.

The browser stays signed in for 30 days (a signed, HttpOnly cookie; the signing key lives in the config folder). **Sign out** ends that session on the server too, so a copied cookie stops working, and changing the password signs everyone out. Wrong passwords are answered one at a time, a second apart (guessing in parallel is no faster), and logged under Metrics → Recent Events; after 10 wrong passwords from one device, it's refused for 15 minutes. Behind a reverse proxy that sends `X-Forwarded-Proto: https`, the cookie is marked Secure. Scripts can use HTTP Basic auth. `/healthz` stays open so TrueNAS can check the app is up.

### Who can reach it

**Local network only** (System → Access, on by default) answers only devices on your network: private addresses (10.x, 172.16–31.x, 192.168.x), the NAS itself, link-local and IPv6 local addresses, and VPNs into your network with their own range, such as Tailscale (100.64–127.x). Anything else gets a page saying why, signed in or not. Behind a reverse proxy, the proxy's address is what counts. It can only be turned off from a device on your network.

Without a sign-in, the app also only answers to its IP address, `localhost` or a local name (`nas.local`, `truenas`), so a web page elsewhere can't reach it through a look-alike name (DNS rebinding). Using another name, set `PLEXCACHE_URL` to it, or turn sign-in on.

## MQTT (Home Assistant, Node-RED and more)

Off until you switch it on in **System → MQTT**. MQTT is the common language of home-automation and monitoring tools: everything connects to a *broker*, which passes messages along. Plex Cache connects to your broker as a client.

The settings card shows a light for each step — broker found, TLS, login accepted, publishing — with the reason when one fails, and a **Reconnect Now** button. Turn off **Allow control** for a watch-only connection. Broker ACLs can limit the app's login to the `plexcache/#` and `homeassistant/#` topics.

### MQTT – Home Assistant

<p align="center">
  <a href="docs/screenshots/home-assistant.png"><img src="docs/screenshots/home-assistant.png" alt="The Plex Cache device in Home Assistant: device info, controls for each feature, sensors and recent activity" width="860"></a>
</p>

With **Home Assistant discovery** on (the default), a **Plex Cache** device appears in Home Assistant by itself — nothing to add to `configuration.yaml`. Its **Visit** link opens the Plex Cache web UI.

1. Install the **Mosquitto broker** add-on and the **MQTT** integration.
2. Add a Home Assistant user just for this app (Settings → People → Users, e.g. `plexcache`). Mosquitto accepts any Home Assistant login.
3. In Plex Cache, tick **Publish over MQTT** and enter the broker address (your Home Assistant's IP), port `1883`, and that username and password.

For TLS, use port `8883` and tick **Use TLS**. The Mosquitto add-on only opens 8883 when it has certificate files (by default `fullchain.pem` and `privkey.pem` in Home Assistant's `/ssl` folder); otherwise its log says `SSL not enabled - No valid certs found!` and the port is closed. If the certificate is for a domain name (e.g. Let's Encrypt) and you connect by IP, leave **Verify** off — the connection is still encrypted.

What Home Assistant gets — a main **Plex Cache** device with three linked ones, so everything sits under a clear heading (each has its own page and dashboard card):

| Device | Sensors | Diagnostic |
| --- | --- | --- |
| **Plex Cache** | health (ok / warning / problem) and summary, *Problem*, streams playing, *Streaming*, what's being preloaded | checks passed / warnings / failures, last check, Plex and TrueNAS connected, health check / benchmark running, version, record size and cache settings per dataset |
| **Plex Cache Metrics** | the Metrics figures for today, 7 or 30 days: verdict, streams, skips (and from cache), plays started from cache, fully cached age, suggested cooldown and the one in use, library cached within 24 hours / 7 / 30 days, still cached 30 min later, data cached ahead, disks busy, items needing attention; lifetime totals for long-term statistics | |
| **Plex Cache Warm-up** | state (off / idle / preparing / running / paused), progress, why it's paused, next and last run, last result, already cached, L2ARC growth | files done / total, data read, files in the last run |
| **Plex Cache RAM & ZFS** | Now Playing Cache and Prefetch RAM in use, ARC size and hit ratio, L2ARC filled | RAM limits, ZFS cache available, L2ARC size |

Open any entity and its **Attributes** say what it measures or does, in a sentence or two.

The main device also has the **controls** — a switch for Now Playing Cache (warm-up and Prefetch follow each library's choices); buttons to run or stop warm-up, re-run checks and benchmark disks; RAM limits, L2ARC pacing and reserve; poll interval, and the two Prefetch choices for each library — and an **update** entity that shows when a newer version is on GitHub, with a link to the project.

The **Visit** link uses the address and port your browser last used to open Plex Cache. Behind a reverse proxy, set `PLEXCACHE_URL` (e.g. `https://plexcache.example.com`) in the app's YAML instead.

### MQTT – other tools

Node-RED, openHAB, ioBroker, Telegraf and the like can use the topics directly (`plexcache` can be changed). Turn off **Home Assistant discovery** if you don't need it.

| Topic | |
| --- | --- |
| `plexcache/state` | everything, as one retained JSON message — sent every 30 s and straight away after a change |
| `plexcache/availability` | `online`, or `offline` (also sent by the broker if the app disappears) |
| `plexcache/update` | the installed and newest version, for Home Assistant's update entity |
| `plexcache/info` | a short description of every entity (retained, sent once per connection); Home Assistant shows it under **Attributes** when you open an entity |
| `plexcache/set/<name>` | commands, when **Allow control** is on: `nowplaying_enabled` (`ON`/`OFF`, for Now Playing Cache); `warmup_pacing_auto` (`ON`/`OFF`, automatic L2ARC pacing); `run_warmup`, `stop_warmup`, `recheck`, `benchmark` (any payload); `nowplaying_ram_percent`, `nextepisode_ram_percent`, `ram_reserve_gb`, and `warmup_pacing_percent` while automatic pacing is off (a whole number, in the same range as the web UI); `poll_seconds` (`3`, `5`, `10`, `30`); `library_<key>_prefetch_start` and `library_<key>_prefetch_end` (a choice's value, such as `default`, `off`, `none`, `next`, `rest` or `library`, or its name as Home Assistant shows it) |
| `homeassistant/…/config` | Home Assistant discovery (openHAB and Domoticz read it too) |

Home Assistant offers the same settings the web UI does, under the same conditions. A control that has no effect right now shows as **unavailable** — L2ARC pacing's share of disk time while automatic pacing is on, the Now Playing Cache RAM limit while Now Playing Cache is off, the L2ARC controls without an L2ARC, *Stop warm-up* when none is running — and a command for it is refused with the reason (Recent Events). `plexcache/state` says which apply in its `available` map. The tests check every control says when it applies, so a new one can't be left out.

### Standards

Plex Cache's MQTT follows the published standards, and its tests check each rule, so an update can't quietly break them:

- **[MQTT 3.1.1](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html) (OASIS)** — the client rules for connecting (the login fields in the right order, a last will, a client ID of letters and digits that every broker accepts), topic names and filters (no wildcards when publishing, `#` and `+` only as whole levels, valid UTF-8, no null characters, 65,535 bytes at most), answering each QoS as the standard says, keep-alive pings, and a clean disconnect. A broker that breaks the rules (a malformed message, say) is disconnected from, as the standard requires.
- **[Home Assistant MQTT discovery](https://www.home-assistant.io/integrations/mqtt/)** — every discovery message names its software (`origin`), has a unique ID and a device, suggests its entity ID with `default_entity_id`, uses topic IDs Home Assistant allows, and reports availability with a last will; sensors pair device classes, units and state classes the way Home Assistant expects; controls have command topics; entities that go away are removed (an empty retained message); and when Home Assistant restarts (its `homeassistant/status` birth message) everything is announced again.

## How it works

Plex doesn't read a whole file at once — it buffers a little ahead of playback and reads more as needed. The disk delays you feel are seeks: starting a title, skipping, moving to the next episode. Plex Cache reads those parts *before* Plex asks for them, so ZFS already has them in RAM or on SSD.

- Bitrates come from Plex per file (or are calculated from size and duration when Plex has none), so a 4K remux gets more bytes cached than a 720p episode for the same number of seconds.
- Caching that needs an L2ARC is paced (by default 10% of disk time) and pauses for anything more important (see L2ARC Cache Settings above). "Run Now" runs at full speed and ignores the busy-disks pause, but still steps aside for buffering, Now Playing Cache and Prefetch reads, and scrubs.
- **Disk Speed** (System) is measured per dataset by *Benchmark Now*, which times reads of a few media files on each one. A read counts as "already cached" when it runs at least 4× faster than its dataset's speed. Unreadable files are reported, never counted as hits.
- L2ARC fills gradually and is evicted over time, which is why warm-up runs on a schedule. Size your L2ARC larger than the estimate so cached data lasts longer — even a modest SSD helps.

## Languages

The web UI follows your browser's language. To pick another one, use the language button at the top right (it's also on the sign-in page). English is the original, written in Canadian English, with US and UK spellings available. The other languages are Spanish, French, German, Portuguese (Brazil), Italian, Dutch, Polish, Russian, Ukrainian, Japanese, Korean, Chinese (Simplified and Traditional), Swedish and Turkish.

Home Assistant entity names, descriptions and state text follow **Language in Home Assistant** in **System → MQTT** (English by default). Entity IDs and enum states (`ok`, `warning`, `running` …) stay in English, so automations keep working when you change it.

The translations were made with AI and haven't been reviewed by native speakers yet, and some are still in progress. Anything not translated yet shows in English. To suggest a fix, use **Suggest a fix** in the language menu or open an issue.

## Requirements

- TrueNAS SCALE / Community Edition 24.10 or later (Docker-based apps)
- Plex Media Server reachable from the NAS
- `primarycache=all` on each media dataset for any feature to work there: ZFS only fills L2ARC from data that passed through RAM, so `primarycache=metadata` means nothing gets cached (the old scripts recommended that — it doesn't work)
- For warm-up (and Prefetch's L2ARC-only choices): at least one cache (L2ARC) device on the media pool, and `secondarycache=all` on the media dataset
- A `recordsize` that suits each library's media, by its average bitrate: `512K` under 1 Mbps, `1M` up to 15 Mbps, `2M` up to 40 Mbps, `4M` above (Health → Libraries shows the recommended size and the second best for each). A new record size only applies to files written afterwards, so Health also checks every file and warns when any don't match it.

### Rewriting files after a record size change

The optimizer does this for you: **Optimizer → TrueNAS compression & record size** writes each file again so it takes the dataset's current record size and compression.

Without the optimizer, `plexcache/tools/rewrite_blocks.py` does the same from the TrueNAS shell: it copies files that were written with smaller blocks, one at a time, and swaps each one in place. It runs as root, so fetch it over HTTPS from the release you have installed (replace the version):

```sh
curl -fsSLO https://raw.githubusercontent.com/drsassafras/TrueNAS-Plex-Caching/v0.16.0/plexcache/tools/rewrite_blocks.py
sudo python3 rewrite_blocks.py tank/plexfiles            # look only
sudo python3 rewrite_blocks.py tank/plexfiles --apply    # rewrite
```

It paces itself (`--mb-per-sec`, default 80), skips files any program has open, copies with plain reads and writes so ZFS block cloning can't reuse the old blocks, checks each copy byte for byte, keeps owners, permissions, timestamps and ACLs, swaps with an atomic rename, leaves hard-linked files alone, and stops before free space runs low. It can be stopped and run again. Snapshots keep the old blocks until they expire, and replication will send rewritten files again.

## Development

To try changes on your own TrueNAS before pushing, see [`dev/README.md`](dev/README.md): a test copy of the app plus `tnsync`, which runs the tests, copies your code to the NAS and restarts the test app in one command.

```sh
python3 tests/run.py      # progress bar; tests use fake Plex, TrueNAS and MQTT servers
python3 dev/coverage.py   # the same, with test coverage per file (Python 3.12+)
python3 tests/demo.py     # UI demo at http://127.0.0.1:8642 with sample data
node dev/screenshots.js   # README screenshots (see the file for how to run it)
docker build -t plex-cache .
```

### Translations

Each language has a catalog in `plexcache/web/i18n/<code>.json`, keyed by the English text. Texts are marked in the code with `T()`, `H()`, `P()` and `N()` in JavaScript, `data-i18n` attributes in HTML and `M()` in Python. You translate in place: a text with no translation yet is in the catalog as `null`, and the app shows the English until it's filled in.

```sh
python3 dev/i18n.py status      # how much of each language is translated
python3 dev/i18n.py sync        # after changing texts: add new ones as null, drop unused ones
python3 dev/i18n.py check       # placeholders, HTML tags and plural forms match the English
python3 dev/i18n.py leftovers   # English in app.js that isn't marked for translation
```

Keep every `{placeholder}` and HTML tag from the English. A plural is an object with the forms its language uses, e.g. `{"one": "{n} file", "other": "{n} files"}`; Russian, Ukrainian and Polish also use `few` and `many`, and Japanese, Korean and Chinese only `other`. `en-US.json` and `en-GB.json` only list the texts whose spelling differs from the Canadian original. The tests fail if a catalog is out of sync or a translation doesn't match its English.

#### Writing the texts

The English is the original (Canadian spelling), so it sets the style for every language:

- **Title Case** for what names a place or an action: page and card titles, and buttons (*Optimize Now*, *Re-run All Checks*, *Media Mappings*). Articles, short conjunctions and prepositions stay lowercase inside (*Convert Folder to Child Dataset*). The features' names are always capitalized: Now Playing Cache, Lead-in Cache, Whole File Cache, Prefetch.
- **Sentence case** for everything else: sub-headings inside a card, field and checkbox labels, choices in a list, column headers, badges, status lines and help.
- Refer to a page or card by its title (*System → Media Mappings*), and to a button or choice in quotes or bold, exactly as it reads.
- Typographic apostrophes and quotes (’ “ ”), and *the L2ARC* (an L2ARC, the SSD cache) in a sentence.
- Say a thing once per screen: a card's hint, its ⓘ and the status line under it shouldn't repeat each other, and a summary doesn't repeat the numbers shown right under it.

The tests check the apostrophes and the Title Case.

### Environment variables

| Variable | |
| --- | --- |
| `PLEXCACHE_USERNAME`, `PLEXCACHE_PASSWORD` | web UI login ([see above](#the-web-ui-login)) |
| `PLEXCACHE_URL` | the address links to the web UI should use (Home Assistant's **Visit**), when it's behind a reverse proxy |
| `PLEXCACHE_CONFIG` | settings folder (default `/config`) |
| `PLEXCACHE_PORT`, `PLEXCACHE_HOST` | where the web UI listens (default `8642` on all addresses) |
| `PLEXCACHE_DEBUG` | any value: more detail in the app's log |
| `PLEXCACHE_OPTIMIZER_URL` | where the optimizer service is (default `http://plex-cache-optimizer:8643`, the second service in the YAML) |
| `PLEXCACHE_OPTIMIZER_TOKEN` | the secret the app and the optimizer share. Not needed: the app makes one and keeps it in its settings folder (`optimizer-token`), and the optimizer reads it there (`/config/optimizer-token`, mounted read-only). Set the same value in both services to use your own |
| `PLEXCACHE_MEDIA_ROOTS` | optimizer only: the folders it may write. Not needed: by default, the folders mounted into it writable |
| `PLEXCACHE_OPTIMIZER_PORT` | optimizer only: the port it listens on inside the app (default `8643`) |
| `PLEXCACHE_TOOLS_PATH` | extra folders to find MKVToolNix, ffmpeg, oxipng and jpegtran in (e.g. the macOS app bundle when testing) |
| `PLEX_URL`, `PLEX_TOKEN`, `TRUENAS_URL`, `TRUENAS_API_KEY` | fill in those settings when they're empty (handy for scripted installs); the web UI can change them afterwards |
| `TZ` | time zone for schedules |

### The config folder

`settings.json` (all settings, including the Plex token, TrueNAS API key and MQTT password — readable only by the app), `history.json` (Metrics history), `session.key` and `signed-out.json` (web UI sessions), and small files remembering the web UI's address, a folder being moved to its own dataset (`migration.json`), the file job running now and the last one that ended (`job.json`, `job-last.json`), the job log (`job-log.jsonl`, six months), the Home Assistant entities published, the token the optimizer accepts (`optimizer-token`), this system's ID in claims (`instance-id`), the ZFS record (`zfs-record.json`: each dataset's settings since when, and each file's compression), and what each file's optimizer tag says (`optimized.json`, with its size, modification time and inode, and the SHA-256 of the file the optimizer left). `GET /api/export` downloads the settings without any secrets.
