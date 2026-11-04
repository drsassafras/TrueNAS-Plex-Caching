# The Python the app runs on: 3.15, pinned to the digest of a patch release (a tag can be re-pushed; the digest can't —
# dev/upkeep.py moves it to the tag's newest at each look, which is how patch releases arrive). The tag is an argument so
# dev/future.py can build the same image on the next Python (--build-arg PYTHON_TAG=3.16-rc-slim-trixie, digest and all).
# Debian, because MKVToolNix's own repository (mkvtoolnix.download) builds for it: each release there within days.
ARG PYTHON_TAG=3.15-slim-trixie@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0
# The Debian that jpegtran and ffmpeg are built on (the stages below): the Python image's own, without Python — neither
# uses it, so a new Python (dev/future.py's alpha, or the image refreshed upstream) doesn't build ffmpeg again (minutes).
# Moved with PYTHON_TAG's Debian (the suite checks the two agree).
ARG DEBIAN_TAG=trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4

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
ARG NODE_TAG=26-slim@sha256:e4db4c914bf8ad241bd24804fc25d272d89bad97e68cdc8a1414862790a02c39

# What each download must hash to (SHA-256) before it's unpacked, and the signing key's fingerprint: a compromised release
# or a man in the middle on the build network fails the build instead of reaching the image. Each hash moves with its
# version's ARG (dev/upkeep.py), and `dev/upkeep.py --hashes` works every one out again from the pins as they stand.
# FFMPEG_GH_SHA256 is the GitHub mirror's tarball of the same release — another file, so a hash of its own; while it's
# empty the mirror isn't fallen back to. A snapshot build (FFMPEG_SOURCE) has no release to pin and isn't checked.
ARG LIBJPEG_TURBO_SHA256=c27ad72a68da151177929aa969ae773a2da5ac2af9ce946079314fdac8cece7f
ARG FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
ARG FFMPEG_GH_SHA256=""
ARG OXIPNG_SHA256_AMD64=46e3c4beb9aae57290ad809dd3374b07153579d3322a3778c53900633618b7c6
ARG OXIPNG_SHA256_ARM64=f51bb7c9836202da52a8181549641940662aa87ec2e3ae68115d822d893015b3
# Moritz Bunkus's repository-signing key, as mkvtoolnix.download/authenticity.html publishes it
ARG MKVTOOLNIX_KEY_FPR=D9199745B0545F2E8197062B0F92290A445B9007

# jpegtran built from libjpeg-turbo's release: the one tool of it the optimizer uses, linked statically against it. Its
# compiles (and ffmpeg's) go through ccache, its cache kept by BuildKit between builds (a cache mount, never in the image): a
# build of the same code again, or of ffmpeg's next weekly snapshot, compiles only what changed.
FROM debian:${DEBIAN_TAG} AS jpegtran
ARG LIBJPEG_TURBO
ARG LIBJPEG_TURBO_SHA256
# (bash, with a pipe's failure the step's: a download that fails can't pass by its tar)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN --mount=type=cache,id=jpegtran-ccache,target=/root/.ccache set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl build-essential cmake nasm ccache; \
    curl -fsSL "https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/${LIBJPEG_TURBO}/libjpeg-turbo-${LIBJPEG_TURBO}.tar.gz" -o libjpeg-turbo.tar.gz; \
    echo "${LIBJPEG_TURBO_SHA256}  libjpeg-turbo.tar.gz" | sha256sum -c -; \
    tar xzf libjpeg-turbo.tar.gz; \
    cmake -S "libjpeg-turbo-${LIBJPEG_TURBO}" -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_SHARED=0 -DWITH_TURBOJPEG=0 -DWITH_JAVA=0 -DCMAKE_C_COMPILER_LAUNCHER=ccache >/dev/null; \
    cmake --build build -j "$(nproc)" >/dev/null; \
    install -m 755 "$(test -x build/jpegtran-static && echo build/jpegtran-static || echo build/jpegtran)" /usr/local/bin/jpegtran; \
    /usr/local/bin/jpegtran -version

# ffmpeg (with ffprobe) built from its release, with no external libraries (zlib for PNG cover art). A few minutes of the build.
# The release's tarball is fetched from ffmpeg.org, tried again a few times; if ffmpeg.org still can't be reached, the same
# release's tag from FFmpeg's own mirror on GitHub (the snapshot has only ffmpeg.org).
FROM debian:${DEBIAN_TAG} AS ffmpeg
ARG FFMPEG
ARG FFMPEG_SHA256
ARG FFMPEG_GH_SHA256
ARG FFMPEG_SOURCE
ARG FFMPEG_STAMP
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils bzip2 build-essential nasm pkg-config zlib1g-dev ccache; \
    fetch() { curl -fsSL --connect-timeout 30 --retry 3 --retry-delay 10 --retry-all-errors "$1" -o "${1##*/}"; }; \
    src="${FFMPEG_SOURCE:-https://ffmpeg.org/releases/ffmpeg-${FFMPEG}.tar.xz}"; sum="${FFMPEG_SHA256}"; echo "ffmpeg from $src ${FFMPEG_STAMP}"; \
    if ! fetch "$src"; then \
        test -z "${FFMPEG_SOURCE}"; \
        src="https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n${FFMPEG}.tar.gz"; sum="${FFMPEG_GH_SHA256}"; echo "ffmpeg.org not reached: ffmpeg from $src"; \
        test -n "$sum" || { echo "no hash pinned for the mirror's tarball (FFMPEG_GH_SHA256: dev/upkeep.py --hashes)"; exit 1; }; \
        fetch "$src"; \
    fi; \
    if [ -z "${FFMPEG_SOURCE}" ]; then echo "$sum  ${src##*/}" | sha256sum -c -; fi; \
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
ARG MKVTOOLNIX_KEY_FPR
ARG OXIPNG
ARG OXIPNG_SHA256_AMD64
ARG OXIPNG_SHA256_ARM64
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
    case "$arch" in amd64) rust=x86_64; oxisum="${OXIPNG_SHA256_AMD64}" ;; arm64) rust=aarch64; oxisum="${OXIPNG_SHA256_ARM64}" ;; *) echo "no builds of the tools for $arch"; exit 1 ;; esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    curl -fsSL https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg -o /usr/share/keyrings/gpg-pub-moritzbunkus.gpg; \
    # the key is what the repository's packages are checked against, so the key itself is checked: its fingerprint,
    # against the one mkvtoolnix.download publishes (authenticity.html)
    test "$(gpg --show-keys --with-colons /usr/share/keyrings/gpg-pub-moritzbunkus.gpg | awk -F: '/^fpr:/ { print $10; exit }')" = "${MKVTOOLNIX_KEY_FPR}"; \
    . /etc/os-release; \
    echo "deb [arch=$arch signed-by=/usr/share/keyrings/gpg-pub-moritzbunkus.gpg] https://mkvtoolnix.download/debian/ ${VERSION_CODENAME} main" \
        > /etc/apt/sources.list.d/mkvtoolnix.download.list; \
    apt-get update; \
    v="$(apt-cache madison mkvtoolnix | awk -v want="${MKVTOOLNIX}-" 'index($3, want) == 1 { print $3; exit }')"; \
    test -n "$v"; \
    apt-get install -y --no-install-recommends "mkvtoolnix=$v"; \
    curl -fsSL "https://github.com/oxipng/oxipng/releases/download/v${OXIPNG}/oxipng-${OXIPNG}-${rust}-unknown-linux-gnu.tar.gz" -o /tmp/oxipng.tar.gz; \
    echo "$oxisum  /tmp/oxipng.tar.gz" | sha256sum -c -; \
    tar xzf /tmp/oxipng.tar.gz -C /tmp; \
    install -m 755 "/tmp/oxipng-${OXIPNG}-${rust}-unknown-linux-gnu/oxipng" /usr/local/bin/; \
    rm -rf /tmp/oxipng*; \
    apt-get purge -y --auto-remove curl gnupg; \
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
