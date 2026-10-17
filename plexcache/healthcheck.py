"""The image's health check (``python -m plexcache.healthcheck``), for both of the app's services:
the web app answers on PLEXCACHE_PORT (8642), the optimizer on PLEXCACHE_OPTIMIZER_PORT (8643)."""

import os
import sys
import urllib.request


def main() -> int:
    ports = (os.environ.get("PLEXCACHE_PORT") or "8642", os.environ.get("PLEXCACHE_OPTIMIZER_PORT") or "8643")
    for port in dict.fromkeys(ports):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4).close()
            return 0
        except OSError:
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
