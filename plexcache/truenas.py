"""Read-only TrueNAS API access over its JSON-RPC 2.0 WebSocket API.

Only ``query``-style methods are called. The app never changes anything on
the NAS.
"""

from __future__ import annotations

import itertools
import json
import urllib.parse

from .wsclient import WebSocket, WebSocketError
from .i18n import M


class TrueNASError(Exception):
    pass


def ws_url(url: str) -> str:
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    if u.scheme != "https":
        # TrueNAS revokes API keys that are sent over unencrypted connections.
        raise TrueNASError(M("Use an https:// address for TrueNAS. API keys sent over plain http get revoked by TrueNAS."))
    return urllib.parse.urlunparse(("wss", u.netloc, "/api/current", "", "", ""))


def probe(url: str, timeout: float = 5) -> bool:
    """True if a TrueNAS API answers at ``url`` (no API key needed)."""
    try:
        ws = WebSocket(ws_url(url), verify_tls=False, timeout=timeout)
    except (WebSocketError, TrueNASError):
        return False
    try:
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "system.version", "params": []}))
        reply = json.loads(ws.recv_text())
        return reply.get("jsonrpc") == "2.0"
    except (WebSocketError, ValueError, OSError):
        return False
    finally:
        ws.close()


class TrueNASClient:
    def __init__(self, url: str, api_key: str, verify_tls: bool = False, timeout: float = 20):
        if not url or not api_key:
            raise TrueNASError(M("TrueNAS address and API key are not set"))
        self._ids = itertools.count(1)
        try:
            self.ws = WebSocket(ws_url(url), verify_tls=verify_tls, timeout=timeout)
        except WebSocketError as e:
            raise TrueNASError(M("Can’t connect to TrueNAS: {why}", why=str(e))) from None
        try:
            ok = self.call("auth.login_with_api_key", [api_key])
        except TrueNASError:
            self.close()
            raise
        if ok is not True:
            self.close()
            raise TrueNASError(M("TrueNAS rejected the API key"))

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
                    raise TrueNASError(M("{method} failed: {why}", method=method, why=str(detail)))
                return reply.get("result")
        except WebSocketError as e:
            raise TrueNASError(M("TrueNAS connection problem: {why}", why=str(e))) from None
        except ValueError:
            raise TrueNASError(M("TrueNAS sent a reply that isn’t JSON")) from None

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

    CACHE_PROPS = ("primarycache", "secondarycache", "recordsize", "compression")

    def dataset_cache_props(self, dataset: str) -> dict | None:
        """primarycache / secondarycache / recordsize / compression for one dataset.

        pool.dataset.query doesn't always include these, so first ask ZFS for
        them by name (zfs.dataset.query with extra.properties), then fall back.
        Values TrueNAS doesn't return come back as None ("couldn't read"),
        never as a wrong value.
        """
        attempts = [
            ("zfs.dataset.query", [[["id", "=", dataset]], {"extra": {"properties": list(self.CACHE_PROPS)}}]),
            ("pool.dataset.query", [[["id", "=", dataset]], {"extra": {"properties": list(self.CACHE_PROPS),
                                                                        "retrieve_children": False}}]),
            ("pool.dataset.query", [[["id", "=", dataset]]]),
        ]
        found: dict = {}
        self.last_props_raw = []
        for method, params in attempts:
            try:
                rows = self.call(method, params)
            except TrueNASError as e:
                self.last_props_raw.append(f"{method}: {e}")
                continue
            if not rows:
                self.last_props_raw.append(f"{method}: no dataset {dataset}")
                continue
            got = extract_props(rows[0], self.CACHE_PROPS)
            self.last_props_raw.append(f"{method}: " + ", ".join(f"{k}={got.get(k)!r}" for k in self.CACHE_PROPS))
            for k, v in got.items():
                found.setdefault(k, v)
            if all(k in found for k in self.CACHE_PROPS):
                break
        if not found:
            return None
        rs = found.get("recordsize")
        return {"primarycache": _norm(found.get("primarycache")), "secondarycache": _norm(found.get("secondarycache")),
                "recordsize": parse_size(rs) if rs is not None else None, "compression": _norm(found.get("compression"))}

    def apps(self, config: bool = False) -> list[dict]:
        """Every app, with its containers and their states (active_workloads.container_details);
        with ``config``, its YAML too (as "config": it holds the app's secrets — never show it)."""
        if config:
            try:
                return self.call("app.query", [[], {"extra": {"retrieve_config": True}}]) or []
            except TrueNASError:
                pass  # not allowed: without it
        return self.call("app.query") or []

    def disk_sizes(self, names: list[str]) -> dict:
        if not names:
            return {}
        rows = self.call("disk.query", [[["name", "in", names]]])
        return {r.get("name"): int(r.get("size") or 0) for r in rows}


def _prop_value(v):
    """A property as TrueNAS returns it: a plain value, or a dict like
    {"value": "ALL", "rawvalue": "all", "parsed": "all", "source": "LOCAL"}."""
    if isinstance(v, dict):
        for k in ("rawvalue", "parsed", "value"):
            x = v.get(k)
            if x not in (None, ""):
                return x
        return None
    return None if v in (None, "") else v


def extract_props(row: dict, names) -> dict:
    """Find properties at the top level or under "properties" (zfs.dataset.query)."""
    out = {}
    nested = row.get("properties") if isinstance(row.get("properties"), dict) else {}
    for n in names:
        v = _prop_value(row.get(n)) if n in row else None
        if v is None and n in nested:
            v = _prop_value(nested[n])
        if v is not None:
            out[n] = v
    return out


def _norm(v) -> str | None:
    return str(v).strip().lower() if v is not None else None


def parse_size(v) -> int | None:
    """'128K' -> 131072, '1M' -> 1048576, '4096' -> 4096."""
    if v is None:
        return None
    t = str(v).strip().upper().rstrip("B")
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
    try:
        if t and t[-1] in mult:
            return int(float(t[:-1]) * mult[t[-1]])
        return int(float(t))
    except ValueError:
        return None


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
    disk_names = []
    for v in data:
        for c in (v.get("children") or [v]):
            if c.get("disk"):
                disk_names.append(c["disk"])
    return {
        "disks": disk_names,  # data disks, e.g. ["sda", "sdb"]
        "name": pool.get("name", ""),
        "status": pool.get("status", ""),
        "layout": ", ".join(layouts),
        "data_disks": disks,
        "vdevs": len(data),
        "cache_devices": cache,
        "l2_total": sum(c["size"] for c in online),
        "l2_online": len(online),
    }
