"""Translations.

Text the app makes on the server (health checks, the Activity report, the event log,
error messages, Home Assistant names) is written as an ``M`` message: a template with
its values kept apart, e.g. ``M("Pool {pool} is {pct}% full", pool="tank", pct=91)``.

* An ``M`` *is* a ``str`` (its English text), so logs, tests and string comparisons
  carry on working unchanged.
* Sent to the browser, it travels as ``{"_t": template, "_p": values}`` and each
  browser shows it in its own language (web/i18n.js does the same lookups as here).
* MQTT has no browser, so Home Assistant gets it rendered here, in the language
  chosen under Settings → MQTT.

Catalogs live in web/i18n/<code>.json, keyed by the English template:
``{"Pool {pool} is {pct}% full": "Le pool {pool} est plein à {pct} %"}``. A plural
has one entry per CLDR category: ``{"{n} files": {"one": "{n} Datei", "other": "{n} Dateien"}}``.

English is the original. The base text is Canadian English; en-US and en-GB only
override the spellings that differ (behaviour/behavior, analyse/analyze...).
"""

import json
import os
import re
import threading
from typing import Any

I18N_DIR = os.path.join(os.path.dirname(__file__), "web", "i18n")

# Code, name in its own language. English first: it's the original.
LANGUAGES = [
    ("en", "English (Canada)"), ("en-US", "English (US)"), ("en-GB", "English (UK)"),
    ("es", "Español"), ("fr", "Français"), ("de", "Deutsch"), ("pt-BR", "Português (Brasil)"),
    ("it", "Italiano"), ("nl", "Nederlands"), ("pl", "Polski"), ("ru", "Русский"),
    ("ja", "日本語"), ("zh-CN", "简体中文"), ("ko", "한국어"), ("zh-TW", "繁體中文"),
    ("tr", "Türkçe"), ("sv", "Svenska"), ("uk", "Українська"),
]
CODES = [c for c, _ in LANGUAGES]

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class M(str):
    """A message to translate. Its ``str`` value is the English text.

    ``M("{n} files", n=3, one="{n} file")`` is a plural: ``one`` is the English
    singular, and ``n`` picks the form in each language.
    """

    tmpl: str
    one: str | None
    params: dict

    def __new__(cls, tmpl: str = "", one: str | None = None, **params):
        n = params.get("n")
        text = one if one is not None and plural_category("en", n) == "one" else tmpl
        self = super().__new__(cls, fill(text, params, "en"))
        self.tmpl, self.one, self.params = tmpl, one, params
        return self

    def __reduce__(self):  # copy / pickle keep the template
        return (_rebuild, (self.tmpl, self.one, self.params))

    def json(self) -> dict:
        out: dict = {"_t": self.tmpl}
        if self.params:
            out["_p"] = {k: jsonable(v) for k, v in self.params.items()}
        if self.one is not None:
            out["_one"] = self.one
        return out


def _rebuild(tmpl, one, params):
    return M(tmpl, one=one, **params)


class Fixed(float):
    """A number shown with a fixed count of decimals: Fixed(2.0, 1) is "2.0" (or "2,0")."""

    places: int

    def __new__(cls, value: float, places: int = 1):
        self = super().__new__(cls, round(value, places))
        self.places = places
        return self

    def __reduce__(self):
        return (Fixed, (float(self), self.places))


def size_text(n: float) -> M:
    """An amount of data in the unit that suits it: "3.2 MB", "41 MB", "1.6 GB"."""
    for tmpl, unit in (("{n} TB", 1e12), ("{n} GB", 1e9), ("{n} MB", 1e6), ("{n} KB", 1e3)):
        if n >= unit:
            return M(tmpl, n=Fixed(n / unit, 1 if n < 10 * unit else 0))
    return M("{n} B", n=round(n))


def duration_text(sec: float) -> M:
    """A length of time in the unit that suits it: "180 ms", "2.4 s", "38 s", "4 min 10 s", "1 h 5 min"."""
    if sec < 1:
        return M("{n} ms", n=round(sec * 1000))
    if sec < 60:
        return M("{n} s", n=Fixed(sec, 1 if sec < 10 else 0))
    if sec < 3600:
        m, s = divmod(round(sec), 60)
        return M("{m} min {s} s", m=m, s=s) if s else M("{m} min", m=m)
    h, m = divmod(round(sec / 60), 60)
    return M("{h} h {m} min", h=h, m=m) if m else M("{h} h", h=h)


def msg(error: BaseException) -> str:
    """An exception's message, keeping it translatable when it was raised with an ``M``."""
    first = error.args[0] if len(error.args) == 1 else None
    return first if isinstance(first, M) else str(error)


def jsonable(v) -> Any:
    """``v`` with every ``M`` inside turned into its JSON form (for the browser)."""
    if isinstance(v, M):
        return v.json()
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


def from_json(v):
    """The other way: an ``{"_t": ...}`` read back from history.json becomes an ``M`` again."""
    if isinstance(v, dict) and isinstance(v.get("_t"), str):
        params = {k: from_json(x) for k, x in (v.get("_p") or {}).items()}
        return M(v["_t"], one=v.get("_one"), **params)
    return v


# ------------------------------------------------------------------ plurals
def plural_category(lang: str, n) -> str:
    """CLDR plural category of ``n`` (the same answer Intl.PluralRules gives)."""
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return "other"
    whole = float(n).is_integer()
    i = int(abs(n))
    base = lang.split("-")[0]
    if base in ("ja", "zh", "ko"):
        return "other"
    if base in ("fr", "pt"):
        return "one" if i in (0, 1) else "other"
    if base in ("ru", "uk"):
        if not whole:
            return "other"
        if i % 10 == 1 and i % 100 != 11:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    if base == "pl":
        if not whole:
            return "other"
        if i == 1:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    return "one" if whole and i == 1 else "other"


# ----------------------------------------------------------------- numbers
# (thousands separator, decimal separator), as each language writes them.
_NBSP = " "
_SEPARATORS = {
    "en": (",", "."), "ja": (",", "."), "zh": (",", "."), "ko": (",", "."),
    "fr": (" ", ","), "pl": (_NBSP, ","), "ru": (_NBSP, ","), "uk": (_NBSP, ","), "sv": (_NBSP, ","),
    "es": (".", ","), "de": (".", ","), "pt": (".", ","), "it": (".", ","), "nl": (".", ","), "tr": (".", ","),
}


def number(v, lang: str) -> str:
    group, dec = _SEPARATORS.get(lang.split("-")[0], (",", "."))
    if isinstance(v, Fixed):
        text = f"{v:,.{v.places}f}"
    elif isinstance(v, float) and not v.is_integer():
        places = min(2, len(repr(v).split(".")[1])) if "." in repr(v) else 1
        text = f"{v:,.{places}f}"  # callers round first; this keeps the decimals they kept
    else:
        text = f"{int(v):,}"
    # Spanish and Polish leave four-digit numbers ungrouped (2000, not 2.000).
    if lang.split("-")[0] in ("es", "pl") and abs(v) < 10000:
        text = text.replace(",", "")
    return text.replace(",", "\0").replace(".", dec).replace("\0", group)


def fill(text: str, params: dict, lang: str) -> str:
    def sub(m):
        if m.group(1) not in params:
            return m.group(0)
        return value(params[m.group(1)], lang)
    return _PLACEHOLDER.sub(sub, text)


def value(v, lang: str) -> str:
    if isinstance(v, M):
        return render(v, lang)
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, (int, float)):
        return number(v, lang)
    if isinstance(v, (list, tuple)):  # a list of names: "a, b, c"
        return ("、" if lang.split("-")[0] in ("ja", "zh") else ", ").join(value(x, lang) for x in v)
    return str(v)


# ---------------------------------------------------------------- catalogs
_catalogs: dict[str, dict] = {}
_lock = threading.Lock()


def _load(code: str) -> dict:
    with _lock:
        if code not in _catalogs:
            try:
                with open(os.path.join(I18N_DIR, code + ".json"), encoding="utf-8") as fh:
                    _catalogs[code] = json.load(fh)
            except (OSError, ValueError):
                _catalogs[code] = {}
        return _catalogs[code]


def chain(lang: str) -> list[dict]:
    """Catalogs to look in, most specific first: en-US -> [en-US]; zh-TW -> [zh-TW]."""
    if lang == "en" or lang not in CODES:
        return []
    return [_load(lang)]


def lookup(key: str, lang: str):
    for cat in chain(lang):
        if key in cat:
            return cat[key]
    return None


def render(m, lang: str = "en") -> str:
    """``m`` in ``lang``. Plain strings come back as they are."""
    if not isinstance(m, M):
        return "" if m is None else str(m)
    if lang == "en" or lang not in CODES:
        return str(m)
    entry = lookup(m.tmpl, lang)
    if m.one is not None:
        cat = plural_category(lang, m.params.get("n"))
        if isinstance(entry, dict) and (entry.get(cat) or entry.get("other")):
            text = entry.get(cat) or entry.get("other") or ""
        elif isinstance(entry, str):
            text = entry
        else:  # not translated yet: English
            text = m.one if plural_category("en", m.params.get("n")) == "one" else m.tmpl
    else:
        text = entry if isinstance(entry, str) else m.tmpl
    return fill(text, m.params, lang)


def tr(text: str, lang: str) -> str:
    """A fixed string (no values) in ``lang``."""
    return render(M(text), lang)


def join(items: list, word: str = "and"):
    """"A", "A and B", "A, B and C", with the joining word translated."""
    items = list(items)
    if len(items) < 2:
        return items[0] if items else ""
    if word == "or":
        return M("{list} or {last}", list=items[:-1], last=items[-1])
    return M("{list} and {last}", list=items[:-1], last=items[-1])
