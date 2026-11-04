# The Python the app runs on: 3.15, its newest patch release at each build (the build workflow rebuilds weekly). The tag is
# an argument so dev/future.py can build the same image on the next Python (--build-arg PYTHON_TAG=3.16-rc-slim-trixie).
# Debian, because MKVToolNix's own repository (mkvtoolnix.download) builds for it: each release there within days.
ARG PYTHON_TAG=3.15-slim-trixie
# The Debian that jpegtran and ffmpeg are built on (the stages below): the Python image's own, without Python — neither
# uses it, so a new Python (dev/future.py's alpha, or the image refreshed upstream) doesn't build ffmpeg again (minutes).
# Moved with PYTHON_TAG's Debian (the suite checks the two agree).
ARG DEBIAN_TAG=trixie-slim

# The tools the optimizer service (the app's second container) runs. MKVToolNix (mkvmerge, mkvpropedit, mkvextract)
# rewrites files; ffmpeg checks every stream is unchanged before a swap; oxipng and jpegtran (libjpeg-turbo) compress cover
# art losslessly. Each comes from its own upstream release, pinned here; dev/upkeep.py moves the pins (the check the suite
# runs by hand). ffmpeg is built from its release with no external libraries: the optimizer only copies streams, hashes
# them and reads cover art, which ffmpeg's own demuxers and decoders do — the static builds around carry every codec
# library, 260 MB of image, and Debian's package pulls in 500 MB of dependencies.
ARG MKVTOOLNIX=103.0
ARG FFMPEG=9.0.2
ARG OXIPNG=10.2.1
ARG LIBJPEG_TURBO=3.2.1
# dev/future.py builds ffmpeg from its git snapshot instead: FFMPEG_SOURCE the tarball's URL (any compression tar knows),
# FFMPEG_STAMP the week, so the layer is built again when it changes. Empty: the release above.
ARG FFMPEG_SOURCE=""
ARG FFMPEG_STAMP=""
# The Node that builds the web app's CSS, scripts and pages (the web stage below): its current LTS release. Only the build uses
# it; the image has none.
ARG NODE_TAG=26-slim

# jpegtran built from libjpeg-turbo's release: the one tool of it the optimizer uses, linked statically against it. Its
# compiles (and ffmpeg's) go through ccache, its cache kept by BuildKit between builds (a cache mount, never in the image): a
# build of the same code again, or of ffmpeg's next weekly snapshot, compiles only what changed.
FROM debian:${DEBIAN_TAG} AS jpegtran
ARG LIBJPEG_TURBO
# (bash, with a pipe's failure the step's: a download that fails can't pass by its tar)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN --mount=type=cache,id=jpegtran-ccache,target=/root/.ccache set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl build-essential cmake nasm ccache; \
    curl -fsSL "https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/${LIBJPEG_TURBO}/libjpeg-turbo-${LIBJPEG_TURBO}.tar.gz" | tar xz; \
    cmake -S "libjpeg-turbo-${LIBJPEG_TURBO}" -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_SHARED=0 -DWITH_TURBOJPEG=0 -DWITH_JAVA=0 -DCMAKE_C_COMPILER_LAUNCHER=ccache >/dev/null; \
    cmake --build build -j "$(nproc)" >/dev/null; \
    install -m 755 "$(test -x build/jpegtran-static && echo build/jpegtran-static || echo build/jpegtran)" /usr/local/bin/jpegtran; \
    /usr/local/bin/jpegtran -version

# ffmpeg (with ffprobe) built from its release, with no external libraries (zlib for PNG cover art). A few minutes of the build.
# The release's tarball is fetched from ffmpeg.org, tried again a few times; if ffmpeg.org still can't be reached, the same
# release's tag from FFmpeg's own mirror on GitHub (the snapshot has only ffmpeg.org).
FROM debian:${DEBIAN_TAG} AS ffmpeg
ARG FFMPEG
ARG FFMPEG_SOURCE
ARG FFMPEG_STAMP
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils bzip2 build-essential nasm pkg-config zlib1g-dev ccache; \
    fetch() { curl -fsSL --connect-timeout 30 --retry 3 --retry-delay 10 --retry-all-errors "$1" -o "${1##*/}"; }; \
    src="${FFMPEG_SOURCE:-https://ffmpeg.org/releases/ffmpeg-${FFMPEG}.tar.xz}"; echo "ffmpeg from $src ${FFMPEG_STAMP}"; \
    if ! fetch "$src"; then \
        test -z "${FFMPEG_SOURCE}"; \
        src="https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n${FFMPEG}.tar.gz"; echo "ffmpeg.org not reached: ffmpeg from $src"; \
        fetch "$src"; \
    fi; \
    mkdir /ffmpeg-src; tar xaf "${src##*/}" -C /ffmpeg-src --strip-components=1
WORKDIR /ffmpeg-src
RUN --mount=type=cache,id=ffmpeg-ccache,target=/root/.ccache set -eux; \
    ./configure --cc="ccache gcc" --disable-doc --disable-debug --disable-ffplay --disable-autodetect --enable-zlib --disable-shared --enable-static >/dev/null; \
    make -j "$(nproc)" >/dev/null; \
    install -m 755 ffmpeg ffprobe /usr/local/bin/; \
    ffmpeg -version | head -1

# The web app's CSS, scripts and pages as browsers get them (dev/web.js): the CSS with the newer features its targets lack
# rewritten by postcss-preset-env and the browser prefixes they need added by Autoprefixer (the targets in dev/package.json,
# "browserslist"), then minified by cssnano; the scripts minified by
# Terser, the pages by html-minifier-terser. plexcache/web's are written as they are read (the CSS without prefixes); these
# copies of them replace those in the image.
FROM node:${NODE_TAG} AS web
WORKDIR /web
COPY dev/package.json dev/package-lock.json dev/web.js dev/
RUN npm ci --prefix dev --no-audit --no-fund
COPY plexcache/web/*.css plexcache/web/*.js plexcache/web/*.html plexcache/web/
RUN for f in plexcache/web/*.css plexcache/web/*.js plexcache/web/*.html; do node dev/web.js build "$f" "$f" || exit 1; done

FROM python:${PYTHON_TAG}
ARG MKVTOOLNIX
ARG OXIPNG
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

LABEL org.opencontainers.image.title="Dionysus" \
      org.opencontainers.image.description="Keeps Plex media warm in ZFS ARC/L2ARC on TrueNAS" \
      org.opencontainers.image.source="https://github.com/drsassafras/TrueNAS-Plex-Caching" \
      org.opencontainers.image.licenses="MIT"

# Which branch this image is from ("stable": main, released; "dev": the dev branch, shown as 0.x.y-dev)
# and the commit, so the update check follows the same branch. Set by the build workflow.
ARG PLEXCACHE_CHANNEL=stable
ARG PLEXCACHE_COMMIT=""
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLEXCACHE_CONFIG=/config \
    PLEXCACHE_PORT=8642 \
    PLEXCACHE_CHANNEL=$PLEXCACHE_CHANNEL \
    PLEXCACHE_COMMIT=$PLEXCACHE_COMMIT

# MKVToolNix from its own repository, at the pinned release; oxipng from its release — for the architecture being built
# (amd64: the NAS; arm64: a build on an Apple-silicon Mac). What fetched them is removed again.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) rust=x86_64 ;; arm64) rust=aarch64 ;; *) echo "no builds of the tools for $arch"; exit 1 ;; esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg -o /usr/share/keyrings/gpg-pub-moritzbunkus.gpg; \
    . /etc/os-release; \
    echo "deb [arch=$arch signed-by=/usr/share/keyrings/gpg-pub-moritzbunkus.gpg] https://mkvtoolnix.download/debian/ ${VERSION_CODENAME} main" \
        > /etc/apt/sources.list.d/mkvtoolnix.download.list; \
    apt-get update; \
    v="$(apt-cache madison mkvtoolnix | awk -v want="${MKVTOOLNIX}-" 'index($3, want) == 1 { print $3; exit }')"; \
    test -n "$v"; \
    apt-get install -y --no-install-recommends "mkvtoolnix=$v"; \
    curl -fsSL "https://github.com/oxipng/oxipng/releases/download/v${OXIPNG}/oxipng-${OXIPNG}-${rust}-unknown-linux-gnu.tar.gz" | tar xz -C /tmp; \
    install -m 755 "/tmp/oxipng-${OXIPNG}-${rust}-unknown-linux-gnu/oxipng" /usr/local/bin/; \
    rm -rf /tmp/oxipng-*; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    mkvmerge --version; oxipng --version
COPY --from=jpegtran /usr/local/bin/jpegtran /usr/local/bin/jpegtran
COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/
# (ffmpeg's here only from now: before, its check above ran with no ffmpeg, and passed only because nothing failed a pipe)
RUN ffmpeg -version | head -1; jpegtran -version

WORKDIR /app
COPY plexcache /app/plexcache
COPY --from=web /web/plexcache/web/ /app/plexcache/web/
# Library Health names codecs the way the MKVToolNix in this image does: its release's own list
# (the committed one stays if it can't be read)
COPY dev/codecs.py /app/dev/codecs.py
RUN python /app/dev/codecs.py --installed && rm -r /app/dev

# pip out of the image: the app is Python's standard library only and installs nothing, so the base image's pip is never
# run. It carries its own copies of certifi, urllib3, msgpack and setuptools (pkg_resources), each with CVEs of its own
# now and then (dev/cves.py); with pip gone, so are they.
# 568 is the "apps" user on TrueNAS.
RUN python -m pip uninstall --yes --root-user-action=ignore pip && mkdir -p /config && chown 568:568 /config
USER 568:568
VOLUME ["/config"]
EXPOSE 8642

# Works for both services: the web app on 8642, the optimizer on 8643.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD ["python", "-m", "plexcache.healthcheck"]

CMD ["python", "-m", "plexcache"]
