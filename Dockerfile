# The Python the app runs on: 3.15, its newest patch release at each build (the build workflow rebuilds weekly). The tag is
# an argument so dev/future.py can build the same image on the next Python (--build-arg PYTHON_TAG=3.16-rc-slim-trixie).
# Debian, because MKVToolNix's own repository (mkvtoolnix.download) builds for it: each release there within days.
ARG PYTHON_TAG=3.15-slim-trixie

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

# jpegtran built from libjpeg-turbo's release: the one tool of it the optimizer uses, linked statically against it.
FROM python:${PYTHON_TAG} AS jpegtran
ARG LIBJPEG_TURBO
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl build-essential cmake nasm; \
    curl -fsSL "https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/${LIBJPEG_TURBO}/libjpeg-turbo-${LIBJPEG_TURBO}.tar.gz" | tar xz; \
    cmake -S "libjpeg-turbo-${LIBJPEG_TURBO}" -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_SHARED=0 -DWITH_TURBOJPEG=0 -DWITH_JAVA=0 >/dev/null; \
    cmake --build build -j "$(nproc)" >/dev/null; \
    install -m 755 "$(test -x build/jpegtran-static && echo build/jpegtran-static || echo build/jpegtran)" /usr/local/bin/jpegtran; \
    /usr/local/bin/jpegtran -version

# ffmpeg (with ffprobe) built from its release, with no external libraries (zlib for PNG cover art). A few minutes of the build.
FROM python:${PYTHON_TAG} AS ffmpeg
ARG FFMPEG
ARG FFMPEG_SOURCE
ARG FFMPEG_STAMP
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils bzip2 build-essential nasm pkg-config zlib1g-dev; \
    src="${FFMPEG_SOURCE:-https://ffmpeg.org/releases/ffmpeg-${FFMPEG}.tar.xz}"; echo "ffmpeg from $src ${FFMPEG_STAMP}"; \
    curl -fsSL "$src" -o "${src##*/}"; mkdir ffmpeg-src; tar xaf "${src##*/}" -C ffmpeg-src --strip-components=1; \
    cd ffmpeg-src; \
    ./configure --disable-doc --disable-debug --disable-ffplay --disable-autodetect --enable-zlib --disable-shared --enable-static >/dev/null; \
    make -j "$(nproc)" >/dev/null; \
    install -m 755 ffmpeg ffprobe /usr/local/bin/; \
    ffmpeg -version | head -1

FROM python:${PYTHON_TAG}
ARG MKVTOOLNIX
ARG OXIPNG

LABEL org.opencontainers.image.title="Plex Cache" \
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
    mkvmerge --version; ffmpeg -version | head -1; oxipng --version
COPY --from=jpegtran /usr/local/bin/jpegtran /usr/local/bin/jpegtran
COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/

WORKDIR /app
COPY plexcache /app/plexcache
# Library Health names codecs the way the MKVToolNix in this image does: its release's own list
# (the committed one stays if it can't be read)
COPY dev/codecs.py /app/dev/codecs.py
RUN python /app/dev/codecs.py --installed && rm -r /app/dev

# 568 is the "apps" user on TrueNAS.
RUN mkdir -p /config && chown 568:568 /config
USER 568:568
VOLUME ["/config"]
EXPOSE 8642

# Works for both services: the web app on 8642, the optimizer on 8643.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD ["python", "-m", "plexcache.healthcheck"]

CMD ["python", "-m", "plexcache"]
