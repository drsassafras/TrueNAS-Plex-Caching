"""Read-only TrueNAS API access over its JSON-RPC 2.0 WebSocket API.

Only ``query``-style methods are called. The app never changes anything on
the NAS.
"""

from __future__ import annotations

import itertools
import json
import urllib.parse

from .wsclient import WebSocket, WebSocketError


class TrueNASError(Exception):
    pass


def ws_url(url: str) -> str:
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    if u.scheme != "https":
        # TrueNAS revokes API keys that are sent over unencrypted connections.
        raise TrueNASError(
            "Use an https:// address for TrueNAS. API keys sent over plain http get revoked by TrueNAS."
        )
    return urllib.parse.urlunparse(("wss", u.netloc, "/api/current", "", "", ""))


class TrueNASClient:
    def __init__(self, url: str, api_key: str, verify_tls: bool = False, timeout: float = 20):
        if not url or not api_key:
            raise TrueNASError("TrueNAS address and API key are not set")
        self._ids = itertools.count(1)
        try:
            self.ws = WebSocket(ws_url(url), verify_tls=verify_tls, timeout=timeout)
        except WebSocketError as e:
            raise TrueNASError(f"Can't connect to TrueNAS: {e}") from None
        try:
            ok = self.call("auth.login_with_api_key", [api_key])
        except TrueNASError:
            self.close()
            raise
        if ok is not True:
            self.close()
            raise TrueNASError("TrueNAS rejected the API key")

    def call(self, method: str, params: list | None = None):
        msg_id = next(self._ids)
        try:
            self.ws.send_text(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or []}))
            while True:
                reply = json.loads(self.ws.recv_text())
                if reply.get("id") != msg_id:
                    continue  # notifications/events
                if "error" in reply and reply["error"]:
                    err = reply["error"]
                    detail = (err.get("data") or {}).get("reason") or err.get("message") or str(err)
                    raise TrueNASError(f"{method} failed: {detail}")
                return reply.get("result")
        except WebSocketError as e:
            raise TrueNASError(f"TrueNAS connection problem: {e}") from None
        except ValueError:
            raise TrueNASError("TrueNAS sent a reply that isn't JSON") from None

    def close(self) -> None:
        self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- queries
    def version(self) -> str:
        try:
            return str(self.call("system.version"))
        except TrueNASError:
            return ""

    def pool(self, name: str) -> dict | None:
        pools = self.call("pool.query", [[["name", "=", name]]])
        return pools[0] if pools else None

    def dataset_cache_props(self, dataset: str) -> dict | None:
        try:
            rows = self.call(
                "pool.dataset.query",
                [[["id", "=", dataset]], {"select": ["id", "primarycache", "secondarycache"]}],
            )
        except TrueNASError:
            rows = None
        if not rows:
            # Versions without "select" support: query plainly.
            rows = self.call("pool.dataset.query", [[["id", "=", dataset]]])
        if not rows:
            return None
        row = rows[0]

        def val(prop):
            v = row.get(prop)
            if isinstance(v, dict):
                return str(v.get("value") or v.get("rawvalue") or "").lower()
            return str(v or "").lower()

        return {"primarycache": val("primarycache"), "secondarycache": val("secondarycache")}

    def disk_sizes(self, names: list[str]) -> dict:
        if not names:
            return {}
        rows = self.call("disk.query", [[["name", "in", names]]])
        return {r.get("name"): int(r.get("size") or 0) for r in rows}


def describe_pool(pool: dict, disk_sizes: dict | None = None) -> dict:
    """Summarise data layout and cache (L2ARC) devices from pool.query output."""
    topo = pool.get("topology") or {}
    data = topo.get("data") or []
    layouts = sorted({v.get("type", "?") for v in data})
    disks = sum(max(1, len(v.get("children") or [])) for v in data)
    cache = []
    for v in topo.get("cache") or []:
        name = v.get("disk") or v.get("name") or ""
        size = int(((v.get("stats") or {}).get("size")) or 0)
        if not size and disk_sizes:
            size = disk_sizes.get(name, 0)
        cache.append({"name": name, "status": v.get("status", ""), "size": size})
    online = [c for c in cache if c["status"] == "ONLINE"]
    return {
        "name": pool.get("name", ""),
        "status": pool.get("status", ""),
        "layout": ", ".join(layouts),
        "data_disks": disks,
        "vdevs": len(data),
        "cache_devices": cache,
        "l2_total": sum(c["size"] for c in online),
        "l2_online": len(online),
    }
