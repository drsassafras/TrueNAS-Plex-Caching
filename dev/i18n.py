#!/usr/bin/env python3
"""Translation catalogs: find every translatable text and check each language has it.

    python3 dev/i18n.py check              # translations match their English (catalogs may lag the code)
    python3 dev/i18n.py check --release    # ...and catalogs in step with the code: nothing missing or unused
    python3 dev/i18n.py check --complete   # ...and every text translated
    python3 dev/i18n.py status             # how much of each language is translated
    python3 dev/i18n.py sync               # add new texts as null, drop texts no longer used
    python3 dev/i18n.py missing de         # what German still needs, as JSON to fill in
    python3 dev/i18n.py merge de new.json  # add translations to plexcache/web/i18n/de.json
    python3 dev/i18n.py leftovers          # English text in app.js not passed through T()/H()...
    python3 dev/i18n.py keys               # every key, one per line

Where the text comes from:
* web/*.js: T("..."), H("..."), N("...") and P(n, "one", "other") / PH(...). Adjacent
  string literals joined with + count as one text, so long ones can be split over lines.
* web/*.html: elements with data-i18n / data-i18n-html, and data-i18n-<attr> attributes.
* plexcache/*.py: M("...") and M("...", one="...").

Each catalog (web/i18n/<code>.json) maps the English text to its translation; a plural maps
to {"one": ..., "few": ..., "many": ..., "other": ...} with the categories its language uses.
en-US.json and en-GB.json only hold the texts whose spelling differs.

Translating happens in place: after ``sync``, every text a language still needs is in its
catalog with the value null. Replace the null with the translation (or a plural object); the
app shows English for anything still null, so a half-done language is safe to ship.

Catalogs are only brought in step with the code for a release (README.md → Translations).
Between releases they lag: a new text isn't in them yet (the app shows the English) and one
the code no longer uses lingers. Plain ``check`` allows that; ``check --release`` doesn't.
"""

import ast
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "plexcache")
WEB = os.path.join(PKG, "web")
CATALOGS = os.path.join(WEB, "i18n")
sys.path.insert(0, ROOT)
from plexcache.i18n import CODES  # noqa: E402

TRANSLATED = [c for c in CODES if c not in ("en", "en-US", "en-GB")]
VARIANTS = ("en-US", "en-GB")
CATEGORIES = {  # CLDR plural categories used for whole numbers (and "other" for fractions)
    "ja": ["other"], "zh-CN": ["other"], "zh-TW": ["other"], "ko": ["other"],
    "ru": ["one", "few", "many", "other"], "uk": ["one", "few", "many", "other"],
    "pl": ["one", "few", "many", "other"],
}
_PLACE = re.compile(r"\{(\w+)\}")
_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")


# ------------------------------------------------------------------ JavaScript
def js_tokens(src: str):
    """(kind, value, start) for identifiers, strings, numbers and punctuation; skips comments
    and regular expression literals (told apart from division by what comes before)."""
    i, n, out = 0, len(src), []
    regex_ok = True
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
            continue
        if src.startswith("/*", i):
            i = src.index("*/", i) + 2
            continue
        if c in "\"'`":
            j, buf = i + 1, []
            while src[j] != c:
                if src[j] == "\\":
                    nxt = src[j + 1]
                    buf.append({"n": "\n", "t": "\t", "u": None}.get(nxt, nxt))
                    if nxt == "u":
                        buf[-1] = chr(int(src[j + 2:j + 6], 16))
                        j += 4
                    j += 2
                else:
                    buf.append(src[j])
                    j += 1
            out.append(("str", "".join(buf), i))
            i = j + 1
            regex_ok = False
            continue
        if c == "/" and regex_ok:
            j, in_class = i + 1, False
            while True:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            out.append(("re", src[i:j], i))
            i = j
            regex_ok = False
            continue
        m = re.compile(r"[A-Za-z_$][\w$]*|\d[\d.]*").match(src, i)
        if m:
            word = m.group(0)
            out.append(("id" if not word[0].isdigit() else "num", word, i))
            i = m.end()
            regex_ok = word in ("return", "typeof", "case", "in", "of", "new", "delete", "void", "throw")
            continue
        out.append(("p", c, i))
        i += 1
        regex_ok = c not in ")]}"
    return out


def _literal_chain(toks, k):
    """A string literal, or several joined with +, starting at toks[k]. -> (text, next k) or None."""
    if k >= len(toks) or toks[k][0] != "str":
        return None
    parts, used = [toks[k][1]], [k]
    k += 1
    while k + 1 < len(toks) and toks[k] == ("p", "+", toks[k][2]) and toks[k + 1][0] == "str":
        parts.append(toks[k + 1][1])
        used.append(k + 1)
        k += 2
    # A chain that goes on with + something-else isn't a fixed text.
    if k < len(toks) and toks[k][0] == "p" and toks[k][1] == "+":
        return None
    return "".join(parts), k, used


def _skip_arg(toks, k):
    depth = 0
    while k < len(toks):
        kind, v, _ = toks[k]
        if kind == "p" and v in "([{":
            depth += 1
        elif kind == "p" and v in ")]}":
            if depth == 0:
                return k
            depth -= 1
        elif kind == "p" and v == "," and depth == 0:
            return k
        k += 1
    return k


def from_js(path: str, problems: list | None = None):
    """{key: None | one-form}, plus the token indexes of the literals that were used."""
    src = open(path, encoding="utf-8").read()
    toks = js_tokens(src)
    keys: dict[str, str | None] = {}
    used: set[int] = set()
    for k, (kind, v, pos) in enumerate(toks):
        if kind != "id" or v not in ("T", "H", "N", "P", "PH") or k + 1 >= len(toks) or toks[k + 1][1] != "(":
            continue
        if k > 0 and toks[k - 1][1] in (".", "function"):
            continue
        line = src.count("\n", 0, pos) + 1
        if v in ("T", "H", "N"):
            got = _literal_chain(toks, k + 2)
            if got:
                keys.setdefault(got[0], None)
                used.update(got[2])
            elif problems is not None and v != "N":
                problems.append(f"{os.path.relpath(path, ROOT)}:{line}: {v}() of a variable (fine if its text is marked with N() elsewhere)")
        else:
            a = _skip_arg(toks, k + 2)
            one = _literal_chain(toks, a + 1)
            other = one and _literal_chain(toks, one[1] + 1)
            if one and other:
                keys[other[0]] = one[0]
                used.update(one[2] + other[2])
            elif problems is not None:
                problems.append(f"{os.path.relpath(path, ROOT)}:{line}: {v}() without literal texts")
    return keys, used, toks, src


class _HTMLTexts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.keys: dict[str, None] = {}
        self.stack: list = []
        self.raw = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for name, val in attrs:
            if name.startswith("data-i18n-") and name not in ("data-i18n-html",):
                self.keys.setdefault(a.get(name[len("data-i18n-"):], ""), None)
        if self.stack:
            self.stack[-1][2].append(self.get_starttag_text())
        if "data-i18n" in a or "data-i18n-html" in a:
            self.stack.append([tag, 0, []])
        elif self.stack and self.stack[-1][0] == tag:
            self.stack[-1][1] += 1

    def handle_endtag(self, tag):
        if not self.stack:
            return
        top = self.stack[-1]
        if top[0] == tag and top[1] == 0:
            self.stack.pop()
            self.keys.setdefault(re.sub(r"\s+", " ", "".join(top[2]).strip()), None)
            return
        if top[0] == tag:
            top[1] -= 1
        top[2].append(f"</{tag}>")

    def handle_data(self, data):
        if self.stack:
            self.stack[-1][2].append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def from_html(path: str) -> dict:
    p = _HTMLTexts()
    p.feed(open(path, encoding="utf-8").read())
    p.keys.pop("", None)
    return p.keys


# ---------------------------------------------------------------------- Python
def from_py(path: str, problems: list | None = None) -> dict:
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    keys: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "M":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                one = next((kw.value.value for kw in node.keywords if kw.arg == "one"
                            and isinstance(kw.value, ast.Constant)), None)
                if keys.get(node.args[0].value) is None:
                    keys[node.args[0].value] = one
            elif problems is not None and os.path.basename(path) != "i18n.py":
                problems.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}: M() of a variable")
    return keys


def all_keys(problems: list | None = None) -> dict:
    keys: dict[str, str | None] = {}

    def add(found):
        for k, one in found.items():
            if keys.get(k) is None:
                keys[k] = one
    for name in sorted(os.listdir(WEB)):
        p = os.path.join(WEB, name)
        if name.endswith(".js"):
            add(from_js(p, problems)[0])
        elif name.endswith(".html"):
            add(from_html(p))
    for dirpath, _, files in os.walk(PKG):
        for name in sorted(files):
            if name.endswith(".py"):
                add(from_py(os.path.join(dirpath, name), problems))
    keys.pop("", None)
    return keys


# --------------------------------------------------------------------- checks
def load(code: str) -> dict:
    try:
        with open(os.path.join(CATALOGS, code + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _shape(text: str):
    return sorted(_PLACE.findall(text)), sorted(t.lower() for t in _TAG.findall(text))


def untranslated(keys: dict, cat: dict) -> list[str]:
    """Texts with no translation yet (absent, null, or a plural with a null form)."""
    def done(v):
        return isinstance(v, str) or (isinstance(v, dict) and all(isinstance(f, str) for f in v.values()))
    return [k for k in keys if not done(cat.get(k))]


def check_catalog(code: str, keys: dict, cat: dict, complete: bool = False, release: bool = False) -> list[str]:
    """Problems with one catalog: placeholders, tags or plural forms that differ; with
    ``release``, texts not in it yet and keys no longer used (what ``sync`` fixes); and with
    ``complete`` (which implies ``release``), texts not translated yet."""
    out = []
    variant = code in VARIANTS
    release = release or complete
    todo = set(untranslated(keys, cat))
    for key, one in keys.items():
        if key in todo:
            if complete and not variant:
                out.append(f"missing: {key[:90]!r}")
            elif release and not variant and key not in cat:
                out.append(f"not in the catalog (run sync): {key[:70]!r}")
            continue
        val = cat[key]
        want_places, want_tags = _shape(key)
        if one is not None:
            need = CATEGORIES.get(code, ["one", "other"])
            if variant and isinstance(val, dict):
                need = ["one", "other"]
            if not isinstance(val, dict) or any(c not in val for c in need):
                out.append(f"plural needs {need}: {key[:80]!r}")
                continue
            forms = list(val.values())
            want_places = sorted(set(want_places) | set(_PLACE.findall(one)) - ({"n"} if code in CATEGORIES else set()))
            for f in forms:
                places, tags = _shape(f)
                extra = set(places) - set(_PLACE.findall(key)) - set(_PLACE.findall(one))
                if extra or not set(want_places) - {"n"} <= set(places):
                    out.append(f"placeholders {places} vs {want_places}: {key[:70]!r}")
                if tags != want_tags:
                    out.append(f"tags {tags} vs {want_tags}: {key[:70]!r}")
            continue
        if not isinstance(val, str):
            out.append(f"should be text: {key[:80]!r}")
            continue
        places, tags = _shape(val)
        if places != want_places:
            out.append(f"placeholders {places} vs {want_places}: {key[:70]!r}")
        if tags != want_tags:
            out.append(f"tags {tags} vs {want_tags}: {key[:70]!r}")
    for key in cat:
        if release and key not in keys:
            out.append(f"no longer used: {key[:90]!r}")
    return out


def save(code: str, cat: dict) -> None:
    with open(os.path.join(CATALOGS, code + ".json"), "w", encoding="utf-8") as fh:
        json.dump(cat, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def leftovers() -> list[str]:
    """English-looking string literals in app.js that don't go through the translation helpers."""
    out = []
    path = os.path.join(WEB, "app.js")
    _, used, toks, src = from_js(path)
    for k, (kind, v, pos) in enumerate(toks):
        if kind != "str" or k in used:
            continue
        text = re.sub(r"<[^>]*>", " ", v)
        if not re.search(r"[A-Za-z]{2,}[ ,.;:!?’]+[A-Za-z]{2,}|^[A-Z][a-z]{2,}", text):
            continue
        prev = toks[k - 1][1] if k else ""
        if prev in ("querySelector", "querySelectorAll", "getElementById") or re.fullmatch(r"[\w.-]+", v):
            continue
        out.append(f"app.js:{src.count(chr(10), 0, pos) + 1}: {v[:100]!r}")
    return out


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    problems: list[str] = []
    keys = all_keys(problems)
    if cmd == "keys":
        for k in keys:
            print(k)
        return 0
    if cmd == "leftovers":
        print("\n".join(leftovers() + problems))
        return 0
    if cmd == "status":
        print(f"{len(keys)} texts")
        for code in TRANSLATED:
            done = len(keys) - len(untranslated(keys, load(code)))
            print(f"{code:6} {done:5} translated  {100 * done // len(keys):3}%")
        return 0
    if cmd == "sync":
        for code in argv[2:] or TRANSLATED:
            cat = load(code)
            new = {k: cat.get(k) for k in keys}
            save(code, new)
            print(f"{code}: {len(keys) - len(untranslated(keys, new))} of {len(keys)} translated")
        return 0
    if cmd == "missing":
        code = argv[2]
        cat = load(code)
        todo = {k: ({"one": one, "other": k} if one is not None else k) for k, one in keys.items() if k in untranslated(keys, cat)}
        print(json.dumps(todo, ensure_ascii=False, indent=1))
        return 0
    if cmd == "merge":
        code, path = argv[2], argv[3]
        cat = load(code)
        with open(path, encoding="utf-8") as fh:
            cat.update(json.load(fh))
        cat = {k: cat[k] for k in keys if k in cat}
        save(code, cat)
        print(f"{code}: {len(keys) - len(untranslated(keys, cat))} of {len(keys)} translated")
        return 0
    bad = 0
    complete = "--complete" in argv
    release = "--release" in argv
    print(f"{len(keys)} texts")
    for code in TRANSLATED + list(VARIANTS):
        cat = load(code)
        issues = check_catalog(code, keys, cat, complete, release)
        bad += bool(issues)
        left = "" if code in VARIANTS else f"  ({len(untranslated(keys, cat))} untranslated)"
        print(f"{code:6} {'ok' if not issues else str(len(issues)) + ' problems'}{left}")
        for p in issues[:15]:
            print("   ", p)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
