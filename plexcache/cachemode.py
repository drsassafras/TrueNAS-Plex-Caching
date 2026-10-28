"""L2ARC or RAM only: which caching mode the NAS is in, and what that means for settings.

Caching is planned around an L2ARC (SSD cache). Without one (or when "I won't use an
L2ARC" is ticked on the SSD Cache page) the app runs in RAM-only mode. Settings follow the
mode in two ways:

* A setting left on "Default" uses its mode's default, looked up every time it's used, so it
  changes by itself when an L2ARC is added or lost. The page shows which value that is.
* Some choices only make sense with an L2ARC. Picked without one, the setting falls back to
  its RAM-only default; that's recorded here, and Health lists it so it doesn't go unnoticed.

Settings register themselves in DYNAMIC: ``DYNAMIC[id] = Dynamic(...)``. The id is
"section.key", or for a kind of per-library setting "prefetch.start" (each library resolves
with its own id, "prefetch.<library key>.start", so fallbacks are tracked per library).
"""

import threading
import time
from dataclasses import dataclass, field


L2ARC, RAM = "l2arc", "ram"
FALLBACK_KEEP = 24 * 3600  # how long Health keeps showing a fallback after the last one


@dataclass
class Dynamic:
    l2arc: str                 # what "default" means with an L2ARC
    ram: str                   # ... and without one
    l2arc_only: tuple = ()     # choices that need an L2ARC (they fall back to `ram`)
    label: object = ""         # the setting's name, for Health
    names: dict = field(default_factory=dict)  # value -> its name on the page, for Health


# Mode-dependent settings register here (prefetch.py adds "prefetch.start" and "prefetch.end").
DYNAMIC: dict[str, Dynamic] = {}


def mode(settings: dict, l2arc_status: str) -> str:
    """L2ARC when the media pool has one (and it isn't switched off); otherwise RAM.
    An L2ARC that can't be confirmed counts as absent: RAM-only choices are always safe."""
    if settings["general"].get("no_l2arc"):
        return RAM
    return L2ARC if l2arc_status == "present" else RAM


@dataclass
class Fallbacks:
    """Settings whose L2ARC-only choice couldn't be used. id -> {label, chosen, used, at}."""
    items: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def note(self, sid: str, label, chosen: str, used: str) -> None:
        with self.lock:
            self.items[sid] = {"id": sid, "label": label, "chosen": chosen, "used": used, "at": time.time()}

    def clear(self, sid: str) -> None:
        with self.lock:
            self.items.pop(sid, None)

    def recent(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self.lock:
            for sid in [k for k, v in self.items.items() if now - v["at"] > FALLBACK_KEEP]:
                del self.items[sid]
            return sorted((dict(v) for v in self.items.values()), key=lambda v: v["id"])


def resolve(sid: str, value: str, current_mode: str, fallbacks: Fallbacks | None = None,
            dyn: Dynamic | None = None) -> str:
    """The value to actually use for setting ``sid`` set to ``value``, in ``current_mode``.
    Call it each time the setting is used, so a lost or added L2ARC takes effect at once."""
    dyn = dyn or DYNAMIC[sid]
    if value == "default":
        if fallbacks:
            fallbacks.clear(sid)
        return dyn.l2arc if current_mode == L2ARC else dyn.ram
    if current_mode != L2ARC and value in dyn.l2arc_only:
        if fallbacks:
            fallbacks.note(sid, dyn.label, dyn.names.get(value, value), dyn.names.get(dyn.ram, dyn.ram))
        return dyn.ram
    if fallbacks:
        fallbacks.clear(sid)
    return value


def describe(current_mode: str, l2arc_status: str, no_l2arc: bool, fallbacks: Fallbacks) -> dict:
    """For the web UI: the mode, what each Default currently means, and recent fallbacks."""
    return {
        "mode": current_mode,
        "l2arc": l2arc_status,
        "no_l2arc": no_l2arc,
        "defaults": {sid: (d.l2arc if current_mode == L2ARC else d.ram) for sid, d in DYNAMIC.items()},
        # Per setting: each mode's default and the choices that need an L2ARC, so every dropdown
        # can mark them (and say what a fallback would use).
        "settings": {sid: {"l2arc": d.l2arc, "ram": d.ram, "l2arc_only": list(d.l2arc_only)} for sid, d in DYNAMIC.items()},
        "fallbacks": fallbacks.recent(),
    }
