"""Entry point: ``python -m plexcache``."""

from __future__ import annotations

import logging
import os
import signal
import sys

from .app import App
from .server import serve


def main() -> int:
    config_dir = os.environ.get("PLEXCACHE_CONFIG", "/config")
    port = int(os.environ.get("PLEXCACHE_PORT", "8642"))
    host = os.environ.get("PLEXCACHE_HOST", "0.0.0.0")
    level = logging.DEBUG if os.environ.get("PLEXCACHE_DEBUG") else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    app = App(config_dir)
    app.start()
    httpd = serve(app, host, port)

    def shutdown(*_):
        app.stopping.set()
        app.warmup_stop.set()
        app.monitor.stop.set()
        httpd.shutdown()

    signal.signal(signal.SIGTERM, lambda *a: __import__("threading").Thread(target=shutdown).start())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
