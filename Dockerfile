FROM python:3.13-alpine

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

# For the optimizer service (the second container of the app): MKVToolNix (mkvmerge, mkvpropedit,
# mkvextract) rewrites files; ffmpeg checks every stream is unchanged before a swap; oxipng and
# jpegtran (libjpeg-turbo-utils) compress cover art losslessly.
RUN apk add --no-cache mkvtoolnix ffmpeg oxipng libjpeg-turbo-utils

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
