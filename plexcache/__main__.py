"""Entry point: ``python -m plexcache``."""

from __future__ import annotations

import html
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import App
from .server import serve

log = logging.getLogger("plexcache")
RETRY_SECONDS = 30


def config_problem(config_dir: str) -> str | None:
    """Why settings can't be saved in ``config_dir``, or None if they can."""
    test = os.path.join(config_dir, ".write-test")
    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(test, "w") as fh:
            fh.write("ok")
        os.remove(test)
        return None
    except OSError as e:
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        return (f"Can't save settings in {config_dir} ({e.strerror or e}). This app runs as user {uid} "
                "(on TrueNAS, the \"apps\" user, 568), and it needs to write there. "
                "In TrueNAS: check that the folder on the /config line of the app's YAML exists "
                "(Datasets → Add Dataset, Dataset Preset \"Apps\"), then Edit Permissions on it and give "
                "user \"apps\" read and write. "
                f"The app checks again every {RETRY_SECONDS} seconds, so there's no need to restart it.")


def _problem_server(host: str, port: int, message: str) -> ThreadingHTTPServer:
    """A one-page server that explains what's wrong, until it's fixed."""
    body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Plex Cache – setup needed</title>"
            "<body style='font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 16px'>"
            "<h1>Plex Cache can’t start yet</h1><p>" + html.escape(message) + "</p>"
            "<p style='color:#666'>This page reloads itself.</p>"
            f"<script>setTimeout(function(){{location.reload()}},{RETRY_SECONDS * 1000})</script>").encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)  # /healthz too: the container is up, the app isn't
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def wait_for_config(config_dir: str, host: str, port: int, stop: threading.Event) -> bool:
    """Wait (serving an explanation) until the config folder is writable. False if stopped first."""
    problem = config_problem(config_dir)
    if not problem:
        return True
    log.error(problem)
    httpd = _problem_server(host, port, problem)
    try:
        while not stop.wait(RETRY_SECONDS):
            problem = config_problem(config_dir)
            if not problem:
                log.info("The config folder is writable now; starting.")
                return True
        return False
    finally:
        httpd.shutdown()
        httpd.server_close()


def main() -> int:
    config_dir = os.environ.get("PLEXCACHE_CONFIG", "/config")
    port = int(os.environ.get("PLEXCACHE_PORT", "8642"))
    host = os.environ.get("PLEXCACHE_HOST", "0.0.0.0")
    level = logging.DEBUG if os.environ.get("PLEXCACHE_DEBUG") else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    # Without a writable config folder the app can't run; crashing would only make Docker
    # restart it over and over, so explain the problem (in the log and on the web page) and wait.
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stopping.set())
    if not wait_for_config(config_dir, host, port, stopping):
        return 0
    app = App(config_dir)
    if not app.auth.enabled:
        log.warning("web UI login is off (PLEXCACHE_USERNAME / PLEXCACHE_PASSWORD not set): anyone on the network can use it")
    elif app.auth.default_password:
        log.warning("PLEXCACHE_PASSWORD is still the default; sign-in is refused until it's changed")
    app.start()
    httpd = serve(app, host, port)

    def shutdown(*_):
        app.stopping.set()
        app.warmup_stop.set()
        app.monitor.stop.set()
        app.ha.close()
        httpd.shutdown()

    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=shutdown).start())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
