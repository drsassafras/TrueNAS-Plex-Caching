#!/bin/bash
#
# Tautulli-triggered Media Preload Script for TrueNAS SCALE
#
# Features:
# - Preloads currently playing media into memory/L2ARC for skip-ahead buffering
# - Waits a calculated period to avoid competing with Plex's initial read
# - Uses file-specific bitrate or defaults to 4K if missing
# - Translates Plex file paths to TrueNAS dataset paths
# - Reports errors if file is not found with helpful context
# - Reports total execution time
#
# Usage:

# Arguments to pass from Tautulli:
# $1 = {file}                   -> Full path to the currently playing episode
# $1 = {bitrate}                -> The bitrate of the original media

#
# Tautulli arguments: feed these into the field in the Arugments tab that it sends when staring a file:
# "{file}" "{bitrate}"
#
# You will also need to configure tatulli notifycation "Script" type trigger to start when a file starts playing.
# Point that trigger to this script. (preload_current_media.sh)

# You will also need to make plex media files avalalve to tatulli by configuring the app.
# Mount your plex files as an additional volume.

# ------------------------
# Configuration
# ------------------------
PLEX_PATH_PREFIX="/data"
TAUTULLI_PATH_PREFIX="/plexfiles"

BLOCK_SIZE_MB=1         # Read in blocks of 1MB; dataset recordsize does not matter here

# ------------------------
# Start timer
# ------------------------
START_TIME=$(date +%s)

# ------------------------
# Input arguments
# ------------------------
FILE="$1"
BITRATE="$2"

# ------------------------
# Input validation
# ------------------------
if [[ -z "$FILE" ]]; then
    echo "ERROR: Media file path not passed to script."
    echo "You need to configure Tautulli to send this information."
    echo "Follow the instructions at the beginning of this script for proper setup."
    exit 1
fi

# Translate Plex path to TrueNAS path
TRANSLATED_FILE="${FILE/$PLEX_PATH_PREFIX/$TAUTULLI_PATH_PREFIX}"

if [[ ! -f "$TRANSLATED_FILE" ]]; then
    echo "ERROR: Media file not found in Tautulli docker."
    echo "File path passed from Tautulli:   $FILE"
    echo "Path translation result:          $TRANSLATED_FILE"
    echo
    echo "Configured path prefixes:"
    echo "  PLEX_PATH_PREFIX     =          $PLEX_PATH_PREFIX"
    echo "  TAUTULLI_PATH_PREFIX =          $TAUTULLI_PATH_PREFIX"
    echo
    echo "This usually indicates a Plex container mount mismatch."
    echo "Verify the Plex App Host Path ↔ Container Path mapping you set in Tatulli."
    exit 1
fi

# Set default bitrate if missing
if [[ -z "$BITRATE" ]]; then
    echo "WARNING: No bitrate provided. Using default 4K estimate of 25000 kbps."
    BITRATE=25000
fi

# ------------------------
# Calculate delay before preloading
# ------------------------
# Worst-case Plex initial buffer time ~60s
# Convert bitrate (kbps) to MB/sec
MB_PER_SEC=$(awk "BEGIN {printf \"%.2f\", $BITRATE/8/1024}")
PRELOAD_DELAY_SEC=$(awk "BEGIN {printf \"%d\", 60 * ($MB_PER_SEC / 1.0)}") # conservative disk read estimate

echo "INFO: Waiting $PRELOAD_DELAY_SEC seconds before starting preload to avoid competing with Plex."
sleep "$PRELOAD_DELAY_SEC"

# ------------------------
# Preload file into memory (L2ARC)
# ------------------------
FILE_SIZE_BYTES=$(stat -c %s "$TRANSLATED_FILE")
COUNT=$(( (FILE_SIZE_BYTES + BLOCK_SIZE_MB*1024*1024 - 1) / (BLOCK_SIZE_MB*1024*1024) ))

echo "INFO: Preloading entire media file into memory/L2ARC..."
echo "File: $TRANSLATED_FILE"
echo "Bitrate: $BITRATE kbps (~$MB_PER_SEC MB/s)"
echo "Total file size: $((FILE_SIZE_BYTES/1024/1024)) MB"
echo "Reading in $BLOCK_SIZE_MB MB blocks, total blocks: $COUNT"

dd if="$TRANSLATED_FILE" of=/dev/null bs="${BLOCK_SIZE_MB}M" count="$COUNT" status=progress

# ------------------------
# End timer
# ------------------------
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo "INFO: Preload complete. Total time: ${DURATION} seconds."
