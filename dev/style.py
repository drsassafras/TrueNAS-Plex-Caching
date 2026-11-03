#!/usr/bin/env python3
"""The style guide: every look the app may wear, approved by the maintainer. Nothing else may be added or changed.

    python3 dev/style.py check     # what's new, changed or gone since the guide (the suite's StyleGuideTests runs the same)
    python3 dev/style.py sample    # each of those Before (as approved) and After (now), side by side: dev/style-sample/
    python3 dev/style.py sample --before <folder>   # the Before from a folder of the approved look files
    python3 dev/style.py review    # the maintainer only, at a terminal: the sample's page with Approve buttons
                                   # (or r on tnsync's board: review --board, its output in dev/style-sample/review.log)
    python3 dev/style.py approve   # the maintainer only, at a terminal: the sample's looks go into the guide

The guide (dev/style-guide.json) lists each look by what it is, with what it is now:
* ``css <at-rule> | <selector>``: a rule of the stylesheet (plexcache/web/app.css) or of a page's own <style>, its
  declarations (a selector written more than once: each time's, in order, joined by " ‖ ");
* ``inline <style>``: a style="..." a page or script writes (what a script fills in shown as …);
* ``scripted <property>``: a style property a script sets itself (el.style.<property> = …, setProperty);
* ``svg <markup>``: a drawing a page or script writes (an icon, the grape arrow, a chart's marks);
* ``mark <file> <name>``: what draws the decorations: themes.py's css() and script(), as written, comments left out;
  and what plexcache/web/decor.js draws (``mark decor.js draws | <where>``: each drawing it puts on a fixed page and
  each style it sets, by its SHA-256; dev/decor-draws.js), so a change to its code that draws the same isn't a new look;
* ``theme <id>/<mode> <colour>``, ``theme <id> font|decor <key>``, ``theme font <name>``: the built-in themes
  (plexcache/themes.py);
* ``file <path>``: an image or font the app ships (plexcache/web's .svg files and fonts/), by its SHA-256.

A look that isn't in the guide, or isn't as the guide has it, or is in the guide but gone from the code, fails
StyleGuideTests. Only ``approve`` writes the guide, and only for a sample made of exactly the looks that differ now
(its fingerprint is kept in the guide with the date): the maintainer sees each look before it is allowed. It asks at a
terminal and won't run without one. Nobody else runs it or edits the guide (CLAUDE.md).

``sample`` starts two demos (tests/demo.py, ports of their own): today's app, and the app as approved (today's with the
approved look files over it: the copy approve keeps in dev/style-approved.zip, or --before). It takes the pictures with Playwright (dev/style-sample.js,
like dev/screenshots.js: ``npm i -g playwright && npx playwright install chromium``), with the progress bar the dev
tools share (dev/progress.py): each look Before and After, cropped alike and compared pixel for pixel. It writes
dev/style-sample/index.html (open it in a browser): what changed on screen side by side, what's judged by its code, and,
folded away, what drew the same; and sample.json.
"""

import ast
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tokenize
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "plexcache", "web")
GUIDE = os.path.join(ROOT, "dev", "style-guide.json")
SAMPLE = os.path.join(ROOT, "dev", "style-sample")
FORMAT = 1
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from progress import Progress, feeding, result, said, tally  # noqa: E402  (the dev tools' shared progress bar, and tnsync's line under it)


def _dev(name: str, file: str):
    """Another dev tool's module (dev/ isn't a package; dev/i18n.py would clash with plexcache/i18n.py by name)."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "dev", file))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_i18n = _dev("i18n_tool", "i18n.py")  # its JavaScript tokenizer: the strings a script writes, as the translations read them

SCRIPTS = ("app.js", "decor.js", "i18n.js", "login.js")
PAGES = ("index.html", "login.html")
DRAWS = ("decor.js",)   # a script that is all decoration: a look for what it draws (dev/decor-draws.js), not for how it's written
_SHAPES = re.compile(r"<(svg|path|circle|ellipse|rect|polygon|polyline|line|g|mask|pattern|linearGradient|radialGradient|stop|use|text)\b")
HOLE = "…"   # what a script fills in


# ------------------------------------------------------------------ CSS
def _squash(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*([:;,>{}+~()])\s*", r"\1", s)
    return _wordless_css(s.strip(";"))


# Words aren't a look: what the app says is content (a card's title reworded), not style, so it never needs approving.
# In a look, every bit of text that has a letter in it is "…": the text between tags, the attributes people read
# (title, aria-label, alt, placeholder), and a stylesheet's content:"…". Text without letters (…%, a glyph, an
# arrow) stays: that's drawing.
_LETTER = re.compile(r"[^\W\d_]")
_READ = re.compile(r'(\s(?:title|aria-label|aria-description|aria-roledescription|alt|placeholder|data-tip|data-title)=")([^"]*)"')


def _word(text: str) -> str:
    return "…" if _LETTER.search(text) and text.strip() != "…" else text


def _wordless(markup: str) -> str:
    markup = _READ.sub(lambda m: m.group(1) + _word(m.group(2)) + '"', markup)
    return re.sub(r">([^<>]+)<", lambda m: ">" + (m.group(1) if not m.group(1).strip() else _word(m.group(1).strip())) + "<", markup)


def _wordless_css(decls: str) -> str:
    return re.sub(r"""(content:[^;]*?)(["'])((?:(?!\2).)*)\2""", lambda m: m.group(1) + m.group(2) + _word(m.group(3)) + m.group(2), decls)


def _head(s: str) -> str:
    """An at-rule's head: @media (max-width:600px), however it's spaced."""
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s*\)", ")", re.sub(r"\(\s*", "(", re.sub(r"\s*:\s*", ":", s)))


def _selector(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s*([,>+~])\s*", r"\1", s)


def css_looks(text: str, where: str = "") -> dict:
    """Each rule of a stylesheet: ``css <at-rules> | <selector>`` -> its declarations (" ‖ " between a selector's
    several rules). A @keyframes or @font-face is one look: its whole body."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    found: dict[str, list[str]] = {}
    i, n, ctx = 0, len(text), []

    def add(key, body):
        found.setdefault(key, []).append(body)

    while i < n:
        j = text.find("{", i)
        close = text.find("}", i)
        if close != -1 and (j == -1 or close < j):     # the end of an at-rule's block
            if ctx:
                ctx.pop()
            i = close + 1
            continue
        if j == -1:
            break
        head = text[i:j].strip()
        if head.startswith("@media") or head.startswith("@supports") or head.startswith("@layer"):
            ctx.append(_head(head))
            i = j + 1
            continue
        depth, k = 1, j + 1
        while depth and k < n:
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            k += 1
        body = text[j + 1:k - 1]
        pre = " ".join(([where] if where else []) + ctx)
        if head.startswith("@"):
            add(" ".join(x for x in (pre, _head(head)) if x), _squash(body))
        else:
            add((pre + " | " if pre else "") + _selector(head), _squash(body))
        i = k
    return {"css " + k: " ‖ ".join(v) for k, v in found.items()}


# ------------------------------------------------------------------ scripts
def js_texts(src: str) -> list[str]:
    """What a script writes: each string, or strings joined with +, what's between them shown as …."""
    toks = _i18n.js_tokens(src)
    out, k, n = [], 0, len(toks)
    while k < n:
        if toks[k][0] != "str":
            k += 1
            continue
        parts = [toks[k][1]]
        k += 1
        while k < n and toks[k][0] == "p" and toks[k][1] == "+" and not (k + 1 < n and toks[k + 1][1] == "="):
            k += 1
            if k < n and toks[k][0] == "str":
                parts.append(toks[k][1])
                k += 1
                continue
            depth = 0
            while k < n:                       # an expression: to the next + (or its end) outside brackets
                kind, v = toks[k][0], toks[k][1]
                if kind == "p" and v in "([{":
                    depth += 1
                elif kind == "p" and v in ")]}":
                    if not depth:
                        break
                    depth -= 1
                elif not depth and kind == "p" and v in "+,;:?":
                    break
                k += 1
            parts.append(HOLE)
        text = re.sub(r"\$\{[^}]*\}", HOLE, "".join(parts))
        out.append(re.sub(HOLE + "+", HOLE, text))
    return out


def _inline(style: str) -> str:
    return "inline " + _squash(style)


def _svg(markup: str) -> str:
    return "svg " + _wordless(re.sub(r"\s+", " ", markup).strip())


def script_looks(src: str) -> dict:
    found = {}
    for text in js_texts(src):
        for m in re.finditer(r'\bstyle="([^"]*)"', text):
            found[_inline(m.group(1))] = ""
        if _SHAPES.search(text):
            found[_svg(text)] = ""
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)
    for m in re.finditer(r"\.style\.([A-Za-z]+)\s*=(?!=)", code):
        found["scripted " + m.group(1)] = ""
    for m in re.finditer(r"""\.style\.setProperty\(\s*["']([^"']+)""", code):
        found["scripted " + m.group(1)] = ""
    return found


def page_looks(html: str, name: str) -> dict:
    found = {}
    for m in re.finditer(r"<style\b[^>]*>(.*?)</style>", html, flags=re.S | re.I):
        found.update(css_looks(m.group(1), name))
    for m in re.finditer(r'\bstyle="([^"]*)"', html):
        found[_inline(m.group(1))] = ""
    for m in re.finditer(r"<svg\b.*?</svg>", html, flags=re.S):
        found[_svg(m.group(0))] = ""
    return found


def mark_looks(src: str, name: str) -> dict:
    """A decoration script's parts: each top-level function or var of its wrapper (two spaces in), as written."""
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = "\n".join(re.sub(r"(^|\s)//.*$", "", line).rstrip() for line in code.split("\n"))
    found, cur, buf = {}, "(start)", []
    for line in code.split("\n"):
        m = re.match(r"  (?:function\s+([\w$]+)|var\s+([\w$]+))", line)
        if m:
            if "".join(buf).strip():
                found[f"mark {name} {cur}"] = re.sub(r"\s+", " ", "\n".join(buf)).strip()
            cur, buf = m.group(1) or m.group(2), []
            while f"mark {name} {cur}" in found:
                cur += "'"
        buf.append(line)
    if "".join(buf).strip():
        found[f"mark {name} {cur}"] = re.sub(r"\s+", " ", "\n".join(buf)).strip()
    return found


def drawn_looks(path: str, name: str) -> dict:
    """What a decoration script draws: dev/decor-draws.js runs it in node on a fixed page (a computer's and a phone's,
    every decoration on and off); each drawing and style it puts there, by where, as its SHA-256."""
    node = shutil.which("node")
    if not node:
        raise RuntimeError(f"node isn't installed: it runs {name} (dev/decor-draws.js) for what it draws")
    run = subprocess.run([node, os.path.join(ROOT, "dev", "decor-draws.js"), path], capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise RuntimeError(f"dev/decor-draws.js couldn't run {name}: {run.stderr.strip()}")
    return {f"mark {name} draws | {where}": hashlib.sha256(what.encode()).hexdigest() for where, what in json.loads(run.stdout).items()}


# ------------------------------------------------------------------ themes and files
def theme_looks(root: str | None = None) -> dict:
    """The built-in themes: plexcache/themes.py's colours, fonts and decorations, and how its css() and script() are
    written. Another tree's themes.py is read in a Python of its own (it's a module of that tree's plexcache)."""
    root = os.path.abspath(root or ROOT)
    path = os.path.join(root, "plexcache", "themes.py")
    if root == ROOT:
        from plexcache import themes
        found = _theme_data(themes)
    else:
        code = ("import importlib.util, json, sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from plexcache import themes\n"   # (that tree's, before this tool's own imports could bring this one's)
                "spec = importlib.util.spec_from_file_location('style_tool', sys.argv[2])\n"
                "tool = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(tool)\n"
                "print(json.dumps(tool._theme_data(themes)))\n")
        run = subprocess.run([sys.executable, "-c", code, root, os.path.abspath(__file__)], capture_output=True, text=True, timeout=120)
        if run.returncode:
            raise RuntimeError(f"couldn't read the themes of {root}: {run.stderr.strip()}")
        found = json.loads(run.stdout)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    found.update(py_marks(src, "themes.py", ("css", "script")))
    return found


def _theme_data(themes) -> dict:
    found = {}
    for t in themes.BUILT_IN:
        for mode in ("light", "dark"):
            for k, v in t[mode]["colors"].items():
                found[f"theme {t['id']}/{mode} {k}"] = v
        for k, v in t["fonts"].items():
            found[f"theme {t['id']} font {k}"] = v
        for k, v in t["decor"].items():
            found[f"theme {t['id']} decor {k}"] = json.dumps(v)
    for k, v in themes._FONT_STACK.items():
        found[f"theme font {k}"] = v
    return found


def py_marks(src: str, name: str, functions) -> dict:
    """Python functions that draw a look, as written: comments and docstring left out, however it's spaced (the same
    on every Python: the source itself, not the parser's rewrite of it)."""
    lines = src.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    at = lambda row, col: starts[row - 1] + col   # noqa: E731
    cut = [(at(*t.start), at(*t.end)) for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.COMMENT]
    found = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            first = node.body[0]
            doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
            spans = cut + ([(at(first.lineno, first.col_offset), at(first.end_lineno or first.lineno, first.end_col_offset or 0))] if doc else [])
            lo, hi = at(node.lineno, node.col_offset), at(node.end_lineno or node.lineno, node.end_col_offset or 0)
            text, pos = [], lo
            for a, b in sorted(x for x in spans if lo <= x[0] < hi):
                text.append(src[pos:a])
                pos = max(pos, b)
            text.append(src[pos:hi])
            found[f"mark {name} {node.name}"] = re.sub(r"\s+", " ", "".join(text)).strip()
    return found


IMAGES = (".svg", ".woff2", ".woff", ".ttf", ".otf", ".png", ".jpg", ".gif", ".webp", ".ico")


def file_looks(root: str | None = None) -> dict:
    root = root or ROOT
    found = {}
    for base, _dirs, files in os.walk(os.path.join(root, "plexcache", "web")):
        if os.path.basename(base) in ("i18n",):
            continue
        for f in sorted(files):
            if f.endswith(IMAGES):
                path = os.path.join(base, f)
                with open(path, "rb") as fh:
                    found["file " + os.path.relpath(path, root).replace(os.sep, "/")] = hashlib.sha256(fh.read()).hexdigest()
    return found


def look_files(root: str | None = None) -> list[str]:
    """The files the looks are read from (relative to the tree): what a copy of the approved app has to keep."""
    root = root or ROOT
    out = ["plexcache/themes.py", "plexcache/web/app.css"] + [f"plexcache/web/{n}" for n in SCRIPTS + PAGES]
    out += [k[len("file "):] for k in file_looks(root)]
    return sorted(set(p for p in out if os.path.exists(os.path.join(root, p))))


def looks(root: str | None = None) -> dict:
    """Every look the app has now, by what it is (above) -> what it is."""
    web = os.path.join(root or ROOT, "plexcache", "web")

    def read(name):
        with open(os.path.join(web, name), encoding="utf-8") as f:
            return f.read()

    found = css_looks(read("app.css"))
    for name in SCRIPTS:
        found.update(drawn_looks(os.path.join(web, name), name) if name in DRAWS else script_looks(read(name)))
    for name in PAGES:
        found.update(page_looks(read(name), name))
    found.update(theme_looks(root))
    found.update(file_looks(root))
    return dict(sorted(found.items()))


# ------------------------------------------------------------------ the guide
def seal(items: dict) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_guide(path: str = GUIDE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compare(approved: dict, now: dict) -> dict:
    """What differs from the guide: {"new": {look: now}, "changed": {look: [approved, now]}, "gone": {look: approved}}."""
    return {
        "new": {k: v for k, v in now.items() if k not in approved},
        "changed": {k: [approved[k], v] for k, v in now.items() if k in approved and approved[k] != v},
        "gone": {k: v for k, v in approved.items() if k not in now},
    }


def fingerprint(diff: dict) -> str:
    """The sample's fingerprint: exactly these differences (a sample of other ones doesn't count)."""
    return hashlib.sha256(json.dumps(diff, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def problems(guide: dict | None = None, now: dict | None = None) -> list[str]:
    """Each look that differs from the guide, in words, and a guide that wasn't written by approve. [] when all agree."""
    guide = load_guide() if guide is None else guide
    out = []
    if guide.get("format") != FORMAT or seal(guide.get("looks", {})) != guide.get("seal"):
        out.append("dev/style-guide.json wasn't written by dev/style.py approve (its seal doesn't match its looks): "
                   "put it back as it was (git checkout dev/style-guide.json)")
    diff = compare(guide.get("looks", {}), looks() if now is None else now)
    out += [f"new: {_short(k)}" for k in diff["new"]]
    out += [f"changed: {_short(k)}" for k in diff["changed"]]
    out += [f"gone: {_short(k)}" for k in diff["gone"]]
    return out


def _short(s: str, n: int = 160) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def write_guide(items: dict, approval: dict, path: str = GUIDE, before: dict | None = None):
    """The guide with these looks, the approval added to its record. Only approve calls it."""
    old = before if before is not None else (load_guide(path) if os.path.exists(path) else {})
    doc = {
        "about": "The style guide: every look the app may wear, each approved by the maintainer from a sample "
                 "(dev/style.py; StyleGuideTests). Written by dev/style.py approve only: never edit it by hand.",
        "format": FORMAT,
        "approvals": old.get("approvals", []) + [approval],
        "seal": seal(items),
        "looks": dict(sorted(items.items())),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")


# ------------------------------------------------------------------ the sample
LOOKS = [("classic", "light"), ("classic", "dark"), ("dionysus", "light"), ("dionysus", "dark")]
FIXED_PAGES = ["health", "tools", "merge", "system"]   # where a mark, a colour or a font is seen
_STATE = re.compile(r"::?(hover|focus-visible|focus-within|focus|active|visited|before|after|placeholder|selection|marker|"
                    r"checked|disabled|first-letter|first-line|-?[a-z-]*scrollbar[a-z-]*)\b")


def finder(look: str, value) -> dict:
    """How the sample finds a look on the demo's pages: by selector, by style, on the usual pages, or not at all."""
    kind, _, rest = look.partition(" ")
    if kind == "css":
        ctx, _, sel = rest.rpartition(" | ") if " | " in rest else ("", "", rest)
        if sel.startswith("@") or ctx.startswith("@") and not sel:
            return {"how": "code"}
        m = re.search(r"max-width:(\d+)px", ctx)
        width = min(1280, int(m.group(1)) - 10) if m else 1280
        if "print" in ctx:
            return {"how": "code"}
        parts = [_STATE.sub("", p).strip() or "body" for p in sel.split(",")]
        return {"how": "selector", "selectors": parts, "width": width}
    if kind == "inline":
        pat = re.escape(rest).replace(re.escape(HOLE), ".*")
        return {"how": "style", "pattern": "^" + pat + ";?$"}
    if kind == "svg":
        m = re.search(r'<svg\b[^>]*\bclass="([\w -]+)"', rest)
        if m and HOLE not in m.group(1):
            return {"how": "selector", "selectors": ["svg." + ".".join(m.group(1).split())], "width": 1280}
        return {"how": "code"}
    if kind in ("mark", "theme"):
        return {"how": "pages"}
    if kind == "file" and rest.endswith(".svg"):
        return {"how": "image", "path": rest}
    if kind == "file":
        return {"how": "pages"}
    return {"how": "code"}   # scripted: its value is worked out as the page draws


def app_pages() -> list[str]:
    """Every page of the app (app.js's PAGES), where the sample looks for each look."""
    with open(os.path.join(WEB, "app.js"), encoding="utf-8") as f:
        m = re.search(r"var PAGES = \{(.*?)\};", f.read(), flags=re.S)
    return re.findall(r"([a-z]+):\s*page[A-Z]", m.group(1)) if m else FIXED_PAGES


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _node_path() -> str:
    try:
        top = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        top = ""
    return os.pathsep.join(x for x in (os.environ.get("NODE_PATH", ""), top) if x)


APPROVED = os.path.join(ROOT, "dev", "style-approved.zip")   # the approved app's look files (approve keeps them): the Before


def keep_approved(root: str = ROOT, dest: str = APPROVED):
    """A copy of the files the approved looks are read from, which approve keeps with the guide: the sample's Before.
    One zip (the same bytes for the same files), so no checker takes the copy for code of the app's own."""
    tmp = dest + ".new"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in look_files(root):
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(root, rel), "rb") as f:
                z.writestr(info, f.read())
    os.replace(tmp, dest)


def before_tree(src: str, tmp: str, approved: dict | None = None) -> str:
    """The app as it was approved: this tree's plexcache and tests, with the look files from ``src`` (a copy approve
    kept, or a folder given with --before) put over theirs, and any look file ``src`` doesn't have taken out. An image
    or font that's as the guide (``approved``) has it now stays as it is."""
    tree = os.path.join(tmp, "before")
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            z.extractall(os.path.join(tmp, "approved"))
        src = os.path.join(tmp, "approved")
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules")
    for d in ("plexcache", "tests"):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(tree, d), ignore=skip)
    for rel in look_files(tree):
        if not os.path.exists(os.path.join(src, rel)):
            os.remove(os.path.join(tree, rel))
    for base, _dirs, files in os.walk(src):
        for f in files:
            rel = os.path.relpath(os.path.join(base, f), src)
            if rel.split(os.sep)[0] == "plexcache" and "__pycache__" not in rel:
                os.makedirs(os.path.dirname(os.path.join(tree, rel)), exist_ok=True)
                shutil.copy2(os.path.join(base, f), os.path.join(tree, rel))
    for look, now in file_looks(ROOT).items():
        rel = look[len("file "):]
        if (approved or {}).get(look) == now:
            shutil.copy2(os.path.join(ROOT, rel), os.path.join(tree, rel))
    return tree


# What the app says, and the markup it says it in: the pages and the scripts that write them, and the translations.
# The Before takes these from today's app, so a reworded title isn't a difference (words aren't a look): "document"
# is the page itself, a path ending in / everything under it.
WORDS = ["document", "/static/app.js", "/static/login.js", "/static/i18n.js", "/static/i18n/"]


def judged_by(look: str, changed_files: list[str]) -> dict:
    """What of the Before a look seen on whole pages is judged against, so a change elsewhere on the page isn't taken
    for its own: a theme's look, or a shipped image, just its own file ({"only": [paths]}) over today's app; anything
    else (decor.js, which can't run with another day's styles) the whole Before but for the shipped images that changed
    and today's words ({"except": [paths]})."""
    if look.startswith(("mark themes.py", "theme ")):
        return {"only": ["/theme.css", "/theme.js"]}
    if look.startswith("file plexcache/web/"):
        return {"only": ["/static/" + look[len("file plexcache/web/"):]]}
    return {"except": ["/static/" + f[len("plexcache/web/"):] for f in changed_files] + WORDS}


def cropped_by(look: str) -> dict | None:
    """What of the Before a look photographed where it is is judged against: a stylesheet's rule, the approved styles
    over today's pages and words (so the crop shows only the style changing); a look written in a page or script (its
    markup, an inline style), the whole Before (None), since its words come with it."""
    return {"except": WORDS} if look.startswith("css ") else None


def stands_in(rel: str, tree: str) -> str | None:
    """For a new image, the image of the Before it's a variant of: the one whose name its own begins with, the longest
    (plexcache/web/theatre-light.svg -> plexcache/web/theatre.svg), of the same kind; None if there's none."""
    folder, name = os.path.split(rel)
    stem, ext = os.path.splitext(name)
    try:
        there = os.listdir(os.path.join(tree, folder))
    except OSError:
        return None
    best = max((f for f in there if f.endswith(ext) and f != name and stem.startswith(os.path.splitext(f)[0] + "-")),
               key=len, default=None)
    return f"{folder}/{best}" if best else None


def _demo(root: str):
    """The demo (tests/demo.py) of a tree, on a port of its own; (process, url) once it answers, or (process, None)."""
    port = _free_port()
    proc = subprocess.Popen([sys.executable, os.path.join(root, "tests", "demo.py"), str(port)], cwd=root,
                            env=dict(os.environ, DEMO_SHOWCASE="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, f"http://127.0.0.1:{port}"


def _answers(url: str) -> bool:
    for _ in range(120):
        try:
            urllib.request.urlopen(url + "/api/state", timeout=2)
            return True
        except OSError:
            time.sleep(0.5)
    return False


def sample(out: str = SAMPLE, before: str | None = None) -> int:
    """Each look that differs from the guide, photographed before (as approved) and after (now) on the demo's pages, in
    each theme and mode, side by side; the looks that come out the same are set aside. It's made in a folder of its own
    (out/making) and only replaces the last sample once it's done: one that fails leaves the last one as it was."""
    work = os.path.join(out, "making")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        rc = _sample(work, before, out)
        if rc == 0 and os.path.exists(os.path.join(work, "index.html")):
            for f in os.listdir(out):
                if f.endswith(".png") or f in ("index.html", "sample.json", "plan.json"):
                    os.remove(os.path.join(out, f))
            for f in os.listdir(work):
                os.replace(os.path.join(work, f), os.path.join(out, f))
        elif rc != 0 and os.path.exists(os.path.join(out, "index.html")):
            print(f"The last sample is still there: {os.path.relpath(os.path.join(out, 'index.html'), os.getcwd())}", file=sys.stderr)
        return rc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _sample(out: str, before: str | None, shown: str) -> int:
    approved = load_guide()["looks"] if os.path.exists(GUIDE) else {}
    diff = compare(approved, looks())
    count = sum(len(v) for v in diff.values())
    if not count:
        print("Every look is in the style guide: nothing to sample.")
        return 0
    src = before or (APPROVED if os.path.exists(APPROVED) else None)
    if before and not (os.path.isdir(before) or zipfile.is_zipfile(before)):
        print(f"There's no folder (or zip) {before} to take the Before from.", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        tree = before_tree(src, tmp, approved) if src else None
        off = compare(approved, looks(tree)) if tree else None   # (where the Before isn't quite as the guide has it)
        items = []
        images = sorted(k[len("file "):] for st in diff.values() for k in st if k.startswith("file plexcache/web/"))
        for state in ("new", "changed", "gone"):
            for look, v in diff[state].items():
                was, now = (None, v) if state == "new" else (v[0], v[1]) if state == "changed" else (v, None)
                find = finder(look, now if state != "gone" else was)
                if find["how"] == "image":
                    find["files"] = {"before": os.path.join(tree, find["path"]) if tree else None, "after": os.path.join(ROOT, find["path"])}
                    find["files"] = {k: p if p and os.path.exists(p) else None for k, p in find["files"].items()}
                    if tree and not find["files"]["before"]:
                        was_file = stands_in(find["path"], tree)
                        if was_file:   # a new image (theatre-light.svg): Before, the one it's a variant of (theatre.svg)
                            find["files"]["before"] = os.path.join(tree, was_file)
                            find["instead"] = was_file
                if find["how"] == "pages":
                    find["judge"] = judged_by(look, images)
                elif find["how"] in ("selector", "style"):
                    find["judge"] = cropped_by(look)
                items.append({"id": len(items), "state": state, "look": look, "was": was, "now": now, "find": find,
                              "side": "before" if state == "gone" and tree else "after"})
        demos = [_demo(ROOT)] + ([_demo(tree)] if tree else [])
        try:
            if not all(_answers(url) for _p, url in demos):
                print("The demo didn't start (tests/demo.py).", file=sys.stderr)
                return 1
            plan = {"looks": [{"theme": t, "mode": m} for t, m in LOOKS], "pages": app_pages(), "fixed": FIXED_PAGES,
                    "items": items, "after": demos[0][1], "before": demos[1][1] if tree else None,
                    "time": int(time.time() * 1000)}   # (both sides see the same moment: "3 min ago" reads alike)
            with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False)
            shots = _pictures(out)
            if shots is None:
                return 1
        finally:
            for proc, _url in demos:
                proc.terminate()
                proc.wait(10)
    fp = fingerprint(diff)
    counts = _page(out, items, shots, fp, off, bool(src), before or (os.path.relpath(APPROVED, ROOT) if src else None))
    files = {p for ps in shots.values() for pair in ps for p in (pair.get("before"), pair.get("after"), pair.get("diff")) if p}
    with open(os.path.join(out, "sample.json"), "w", encoding="utf-8") as f:
        json.dump({"fingerprint": fp, "made": datetime.datetime.now().isoformat(timespec="seconds"), "looks": count,
                   "seen": counts["seen"], "code": counts["code"], "same": counts["same"], "shots": sorted(files),
                   "items": [{k: it[k] for k in ("id", "state", "look", "was", "now")} for it in items]}, f, indent=1)
    os.remove(os.path.join(out, "plan.json"))
    where = os.path.relpath(os.path.join(shown, "index.html"), os.getcwd())
    print(f"The sample of {count} look{'s' if count != 1 else ''}: {where}")
    if src:
        print(f"  {counts['seen']} changed what's drawn, {counts['code']} aren't drawn on the demo pages (judge by the code), "
              f"{counts['same']} drew exactly the same before and after.")
    else:
        print("  No Before to compare with (dev/style-approved.zip is kept by approve; or give one: sample --before <folder>).")
    print("Open it in a browser. If they're right, the maintainer runs: python3 dev/style.py approve")
    return 0


def _pictures(out: str):
    """dev/style-sample.js's pictures, with the dev tools' progress bar: {item id: [pair, ...]}, or None."""
    node = subprocess.Popen(["node", os.path.join(ROOT, "dev", "style-sample.js"), out],
                            cwd=ROOT, env=dict(os.environ, NODE_PATH=_node_path()), stdout=subprocess.PIPE, text=True)
    assert node.stdout
    bar, shots = None, {}
    for line in node.stdout:
        msg = json.loads(line)
        if msg.get("total") is not None:   # (it says again once it knows which pages it needs in each look)
            if bar:
                bar.total = msg["total"]
            else:
                bar = Progress(msg["total"], unit="pages looked at")
        elif msg.get("page") and bar:
            bar.draw(msg["page"])
        elif msg.get("shots") is not None:
            shots = msg["shots"]
    code = node.wait()
    if bar:
        bar.clear()
    if code:
        print("Playwright couldn't take the pictures (see above). It needs: npm i -g playwright && npx playwright install chromium",
              file=sys.stderr)
        return None
    return shots


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def sort_looks(items: list, shots: dict, compared: bool) -> dict:
    """The looks by what the maintainer has to do with them: {"seen": [group], "code": [item], "same": [item]}.
    seen: drawn otherwise somewhere (or, with no Before, drawn at all), each group the looks that changed the same
    pictures, with those pictures; code: drawn on no demo page; same: drawn, and the same before and after everywhere."""
    groups, code, same = {}, [], []
    for it in items:
        pairs = shots.get(str(it["id"]), [])
        if not pairs:
            code.append(it)
            continue
        differ = [p for p in pairs if not (compared and p.get("same"))]
        if not differ:
            same.append(it)
            continue
        key = tuple((p["label"], p.get("before"), p.get("after")) for p in differ)
        g = groups.setdefault(key, {"pairs": differ, "items": [], "alike": []})
        g["items"].append(it)
        g["alike"] = sorted({p["label"] for p in pairs if compared and p.get("same")} | set(g["alike"]))
    return {"seen": one_spot(list(groups.values())), "code": code, "same": same}


def _overlap(a: dict | None, b: dict | None) -> float:
    """How much two crops of a page cover the same spot: their overlap over the smaller one (0 to 1)."""
    if not a or not b:
        return 0.0
    w = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
    h = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / max(1.0, min(a["width"] * a["height"], b["width"] * b["height"]))


def one_spot(groups: list, enough: float = 0.5) -> list:
    """The groups of looks photographed at the same spot (one change made of several rules: a vine's position, size and
    colour) on one card: two groups whose crops on every page they share cover mostly the same place. A card shows, on
    each page, the biggest of its crops there (it holds the others), and lists every look."""
    def spots(g):
        return {p["label"]: p.get("clip") for p in g["pairs"]}
    parent = list(range(len(groups)))

    def top(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            a, b = spots(groups[i]), spots(groups[j])
            shared = set(a) & set(b)
            if shared and all(_overlap(a[k], b[k]) >= enough for k in shared):
                parent[top(j)] = top(i)
    merged: dict[int, list] = {}
    for i in range(len(groups)):
        merged.setdefault(top(i), []).append(groups[i])
    out = []
    for gs in merged.values():
        if len(gs) == 1:
            out.append(gs[0])
            continue
        best: dict[str, dict] = {}
        for g in gs:
            for p in g["pairs"]:
                c = p.get("clip") or {}
                if p["label"] not in best or c.get("width", 0) * c.get("height", 0) > \
                        (best[p["label"]].get("clip") or {}).get("width", 0) * (best[p["label"]].get("clip") or {}).get("height", 0):
                    best[p["label"]] = p
        order = [p["label"] for g in gs for p in g["pairs"]]
        out.append({"pairs": [best[k] for k in dict.fromkeys(order)], "items": [it for g in gs for it in g["items"]],
                    "alike": sorted({a for g in gs for a in g["alike"]})})
    return out


_LOOK_OF = {"new": "New", "changed": "Changed", "gone": "Gone"}


def _ids(items) -> str:
    return ",".join(str(it["id"]) for it in items)


def _ok(items, text: str = "Approve") -> str:
    """The button that approves these looks: shown only when the page is served by dev/style.py review."""
    return f'<button type="button" class="ok" data-ids="{_ids(items)}" hidden>{_esc(text)}</button>'


_REVIEW_BAR = ('<div class="bar"><p class="offline">To approve on this page, run <code>python3 dev/style.py review</code> at a '
               'terminal: it opens this page with Approve buttons. An approved change leaves the page.</p>'
               '<p class="online" hidden><span class="tally"><span class="pill good"></span><span class="pill left"></span></span>'
               '<button type="button" class="ok all">Approve all</button> <span class="msg" role="status"></span></p>'
               '<div class="alldone" hidden role="status"><b></b><span>Commit <code>dev/style-guide.json</code> and '
               '<code>dev/style-approved.zip</code>.</span></div></div>')


def _swatch(v) -> str:
    return f'<span class="sw" style="background:{_esc(v)}"></span>' if v and re.fullmatch(r"#[0-9A-Fa-f]{3,8}", str(v)) else ""


def _code(it) -> str:
    """A look's code, Before and After side by side (a theme colour with its swatch)."""
    def side(name, v):
        text = "(not there)" if v is None else (v or "(as named)")
        sw = _swatch(v) if it["look"].startswith("theme ") else ""
        return f'<div><div class="side{" after" if name == "After" else ""}">{name}</div>{sw}<pre>{_esc(text)}</pre></div>'
    return f'<div class="code">{side("Before", it["was"])}{side("After", it["now"])}</div>'


def _name(it) -> str:
    return f'<span class="tag {it["state"]}">{_LOOK_OF[it["state"]]}</span> <code>{_esc(_short(it["look"], 200))}</code>'


def _page(out: str, items: list, shots: dict, fp: str, off: dict | None, compared: bool, source: str | None) -> dict:
    """The review page: what changed on screen, Before and After side by side; then what can only be judged by its
    code; then, folded away, what drew the same."""
    s = sort_looks(items, shots, compared)
    seen_looks = sum(len(g["items"]) for g in s["seen"])
    parts = []
    for n, g in enumerate(s["seen"], 1):
        title = (_name(g["items"][0]) if len(g["items"]) == 1 else
                 f'{len(g["items"])} looks seen across whole pages (decorations, theme colours, fonts)'
                 if all(it["find"]["how"] == "pages" for it in g["items"]) else
                 f'{len(g["items"])} looks that change the same pictures')
        rows = []
        for p in g["pairs"]:
            def img(f, what, diff=None):
                if not f:
                    return f'<div class="nopic">{"Not in the app before" if what == "Before" else "Gone from the app now"}</div>'
                d = f' data-diff="{_esc(diff)}" data-plain="{_esc(f)}"' if diff else ""
                return f'<a href="{_esc(f)}" target="_blank"><img src="{_esc(f)}" alt="{what}" loading="lazy"{d}></a>'
            both = p.get("before") and p.get("after")
            tools = (('<button type="button" class="hl" aria-pressed="false">Highlight the difference</button>' if p.get("diff") else "") +
                     ('<button type="button" class="sv" aria-pressed="false">Slider</button>' if both else ""))
            slider = (f'<div class="slider" hidden><div class="sl" tabindex="0" role="slider" aria-label="Before ← → After (drag, or the arrow keys)" '
                      f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="50"><img src="{_esc(p["after"])}" alt="After">'
                      # (not lazy: hidden until Slider is pressed, a lazy one never loads, and these are the side by side's own files)
                      f'<img class="slb" src="{_esc(p["before"])}" alt="Before" style="clip-path:inset(0 50% 0 0)">'
                      '<span class="sll side">Before</span><span class="slr side after">After</span><span class="slx" style="left:50%"></span></div></div>') if both else ""
            rows.append(f'<div class="pair"><div class="where">{_esc(p["label"])}{tools}</div>'
                        f'<div class="ba"><figure><figcaption class="side">Before</figcaption>{img(p.get("before"), "Before")}</figure>'
                        f'<figure><figcaption class="side after">After</figcaption>{img(p.get("after"), "After", p.get("diff"))}</figure></div>'
                        f'{slider}</div>')
        alike = f'<p class="alike">The same before and after in: {_esc(", ".join(g["alike"]))}.</p>' if g["alike"] else ""
        looks_list = "".join(f'<details class="look"><summary>{_name(it)}</summary>{_code(it)}</details>' for it in g["items"])
        parts.append(f'<section class="change" data-ids="{_ids(g["items"])}"><h3><span class="num">{n}</span>{title}{_ok(g["items"])}</h3>{"".join(rows)}{alike}'
                     f'<details class="why"><summary>What changed in the code ({len(g["items"])})</summary>{looks_list}</details></section>')
    code = "".join(f'<section class="change" data-ids="{it["id"]}"><h3>{_name(it)}{_ok([it])}</h3>{_code(it)}</section>' for it in s["code"])
    same = "".join(f'<details class="look"><summary>{_name(it)}</summary>{_code(it)}</details>' for it in s["same"])
    note = ""
    if not compared:
        note = ('<p class="warn">There\'s no Before to compare with, so this shows each look as it is now. approve keeps '
                'a copy of the approved app in dev/style-approved.zip for next time; for now one can be given with '
                '<code>python3 dev/style.py sample --before &lt;folder&gt;</code>.</p>')
    elif off and any(off.values()):
        k = sum(len(v) for v in off.values())
        listing = "".join(f"<li>{_esc(st)}: <code>{_esc(_short(x, 160))}</code></li>" for st in off for x in off[st])
        drawn = sum(1 for st in off for x in off[st] if x.startswith("mark decor.js "))
        if drawn == k and any(x.startswith("mark decor.js draws | ") for x in off["new"]):
            why = (f"The Before matches the style guide in every look but decor.js's {k}: the guide still has decor.js "
                   "by its code, and it is now judged by what it draws, so those two can't be lined up until this approval.")
        else:
            why = (f"The Before ({_esc(source)}) differs from the style guide in {k} look{'s' if k != 1 else ''}, so those "
                   "may not show exactly as approved.")
        note = f'<details class="warn"><summary>{why}</summary><ul>{listing}</ul></details>'
    total = len(items)
    n_pics = sum(len(g["pairs"]) for g in s["seen"])
    pics = f" ({n_pics} picture{'s' if n_pics != 1 else ''} to compare)" if n_pics else ""
    summary = (f'<div class="sum"><div><b>{seen_looks}</b> look{"s" if seen_looks != 1 else ""} changed what you see'
               f'{pics}</div>'
               f'<div><b>{len(s["code"])}</b> aren\'t drawn on the demo pages: judge by the code</div>'
               f'<div><b>{len(s["same"])}</b> drew exactly the same before and after</div></div>') if compared else ""
    html = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Style review</title><style>" + _CSS + "</style></head><body><main>"
            "<h1>Style review</h1>"
            f"<p class=\"lead\">{total} look{'s' if total != 1 else ''} differ from the style guide. Each change is shown "
            "<b>Before</b> (as approved) and <b>After</b> (now), side by side, in Classic and Dionysus, light and dark, "
            "only where it looks different.</p>" + summary + note + _REVIEW_BAR +
            (f"<div class=\"part\"><h2>Changed on screen</h2>{''.join(parts)}</div>" if parts else
             ("<h2>Changed on screen</h2><p class=\"none\">Nothing on the demo pages looks different.</p>" if compared else "")) +
            (f"<div class=\"part\"><h2>Not drawn on the demo pages</h2><p class=\"none\">These only show in a state the demo doesn't open in "
             f"(a hover, a print, an animation). Before and After are their code.</p>{code}</div>" if code else "") +
            (f"<div class=\"part\"><h2>Drew the same</h2><div class=\"change bare\" data-ids=\"{_ids(s['same'])}\">{_ok(s['same'], 'Approve these ' + str(len(s['same'])))}<details class=\"same\"><summary>{len(s['same'])} look{'s' if len(s['same']) != 1 else ''} "
             "whose code changed but which drew exactly the same, pixel for pixel, everywhere they're seen. Nothing to "
             f"look at; approving includes them.</summary>{same}</details></div></div>" if same else "") +
            "<p class=\"foot\">If it's all right, the maintainer runs <code>python3 dev/style.py approve</code> at a terminal. "
            f"Fingerprint {fp}</p></main><script>" + _JS + "</script></body></html>")
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return {"seen": seen_looks, "code": len(s["code"]), "same": len(s["same"])}


_DARK = ("--bg:#161917;--card:#1f2321;--ink:#e6e9e7;--mute:#a3aca6;--line:#343a37;--code:#272c29;--before:#f3c58e;"
         "--before-bg:#4a3213;--after:#9fdcb8;--after-bg:#173b28;--gone:#f2aaa3;--gone-bg:#4a1e1a;color-scheme:dark")
_CSS = (":root{--bg:#f4f4f1;--card:#fff;--ink:#1b1f1d;--mute:#5a625d;--line:#dcdcd6;--code:#f6f7f5;--before:#7f4205;"
        "--before-bg:#fbe8cf;--after:#155c37;--after-bg:#ddefe3;--gone:#8a1c14;--gone-bg:#f8d9d6}"
        "@media (prefers-color-scheme:dark){:root:not([data-theme=light]){" + _DARK + "}}:root[data-theme=dark]{" + _DARK + "}"
        "*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}"
        "main{max-width:1500px;margin:0 auto;padding:20px 16px 40px}h1{margin:0 0 6px;font-size:26px}"
        "h2{font-size:19px;margin:28px 0 10px}.lead{margin:0 0 14px;max-width:80ch}"
        ".sum{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 14px}.sum div{background:var(--card);border:1px solid var(--line);"
        "border-radius:10px;padding:8px 14px}.sum b{font-size:18px;margin-right:4px}"
        ".change{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 16px}"
        "h3{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;word-break:break-word}"
        ".num{display:inline-grid;place-items:center;min-width:26px;height:26px;border-radius:99px;background:var(--ink);"
        "color:var(--card);font-size:13px}"
        ".pair{margin:0 0 14px}.where{font-weight:600;font-size:13px;color:var(--mute);margin:0 0 6px;display:flex;gap:10px;"
        "align-items:center;flex-wrap:wrap}.ba{display:grid;grid-template-columns:1fr 1fr;gap:12px}"
        "@media (max-width:760px){.ba{grid-template-columns:1fr}}figure{margin:0;min-width:0}"
        "figure img{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:6px;background:#fff}"
        ".side{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;"
        "padding:2px 10px;border-radius:99px;margin:0 0 5px;background:var(--before-bg);color:var(--before)}"
        ".side.after{background:var(--after-bg);color:var(--after)}"
        ".nopic{border:1px dashed var(--line);border-radius:6px;padding:24px;color:var(--mute);text-align:center;font-style:italic}"
        ".hl{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--code);color:var(--ink);border-radius:99px;"
        "padding:2px 10px;cursor:pointer}.hl[aria-pressed=true]{background:var(--ink);color:var(--card)}"
        ".hl,.sv{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--code);color:var(--ink);border-radius:99px;"
        "padding:2px 10px;cursor:pointer}.sv[aria-pressed=true]{background:var(--ink);color:var(--card)}"
        ".sl{position:relative;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:#fff;cursor:ew-resize;"
        "touch-action:pan-y;user-select:none;-webkit-user-select:none}"
        ".sl img{display:block;width:100%;height:auto;pointer-events:none}.sl .slb{position:absolute;inset:0}"
        ".slx{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:var(--ink);box-shadow:0 0 0 1px var(--card);pointer-events:none}"
        ".sll,.slr{position:absolute;top:8px;margin:0;pointer-events:none}.sll{left:8px}.slr{right:8px}"
        ".stalenote{background:var(--before-bg);color:var(--ink);border-radius:8px;padding:8px 12px;margin:0 0 10px}"
        ".ok{font:inherit;font-size:13px;font-weight:600;border:0;border-radius:99px;padding:4px 14px;cursor:pointer;"
        "background:var(--after);color:var(--card);margin-left:auto}.ok:disabled{opacity:.5}.ok.all{margin:0}"
        ".tally{display:inline-flex;gap:6px;margin-right:10px;vertical-align:middle}"
        ".pill{font-size:13px;font-weight:700;border-radius:99px;padding:4px 12px;color:var(--card)}"
        ".pill.good{background:var(--after)}.pill.left{background:var(--before)}"
        ".alldone{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;background:var(--after);color:var(--card);"
        "border-radius:12px;padding:16px 20px;font-size:16px}.alldone b{font-size:20px}.alldone code{color:inherit}"
        ".bar{margin:0 0 14px}.bar p{margin:0}.offline{color:var(--mute);font-size:13px}.msg{color:var(--mute);margin-left:10px}"
        ".bare{background:none;border:0;padding:0}.bare>.ok{float:right}"
        ".alike{color:var(--mute);font-size:13px;margin:0 0 8px}"
        "details{margin:6px 0}summary{cursor:pointer}.why>summary,.same>summary{color:var(--mute)}"
        ".look{margin:6px 0 6px 14px}.look>summary{word-break:break-word}"
        ".code{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:6px 0}@media (max-width:760px){.code{grid-template-columns:1fr}}"
        ".code>div{min-width:0}pre{white-space:pre-wrap;word-break:break-all;background:var(--code);padding:8px 10px;"
        "border-radius:6px;font-size:12.5px;margin:0}"
        ".tag{font-size:11px;font-weight:700;text-transform:uppercase;padding:2px 8px;border-radius:99px;background:var(--after-bg);"
        "color:var(--after)}.tag.changed{background:var(--before-bg);color:var(--before)}.tag.gone{background:var(--gone-bg);color:var(--gone)}"
        "code{font-size:13px;word-break:break-word}.sw{display:inline-block;width:28px;height:28px;border-radius:6px;"
        "border:1px solid var(--mute);vertical-align:middle;margin:0 8px 5px}"
        ".none{color:var(--mute)}.warn{background:var(--before-bg);color:var(--ink);border-radius:10px;padding:10px 14px}"
        ".foot{color:var(--mute);font-size:13px;margin-top:28px;word-break:break-all}")

_JS = ("var R=window.REVIEW;if(R){document.querySelectorAll('.ok,.online').forEach(function(x){x.hidden=false});"
       "document.querySelector('.offline').hidden=true;}"
       # (the tally: green, the looks approved; orange, the ones left; and once none are left, all of it says so in green)
       "function idsOf(cs){var s={};[].forEach.call(cs,function(c){c.dataset.ids.split(',').filter(Boolean).forEach(function(i){s[i]=1})});return s;}"
       "var ALL=Object.keys(idsOf(document.querySelectorAll('.change[data-ids]'))).length;"
       "function left(){if(!R)return;var n=Object.keys(idsOf(document.querySelectorAll('.change[data-ids]'))).length;"
       "document.querySelector('.pill.good').textContent=(ALL-n)+' approved';"
       "var l=document.querySelector('.pill.left');l.textContent=n+' remaining';l.hidden=!n;"
       "document.querySelectorAll('.part').forEach(function(p){p.hidden=!p.querySelector('.change[data-ids]')});"
       "if(!n){document.querySelector('.alldone b').textContent='All '+ALL+' looks approved.';"
       "document.querySelector('.alldone').hidden=false;"
       "document.querySelectorAll('.online,.lead,.sum,.warn,.foot').forEach(function(x){x.hidden=true});}}"
       "left();"
       "function gone(done){document.querySelectorAll('.change[data-ids]').forEach(function(c){"
       "if(c.dataset.ids.split(',').filter(Boolean).every(function(i){return done[i]}))c.remove();});}"
       "if(R&&R.done&&R.done.length){var was={};R.done.forEach(function(i){was[i]=1});gone(was);left();}"
       "document.addEventListener('click',function(e){var a=e.target.closest('.ok');if(!a||!R)return;"
       "var ids=a.classList.contains('all')?[].concat.apply([],[].map.call(document.querySelectorAll('.change[data-ids]'),"
       "function(c){return c.dataset.ids.split(',')})):a.dataset.ids.split(',');"
       "ids=ids.filter(Boolean).map(Number);a.disabled=true;var msg=document.querySelector('.msg');msg.textContent='Approving…';"
       "fetch('/approve',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':R.token},"
       "body:JSON.stringify({ids:ids})}).then(function(r){return r.json()}).then(function(d){"
       "var done={};d.approved.forEach(function(i){done[i]=1});"
       "gone(done);"
       "var bad=Object.keys(d.refused||{});"
       # (a change whose code moved on since the sample: said on it, its Approve off, until a new sample)
       "document.querySelectorAll('.change[data-ids]').forEach(function(c){"
       "if(c.dataset.ids.split(',').some(function(i){return d.refused&&d.refused[i]})){"
       "c.classList.add('stale');var k=c.querySelector('.ok');if(k){k.disabled=true;k.textContent='Changed since this sample';}"
       "if(!c.querySelector('.stalenote')){var n=document.createElement('p');n.className='stalenote';"
       "n.textContent='Its code has changed since this sample was made, so what you see here is not what it looks like now. '"
       "+'Run python3 dev/style.py sample to see it as it is, then review it again.';c.insertBefore(n,c.children[1]||null);}}});"
       "var bad=Object.keys(d.refused||{});msg.textContent=d.approved.length+' approved'+(bad.length?'; '+bad.length+"
       "' not, because their code changed since this sample (run python3 dev/style.py sample again)':'')+'.';"
       "if(!a.closest('.stale'))a.disabled=false;left();}).catch(function(){msg.textContent='The review server didn\\'t answer: is python3 dev/style.py review still running?';"
       "a.disabled=false;});});"
       # (a change's buttons act on every theme of it at once: all its pairs)
       "function pairs(x){var c=x.closest('.change');return c?[].slice.call(c.querySelectorAll('.pair')):[x.closest('.pair')];}"
       "document.addEventListener('click',function(e){var b=e.target.closest('.hl,.sv');if(!b)return;"
       "var sv=b.classList.contains('sv'),on=b.getAttribute('aria-pressed')!=='true';"
       "pairs(b).forEach(function(pair){var k=pair.querySelector(sv?'.sv':'.hl');if(k)k.setAttribute('aria-pressed',on?'true':'false');"
       "if(sv){var s=pair.querySelector('.slider');if(s){pair.querySelector('.ba').hidden=on;s.hidden=!on;}return;}"
       "var img=pair.querySelector('img[data-diff]');if(!img)return;"
       "img.src=on?img.dataset.diff:img.dataset.plain;img.parentNode.href=img.src;});});"
       "function slide(box,v){pairs(box).forEach(function(pair){var b=pair.querySelector('.slider');if(!b)return;"
       "var sl=b.querySelector('.sl');sl.setAttribute('aria-valuenow',Math.round(v));"
       "sl.querySelector('.slb').style.clipPath='inset(0 '+(100-v)+'% 0 0)';sl.querySelector('.slx').style.left=v+'%';});}"
       "document.addEventListener('keydown',function(e){var sl=e.target.closest&&e.target.closest('.sl');if(!sl)return;"
       "var d={ArrowLeft:-5,ArrowRight:5,Home:-100,End:100}[e.key];if(d===undefined)return;e.preventDefault();"
       "slide(sl.parentNode,Math.max(0,Math.min(100,Number(sl.getAttribute('aria-valuenow'))+d)));});"
       # (a click or a drag on the picture moves the line there too)
       "function at(sl,e){var b=sl.getBoundingClientRect();slide(sl.parentNode,Math.max(0,Math.min(100,(e.clientX-b.left)/b.width*100)));}"
       "document.addEventListener('pointerdown',function(e){var sl=e.target.closest('.sl');if(!sl||e.button)return;"
       "e.preventDefault();sl.setPointerCapture(e.pointerId);sl.dataset.drag='1';at(sl,e);});"
       "document.addEventListener('pointermove',function(e){var sl=e.target.closest('.sl');if(sl&&sl.dataset.drag)at(sl,e);});"
       "['pointerup','pointercancel'].forEach(function(n){document.addEventListener(n,function(e){var sl=e.target.closest('.sl');"
       "if(sl)delete sl.dataset.drag;});});")


# ------------------------------------------------------------------ approving
def approve(out: str = SAMPLE, ask=input, terminal: bool | None = None, guide_path: str = GUIDE, now: dict | None = None,
            keep=None) -> int:
    """The sample's looks into the guide: only at a terminal, only for a sample of exactly what differs now, only when
    the maintainer types approve. With the guide it keeps a copy of the approved app's look files (dev/style-approved.zip),
    the next sample's Before."""
    terminal = (sys.stdin.isatty() and sys.stdout.isatty()) if terminal is None else terminal
    if not terminal:
        print("approve asks the maintainer at a terminal, and won't run without one.", file=sys.stderr)
        return 2
    before = load_guide(guide_path) if os.path.exists(guide_path) else {}
    current = looks() if now is None else now
    diff = compare(before.get("looks", {}), current)
    count = sum(len(v) for v in diff.values())
    if not count:
        print("Every look is in the style guide: nothing to approve.")
        return 0
    try:
        with open(os.path.join(out, "sample.json"), encoding="utf-8") as f:
            made = json.load(f)
    except (OSError, ValueError):
        made = {}
    fp = fingerprint(diff)
    if made.get("fingerprint") != fp or not os.path.exists(os.path.join(out, "index.html")) or \
            any(not os.path.exists(os.path.join(out, p)) for p in made.get("shots", [])):
        print("There's no sample of the looks that differ now (or the code changed since it was made): run "
              "python3 dev/style.py sample, look at it, then approve.", file=sys.stderr)
        return 1
    for state in ("new", "changed", "gone"):
        for k in diff[state]:
            print(f"  {state}: {_short(k, 110)}")
    print(f"The sample: {os.path.join(out, 'index.html')}")
    if ask(f"Type approve to add these {count} look{'s' if count != 1 else ''} to the style guide: ").strip() != "approve":
        print("Nothing approved.")
        return 1
    write_guide(current, {"date": datetime.date.today().isoformat(), "sample": fp, "looks": count}, guide_path, before)
    if keep is None and guide_path == GUIDE:
        keep = keep_approved
    if keep:
        keep()
    print(f"Approved: dev/style-guide.json has {len(current)} looks. Commit it, and dev/style-approved.zip, with the change.")
    return 0


def approve_looks(items: list, ids, now: dict, guide_path: str = GUIDE, keep=None) -> tuple[list, dict]:
    """Some of a sample's looks into the guide (the review page's Approve buttons): each only if it still differs
    from the guide exactly as the sample showed it. (approved ids, {refused id: why})."""
    before = load_guide(guide_path) if os.path.exists(guide_path) else {}
    looks_ = dict(before.get("looks", {}))
    diff = compare(looks_, now)
    by_id = {it["id"]: it for it in items}
    done, refused, already = [], {}, []
    for i in ids:
        it = by_id.get(i)
        if it is None:
            refused[i] = "not in this sample"
            continue
        k, st = it["look"], it["state"]
        if (k not in looks_ if st == "gone" else looks_.get(k) == it["now"]) and now.get(k) == it["now"]:
            already.append(i)   # (approved already, on another change that shows it too: done)
            continue
        shown = {"new": it["now"], "changed": [it["was"], it["now"]], "gone": it["was"]}[st]
        if k not in diff[st] or diff[st][k] != shown:
            refused[i] = "its code changed since the sample"
            continue
        if st == "gone":
            looks_.pop(k, None)
        else:
            looks_[k] = it["now"]
        done.append(i)
    if done:
        fp = fingerprint({st: {it["look"]: diff[it["state"]][it["look"]] for it in (by_id[i] for i in done) if it["state"] == st}
                          for st in ("new", "changed", "gone")})
        write_guide(looks_, {"date": datetime.date.today().isoformat(), "sample": fp, "looks": len(done), "on": "review page"},
                    guide_path, before)
        if not any(compare(looks_, now).values()):   # all of it approved: the app as it is now is the next Before
            if keep is None and guide_path == GUIDE:
                keep = keep_approved
            if keep:
                keep()
    return sorted(done + already), refused


IDLE = 3600   # seconds the review page waits unused before it closes (opened from tnsync's board, there's no Ctrl-C)


def review(out: str = SAMPLE, terminal: bool | None = None, open_browser: bool = True, guide_path: str = GUIDE,
           ready=None, log: str | None = None, idle: float = IDLE) -> int:
    """The sample's page with Approve buttons, served to this computer only (127.0.0.1) while it runs: Approve all, or a
    change at a time, and what's approved leaves the page. Like approve: only at a terminal, and only for looks as the
    sample showed them. It closes once everything is approved, or after an hour unused. With log (tnsync's board, the
    r key: the board is on the terminal), what it says goes to that file instead."""
    import http.server
    import secrets
    import threading
    import webbrowser
    terminal = (sys.stdin.isatty() and sys.stdout.isatty()) if terminal is None else terminal
    if not terminal:
        print("review is the maintainer's, at a terminal, and won't run without one.", file=sys.stderr)
        return 2
    try:
        with open(os.path.join(out, "sample.json"), encoding="utf-8") as f:
            made = json.load(f)
        with open(os.path.join(out, "index.html"), encoding="utf-8") as f:
            page = f.read()
    except (OSError, ValueError):
        made, page = {}, ""
    if not page or "items" not in made:
        print("There's no sample to review: run python3 dev/style.py sample first.", file=sys.stderr)
        return 1
    saved = sys.stdout, sys.stderr
    if log is not None:   # (the terminal is the board's: say it to the file)
        sys.stdout = sys.stderr = open(log, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 — (until it ends)
    token = secrets.token_urlsafe(24)
    lock = threading.Lock()
    used = [time.time()]
    ended = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):   # noqa: A002  (quiet: the terminal says what's approved)
            pass

        def _host_ok(self):   # (not a page of another site's name pointed at this computer)
            return self.headers.get("Host") == f"127.0.0.1:{port}"

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            used[0] = time.time()
            if not self._host_ok():
                return self._send(403, b"", "text/plain")
            name = self.path.split("?")[0].lstrip("/") or "index.html"
            if name == "index.html":
                # the token the Approve buttons send, set before the page's own script reads it
                # and the changes approved already (since the sample, here or before): left off the page
                guide = load_guide(guide_path)["looks"]
                done = [it["id"] for it in made["items"] if guide.get(it["look"]) == it["now"]]
                body = page.replace("<script>var R=window.REVIEW;",
                                    f"<script>window.REVIEW={{token:{json.dumps(token)},done:{json.dumps(done)}}};var R=window.REVIEW;", 1)
                return self._send(200, body.encode(), "text/html; charset=utf-8")
            path = os.path.join(out, os.path.basename(name))
            if name != os.path.basename(name) or not name.endswith(".png") or not os.path.isfile(path):
                return self._send(404, b"", "text/plain")
            with open(path, "rb") as f:
                return self._send(200, f.read(), "image/png")

        def do_POST(self):
            used[0] = time.time()
            if not self._host_ok() or self.path != "/approve" or not secrets.compare_digest(self.headers.get("X-Review-Token", ""), token):
                return self._send(403, b"", "text/plain")
            try:
                ids = [int(i) for i in json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))["ids"]]
            except (ValueError, KeyError, TypeError):
                return self._send(400, b"", "text/plain")
            with lock:
                now = looks()
                done, refused = approve_looks(made["items"], ids, now, guide_path)
                left = any(compare(load_guide(guide_path)["looks"], now).values())
            for i in done:
                it = next(x for x in made["items"] if x["id"] == i)
                print(f"  approved {it['state']}: {_short(it['look'], 110)}")
            self._send(200, json.dumps({"approved": done, "refused": refused}).encode(), "application/json")
            if not left:   # (everything approved: nothing more to do here)
                ended.set()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    url = f"http://127.0.0.1:{port}/"
    print(f"The review page: {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    def watch():   # (closes it once everything is approved, or after an hour unused)
        while not ended.wait(min(5.0, idle)):
            if time.time() - used[0] > idle:
                print(f"No one has used the page for {round(idle / 60)} minutes, so it's closed: python3 dev/style.py review opens it again.")
                break
        server.shutdown()
    threading.Thread(target=watch, daemon=True).start()
    if ready:
        ready(server, url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ended.set()
        server.server_close()
    left = sum(len(v) for v in compare(load_guide(guide_path)["looks"], looks()).values())
    print("Every look is in the style guide. Commit dev/style-guide.json and dev/style-approved.zip." if not left else
          f"{left} look{'s' if left != 1 else ''} still to approve. Commit dev/style-guide.json with what you approved.")
    if log is not None:
        sys.stdout.close()
        sys.stdout, sys.stderr = saved
    return 0


def review_line(n: int, out: str = SAMPLE, now: dict | None = None) -> str:
    """tnsync's line for looks waiting: how many, and the command that opens the page to approve them (in another
    terminal, while the other checks run), or that the page must be made first: when a look differs in a way the
    sample didn't show (approving some of it since doesn't make it out of date)."""
    page = os.path.join(out, "index.html")
    try:
        with open(os.path.join(out, "sample.json"), encoding="utf-8") as f:
            made = json.load(f)
    except (OSError, ValueError):
        made = {}
    diff = compare(load_guide()["looks"] if os.path.exists(GUIDE) else {}, looks() if now is None else now)
    shown = {(it["state"], it["look"]): it for it in made.get("items", [])}

    def in_sample(st, k, v):
        it = shown.get((st, k))
        return it is not None and v == {"new": it["now"], "changed": [it["was"], it["now"]], "gone": it["was"]}[st]
    fresh = os.path.exists(page) and (made.get("fingerprint") == fingerprint(diff) or
                                      (bool(shown) and all(in_sample(st, k, v) for st in diff for k, v in diff[st].items())))
    return (f"{n} look{'s need' if n != 1 else ' needs'} your approval: python3 dev/style.py "
            + ("review" if fresh else "sample, then review"))


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "check":
        now = looks()
        found = problems(now=now)
        if feeding():   # (on tnsync's board: its bar, every look compared with the guide)
            bar = Progress(len(now), unit="looks compared", bar="style")
            bar.workers = 1   # (this process alone)
            bar.at(len(now), len(now), "looks · against the style guide")
        for p in found:
            print("  " + p)
        if found:
            print(f"{len(found)} look{'s' if len(found) != 1 else ''} not in the style guide. "
                  "python3 dev/style.py sample, then the maintainer approves.")
            said(review_line(len(found), now=now))
        else:
            print("Every look is in the style guide.")
            said(result(tally(len(now), "look", "approved")))
        return 1 if found else 0
    if cmd == "sample":
        rest = argv[2:]
        if rest and (rest[0] != "--before" or len(rest) != 2):
            print("python3 dev/style.py sample [--before <folder>]", file=sys.stderr)
            return 2
        return sample(before=os.path.abspath(rest[1]) if rest else None)
    if cmd == "approve":
        return approve()
    if cmd == "review":
        if argv[2:] not in ([], ["--board"]):
            print("python3 dev/style.py review [--board]", file=sys.stderr)
            return 2
        # --board: opened from tnsync's board (its r key), which is on the terminal: what it says goes to a file
        return review(log=os.path.join(SAMPLE, "review.log") if argv[2:] else None)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
