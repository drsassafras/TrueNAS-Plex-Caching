"""Themes: how the web UI looks.

A theme is its colours for a light look and for a dark one, two fonts and a few decorations, kept as one small JSON
object:

    {"dionysus_theme": 1, "id": "dionysus", "name": "Dionysus", "fonts": {"text": "Source Sans 3", "headings": "Cinzel"},
     "decor": {"spots": 5, "vines": true, "film": true, "grapes": true},
     "light": {"colors": {"bg": "#E7E8E2", ...}}, "dark": {"colors": {"bg": "#111513", ...}}}

Two ship with the app (BUILT_IN): Classic (the app's look before themes) and Dionysus. People can add their own (the
"themes" section of the settings, "custom"): a downloaded theme, edited and uploaded again. Which theme shows and in which
mode (light, dark, or auto: by the device's own setting) are settings, and a browser can keep a choice of its own for
itself (theme.js, kept in that browser): Classic on the phone, say, and Dionysus on the computer. Every value is checked
here before it reaches a page (validate()): colours are #hex only and fonts and decorations come from fixed lists, so a
theme file can't carry anything but a look.

The page gets every theme's colours as CSS (css(), served at /theme.css) and which one to show as a small script
(script(), /theme.js), loaded before the page draws so it never flashes the wrong colours.
"""

import json
import re

from .i18n import M

FORMAT = 1
MAX_CUSTOM = 20

# Every colour a theme sets, in the order a downloaded theme lists them, with what each colours (for the download's
# "about"). The page's CSS uses each as var(--<name>).
COLORS = {
    "bg": "the page behind the cards",
    "panel": "cards, fields and buttons",
    "ink": "text",
    "muted": "quieter text: hints, notes",
    "soft": "details under a row's title",
    "line": "card borders and lines",
    "line2": "lines between rows",
    "accent": "buttons, links and the open tab",
    "accent-ink": "text on an accent-coloured button",
    "side": "the menu",
    "side-ink": "the menu's text",
    "ok-bg": "a green badge's background", "ok": "a green badge's text",
    "warn-bg": "a yellow badge's background", "warn": "a yellow badge's text",
    "bad-bg": "a red badge's background", "bad": "a red badge's text",
    "lost": "what goes, beside what's kept (Merge Duplicates and the other side-by-side cards): its box and title",
    "info-bg": "a grey badge's background", "info": "a grey badge's text",
    "run-bg": "a running badge's background", "run": "a running badge's text",
    "guide-bg": "a guide box's background", "guide-line": "a guide box's border",
    "note-bg": "a note's background", "note-line": "a note's border", "note-ink": "a note's text",
    "code-bg": "boxes of code and paths",
    "c-movie": "charts: movies", "c-tv": "charts: TV", "c-audio": "charts: music and audiobooks",
    "c-res": "charts: what's left", "c-idx": "charts: indexes",
    "track": "the empty part of a bar or slider",
    "chart-a": "charts: the first line", "chart-b": "charts: the second line",
    # the decorations' colours (used when the theme's decor turns them on)
    "gold": "gold marks: the logo's screen, film symbols",
    "wine": "ripe grapes",
    "leaf": "leaves",
    "unripe": "unripe grapes",
    "film": "the film under a page's title",
    "film-mark": "the symbols on the film",
    "spots": "the spots round the app",
    "menu-spots": "the spots on the menu",
    "vine": "the vine round each card",
    "vine-leaf": "the leaves on a card's vine",
    "menu-vine": "the vine between the menu and the page",
}
# colours a theme once set, for what's no longer drawn: a theme file that has them still uploads (they're left out)
RETIRED = ("frame", "frame-leaf")   # the vine round the whole app, and its leaves
FONTS = ("IBM Plex Sans", "Source Sans 3", "Cinzel", "System")  # (Source Sans 3 and Cinzel ship with the app: web/fonts)
DECOR = {
    "spots": "the spots round the app and on the menu: how strong, 0 (none) to 20 per cent",
    "vines": "true: a vine with a leaf now and then round each card, and down the side of the menu",
    "film": "true: a reel of film under each page's title",
    "grapes": "true: the arrow between two sides is a bunch of grapes on its stem",
}

_CLASSIC_LIGHT = {
    "bg": "#F3F4F1", "panel": "#FFFFFF", "ink": "#1B1F1D", "muted": "#5A625D", "soft": "#4C544F", "line": "#DDE1DD", "line2": "#EDEFEC",
    "accent": "#0F5E57", "accent-ink": "#FFFFFF", "side": "#1B1F1D", "side-ink": "#C9CFCB",
    "ok-bg": "#DDEFE3", "ok": "#155C37", "warn-bg": "#FBE8CF", "warn": "#7F4205", "bad-bg": "#F8D9D6", "bad": "#8A1C14", "lost": "#8A1C14",
    "info-bg": "#E7EAE6", "info": "#4C544F", "run-bg": "#D8E9F4", "run": "#14476B",
    "guide-bg": "#F1F5F3", "guide-line": "#D6E2DD", "note-bg": "#FDF4E7", "note-line": "#F1D9B4", "note-ink": "#5E3404",
    "code-bg": "#F6F7F5", "c-movie": "#0F5E57", "c-tv": "#5FA79B", "c-audio": "#B9772A", "c-res": "#9AA39E", "c-idx": "#6E5BA8",
    "track": "#E7EAE6", "chart-a": "#008F7E", "chart-b": "#C2651A",
    "gold": "#E5A00D", "wine": "#8B3A62", "leaf": "#2E7D5B", "unripe": "#D3E4DA", "film": "#3A423E", "film-mark": "#F0AE1F",
    "spots": "#B07A00", "menu-spots": "#F0AE1F", "vine": "#DDE1DD", "vine-leaf": "#2E7D5B",
    "menu-vine": "#AAACA7",
}
_CLASSIC_DARK = {
    "bg": "#121614", "panel": "#1A1F1C", "ink": "#E6EAE7", "muted": "#9CA59F", "soft": "#B3BBB6", "line": "#2C332F", "line2": "#252B28",
    "accent": "#3FA596", "accent-ink": "#0B1311", "side": "#0C0F0E", "side-ink": "#C9CFCB",
    "ok-bg": "#16382A", "ok": "#8FD9AE", "warn-bg": "#3D2A12", "warn": "#F2C08A", "bad-bg": "#3E1B18", "bad": "#F2A49C", "lost": "#F2A49C",
    "info-bg": "#262C29", "info": "#B3BBB6", "run-bg": "#14304A", "run": "#9BC8EE",
    "guide-bg": "#18231F", "guide-line": "#2A3A34", "note-bg": "#2E2414", "note-line": "#4A3A20", "note-ink": "#F2D3A6",
    "code-bg": "#151A18", "c-movie": "#3FA596", "c-tv": "#8CC9BE", "c-audio": "#D9A15A", "c-res": "#6B746F", "c-idx": "#A797D8",
    "track": "#2A302D", "chart-a": "#159C8A", "chart-b": "#CF7A2A",
    "gold": "#F0AE1F", "wine": "#B5558A", "leaf": "#3FA676", "unripe": "#2B4136", "film": "#3A423E", "film-mark": "#F0AE1F",
    "spots": "#F0AE1F", "menu-spots": "#F0AE1F", "vine": "#2C332F", "vine-leaf": "#3FA676",
    "menu-vine": "#555957",
}
# Dionysus: the film's pale stone behind the cards, wine for the buttons and links, a vine round the cards and the app
_DIONYSUS_LIGHT = dict(_CLASSIC_LIGHT, **{
    "bg": "#E7E8E2", "line2": "#E9EAE5", "accent": "#8B3A62", "note-bg": "#FBF1DF", "note-line": "#E5C98F",
    "guide-bg": "#F3F4EF", "guide-line": "#D9DBD2", "code-bg": "#F5F5F1", "info-bg": "#E9EAE5",
    "c-movie": "#8B3A62", "c-tv": "#C98BAA", "chart-a": "#8B3A62", "chart-b": "#C2651A",
    "vine": "#97BEAD", "vine-leaf": "#2E7D5B", "menu-vine": "#AAACA7",
    "lost": "#6B726E",
})
_DIONYSUS_DARK = dict(_CLASSIC_DARK, **{
    "bg": "#111513", "muted": "#A3ABA6", "accent": "#B5558A", "accent-ink": "#FFFFFF",
    "note-bg": "#2A2214", "note-line": "#5A4622", "guide-bg": "#1A201D", "guide-line": "#2E3632",
    "c-movie": "#B5558A", "c-tv": "#D99BBB", "chart-a": "#B5558A", "chart-b": "#CF7A2A",
    "vine": "#2D6249", "vine-leaf": "#3FA676", "menu-vine": "#555957",
    "lost": "#9AA19D",
})

_PLAIN = {"spots": 0, "vines": False, "film": False, "grapes": False}
BUILT_IN = [
    {"id": "classic", "name": "Classic", "fonts": {"text": "IBM Plex Sans", "headings": "IBM Plex Sans"}, "decor": _PLAIN,
     "light": {"colors": _CLASSIC_LIGHT}, "dark": {"colors": _CLASSIC_DARK}},
    {"id": "dionysus", "name": "Dionysus", "fonts": {"text": "Source Sans 3", "headings": "Cinzel"},
     "decor": {"spots": 5, "vines": True, "film": True, "grapes": True},
     "light": {"colors": _DIONYSUS_LIGHT}, "dark": {"colors": _DIONYSUS_DARK}},
]
BUILT_IN_IDS = tuple(t["id"] for t in BUILT_IN)
MODES = ("auto", "light", "dark")
DEFAULT, DEFAULT_MODE = "dionysus", "light"

_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_FONT_STACK = {
    "IBM Plex Sans": '"IBM Plex Sans","Segoe UI",system-ui,-apple-system,sans-serif',
    "Source Sans 3": '"Source Sans 3","Segoe UI",system-ui,-apple-system,sans-serif',
    "Cinzel": '"Cinzel","Source Sans 3","Segoe UI",system-ui,serif',
    "System": 'system-ui,-apple-system,"Segoe UI",sans-serif',
}


class ThemeError(ValueError):
    pass


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return s or "theme"


def built_in(theme_id: str) -> dict:
    """A built-in theme by its id; Classic when there's none of that id."""
    return next((t for t in BUILT_IN if t["id"] == theme_id), None) or BUILT_IN[0]


def _colors(given, base: dict, mode: str) -> dict:
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise ThemeError(M("A theme’s {mode} colours are an object of names and #hex colours", mode=mode))
    unknown = [k for k in given if k not in COLORS and k not in RETIRED]
    if unknown:
        raise ThemeError(M("Not a colour a theme sets: {names}", names=", ".join(map(str, unknown[:5]))))
    out = {}
    for key in COLORS:
        v = given.get(key, base[key])
        if not isinstance(v, str) or not _HEX.fullmatch(v.strip()):
            raise ThemeError(M("The {mode} colour {name} must be a #hex colour, like #8B3A62", mode=mode, name=key))
        out[key] = v.strip().upper()
    return out


def validate(theme) -> dict:
    """A theme from a file or the settings, checked and filled in: only the keys a theme has, every colour of its light
    and its dark look #hex (any left out come from the theme it's based on, or Classic), fonts and decorations from their
    lists. Its id comes from its name when it has none, and may not be a built-in's."""
    if not isinstance(theme, dict):
        raise ThemeError(M("A theme is a JSON object"))
    if theme.get("dionysus_theme", FORMAT) != FORMAT:
        raise ThemeError(M("This theme is for another version of Dionysus"))
    name = theme.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60 or re.search(r"[\x00-\x1f<>\"&]", name):
        raise ThemeError(M("A theme needs a name of up to 60 characters, without < > \" or &"))
    name = name.strip()
    tid = theme.get("id") or _slug(name)
    if not isinstance(tid, str) or not _ID.fullmatch(tid):
        raise ThemeError(M("A theme’s id may only use small letters, numbers and -"))
    if tid in BUILT_IN_IDS:
        raise ThemeError(M("“{id}” is a theme that comes with Dionysus: give yours another name or id", id=tid))
    base = built_in(str(theme.get("based_on") or ""))
    looks = {}
    for mode in ("light", "dark"):
        look = theme.get(mode) or {}
        if not isinstance(look, dict):
            raise ThemeError(M("A theme’s {mode} look is an object with its colours", mode=mode))
        looks[mode] = {"colors": _colors(look.get("colors"), base[mode]["colors"], mode)}
    fonts = theme.get("fonts") or {}
    if not isinstance(fonts, dict):
        raise ThemeError(M("A theme’s fonts are an object: text and headings"))
    out_fonts = {}
    for key in ("text", "headings"):
        v = fonts.get(key, base["fonts"][key])
        if v not in FONTS:
            raise ThemeError(M("The {which} font must be one of {fonts}", which=key, fonts=", ".join(FONTS)))
        out_fonts[key] = v
    decor = theme.get("decor") or {}
    if not isinstance(decor, dict):
        raise ThemeError(M("A theme’s decor is an object: spots, vines, film and grapes"))
    d = dict(base["decor"], **{k: v for k, v in decor.items() if k in DECOR})
    if isinstance(d["spots"], bool) or not isinstance(d["spots"], int) or not 0 <= d["spots"] <= 20:
        raise ThemeError(M("The spots are a whole number from 0 to 20"))
    if not all(isinstance(d[k], bool) for k in ("vines", "film", "grapes")):
        raise ThemeError(M("The vines, film and grapes are true or false"))
    return {"dionysus_theme": FORMAT, "id": tid, "name": name, "fonts": out_fonts,
            "decor": {"spots": d["spots"], "vines": d["vines"], "film": d["film"], "grapes": d["grapes"]},
            "light": looks["light"], "dark": looks["dark"]}


def coerce(key: str, value, current):
    """The "themes" settings (config._coerce): the theme that shows, its mode, and the uploaded themes."""
    if key == "custom":
        if not isinstance(value, dict) or len(value) > MAX_CUSTOM:
            raise ThemeError(M("themes.custom holds up to {n} themes, by id", n=str(MAX_CUSTOM)))
        out = {}
        for tid, theme in value.items():
            t = validate(dict(theme, id=tid) if isinstance(theme, dict) else theme)
            out[t["id"]] = t
        return out
    if key == "mode":
        if value not in MODES:
            raise ThemeError(M("themes.mode must be auto, light or dark"))
        return value
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ThemeError(M("themes.{setting} must be a theme’s id", setting=key))
    return value


def everything(custom: dict | None) -> list:
    """The built-in themes, then the uploaded ones."""
    return [dict(t, dionysus_theme=FORMAT, builtin=True) for t in BUILT_IN] + \
           [dict(t, builtin=False) for t in (custom or {}).values() if isinstance(t, dict) and t.get("id") not in BUILT_IN_IDS and "light" in t]


def resolve(themes_settings: dict | None) -> dict:
    """Which theme shows, and in which mode: {"theme": id, "mode": "auto" | "light" | "dark"}, the id one that exists
    (a removed uploaded theme falls back to Dionysus)."""
    s = themes_settings or {}
    ids = {t["id"] for t in everything(s.get("custom"))}
    theme = s.get("theme")
    return {"theme": theme if theme in ids else DEFAULT, "mode": s.get("mode") if s.get("mode") in MODES else DEFAULT_MODE}


def css(themes_settings: dict | None) -> str:
    """Every theme's colours and fonts as CSS, each look under :root[data-theme="<id>"][data-mode="light" | "dark"]
    (theme.js sets both on <html>)."""
    out = ["/* Dionysus themes (plexcache/themes.py): the page shows the one named by <html data-theme data-mode> */"]
    for t in everything((themes_settings or {}).get("custom")):
        for mode in ("light", "dark"):
            props = [f"--{k}:{v}" for k, v in t[mode]["colors"].items()]
            props += [f"--sans:{_FONT_STACK[t['fonts']['text']]}", f"--head:{_FONT_STACK[t['fonts']['headings']]}",
                      f"--spots-a:{t['decor']['spots'] / 100:g}", f"color-scheme:{mode}"]
            out.append(f':root[data-theme="{t["id"]}"][data-mode="{mode}"]{{{";".join(props)}}}')
    return "\n".join(out) + "\n"


def script(themes_settings: dict | None) -> str:
    """The script that picks the look before the page draws. The theme and mode come from the settings, unless this
    browser keeps its own choice (localStorage "pc-theme": {"theme", "mode"}, set on System → Themes); "auto" follows the
    device's own light or dark setting, and changes with it. It sets data-theme, data-mode (light | dark) and the theme's
    decorations (data-vines, data-film, data-spots, data-grapes) on <html>. app.js calls PCTHEME.use() after a change."""
    info = {t["id"]: t["decor"] for t in everything((themes_settings or {}).get("custom"))}
    return ("(function(){var INFO=" + json.dumps(info, separators=(",", ":")) + ",SEL=" + json.dumps(resolve(themes_settings), separators=(",", ":")) + ";\n"
            "var d=document.documentElement,mq=window.matchMedia?window.matchMedia(\"(prefers-color-scheme: dark)\"):null;\n"
            "function own(){try{var o=JSON.parse(localStorage.getItem(\"pc-theme\")||\"null\");return o&&typeof o===\"object\"?o:null;}catch{return null;}}\n"
            "function apply(){var o=own()||{},id=INFO[o.theme]?o.theme:SEL.theme,m=o.mode||SEL.mode;if(!INFO[id])id=\"" + DEFAULT + "\";\n"
            "if(m===\"auto\")m=mq&&mq.matches?\"dark\":\"light\";if(m!==\"dark\")m=\"light\";var x=INFO[id];\n"
            "d.setAttribute(\"data-theme\",id);d.setAttribute(\"data-mode\",m);d.setAttribute(\"data-vines\",x.vines?\"on\":\"off\");d.setAttribute(\"data-film\",x.film?\"on\":\"off\");d.setAttribute(\"data-spots\",x.spots?\"on\":\"off\");d.setAttribute(\"data-grapes\",x.grapes?\"on\":\"off\");\n"
            "if(window.PCDECOR)window.PCDECOR.themed();}\n"
            "apply();if(mq){if(mq.addEventListener)mq.addEventListener(\"change\",apply);else if(mq.addListener)mq.addListener(apply);}\n"
            "window.PCTHEME={apply:apply,use:function(sel,info){if(info)INFO=info;SEL=sel;apply();},own:own,\n"
            "setOwn:function(o){try{if(o)localStorage.setItem(\"pc-theme\",JSON.stringify(o));else localStorage.removeItem(\"pc-theme\");}catch{/* storage unavailable */}apply();}};})();\n")


def upload(settings, theme) -> str:
    """A theme file added (System → Themes): checked, kept with the settings (one of the same id replaced); its id."""
    t = validate(theme)
    custom = dict(settings.section("themes").get("custom") or {})
    if t["id"] not in custom and len(custom) >= MAX_CUSTOM:
        raise ThemeError(M("Up to {n} themes can be added: remove one first", n=str(MAX_CUSTOM)))
    custom[t["id"]] = t
    settings.update({"themes": {"custom": custom}})
    return t["id"]


def remove(settings, theme_id) -> None:
    """An uploaded theme taken away; where it was in use, Dionysus takes its place."""
    ts = settings.section("themes")
    patch: dict = {"custom": {k: v for k, v in (ts.get("custom") or {}).items() if k != theme_id}}
    if ts.get("theme") == theme_id:
        patch["theme"] = DEFAULT
    settings.update({"themes": patch})


def download(theme: dict) -> str:
    """A theme as a file to keep or edit: the theme, then what each value does."""
    t = {k: theme[k] for k in ("dionysus_theme", "id", "name", "fonts", "decor", "light", "dark")}
    if theme.get("builtin"):
        t = dict(t, id=theme["id"] + "-copy", name=theme["name"] + " Copy", based_on=theme["id"])
    t["about"] = {
        "how": "Change the values below, give the theme a new name, and upload it in Dionysus: System → Themes. "
               "Colours are #hex: light holds the colours of its light look, dark of its dark look. Any colour left out "
               "comes from the theme named in based_on (or Classic).",
        "colors": COLORS,
        "fonts": "text and headings, each one of: " + ", ".join(FONTS),
        "decor": DECOR,
    }
    return json.dumps(t, indent=2, ensure_ascii=False) + "\n"
