#!/bin/bash
#
# Tautulli Prefetch Script for TV Shows Only
# ------------------------------------------
# Arguments to pass from Tautulli:
# $1 = {media_type}             -> Must be "episode" for TV shows
# $2 = {file}                   -> Full path to the currently playing episode
# $3 = {progress_duration_sec}  -> Current playback offset in seconds
# $4 = {duration_sec}           -> Total duration of the episode in seconds
# $5 = {episode_num}            -> Episode number (for logging)
# $6 = {credits_marker_final}   -> 0 or 1 (consider credits if 1)
# $7 = {marker_start}           -> Start of credits marker in milliseconds (only if credits_marker_final=1)
#
# Tautulli arguments:
# "{media_type}" "{file}" "{progress_duration_sec}" "{duration_sec}" "{episode_num}" "{credits_marker_final}" "{marker_start}"

# ------------------------
# Configuration
# ------------------------
PLEX_PATH_PREFIX="/data"
TAUTULLI_PATH_PREFIX="/plexfiles"
MAX_PREFETCH_BYTES=$((6 * 1024 * 1024 * 1024))  # 6GB prefetch limit per next episode

START_TIME=$(date +%s)

log() {
    echo "$1"
}

# ------------------------
# Arguments from Tautulli
# ------------------------
MEDIA_TYPE="$1"
FILE="$2"
VIEW_OFFSET="$3"
DURATION="$4"
EPISODE_NUM="$5"
CREDITS_MARKER_FINAL="$6"
MARKER_START_MS="$7"

# ------------------------
# Only process TV episodes
# ------------------------
if [[ "$MEDIA_TYPE" != "episode" ]]; then
    log "Media type is not a TV episode: $MEDIA_TYPE. Exiting."
    exit 0
fi

# ------------------------
# Translate Plex path to Tautulli container path
# ------------------------
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
    echo "Verify the Plex App Host Path ↔ Container Path mapping you set in Tautulli."
    exit 1
fi

# ------------------------
# Extract TV show name and filename
# ------------------------
CURRENT_FILENAME=$(basename "$TRANSLATED_FILE")
TV_FOLDER=$(dirname "$TRANSLATED_FILE")
SHOW_NAME=$(basename "$TV_FOLDER")

log "Processing TV show: $SHOW_NAME, Episode: $EPISODE_NUM, File: $CURRENT_FILENAME"

# ------------------------
# Extract season & episode number (SxxExx)
# ------------------------
if [[ "$CURRENT_FILENAME" =~ [Ss]([0-9]{1,4})[Ee]([0-9]{1,3}) ]]; then
    SEASON_NUM=$((10#${BASH_REMATCH[1]}))
    EPISODE_NUM_INT=$((10#${BASH_REMATCH[2]}))
    log "Detected Season $SEASON_NUM Episode $EPISODE_NUM_INT"
else
    log "ERROR: Could not detect season/episode number from filename: $CURRENT_FILENAME"
    exit 1
fi

# ------------------------
# Build episode map
# ------------------------
declare -A EPISODES_MAP

while IFS= read -r file; do
    fname=$(basename "$file")
    if [[ "$fname" =~ [Ss]([0-9]{1,4})[Ee]([0-9]{1,3}) ]]; then
        s=$((10#${BASH_REMATCH[1]}))
        e=$((10#${BASH_REMATCH[2]}))
        key=$(printf "%04d-%03d" "$s" "$e")
        EPISODES_MAP[$key]+="$file|"
    fi
done < <(find "$TV_FOLDER" -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.m4v" \))

# ------------------------
# Sort episodes
# ------------------------
IFS=$'\n' sorted_keys=($(printf "%s\n" "${!EPISODES_MAP[@]}" | sort))
unset IFS

# ------------------------
# Find next episode
# ------------------------
CURRENT_KEY=$(printf "%04d-%03d" "$SEASON_NUM" "$EPISODE_NUM_INT")
NEXT_EP_KEY=""
found_current=0

for k in "${sorted_keys[@]}"; do
    if [[ $found_current -eq 1 ]]; then
        NEXT_EP_KEY="$k"
        break
    fi
    if [[ "$k" == "$CURRENT_KEY" ]]; then
        found_current=1
    fi
done

if [[ -z "$NEXT_EP_KEY" ]]; then
    log "No next episode found after $CURRENT_FILENAME"
    exit 0
fi

# ------------------------
# Handle duplicates
# ------------------------
IFS='|' read -r -a NEXT_EP_FILES <<< "${EPISODES_MAP[$NEXT_EP_KEY]}"
unset IFS
NUM_DUPLICATES=${#NEXT_EP_FILES[@]}

log "Next episode key: $NEXT_EP_KEY, found $NUM_DUPLICATES file(s)"

BYTES_PER_FILE=$((MAX_PREFETCH_BYTES / NUM_DUPLICATES))

# ------------------------
# Prefetch next episode(s)
# ------------------------
for file in "${NEXT_EP_FILES[@]}"; do
    [[ -f "$file" ]] || continue
    log "Prefetching: $file (max $(($BYTES_PER_FILE / 1024 / 1024)) MB)"
    dd if="$file" of=/dev/null bs=1M count=$(($BYTES_PER_FILE / 1048576)) status=none
done

# ------------------------
# Timing + reporting (FIXED)
# ------------------------
END_TIME=$(date +%s)
PREFETCH_DURATION=$((END_TIME - START_TIME))
REMAINING_PLAY=$((DURATION - VIEW_OFFSET))

if [[ "$CREDITS_MARKER_FINAL" -eq 1 && "$MARKER_START_MS" -gt 0 ]]; then
    MARKER_START_SEC=$((MARKER_START_MS / 1000))
    TIME_UNTIL_CREDITS=$((MARKER_START_SEC - VIEW_OFFSET))
    TIME_BEFORE_CREDITS=$((TIME_UNTIL_CREDITS - PREFETCH_DURATION))

    if [[ $TIME_BEFORE_CREDITS -ge 0 ]]; then
        log "Prefetch finished $TIME_BEFORE_CREDITS seconds before credits."
    else
        log "WARNING: Prefetch finished $((-TIME_BEFORE_CREDITS)) seconds after credits started."
    fi
else
    TIME_BEFORE_END=$((REMAINING_PLAY - PREFETCH_DURATION))
    if [[ $TIME_BEFORE_END -ge 0 ]]; then
        log "Prefetch finished $TIME_BEFORE_END seconds before episode end."
    else
        log "ERROR: Prefetch finished $((-TIME_BEFORE_END)) seconds after episode ended!"
    fi
fi
