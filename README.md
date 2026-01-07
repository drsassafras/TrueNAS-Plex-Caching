# TrueNAS-Plex-Caching

**Superspeed your HDD-based Plex server!**

This project is a collection of scripts designed to dramatically improve Plex performance on **TrueNAS SCALE** systems that primarily use **HDD storage**. With intelligent preloading and caching, your Plex library can feel almost as fast as if it were entirely stored on SSDs—without the cost.

---

## What This Does

These scripts optimize how Plex reads media by leveraging:

- **System RAM**
- **ZFS ARC**
- **ZFS L2ARC (optional but recommended)**

The goal is to eliminate HDD seek latency during:
- Initial playback
- Skipping forward/backward
- Episode transitions

---

## Scripts Overview

### 1. `preload_current_media.sh`

**Works even with zero SSDs**

- Triggered by **Tautulli** when playback starts
- Waits for Plex to finish its initial buffering
- Reads the *entire currently playing file* into memory/L2ARC
- Makes skip-ahead and rewind operations instant
- Ideal for HDD-only systems

This ensures that once playback has started, all seeking happens at RAM or SSD speed instead of HDD speed.

---

### 2. Next-Episode Preload Script (TV)

- Preloads the *next episode* while the current one is playing
- Ensures smooth episode transitions
- Eliminates delays when skipping intros or credits
- Especially useful without L2ARC, but still helpful with it

---

### 3. Library-Wide L2ARC Preloader (SSD Required)

To take things even further, this script:

- Requires **at least one SSD configured as L2ARC**
- Preloads the *initial playback segments* of your entire Plex library
- Allows Plex to start playback at SSD speed for most media
- Keeps frequently accessed content hot in cache

---

## Why This Works

Plex does **not** read entire media files at once. Instead, it:
- Buffers a limited window ahead of playback
- Reads more data only when needed

These scripts take advantage of that behavior by ensuring:
- The data Plex is most likely to need is already cached
- Disk I/O happens *before* Plex asks for it
- HDD latency is avoided during user interaction

In many cases, playback and seeking are served directly from **RAM**, which can be even faster than SSDs.

---

## Result

Your Plex experience becomes:

- ⚡ Instant playback
- ⏩ Smooth seeking and skipping
- 📺 Seamless episode transitions
- 💰 SSD-like performance at HDD prices

---

## Important Notes on L2ARC Sizing

The L2ARC preload script reports how much cache space it *needs*, but:

- That number includes **reserved space**
- Cached data will naturally be evicted over time as the system is used
- This is why the preload script runs **periodically**

### Recommendations

- Size your L2ARC **larger than the reported requirement**
- Smaller L2ARC caches will evict preloaded data faster
- Even a modest SSD is far cheaper than storing your entire library on SSDs

Buy whatever SSD size you can afford—every bit helps.

---

## Setup & Configuration

Each script includes detailed setup instructions at the **top of the file**, including:

- Path translation configuration
- Tautulli trigger setup
- Recommended ZFS dataset settings

⚠️ **Be sure to read the script headers carefully before running them.**

---

## Final Thoughts

With these scripts:
- You keep the storage density and low cost of HDDs
- You gain near-SSD (or better) playback performance
- You let ZFS and RAM do what they do best

Enjoy your newly supercharged Plex server! 🚀
