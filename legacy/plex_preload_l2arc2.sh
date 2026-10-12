#!/bin/bash
#
# Plex L2ARC Preload Script for TrueNAS SCALE
#
# Description:
# Preload the first segment of Plex media files into L2ARC to improve playback performance.
#

#
# Dataset recommendations:
# - PRIMARYCACHE: metadata
# - SECONDARYCACHE: all

# ------------------------
# Load configuration
# ------------------------

CONFIG_FILE="./plex-l2arc.conf"


if [[ -r "$CONFIG_FILE" ]]; then
    source "$CONFIG_FILE"
else
    echo "[WARNING] Configuration file not found or not readable: $CONFIG_FILE"
fi


# ------------------------
# Safety defaults (only if missing from config)
# ------------------------


: "${PLEX_PATH_PREFIX:=/data}"
: "${TRUENAS_PATH_PREFIX:=/mnt/tank/plexfiles}"

: "${PRELOAD_PACING_PERCENT:=10}"
: "${PRELOAD_PACING_PERCENT_CONSOLE:=100}"
: "${RESERVED_L2ARC_GB:=100}"

: "${MOVIE_PRELOAD_MIN_SEC:=3}"
: "${MOVIE_PRELOAD_MAX_SEC:=60}"
: "${TV_PRELOAD_MIN_SEC:=3}"
: "${TV_PRELOAD_MAX_SEC:=30}"
: "${AUDIO_PRELOAD_MIN_SEC:=1}"
: "${AUDIO_PRELOAD_MAX_SEC:=10}"

: "${HDD_MB_PER_SEC:=150}"

: "${PLEX_HOST:=127.0.0.1}"
: "${PLEX_PORT:=32400}"
: "${PLEX_PROTOCOL:=http}"

: "${PADDING_VIDEO:=5}"
: "${PADDING_AUDIO:=15}"

: "${DEBUG:=0}"
: "${CONFIG:=0}"

# ------------------------
# Load config file if exists
# ------------------------
CONFIG_FILE="./plex-l2arc.conf"
if [[ -r "$CONFIG_FILE" ]]; then
    source "$CONFIG_FILE"
fi


# ------------------------
# Parse CLI arguments
# ------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--token)
            PLEX_TOKEN="$2"
            shift 2
            ;;
        -h|--host)
            PLEX_HOST="$2"
            shift 2
            ;;
        -P|--port)
            PLEX_PORT="$2"
            shift 2
            ;;
        -p|--protocol)
            PLEX_PROTOCOL="$2"
            shift 2
            ;;
        -d|--debug)
            DEBUG=1
            shift
            ;;
        -c|--config)
            CONFIG=1
            shift
            ;;
        -h|--help)
echo "Usage: $0 [OPTIONS]"
echo ""
echo "Options:"
echo "  -t, --token <token>     Plex token (default: $PLEX_TOKEN)"
echo "  -h, --host <host>       Plex server hostname or IP (default: $PLEX_HOST)"
echo "  -P, --port <port>       Plex server port (default: $PLEX_PORT)"
echo "  -p, --protocol <proto>  Protocol to use: http or https (default: $PLEX_PROTOCOL)"
echo "  -c, --config            Set configs/validation mode (console only)"
echo "  -d, --debug             Enable debug output"
echo "  --help                  Show this help message"

            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ------------------------
# Validate required inputs
# ------------------------
if [[ -z "$PLEX_TOKEN" ]]; then
    echo "ERROR: Plex token must be specified with -t or --token, or in config file"
    exit 1
fi



# ------------------------
# Construct full Plex host URL
# ------------------------
PLEX_URL="${PLEX_PROTOCOL}://${PLEX_HOST}:${PLEX_PORT}"

# ------------------------
# Debug output
# ------------------------
if [[ $DEBUG -eq 1 ]]; then
    set -o nounset
    echo "[DEBUG] Plex URL: $PLEX_URL"
    echo "[DEBUG] Plex token: $PLEX_TOKEN"
fi

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
# Ensure script is running under Bash
# ------------------------
if [ -z "$BASH_VERSION" ]; then
    echo "\e[31mERROR:\e[0m This script must be run with Bash, not sh or dash."
    echo "Use: bash $0 or make the script executable and run directly or set ACL and run directly"
    exit 1
fi

# ------------------------
# Begin Functions
# ------------------------


debug() {
    [[ "$DEBUG" -eq 1 ]] && echo "[DEBUG] $*"
}

debug_xml_head() {
    [[ "$DEBUG" -eq 1 ]] || return
    echo "[DEBUG] --- XML HEAD (first 30 lines) ---"
    echo "$1" | head -n 30
    echo "[DEBUG] --- END XML HEAD ---"
}



# ------------------------
# Function: Enter CLI Configuration Mode
# ------------------------
configure_plex_l2arc() {
    local CONFIG_FILE="$1"

    # ------------------------
    # Colors for user-friendly messages
    # ------------------------
    local RED='\e[31m'
    local GREEN='\e[32m'
    local YELLOW='\e[33m'
    local BLUE='\e[34m'
    local NC='\e[0m' # No Color

    echo -e "${BLUE}Entering Plex L2ARC configuration mode...${NC}"
    echo "Press ENTER to accept the current value in brackets []"

    local CONFIG_VARS=(
        "PLEX_PATH_PREFIX"
        "TRUENAS_PATH_PREFIX"
        "PLEX_HOST"
        "PLEX_PORT"
        "PLEX_PROTOCOL"
        "PRELOAD_PACING_PERCENT"
        "PRELOAD_PACING_PERCENT_CONSOLE"
        "MOVIE_PRELOAD_MIN_SEC"
        "MOVIE_PRELOAD_MAX_SEC"
        "TV_PRELOAD_MIN_SEC"
        "TV_PRELOAD_MAX_SEC"
        "AUDIO_PRELOAD_MIN_SEC"
        "AUDIO_PRELOAD_MAX_SEC"
        "HDD_MB_PER_SEC"
    )

    # ------------------------
    # Helper: validate numeric input
    # ------------------------
    validate_numeric() {
        local name="$1"
        local val="$2"
        local min="$3"
        local max="$4"
        if ! [[ "$val" =~ ^[0-9]+$ ]]; then
            echo -e "${RED}Invalid input for $name. Must be a positive integer.${NC}"
            return 1
        fi
        if [[ -n "$min" ]] && (( val < min )); then
            echo -e "${RED}$name must be at least $min.${NC}"
            return 1
        fi
        if [[ -n "$max" ]] && (( val > max )); then
            echo -e "${RED}$name must be at most $max.${NC}"
            return 1
        fi
        return 0
    }

    # ------------------------
    # Prompt user for configuration
    # ------------------------
    for var in "${CONFIG_VARS[@]}"; do
        local current_val="${!var}"
        while true; do
            read -p "$var [$current_val]: " input
            if [[ -z "$input" ]]; then
                input="$current_val"
            fi

            # Validate numeric fields
            if [[ "$var" =~ _MIN_SEC$|_MAX_SEC$|PORT$|_PERCENT$|HDD_MB_PER_SEC$ ]]; then
                if ! validate_numeric "$var" "$input"; then
                    continue
                fi
            fi

            declare "$var=$input"
            break
        done
    done

    # ------------------------
    # Helper: test directories
    # ------------------------
    test_directory() {
        local var_name="$1"
        local val="${!var_name}"
        while [[ ! -d "$val" ]]; do
            read -p "Directory $val does not exist. Enter new path, S to skip, A to save anyway [try again]: " resp
            case "$resp" in
                S|s)
                    echo -e "${YELLOW}Skipping check for $var_name${NC}"
                    break
                    ;;
                A|a)
                    echo -e "${YELLOW}Saving $var_name=$val anyway${NC}"
                    break
                    ;;
                *)
                    val="$resp"
                    ;;
            esac
        done
        declare "$var_name=$val"
    }

    # ------------------------
    # Validate directories
    # ------------------------
    for path_var in "PLEX_PATH_PREFIX" "TRUENAS_PATH_PREFIX"; do
        test_directory "$path_var"
    done

    # ------------------------
    # Helper: test Plex connection
    # ------------------------
    test_plex_connection() {
        if [[ -z "$PLEX_TOKEN" ]]; then
            echo -e "${YELLOW}No PLEX_TOKEN set, skipping Plex connection test.${NC}"
            return
        fi
        local PLEX_URL="${PLEX_PROTOCOL}://${PLEX_HOST}:${PLEX_PORT}"
        while true; do
            local status
            status=$(curl -s -o /dev/null -w "%{http_code}" "$PLEX_URL/library/sections?X-Plex-Token=$PLEX_TOKEN")
            if [[ "$status" == "200" ]]; then
                echo -e "${GREEN}Plex connection successful at $PLEX_URL${NC}"
                break
            else
                read -p "Failed to connect to Plex ($status). Retry, S to skip, A to save anyway [Retry]: " resp
                case "$resp" in
                    S|s)
                        echo -e "${YELLOW}Skipping Plex test${NC}"
                        break
                        ;;
                    A|a)
                        echo -e "${YELLOW}Saving config anyway${NC}"
                        break
                        ;;
                    *)
                        echo "Retrying..."
                        ;;
                esac
            fi
        done
    }

    test_plex_connection

    # ------------------------
    # Validate min/max consistency
    # ------------------------
    for type in MOVIE TV AUDIO; do
        local min_var="${type}_PRELOAD_MIN_SEC"
        local max_var="${type}_PRELOAD_MAX_SEC"
        if (( ${!min_var} > ${!max_var} )); then
            echo -e "${YELLOW}Adjusting $max_var to match $min_var (${!min_var})${NC}"
            declare "$max_var=${!min_var}"
        fi
    done

    # ------------------------
    # Save configuration
    # ------------------------
    echo -e "${BLUE}Saving configuration to $CONFIG_FILE...${NC}"
    {
        for var in "${CONFIG_VARS[@]}"; do
            echo "$var=\"${!var}\""
        done
    } > "$CONFIG_FILE"

    echo -e "${GREEN}Configuration saved successfully! Exiting.${NC}"
}





table_header() {
    local text="${1:-}"
    local total_length=79

    # If the string is empty (but not zero), just output dashes
    if [[ -z "$text" ]]; then
        printf '%*s\n' "$total_length" '' | tr ' ' '-'
        return
    fi

    local preferred_eq=20
    local min_space=3

    # Compute remaining space after text
    local remaining=$(( total_length - ${#text} - 2*min_space ))
    local eq_len

    if (( remaining >= 2*preferred_eq )); then
        eq_len=$preferred_eq
    else
        # reduce eq_len to fit the line, but minimum 5
        eq_len=$(( remaining / 2 ))
        (( eq_len < 5 )) && eq_len=5
    fi

    # Calculate extra space to center the text
    local extra_space=$(( remaining - 2*eq_len ))
    local left_space=$(( extra_space / 2 + min_space ))
    local right_space=$(( extra_space - extra_space / 2 + min_space ))

    printf "%s%s%s%s%s\n" \
        "$(printf '=%.0s' $(seq 1 $eq_len))" \
        "$(printf ' %.0s' $(seq 1 $left_space))" \
        "$text" \
        "$(printf ' %.0s' $(seq 1 $right_space))" \
        "$(printf '=%.0s' $(seq 1 $eq_len))"
}


# ------------------------
# Helper: Plex API call
# ------------------------
plex_api() {
    local endpoint="$1"
    
    if [[ "$endpoint" == *"?"* ]]; then
        response=$(curl -s "$PLEX_URL$endpoint&X-Plex-Token=$PLEX_TOKEN&includeMedia=1&includePart=1&includeGuids=0&includeReviews=0")
    else
        response=$(curl -s "$PLEX_URL$endpoint?X-Plex-Token=$PLEX_TOKEN&includeMedia=1&includePart=1&includeGuids=0&includeReviews=0")
    fi


        # Detect Plex 404 HTML response (first two lines only)
    if echo "$response" | head -n 2 | grep -q '<h1>404 Not Found</h1>'; then
        if [ -t 2 ]; then
            # Console: red warning
            echo -e "\e[31mWARNING: Plex API returned 404 for endpoint:\e[0m $endpoint" >&2
        else
            # Logs / cron: plain text
            echo "WARNING: Plex API returned 404 for endpoint: $endpoint" >&2
        fi
    fi

        # Return the response
    echo "$response"
}

benchmark_files() {
    total_speed=0
    total_size=0
    total_files=0

    # Table header
    table_header "Benchmarking Files ARC/L2ARC Bypassed"
    printf "%-8s | %-5s | %-9s | %s\n" "Type" "MB/s" "Size(MB)" "Filename"
    table_header

    for arr_name in MOVIES_ARRAY TV_ARRAY AUDIO_ARRAY; do
        case "$arr_name" in
            MOVIES_ARRAY) type="Movies" ;;
            TV_ARRAY) type="TV" ;;
            AUDIO_ARRAY) type="Audio" ;;
        esac

        # Access the array dynamically
        eval "files=(\"\${${arr_name}[@]}\")"

        if (( ${#files[@]} == 0 )); then
            continue
        fi

        # Determine how many files to select (up to 3)
        num_select=3
        (( ${#files[@]} < num_select )) && num_select=${#files[@]}

        # Shuffle safely without exceeding argument limit
        mapfile -t selected_files < <(printf "%s\n" "${files[@]}" | shuf -n "$num_select")

        for file_entry in "${selected_files[@]}"; do
            file="${file_entry%%|*}"  # extract file path

            if [[ ! -f "$file" ]]; then
                continue
            fi

            size_mb=$(( ( $(stat -c%s "$file") + 1024*1024 - 1 ) / 1024 / 1024 ))

            START=$(date +%s%3N)
            dd if="$file" of=/dev/null bs=1M iflag=direct status=none
            END=$(date +%s%3N)

            duration_ms=$((END - START))
            (( duration_ms == 0 )) && duration_ms=1  # prevent division by zero

            mbps=$(( size_mb * 1000 / duration_ms ))  # integer MB/s

            # Print table row
            printf "%-8s | %-5d | %-9d | %s\n" "$type" "$mbps" "$size_mb" "$(basename "$file")"

            total_speed=$(( total_speed + mbps ))
            total_size=$(( total_size + size_mb ))
            total_files=$(( total_files + 1 ))
        done
    done

# Compute and report average speed and size
if (( total_files > 0 )); then
    avg_speed=$(( total_speed / total_files ))
    avg_size=$(( total_size / total_files ))

    # Print summary row with green average speed if terminal
    if [ -t 1 ]; then
        printf "Average  | \033[32m%-5d\033[0m | %-9d | read speed over %d file(s)\n" \
               "$avg_speed" "$avg_size" "$total_files"
    else
        printf "Average  | %-5d | %-9d | read speed over %d file(s)\n" \
               "$avg_speed" "$avg_size" "$total_files"
    fi

    # Print user-configured HDD speed
    printf "Setting  | %-5d | %-9s | User configured with HDD_MB_PER_SEC\n" \
           "$HDD_MB_PER_SEC" "-"


# Compare with HDD_MB_PER_SEC using 30% margin (integer math)
lower_bound=$(( HDD_MB_PER_SEC * 70 / 100 ))
upper_bound=$(( HDD_MB_PER_SEC * 130 / 100 ))

if (( avg_speed < lower_bound || avg_speed > upper_bound )); then
    echo -e "\n\e[33mWARNING: Measured average read speed differs from HDD_MB_PER_SEC by more than 30%.\e[0m"
    echo "  Consider updating HDD_MB_PER_SEC in the script for accurate pacing and reporting."
fi

    else
        echo "No files were benchmarked, cannot compute average read speed."
    fi
}

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
    debug "preloading $type"

    for file_entry in "${files[@]}"; do
        local file=$(echo "$file_entry" | cut -d'|' -f1)
        local bitrate=$(echo "$file_entry" | cut -d'|' -f2)

# Ensure max_sec and bitrate are integers
local max_sec_int=$(printf "%.0f" "$max_sec")
local bitrate_int=$(printf "%.0f" "$bitrate")

# Compute preload bytes using per-file bitrate (kbps), round up to nearest 1MB
local preload_bytes=$(( bitrate_int * max_sec_int * 125 ))  # kbps → bytes
local preload_mb=$(( (preload_bytes + 1024*1024 - 1) / 1024 / 1024 ))
[[ $preload_mb -lt 1 ]] && preload_mb=1


# Measure actual read time
START_READ=$(date +%s%3N)
dd if="$file" of=/dev/null bs=1M count="$preload_mb" status=none
END_READ=$(date +%s%3N)
DURATION_MS=$((END_READ - START_READ))

# Expected HDD duration for pacing
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

log() { echo "$1"; }

# ------------------------
# End Functions
# ------------------------


# ------------------------
# Start Script
# ------------------------



# ------------------------
# enter config?
# ------------------------
if [[ $CONFIG == 1 ]]; then
    configure_plex_l2arc $CONFIG_FILE
    exit 0
fi


START_TIME=$(date +%s)


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

if [[ "$DEBUG" -eq 1 ]]; then
    echo ""
    table_header "DEBUG: Detected Plex Libraries"
    printf "%-15s | %-20s | %s\n" "Library ID" "Plex Library Type" "Library Name"
    table_header

    for lib in "${library_list[@]}"; do
        IFS="|" read -r title key type <<< "$lib"
        printf "%-15s | %-20s | %s\n" "$key" "$type" "$title"
    done

    table_header
    echo ""
fi




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
# Iterate through the libraries and populate arrays with file paths and bitrate totals
# Optimized version with streaming sums (no mapfile for totals)
# ------------------------
for lib in "${library_list[@]}"; do
    IFS="|" read -r lib_title lib_key lib_type <<< "$lib"

    table_header "DEBUG: Processing library: $lib_title"

    case "$lib_type" in
        movie)
            items_xml=$(plex_api "/library/sections/$lib_key/all?type=1")
items_xml=$(echo "$items_xml" | tr '\n' ' ' | sed -E 's/<Part /\
<Part /g')

            MOVIES_BITRATE_TOTAL=0
            while IFS='|' read -r file bitrate; do
                # Decode &amp; only
                file="$(echo "$file" | sed 's/&amp;/\&/g')"


                MOVIES_ARRAY+=("$file|$bitrate")
                ((MOVIES_BITRATE_TOTAL += bitrate))
            done < <(
                xmllint --noent --xpath '//Video[@type="movie"]/Media' - <<<"$items_xml" | \
                awk -v plex_prefix="$PLEX_PATH_PREFIX" -v truenas_prefix="$TRUENAS_PATH_PREFIX" '
                    /<Media / { match($0, /bitrate="([^"]+)"/, b); media_bitrate = b[1] }
                    /<Part / { match($0, /file="([^"]+)"/, f); file=f[1];
                               if(file && media_bitrate!="") { sub("^" plex_prefix, truenas_prefix, file);
                                gsub(/&amp;/,"&",file);    # decode &amp; in awk
                                print file "|" media_bitrate } }'
            )
            ;;

        show)
            episodes_xml=$(plex_api "/library/sections/$lib_key/all?type=4")

            TV_BITRATE_TOTAL=0
            while IFS='|' read -r file bitrate; do
                # Decode &amp; only
                file="$(echo "$file" | sed 's/&amp;/\&/g')"

                TV_ARRAY+=("$file|$bitrate")
                ((TV_BITRATE_TOTAL += bitrate))
            done < <(
                xmllint --noent --xpath '//Video[@type="episode"]/Media' - <<<"$episodes_xml" | \
                awk -v plex_prefix="$PLEX_PATH_PREFIX" -v truenas_prefix="$TRUENAS_PATH_PREFIX" '
                    /<Media / { match($0, /bitrate="([^"]+)"/, b); media_bitrate = b[1] }
                    /<Part / { match($0, /file="([^"]+)"/, f); file=f[1];
                               if(file && media_bitrate!="") { sub("^" plex_prefix, truenas_prefix, file); print file "|" media_bitrate } }'
            )
            ;;

        artist|audio|audiobook)
            audio_xml=$(plex_api "/library/sections/$lib_key/all?type=10")

            AUDIO_BITRATE_TOTAL=0
            while IFS='|' read -r file bitrate; do
                # Decode &amp; only
                file="$(echo "$file" | sed 's/&amp;/\&/g')"

                AUDIO_ARRAY+=("$file|$bitrate")
                ((AUDIO_BITRATE_TOTAL += bitrate))
            done < <(
                xmllint --noent --format <<<'//Video[@type="audiobook"]/Media' - <<<"$audio_xml" 2>/dev/null | \
                awk -v plex_prefix="$PLEX_PATH_PREFIX" -v truenas_prefix="$TRUENAS_PATH_PREFIX" '
                    /<Media / { match($0, /bitrate="([^"]+)"/, b); media_bitrate = b[1] }
                    /<Part / { match($0, /file="([^"]+)"/, f); file=f[1];
                               if(file) { sub("^" plex_prefix, truenas_prefix, file);
                                          if(media_bitrate=="") media_bitrate=0;
                                          print file "|" media_bitrate } }'
            )
            ;;

        *)
            debug "Skipping unsupported library type: $lib_type"
            ;;
    esac





# ------------------------
# DEBUG: Show first 8 entries of current library array
# ------------------------
if [[ "$DEBUG" -eq 1 ]]; then

    case "$lib_type" in
        movie)
            sample_array=("${MOVIES_ARRAY[@]}")
            ;;
        show)
            sample_array=("${TV_ARRAY[@]}")
            ;;
        artist|audio|audiobook)
            sample_array=("${AUDIO_ARRAY[@]}")
            ;;
        *)
            sample_array=()
            ;;
    esac

    if (( ${#sample_array[@]} > 0 )); then
        # Print header
        printf "%-15s | %s\n" "Bitrate" "File Location (Prefixed for TrueNAS)"
        printf "%-15s-+-%s\n" "---------------" "----------------"

        for ((i=0; i<${#sample_array[@]} && i<8; i++)); do
            file_entry="${sample_array[i]}"
            bitrate="${file_entry##*|}"        # extract bitrate
            file="${file_entry%%|*}"          # extract filename
            # Print: bitrate + " kbps", padded to 15 chars, then pipe, then filename
            printf "%-15s | %s\n" "${bitrate} kbps" "$file"
        done
    else
        echo "(no entries)"
    fi
    echo
fi

done


NUM_MOVIES=${#MOVIES_ARRAY[@]}
NUM_EPISODES=${#TV_ARRAY[@]}
NUM_AUDIO=${#AUDIO_ARRAY[@]}

if [[ $NUM_MOVIES -eq 0 && $NUM_EPISODES -eq 0 && $NUM_AUDIO -eq 0 ]]; then
    log "No media found for preloading. Exiting."
    exit 0
fi


# ------------------------
# Determine ZFS pool based on actual Plex media location
# ------------------------

# Initialize a variable to hold a sample media file path
MEDIA_SAMPLE_PATH=""

# We attempt to detect the location of media files in the following order of preference:
# 1. Movies
# 2. TV episodes
# 3. Audio
# This ensures we are using a representative file from whatever media type exists
if (( NUM_MOVIES > 0 )) && [[ -n "${MOVIES_ARRAY[0]}" ]]; then
    MEDIA_SAMPLE_PATH="${MOVIES_ARRAY[0]%%|*}"  # Extract just the file path, ignoring any metadata after a '|'
elif (( NUM_EPISODES > 0 )) && [[ -n "${TV_ARRAY[0]}" ]]; then
    MEDIA_SAMPLE_PATH="${TV_ARRAY[0]%%|*}"
elif (( NUM_AUDIO > 0 )) && [[ -n "${AUDIO_ARRAY[0]}" ]]; then
    MEDIA_SAMPLE_PATH="${AUDIO_ARRAY[0]%%|*}"
fi

# Output the selected sample file for debugging purposes
debug "Selected media sample file: $MEDIA_SAMPLE_PATH"

# Initialize ZPOOL variable, which will eventually contain the name of the ZFS pool
DATASET=""
ZPOOL=""

# If a sample media file was successfully found
if [[ -n "$MEDIA_SAMPLE_PATH" ]]; then
    # Use 'df' to determine the filesystem that contains this file
    # Note: On ZFS systems, 'df' will return a string like 'tank' (pool) or 'tank/plexfiles' (dataset)
    DATASET=$(df -P "$MEDIA_SAMPLE_PATH" 2>/dev/null | awk 'NR==2 {print $1}')

    # Strip any dataset path from the filesystem name to get just the pool name
    # For example:
    #   "tank/plexfiles" -> "tank"
    #   "tank/media"     -> "tank"
    # This is required because L2ARC stats are stored at the pool level, not dataset level
    ZPOOL="${DATASET%%/*}"

    debug "Detected ZFS pool from media file: $ZPOOL"
fi

# If we failed to detect a pool from the media file path
if [[ -z "$ZPOOL" ]]; then
    # Check if a default pool named 'tank' exists
    if zpool list -Ho name | grep -qx "tank"; then
        ZPOOL="tank"
        debug "Falling back to default pool: tank"
    else
        # If no pool could be detected and 'tank' does not exist, leave ZPOOL empty
        # We will handle this case downstream by setting L2ARC size to zero
        debug "No ZFS pool detected and tank not present"
        ZPOOL=""
    fi
fi

# ------------------------
# Detect total available and currently used L2ARC space
# ------------------------

# L2ARC_TOTAL  = total usable L2ARC capacity (bytes)
# L2ARC_BEFORE = currently populated / used L2ARC space (bytes)
L2ARC_TOTAL=0
L2ARC_BEFORE=0

 # Only attempt detection if a valid ZFS pool was identified
if [[ -n "$ZPOOL" ]]; then

    # ----------------------------------
    # 1) Detect TOTAL available L2ARC size
    #    (sum of ONLINE cache vdev sizes in the pool)
    # ----------------------------------
    # We will parse 'zpool list -pPLv' output to find all cache devices,
    # grab their size in bytes (column 2) and health status (column 3),
    # sum sizes of ONLINE devices, and store total in L2ARC_TOTAL.

while read -r dev size health; do
    # Only count ONLINE cache devices
    if [[ "$health" != "ONLINE" ]]; then
        debug "Skipping non-ONLINE L2ARC device: $dev ($health)"
        continue
    fi

    # Size is already in bytes due to -p flag
    if [[ -n "$size" && "$size" -gt 0 ]]; then
        (( L2ARC_TOTAL += size ))
        debug "Detected ONLINE L2ARC device: $dev - $(numfmt --to=iec "$size")"


    else
        debug "Invalid L2ARC size for device $dev: '$size'"
    fi
done < <(
    zpool list -pPLv "$ZPOOL" |
    awk '
        /cache/ {in_cache=1; next}
        /^[^ ]/ {in_cache=0}
        in_cache && /^[[:space:]]+\/dev/ {print $1, $2, $10}
    '
)



# Get current settings
CURRENT_PRIMARY=$(zfs get -H -o value primarycache "$DATASET")
CURRENT_SECONDARY=$(zfs get -H -o value secondarycache "$DATASET")

# Check if settings match
if [[ "$CURRENT_PRIMARY" == "metadata" && "$CURRENT_SECONDARY" == "all" ]]; then
    echo "✅ $DATASET: PRIMARYCACHE and SECONDARYCACHE are set correctly."
else
    echo "❌ $DATASET: Cache settings are not as expected."
    echo "  Current PRIMARYCACHE: $CURRENT_PRIMARY"
    echo "  Current SECONDARYCACHE: $CURRENT_SECONDARY"
    echo ""
    echo "To fix this, run the following commands in the CLI:"
    echo "  zfs set primarycache=metadata $DATASET"
    echo "  zfs set secondarycache=all $DATASET"
fi


debug "Total available L2ARC capacity: $(numfmt --to=iec "$L2ARC_TOTAL" 2>/dev/null)"

fi



# ----------------------------------
# Final safety handling
# ----------------------------------
if (( L2ARC_TOTAL == 0 )); then
    if zpool status "$ZPOOL" 2>/dev/null | grep -q 'cache'; then
        echo "L2ARC devices are attached to pool '$ZPOOL', but no usable capacity is available."
    else
        echo "No L2ARC devices attached to pool '$ZPOOL'."
    fi
fi


echo
table_header "L2ARC Preload Size Report"
printf "%-20s | %-11s | %-11s | %-11s | %-11s\n" "Metric" "Movies" "TV" "Audio" "Total"
table_header


# Processed entries
printf "%-20s | %-11d | %-11d | %-11d | %-11d\n" \
    "Processed entries" \
    "$NUM_MOVIES" \
    "$NUM_EPISODES" \
    "$NUM_AUDIO" \
    "$(( NUM_MOVIES + NUM_EPISODES + NUM_AUDIO ))"


# ------------------------
# Compute average bitrates for library planning
# ------------------------

AVG_MOVIE_BITRATE=$(( NUM_MOVIES > 0 ? MOVIES_BITRATE_TOTAL / NUM_MOVIES : 0 ))
AVG_MOVIE_BITRATE=${AVG_MOVIE_BITRATE%.*}

AVG_TV_BITRATE=$(( NUM_EPISODES > 0 ? TV_BITRATE_TOTAL / NUM_EPISODES : 0 ))
AVG_TV_BITRATE=${AVG_TV_BITRATE%.*}

AVG_AUDIO_BITRATE=$(( NUM_AUDIO > 0 ? AUDIO_BITRATE_TOTAL / NUM_AUDIO : 0 ))
AVG_AUDIO_BITRATE=${AVG_AUDIO_BITRATE%.*}



# Average bitrate in kbps
printf "%-20s | %-11s | %-11s | %-11s | %-11s\n" \
    "Average bitrate" \
    "$(( NUM_MOVIES > 0 ? MOVIES_BITRATE_TOTAL / NUM_MOVIES : 0 )) kbps" \
    "$(( NUM_EPISODES > 0 ? TV_BITRATE_TOTAL / NUM_EPISODES : 0 )) kbps" \
    "$(( NUM_AUDIO > 0 ? AUDIO_BITRATE_TOTAL / NUM_AUDIO : 0 )) kbps" \
    "-"


# ------------------------------------------------------------
# Compute min/max preload bytes for L2ARC planning (average-based)
# ------------------------------------------------------------
# The goal here is to estimate the amount of data to preload into
# the L2ARC (Level 2 ARC, a ZFS cache layer) based on:
#   - Average bitrate of media types
#   - Number of items (movies, episodes, audio tracks)
#   - Desired minimum and maximum preload duration per item
#   - Safety padding
# ------------------------------------------------------------

# Movies: minimum and maximum bytes to preload
MOVIES_MIN_BYTES=$(( AVG_MOVIE_BITRATE * MOVIE_PRELOAD_MIN_SEC * NUM_MOVIES * 125 ))
MOVIES_MAX_BYTES=$(( AVG_MOVIE_BITRATE * MOVIE_PRELOAD_MAX_SEC * NUM_MOVIES * 125 ))


#   Explanation:
#     AVG_MOVIE_BITRATE       = average bitrate in bits per second
#     MOVIE_PRELOAD_MIN_SEC   = minimum preload duration per movie (seconds)
#     MOVIE_PRELOAD_MAX_SEC   = maximum preload duration per movie (seconds)
#     NUM_MOVIES              = total number of movies in library
#     /8                       = convert bits to bytes
#   The result is total number of bytes to preload for all movies.

# TV shows: same calculation
TV_MIN_BYTES=$(( AVG_TV_BITRATE * TV_PRELOAD_MIN_SEC * NUM_EPISODES * 125 ))
TV_MAX_BYTES=$(( AVG_TV_BITRATE * TV_PRELOAD_MAX_SEC * NUM_EPISODES * 125 ))

# Audio / Audiobooks: same calculation
AUDIO_MIN_BYTES=$(( AVG_AUDIO_BITRATE * AUDIO_PRELOAD_MIN_SEC * NUM_AUDIO * 125 ))
AUDIO_MAX_BYTES=$(( AVG_AUDIO_BITRATE * AUDIO_PRELOAD_MAX_SEC * NUM_AUDIO * 125 ))



# Minimum preload in GB
printf "%-20s | %-11s | %-11s | %-11s | %-11s\n" \
    "Minimum preload" \
    "$(awk "BEGIN {printf \"%.1f GB\", $MOVIES_MIN_BYTES/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", $TV_MIN_BYTES/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", $AUDIO_MIN_BYTES/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", ($MOVIES_MIN_BYTES+$TV_MIN_BYTES+$AUDIO_MIN_BYTES)/1024/1024/1024}")"




# ------------------------------------------------------------
# Apply padding to the maximum bytes for rounding / safety
# ------------------------------------------------------------
# PADDING_VIDEO and PADDING_AUDIO are percentages (e.g., 5-10%) to
# add extra buffer for rounding errors, unexpected bitrate spikes, or metadata overhead.

MOVIES_MAX_BYTES=$(( MOVIES_MAX_BYTES * (100 + PADDING_VIDEO) / 100 ))
TV_MAX_BYTES=$(( TV_MAX_BYTES * (100 + PADDING_VIDEO) / 100 ))
AUDIO_MAX_BYTES=$(( AUDIO_MAX_BYTES * (100 + PADDING_AUDIO) / 100 ))



# Maximum preload in GB
printf "%-20s | %-11s | %-11s | %-11s | %-11s\n" \
    "Maximum preload" \
    "$(awk "BEGIN {printf \"%.1f GB\", $MOVIES_MAX_BYTES/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", $TV_MAX_BYTES/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", $AUDIO_MAX_BYTES/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", ($MOVIES_MAX_BYTES+$TV_MAX_BYTES+$AUDIO_MAX_BYTES)/1024/1024/1024}")"


# ------------------------------------------------------------
# Compute the overall minimum and maximum preload sizes
# ------------------------------------------------------------
# MIN_PRELOAD_BYTES includes reserved space for L2ARC not used by media
# MAX_PRELOAD_BYTES is the total maximum bytes we may attempt to preload
# for movies, TV, and audio combined.

MIN_PRELOAD_BYTES=$(( MOVIES_MIN_BYTES + TV_MIN_BYTES + AUDIO_MIN_BYTES + RESERVED_L2ARC_GB * 1024 * 1024 * 1024 ))
MAX_PRELOAD_BYTES=$(( MOVIES_MAX_BYTES + TV_MAX_BYTES + AUDIO_MAX_BYTES ))
#   RESERVED_L2ARC_GB = pre-allocated space for system or non-media purposes
#   Multiplying by 1024^3 converts GB to bytes.


# ------------------------------------------------------------
# Preload adjustment
# ------------------------------------------------------------
# Handles three scenarios:
# 1. Not enough L2ARC available – terminate with a detailed report
# 2. L2ARC available but less than ideal – issue warning with detailed info
# 3. L2ARC more than enough – debug notice only
# ------------------------------------------------------------

# Compute available L2ARC after reserved buffer
AVAILABLE_BYTES=$(( L2ARC_TOTAL - RESERVED_L2ARC_GB * 1024 * 1024 * 1024 ))

# ------------------------------------------------------------
# Compute expected preload sizes per media type (in MB) properly
# Using: average bitrate, seconds, number of items, and converting to MB
# ------------------------------------------------------------

# Movies expected
MOVIES_EXPECTED_MIN_MB=$(( AVG_MOVIE_BITRATE * MOVIE_PRELOAD_MIN_SEC * NUM_MOVIES * 125 / 1024 / 1024))
MOVIES_EXPECTED_MAX_MB=$(( AVG_MOVIE_BITRATE * MOVIE_PRELOAD_MAX_SEC * NUM_MOVIES * 125 / 1024 / 1024))
MOVIES_EXPECTED_MAX_MB=$(( MOVIES_EXPECTED_MAX_MB * (100 + PADDING_VIDEO) / 100 ))

# TV expected
TV_EXPECTED_MIN_MB=$(( AVG_TV_BITRATE * TV_PRELOAD_MIN_SEC * NUM_EPISODES * 125 / 1024 / 1024))
TV_EXPECTED_MAX_MB=$(( AVG_TV_BITRATE * TV_PRELOAD_MAX_SEC * NUM_EPISODES * 125 / 1024 / 1024))
TV_EXPECTED_MAX_MB=$(( TV_EXPECTED_MAX_MB * (100 + PADDING_VIDEO) / 100 ))

# Audio expected
AUDIO_EXPECTED_MIN_MB=$(( AVG_AUDIO_BITRATE * AUDIO_PRELOAD_MIN_SEC * NUM_AUDIO * 125 / 1024 / 1024))
AUDIO_EXPECTED_MAX_MB=$(( AVG_AUDIO_BITRATE * AUDIO_PRELOAD_MAX_SEC * NUM_AUDIO * 125 / 1024 / 1024))
AUDIO_EXPECTED_MAX_MB=$(( AUDIO_EXPECTED_MAX_MB * (100 + PADDING_AUDIO) / 100 ))


# Totals
TOTAL_EXPECTED_MIN_MB=$(( MOVIES_EXPECTED_MIN_MB + TV_EXPECTED_MIN_MB + AUDIO_EXPECTED_MIN_MB ))
TOTAL_EXPECTED_MAX_MB=$(( MOVIES_EXPECTED_MAX_MB + TV_EXPECTED_MAX_MB + AUDIO_EXPECTED_MAX_MB ))


# Minimum L2ARC required (maximum expected + reserved buffer)
MIN_L2ARC_REQUIRED_MB=$(( TOTAL_EXPECTED_MAX_MB + RESERVED_L2ARC_GB * 1024 ))

# Note: RESERVED_L2ARC_GB * 1024 converts GB → MB

# ------------------------------------------------------------
# Case 1: Not enough L2ARC to even store min ideal preload
# ------------------------------------------------------------
if [[ $AVAILABLE_BYTES -le 0 || $AVAILABLE_BYTES -lt $(( TOTAL_EXPECTED_MIN_MB * 1024 * 1024 )) ]]; then


printf "%-20s | %-11s | %-11s | %-11s | %-11s\n" \
    "Used Preload" \
    "$(awk 'BEGIN {printf "%.1f GB", 0}')" \
    "$(awk 'BEGIN {printf "%.1f GB", 0}')" \
    "$(awk 'BEGIN {printf "%.1f GB", 0}')" \
    "$(awk 'BEGIN {printf "%.1f GB", 0}')"

    echo

    if (( L2ARC_TOTAL == 0 )); then
    table_header "* Action Needed - L2ARC Not Detected *"
else
        table_header "WARNING: Insufficient L2ARC for ideal preload."
    echo "  - L2ARC detected on pool $ZPOOL: $(numfmt --to=iec $L2ARC_TOTAL) bytes"
fi

echo "  - Your minimum L2ARC needed for caching script maximum amounts: $(numfmt --to=iec $((MIN_L2ARC_REQUIRED_MB * 1024 * 1024)))"
    echo "  - Reserved buffer: $RESERVED_L2ARC_GB GB"



table_header "Notes"
    log "  - L2ARC should be sized larger than the minimum ideal size."
    log "  - This ensures excess free space acts as an additional buffer, "
    log "  - allowing cached files to remain longer before eviction. "
    log "  - The optimal size depends on your system's L2ARC pressure and available RAM ARC."

# ------------------------
# Run benchmarks
# ------------------------
benchmark_files

    exit 1
fi

# ------------------------------------------------------------
# Case 2: Available L2ARC is less than total ideal maximum
# ------------------------------------------------------------
if [[ $AVAILABLE_BYTES -lt $(( TOTAL_EXPECTED_MAX_MB * 1024 * 1024 )) ]]; then

    # Calculate excess for proportional reduction
    EXCESS=$(( TOTAL_EXPECTED_MAX_MB * 1024 * 1024 - AVAILABLE_BYTES ))

    # Calculate total wiggle room
    TOTAL_WIGGLE=$(( (MOVIES_EXPECTED_MAX - MOVIES_EXPECTED_MIN) + (TV_EXPECTED_MAX - TV_EXPECTED_MIN) + (AUDIO_EXPECTED_MAX - AUDIO_EXPECTED_MIN) ))

    # Compute reduction factor capped at 1
    REDUCTION_FACTOR=$(awk "BEGIN {f=$EXCESS/$TOTAL_WIGGLE; if(f>1) print 1; else print f}")

    # Adjust maximum preload seconds proportionally
NEW_MOVIE_MAX_SEC=$(awk "BEGIN {printf \"%d\", ($MOVIE_PRELOAD_MAX_SEC - ($MOVIE_PRELOAD_MAX_SEC - $MOVIE_PRELOAD_MIN_SEC)*$REDUCTION_FACTOR + 0.5)}")
NEW_TV_MAX_SEC=$(awk "BEGIN {printf \"%d\", ($TV_PRELOAD_MAX_SEC - ($TV_PRELOAD_MAX_SEC - $TV_PRELOAD_MIN_SEC)*$REDUCTION_FACTOR + 0.5)}")
NEW_AUDIO_MAX_SEC=$(awk "BEGIN {printf \"%d\", ($AUDIO_PRELOAD_MAX_SEC - ($AUDIO_PRELOAD_MAX_SEC - $AUDIO_PRELOAD_MIN_SEC)*$REDUCTION_FACTOR + 0.5)}")


    # Compute expected L2ARC per media type after reduction
MOVIES_L2ARC_EXPECTED=$(( $(printf "%.0f" "$NEW_MOVIE_MAX_SEC") * AVG_MOVIE_BITRATE / 8 * NUM_MOVIES ))
TV_L2ARC_EXPECTED=$(( $(printf "%.0f" "$NEW_TV_MAX_SEC") * AVG_TV_BITRATE / 8 * NUM_EPISODES ))
AUDIO_L2ARC_EXPECTED=$(( $(printf "%.0f" "$NEW_AUDIO_MAX_SEC") * AVG_AUDIO_BITRATE / 8 * NUM_AUDIO ))
TOTAL_L2ARC_EXPECTED=$(( MOVIES_L2ARC_EXPECTED + TV_L2ARC_EXPECTED + AUDIO_L2ARC_EXPECTED ))


printf "%-20s | %-11s | %-11s | %-11s | %-11s\n" \
    "Used Preload" \
    "$(awk "BEGIN {printf \"%.1f GB\", $MOVIES_L2ARC_EXPECTED/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", $TV_L2ARC_EXPECTED/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", $AUDIO_L2ARC_EXPECTED/1024/1024/1024}")" \
    "$(awk "BEGIN {printf \"%.1f GB\", ($MOVIES_L2ARC_EXPECTED+$TV_L2ARC_EXPECTED+$AUDIO_L2ARC_EXPECTED)/1024/1024/1024}")"



    # Warning report for user
    log "WARNING: L2ARC available ($ZPOOL) is less than ideal maximum preload."
    log "Total L2ARC: $(numfmt --to=iec $L2ARC_TOTAL) bytes"
    log "Reserved buffer: $(numfmt --to=iec $(( RESERVED_L2ARC_GB * 1024 * 1024 * 1024 ))) bytes"
log "Expected preload sizes after reduction:"
log "  Movies: $(numfmt --to=iec $((MOVIES_L2ARC_EXPECTED)))"
log "  TV:     $(numfmt --to=iec $((TV_L2ARC_EXPECTED)))"
log "  Audio:  $(numfmt --to=iec $((AUDIO_L2ARC_EXPECTED)))"
log "  Total:  $(numfmt --to=iec $((TOTAL_L2ARC_EXPECTED)))"
log "Minimum L2ARC available for caching ideal amounts (max preload + reserved buffer): $(numfmt --to=iec $((MIN_L2ARC_REQUIRED_MB * 1024 * 1024)))"

    log "NOTE: L2ARC should be sized larger than the minimum ideal size."
    log "      This ensures excess free space acts as an additional buffer, "
    log "      allowing cached files to remain longer before eviction. "
    log "      The optimal size depends on your system's L2ARC pressure and available RAM."
else

    # ------------------------------------------------------------
    # Case 3: More than enough L2ARC available
    # ------------------------------------------------------------
    NEW_MOVIE_MAX_SEC=$MOVIE_PRELOAD_MAX_SEC
    NEW_TV_MAX_SEC=$TV_PRELOAD_MAX_SEC
    NEW_AUDIO_MAX_SEC=$AUDIO_PRELOAD_MAX_SEC
    debug "INFO: L2ARC on pool $ZPOOL is sufficient. Expected preload total: $(numfmt --to=iec $((TOTAL_EXPECTED_MAX_MB * 1024 * 1024)))"
fi


# ------------------------
# Shuffle files to avoid LBA clustering
# ------------------------
mapfile -t MOVIES_ARRAY < <(shuf -e "${MOVIES_ARRAY[@]}")
mapfile -t TV_ARRAY     < <(shuf -e "${TV_ARRAY[@]}")
mapfile -t AUDIO_ARRAY  < <(shuf -e "${AUDIO_ARRAY[@]}")

# ------------------------
# Initialize counters
# ------------------------
MOVIE_HITS=0
MOVIE_MISSES=0
TV_HITS=0
TV_MISSES=0
AUDIO_HITS=0
AUDIO_MISSES=0


# If running in an interactive console, override pacing percent
if [[ -t 1 ]]; then
    PRELOAD_PACING_PERCENT="$PRELOAD_PACING_PERCENT_CONSOLE"
    debug "Interactive console detected; using console pacing: ${PRELOAD_PACING_PERCENT}%"
else
    debug "Non-interactive environment detected; using cron pacing: ${PRELOAD_PACING_PERCENT}%"
fi

    # ----------------------------------
    # 2) Detect CURRENTLY USED L2ARC size
    #    (from ZFS arcstats)
    # ----------------------------------
    if [[ -r /proc/spl/kstat/zfs/arcstats ]]; then
        # l2_asize shows the size of L2ARC currently populated with data
        L2ARC_BEFORE=$(awk '/l2_asize/ {print $3}' /proc/spl/kstat/zfs/arcstats 2>/dev/null)
        debug "Before L2ARC populated size (arcstats l2_asize): $(numfmt --to=iec "$L2ARC_BEFORE" 2>/dev/null)"

    else
        debug "arcstats not readable; assuming L2ARC_BEFORE=0"
        L2ARC_BEFORE=0
    fi

# ------------------------
# Preload Movies, TV, Audio
# ------------------------
preload_files "Movies" "$NEW_MOVIE_MAX_SEC" "${MOVIES_ARRAY[@]}"
preload_files "TV" "$NEW_TV_MAX_SEC" "${TV_ARRAY[@]}"
preload_files "Audio" "$NEW_AUDIO_MAX_SEC" "${AUDIO_ARRAY[@]}"


    # ----------------------------------
    # 2) Detect CURRENTLY USED L2ARC size
    #    (from ZFS arcstats)
    # ----------------------------------
    if [[ -r /proc/spl/kstat/zfs/arcstats ]]; then
        # l2_asize shows the size of L2ARC currently populated with data
        L2ARC_AFTER=$(awk '/l2_asize/ {print $3}' /proc/spl/kstat/zfs/arcstats 2>/dev/null)
        debug "After L2ARC populated size (arcstats l2_asize): $(numfmt --to=iec "$L2ARC_AFTER" 2>/dev/null) bytes ($L2ARC_AFTER bytes raw)"

    else
        debug "arcstats not readable"
        L2ARC_AFTER=0
    fi
# ------------------------
# Summary Report
# ------------------------
table_header "Preload Summary"
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

echo "Movies: $MOVIE_HITS already cached , $MOVIE_MISSES HDD reads into L2ARC, hit rate: $MOVIE_HIT_RATE%"
echo "TV Shows: $TV_HITS already cached , $TV_MISSES HDD reads into L2ARC hit rate: $TV_HIT_RATE%"
echo "Audio: $AUDIO_HITS already cached , $AUDIO_MISSES HDD reads into L2ARC, hit rate: $AUDIO_HIT_RATE%"


# ------------------------
# Script duration
# ------------------------
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
log "Total script execution time: $DURATION seconds while pacing at $PRELOAD_PACING_PERCENT% of system capacity."
