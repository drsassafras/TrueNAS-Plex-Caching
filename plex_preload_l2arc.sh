#!/bin/bash
#
# Plex L2ARC Preload Script for TrueNAS SCALE
#
# Description:
# Preload the first segment of Plex media files into L2ARC to improve playback performance.
#
# Preload sizes are calculated based on **seconds of playback** and **per-file bitrate**.
# Typical Plex buffering behavior (seconds of content):
# -------------------------------------------------------------------------
# Playback Type           | Minimum Buffer | Maximum Buffer
# -------------------------------------------------------------------------
# Movies / TV
# Direct play (local)     | 3 s            | 15 s
# Transcoding (local)     | 5 s            | 30 s
# Remote / slow network   | 10 s           | 60 s
#
# Audio
# Local / Direct play     | 1–2 s          | 3–5 s
# Remote / Slow network   | 2 s            | 10 s
# -------------------------------------------------------------------------
# Defaults are configurable per content type:
# - Movies: MOVIE_PRELOAD_MIN_SEC / MOVIE_PRELOAD_MAX_SEC
# - TV Shows: TV_PRELOAD_MIN_SEC / TV_PRELOAD_MAX_SEC
# - Audio: AUDIO_PRELOAD_MIN_SEC / AUDIO_PRELOAD_MAX_SEC
#
# Dataset recommendations:
# - PRIMARYCACHE: metadata
# - SECONDARYCACHE: all

# ------------------------
# User Configuration
# ------------------------

PRELOAD_PACING_PERCENT=10          # % of system capacity to use for preloads
RESERVED_L2ARC_GB=100              # Minimum free L2ARC space to leave after preloading

# Typical HDD read speed in MB/s, used to determine cache hits dynamically
HDD_MB_PER_SEC=150

# Preload buffer in seconds
MOVIE_PRELOAD_MIN_SEC=3
MOVIE_PRELOAD_MAX_SEC=15
TV_PRELOAD_MIN_SEC=3
TV_PRELOAD_MAX_SEC=15
AUDIO_PRELOAD_MIN_SEC=2
AUDIO_PRELOAD_MAX_SEC=5

# L2ARC estimate padding
PADDING_VIDEO=5   # % padding for Movies/TV
PADDING_AUDIO=15  # % padding for Audio

# Defaults for host (normally changed by sending varables to script)
DEFAULT_PLEX_HOST="127.0.0.1"
DEFAULT_PLEX_PORT=32400
DEFAULT_PLEX_PROTOCOL="http"

# Parse positional arguments
PLEX_TOKEN="$1"
HOST_INPUT="$2"

# ------------------------
# Ensure script is running under Bash
# ------------------------
if [ -z "$BASH_VERSION" ]; then
    echo "\e[31mERROR:\e[0m This script must be run with Bash, not sh or dash."
    echo "Use: bash $0 or make the script executable and run directly or set ACL and run directly"
    exit 1
fi

DEBUG=${DEBUG:-0}

debug() {
    [[ "$DEBUG" -eq 1 ]] && echo "[DEBUG] $*"
}

debug_xml_head() {
    [[ "$DEBUG" -eq 1 ]] || return
    echo "[DEBUG] --- XML HEAD (first 30 lines) ---"
    echo "$1" | head -n 30
    echo "[DEBUG] --- END XML HEAD ---"
}


if [[ -z "$PLEX_TOKEN" ]]; then
    echo -e "\e[31mERROR: Plex token is required as the first argument.\e[0m"
    echo "Usage: $0 <PLEX_TOKEN> [HOST]"
    echo "  HOST can be just a hostname/IP (protocol=http, port=32400) or full URL"
    echo "  "
    echo "To obtain your plex token, log into the Plex Web App and look at the URL"
    echo "when streaming content, you’ll often see X-Plex-Token=<token> in the URL"
    echo "query string. Copy the token that follows."
    echo "  "
    echo "Calling this script might look like this:"
echo -e "\e[34m./plex_preload_l2arc.sh abc123xyz456\e[0m"
echo -e "\e[34m./plex_preload_l2arc.sh abc123xyz456 plex.mydomain.com\e[0m"
echo -e "\e[34m./plex_preload_l2arc.sh abc123xyz456 192.168.20.230 https 32401\e[0m"

if [ -t 1 ]; then
    echo -e "\e[31mERROR: Plex token is required as the first argument.\e[0m"
    echo "Usage: $0 <PLEX_TOKEN> [HOST]"
    echo "  HOST can be just a hostname/IP (protocol=http, port=32400) or full URL"
    echo "  "
    echo "To obtain your plex token, log into the Plex Web App and look at the URL"
    echo "when streaming content, you’ll often see X-Plex-Token=<token> in the URL"
    echo "query string. Copy the token that follows."
    echo "  "
    echo "Calling this script might look like this:"
echo -e "\e[34m./plex_preload_l2arc.sh abc123xyz456\e[0m"
echo -e "\e[34m./plex_preload_l2arc.sh abc123xyz456 plex.mydomain.com\e[0m"
echo -e "\e[34m./plex_preload_l2arc.sh abc123xyz456 192.168.20.230 https 32401\e[0m"
else
    echo -e "ERROR: Plex token is required as the first argument."
    echo "Usage: $0 <PLEX_TOKEN> [HOST]"
    echo "  HOST can be just a hostname/IP (protocol=http, port=32400) or full URL"
    echo "  "
    echo "To obtain your plex token, log into the Plex Web App and look at the URL"
    echo "when streaming content, you’ll often see X-Plex-Token=<token> in the URL"
    echo "query string. Copy the token that follows."
    echo "  "
    echo "Calling this script might look like this:"
echo  "./plex_preload_l2arc.sh abc123xyz456"
echo  "./plex_preload_l2arc.sh abc123xyz456 plex.mydomain.com"
echo  "./plex_preload_l2arc.sh abc123xyz456 192.168.20.230 https 32401"
fi


    exit 1
fi

# Determine host
if [[ -n "$HOST_INPUT" ]]; then
    if [[ "$HOST_INPUT" =~ ^http ]]; then
        PLEX_HOST="$HOST_INPUT"
    else
        PLEX_HOST="$DEFAULT_PLEX_PROTOCOL://$HOST_INPUT:$DEFAULT_PLEX_PORT"
    fi
else
    PLEX_HOST="$DEFAULT_PLEX_PROTOCOL://$DEFAULT_PLEX_HOST:$DEFAULT_PLEX_PORT"
fi

# ------------------------
# Start Script
# ------------------------
START_TIME=$(date +%s)
log() { echo "$1"; }


# ------------------------
# Arrays to hold media
# ------------------------
declare -a MOVIES_ARRAY
declare -a TV_ARRAY
declare -a AUDIO_ARRAY

MOVIES_BITRATE_TOTAL=0
TV_BITRATE_TOTAL=0
AUDIO_BITRATE_TOTAL=0

# ------------------------
# Helper: Plex API call
# ------------------------
plex_api() {
    local endpoint="$1"
    
    if [[ "$endpoint" == *"?"* ]]; then
        curl -s "$PLEX_HOST$endpoint&X-Plex-Token=$PLEX_TOKEN&includeMedia=1&includePart=1&includeGuids=0&includeReviews=0"
    else
        curl -s "$PLEX_HOST$endpoint?X-Plex-Token=$PLEX_TOKEN&includeMedia=1&includePart=1&includeGuids=0&includeReviews=0"
    fi
    
    # Detect Plex 404 HTML response (first two lines only)
    if echo "$response" | head -n 2 | grep -q '<h1>404 Not Found</h1>'; then
        if [ -t 1 ]; then
            # Console: red warning
            echo -e "\e[31mWARNING: Plex API returned 404 for endpoint:\e[0m $endpoint" >&2
        else
            # Logs / cron: plain text
            echo "WARNING: Plex API returned 404 for endpoint: $endpoint" >&2
        fi
    fi

}




# ------------------------
# Fetch all libraries (XML)
# ------------------------
sections_xml=$(plex_api "/library/sections")

# Build library list as array: title|key|type
mapfile -t library_list < <(
    echo "$sections_xml" | xmllint --format - 2>/dev/null | \
    awk '/<Directory /{print}' | \
    while read -r line; do
        title=$(echo "$line" | sed -n 's/.*title="\([^"]*\)".*/\1/p')
        key=$(echo "$line" | sed -n 's/.*key="\([^"]*\)".*/\1/p')
        type=$(echo "$line" | sed -n 's/.*type="\([^"]*\)".*/\1/p')
        echo "$title|$key|$type"
    done
)

#Debug output
debug "Libaray built:"
for i in "${!library_list[@]}"; do
    debug "library_list[$i]=${library_list[$i]}"
done

# ------------------------------
# Function: extract_media_parts
# ------------------------------
# Arguments:
#   $1 = library XML content
# Outputs:
#   echoes each media file as "path|bitrate"
extract_media_parts() {
    local xml="$1"  # Input: full XML content from Plex library/metadata

    # -------------------------------
    # Step 1: Use xmllint to select only <Part> nodes that have BOTH 'file' and 'bitrate' attributes
    # -------------------------------
    # - The XPath '//Part[@file and @bitrate]' ensures we skip any Part elements missing either attribute.
    # - This reduces processing and avoids invalid entries.
    echo "$xml" | xmllint --xpath '//Part[@file and @bitrate]' - 2>/dev/null | \

    # -------------------------------
    # Step 2: Extract the 'file' and 'bitrate' attributes using grep
    # -------------------------------
    # - The -o flag in grep ensures only the matching substring is returned.
    # - Each match looks like: file="path/to/file" bitrate="123456"
    grep -o 'file="[^"]*" bitrate="[^"]*"' | \

    # -------------------------------
    # Step 3: Format output as 'file|bitrate' for easy array processing
    # -------------------------------
    # - sed captures the values of file and bitrate and joins them with a pipe '|'.
    # - This matches the expected format in your arrays (e.g., MOVIES_ARRAY, TV_ARRAY).
    sed 's/file="\([^"]*\)" bitrate="\([^"]*\)"/\1|\2/'
}


# ------------------------
# Verify Plex API returned valid XML
# ------------------------
if ! echo "$sections_xml" | xmllint --noout - >/dev/null 2>&1; then
    if [ -t 1 ]; then
        # Output to terminal with red color
        echo -e "\e[31mERROR: Failed to fetch Plex libraries or returned content is not valid XML. Check token and host.\e[0m"
        echo -e "Returned content (first 20 lines):"
        echo "$sections_xml" | head -n20
    else
        # Output plain text (for cron logs)
        echo "ERROR: Failed to fetch Plex libraries or returned content is not valid XML. Check token and host."
        echo "Returned content (first 20 lines):"
        echo "$sections_xml" | head -n20
    fi
    exit 1
fi
    


# ------------------------
# Iterate libraries and populate arrays + bitrate totals
# ------------------------
#printf '%s\n' "${library_list[@]}" | \
while IFS="|" read -r lib_title lib_key lib_type; do

    log "Processing library: $lib_title ($lib_type)"

    # Fetch ALL items for this library from Plex
    items_xml=$(plex_api "/library/sections/$lib_key/all")

    case "$lib_type" in
        movie|show)
            # ------------------------
            # Extract media parts: file|bitrate
            # Works for Movies and TV
            # ------------------------
mapfile -t entries < <(
    xmllint --xpath '//Track/Media/Part/@file | //Track/Media/@bitrate' - <<< "$album_xml" 2>/dev/null | \
    awk '
    {
        for(i=1;i<=NF;i++){
            if($i ~ /^file=/){file=$i; sub(/^file="/,"",file); sub(/"$/,"",file)}
            if($i ~ /^bitrate=/){bitrate=$i; sub(/^bitrate="/,"",bitrate); sub(/"$/,"",bitrate)}
            if(file && bitrate){print file "|" bitrate; file=""; bitrate=""}
        }
    }'
)
# Dump all entries line by line
printf '%s\n' "${entries[@]}"


            debug "$lib_type library [$lib_title] found ${#entries[@]} parts"

            # Populate arrays and sum bitrates
            for entry in "${entries[@]}"; do
                file="${entry%%|*}"
                bitrate="${entry##*|}"
                if [ "$lib_type" = "movie" ]; then
                    MOVIES_ARRAY+=("$file|$bitrate")
                    MOVIES_BITRATE_TOTAL=$((MOVIES_BITRATE_TOTAL + bitrate))
                else
                    TV_ARRAY+=("$file|$bitrate")
                    TV_BITRATE_TOTAL=$((TV_BITRATE_TOTAL + bitrate))
                fi
            done
            ;;

        artist|audio|audiobook)
    # ------------------------
    # Audiobooks are nested under authors → albums → tracks
    # ------------------------

    # Get author IDs
    mapfile -t author_keys < <(
        xmllint --xpath '//Directory[@type="artist"]/@key' - <<< "$items_xml" 2>/dev/null | \
        grep -oP '/metadata/\K[^/]+'
    )

    printf '[Authors] %s\n' "${author_keys[@]}"

    total_tracks=0

    for author_id in "${author_keys[@]}"; do
        # Fetch albums under this author
        author_xml=$(plex_api "/library/metadata/$author_id/children")

        mapfile -t album_keys < <(
            xmllint --xpath '//Directory[@type="album"]/@key' - <<< "$author_xml" 2>/dev/null | \
            grep -oP '/metadata/\K[^/]+'
        )


        for album_id in "${album_keys[@]}"; do
            # Fetch tracks under this album
            album_xml=$(plex_api "/library/metadata/$album_id/children")

                # Extract file|bitrate
                mapfile -t entries < <(
                    xmllint --xpath '//Audio/Media/Part/@file | //Audio/Media/@bitrate' - <<< "$album_xml" 2>/dev/null | \
                    awk '
                    {
                        for(i=1;i<=NF;i++){
                            if($i ~ /^file=/){file=$i; sub(/^file="/,"",file); sub(/"$/,"",file)}
                            if($i ~ /^bitrate=/){bitrate=$i; sub(/^bitrate="/,"",bitrate); sub(/"$/,"",bitrate)}
                            if(file && bitrate){print file "|" bitrate; file=""; bitrate=""}
                        }
                    }'
                )

                  printf '[Albums] %s\n' "${entries[@]}"

                for entry in "${entries[@]}"; do
                    file="${entry%%|*}"
                    bitrate="${entry##*|}"
                    AUDIO_ARRAY+=("$file|$bitrate")
                    AUDIO_BITRATE_TOTAL=$((AUDIO_BITRATE_TOTAL + bitrate))
                done
            done
        done


    debug "Audio library [$lib_title] found $total_tracks tracks"
    ;;


        *)
            debug "Skipping unsupported library type: $lib_type"
            ;;
    esac

done




# ------------------------
# Debug: Show Movies array
# ------------------------
echo "[DEBUG] MOVIES_ARRAY (${#MOVIES_ARRAY[@]} entries):"
for i in "${!MOVIES_ARRAY[@]}"; do
    file="${MOVIES_ARRAY[$i]%%|*}"
    bitrate="${MOVIES_ARRAY[$i]##*|}"
    echo "  [$i] File: $file, Bitrate: $bitrate"
done




NUM_MOVIES=${#MOVIES_ARRAY[@]}
NUM_EPISODES=${#TV_ARRAY[@]}
NUM_AUDIO=${#AUDIO_ARRAY[@]}

if [[ $NUM_MOVIES -eq 0 && $NUM_EPISODES -eq 0 && $NUM_AUDIO -eq 0 ]]; then
    log "No media found for preloading. Exiting."
    exit 0
fi

# ------------------------
# Compute average bitrates for library planning
# ------------------------

# Log the number of entries processed
log "Processed $NUM_MOVIES Movies entries"
log "Processed $NUM_EPISODES TV show entries"
log "Processed $NUM_AUDIO Audio entries"

AVG_MOVIE_BITRATE=$(( NUM_MOVIES > 0 ? MOVIES_BITRATE_TOTAL / NUM_MOVIES : 0 ))
AVG_TV_BITRATE=$(( NUM_EPISODES > 0 ? TV_BITRATE_TOTAL / NUM_EPISODES : 0 ))
AVG_AUDIO_BITRATE=$(( NUM_AUDIO > 0 ? AUDIO_BITRATE_TOTAL / NUM_AUDIO : 0 ))

log "Average movie bitrate: $AVG_MOVIE_BITRATE bps"
log "Average TV bitrate: $AVG_TV_BITRATE bps"
log "Average audio bitrate: $AVG_AUDIO_BITRATE bps"


# ------------------------
# Compute min/max preload bytes for L2ARC planning (average-based)
# ------------------------
MOVIES_MIN_BYTES=$(( AVG_MOVIE_BITRATE * MOVIE_PRELOAD_MIN_SEC / 8 * NUM_MOVIES ))
MOVIES_MAX_BYTES=$(( AVG_MOVIE_BITRATE * MOVIE_PRELOAD_MAX_SEC / 8 * NUM_MOVIES ))

TV_MIN_BYTES=$(( AVG_TV_BITRATE * TV_PRELOAD_MIN_SEC / 8 * NUM_EPISODES ))
TV_MAX_BYTES=$(( AVG_TV_BITRATE * TV_PRELOAD_MAX_SEC / 8 * NUM_EPISODES ))

AUDIO_MIN_BYTES=$(( AVG_AUDIO_BITRATE * AUDIO_PRELOAD_MIN_SEC / 8 * NUM_AUDIO ))
AUDIO_MAX_BYTES=$(( AVG_AUDIO_BITRATE * AUDIO_PRELOAD_MAX_SEC / 8 * NUM_AUDIO ))

# Apply padding for rounding / safety
MOVIES_MAX_BYTES=$(( MOVIES_MAX_BYTES * (100 + PADDING_VIDEO) / 100 ))
TV_MAX_BYTES=$(( TV_MAX_BYTES * (100 + PADDING_VIDEO) / 100 ))
AUDIO_MAX_BYTES=$(( AUDIO_MAX_BYTES * (100 + PADDING_AUDIO) / 100 ))

MIN_PRELOAD_BYTES=$(( MOVIES_MIN_BYTES + TV_MIN_BYTES + AUDIO_MIN_BYTES + RESERVED_L2ARC_GB * 1024 * 1024 * 1024 ))
MAX_PRELOAD_BYTES=$(( MOVIES_MAX_BYTES + TV_MAX_BYTES + AUDIO_MAX_BYTES ))


# ------------------------
# L2ARC detection
# ------------------------
ZPOOL=$(zpool list -Ho name | head -n1)
L2ARC_TOTAL=$(awk '/l2_size/ {print $3}' /proc/spl/kstat/zfs/$ZPOOL/arcstats 2>/dev/null)

if [[ -z "$L2ARC_TOTAL" || "$L2ARC_TOTAL" -le 0 ]]; then
    log "ERROR: No L2ARC detected. Aborting."
    exit 1
fi


# ------------------------
# Adjust maximum preload seconds if needed
# ------------------------
AVAILABLE_BYTES=$(( L2ARC_TOTAL - RESERVED_L2ARC_GB * 1024 * 1024 * 1024 ))

if [[ $MAX_PRELOAD_BYTES -gt $AVAILABLE_BYTES ]]; then
    EXCESS=$(( MAX_PRELOAD_BYTES - AVAILABLE_BYTES ))
    TOTAL_WIGGLE=$(( (MOVIES_MAX_BYTES - MOVIES_MIN_BYTES) + (TV_MAX_BYTES - TV_MIN_BYTES) + (AUDIO_MAX_BYTES - AUDIO_MIN_BYTES) ))
    REDUCTION_FACTOR=$(awk "BEGIN {print $EXCESS/$TOTAL_WIGGLE}")
    REDUCTION_FACTOR=$(awk "BEGIN {if($REDUCTION_FACTOR>1) print 1; else print $REDUCTION_FACTOR}")

    NEW_MOVIE_MAX_SEC=$(awk "BEGIN {printf \"%.2f\", $MOVIE_PRELOAD_MAX_SEC - ($MOVIE_PRELOAD_MAX_SEC - $MOVIE_PRELOAD_MIN_SEC)*$REDUCTION_FACTOR}")
    NEW_TV_MAX_SEC=$(awk "BEGIN {printf \"%.2f\", $TV_PRELOAD_MAX_SEC - ($TV_PRELOAD_MAX_SEC - $TV_PRELOAD_MIN_SEC)*$REDUCTION_FACTOR}")
    NEW_AUDIO_MAX_SEC=$(awk "BEGIN {printf \"%.2f\", $AUDIO_PRELOAD_MAX_SEC - ($AUDIO_PRELOAD_MAX_SEC - $AUDIO_PRELOAD_MIN_SEC)*$REDUCTION_FACTOR}")

    log "Adjusted preload seconds: Movie $NEW_MOVIE_MAX_SEC s, TV $NEW_TV_MAX_SEC s, Audio $NEW_AUDIO_MAX_SEC s"
else
    NEW_MOVIE_MAX_SEC=$MOVIE_PRELOAD_MAX_SEC
    NEW_TV_MAX_SEC=$TV_PRELOAD_MAX_SEC
    NEW_AUDIO_MAX_SEC=$AUDIO_PRELOAD_MAX_SEC
fi

# ------------------------
# Shuffle files to avoid LBA clustering
# ------------------------
MOVIES_ARRAY=($(shuf -e "${MOVIES_ARRAY[@]}"))
TV_ARRAY=($(shuf -e "${TV_ARRAY[@]}"))
AUDIO_ARRAY=($(shuf -e "${AUDIO_ARRAY[@]}"))

# ------------------------
# Initialize counters
# ------------------------
MOVIE_HITS=0
MOVIE_MISSES=0
TV_HITS=0
TV_MISSES=0
AUDIO_HITS=0
AUDIO_MISSES=0

# ------------------------
# Preload function (per-file bitrate, dynamic HDD calculation)
# ------------------------
preload_files() {
    local type=$1
    local max_sec=$2
    shift 2
    local files=("$@")

    local hits=0
    local misses=0

    for file_entry in "${files[@]}"; do
        local file=$(echo "$file_entry" | cut -d'|' -f1)
        local bitrate=$(echo "$file_entry" | cut -d'|' -f2)

        # Compute preload bytes using per-file bitrate, round up to nearest 1MB
        local preload_bytes=$(( bitrate * max_sec / 8 ))
        local preload_mb=$(( (preload_bytes + 1024*1024 - 1) / 1024 / 1024 ))
        [[ $preload_mb -lt 1 ]] && preload_mb=1

        START_READ=$(date +%s%3N)
        dd if="$file" of=/dev/null bs=1M count="$preload_mb" status=none
        END_READ=$(date +%s%3N)
        DURATION_MS=$((END_READ - START_READ))

        # Expected HDD duration
        EXPECTED_HDD_MS=$(( preload_mb * 1000 / HDD_MB_PER_SEC ))

        if (( DURATION_MS < EXPECTED_HDD_MS / 4 )); then
            hits=$((hits+1))
        else
            misses=$((misses+1))
        fi

        # Preload pacing
        WAIT_MS=$(( DURATION_MS * (100 - PRELOAD_PACING_PERCENT) / PRELOAD_PACING_PERCENT ))
        sleep $(awk "BEGIN {print $WAIT_MS/1000}")
    done

    # Update global counters
    if [[ $type == "Movies" ]]; then
        MOVIE_HITS=$((MOVIE_HITS + hits))
        MOVIE_MISSES=$((MOVIE_MISSES + misses))
    elif [[ $type == "TV" ]]; then
        TV_HITS=$((TV_HITS + hits))
        TV_MISSES=$((TV_MISSES + misses))
    else
        AUDIO_HITS=$((AUDIO_HITS + hits))
        AUDIO_MISSES=$((AUDIO_MISSES + misses))
    fi
}

# ------------------------
# Preload Movies, TV, Audio
# ------------------------
preload_files "Movies" "$NEW_MOVIE_MAX_SEC" "${MOVIES_ARRAY[@]}"
preload_files "TV" "$NEW_TV_MAX_SEC" "${TV_ARRAY[@]}"
preload_files "Audio" "$NEW_AUDIO_MAX_SEC" "${AUDIO_ARRAY[@]}"

# ------------------------
# Summary Report
# ------------------------
echo "===== Preload Summary ====="
# Movies hit rate
if (( MOVIE_HITS + MOVIE_MISSES > 0 )); then
    MOVIE_HIT_RATE=$(awk "BEGIN {printf \"%.2f\", $MOVIE_HITS/($MOVIE_HITS+$MOVIE_MISSES)*100}")
else
    MOVIE_HIT_RATE=0
fi

# TV hit rate
if (( TV_HITS + TV_MISSES > 0 )); then
    TV_HIT_RATE=$(awk "BEGIN {printf \"%.2f\", $TV_HITS/($TV_HITS+$TV_MISSES)*100}")
else
    TV_HIT_RATE=0
fi

# Audio hit rate
if (( AUDIO_HITS + AUDIO_MISSES > 0 )); then
    AUDIO_HIT_RATE=$(awk "BEGIN {printf \"%.2f\", $AUDIO_HITS/($AUDIO_HITS+$AUDIO_MISSES)*100}")
else
    AUDIO_HIT_RATE=0
fi

echo "Movies: $MOVIE_HITS hits, $MOVIE_MISSES HDD reads, hit rate: $MOVIE_HIT_RATE%"
echo "TV Shows: $TV_HITS hits, $TV_MISSES HDD reads, hit rate: $TV_HIT_RATE%"
echo "Audio: $AUDIO_HITS hits, $AUDIO_MISSES HDD reads, hit rate: $AUDIO_HIT_RATE%"


# ------------------------
# Script duration
# ------------------------
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
log "Total script execution time: $DURATION seconds while pacing at $PRELOAD_PACING_PERCENT% of system capacity."
