FROM python:3.13-alpine

LABEL org.opencontainers.image.title="Plex Cache" \
      org.opencontainers.image.description="Keeps Plex media warm in ZFS ARC/L2ARC on TrueNAS" \
      org.opencontainers.image.source="https://github.com/drsassafras/TrueNAS-Plex-Caching" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLEXCACHE_CONFIG=/config \
    PLEXCACHE_PORT=8642

WORKDIR /app
COPY plexcache /app/plexcache

# 568 is the "apps" user on TrueNAS.
RUN mkdir -p /config && chown 568:568 /config
USER 568:568
VOLUME ["/config"]
EXPOSE 8642

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PLEXCACHE_PORT','8642'),timeout=4)"

CMD ["python", "-m", "plexcache"]
