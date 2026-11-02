"""Themes: how the web UI looks.

A theme is a set of colours, two fonts and a few decorations, kept as one small JSON object:

    {"dionysus_theme": 1, "id": "dionysus-light", "name": "Dionysus Light", "mode": "light",
     "colors": {"bg": "#E7E8E2", ...}, "fonts": {"text": "Source Sans 3", "headings": "Cinzel"},
     "decor": {"logo": "theatre", "spots": 5, "vines": true, "film": true}}

Four ship with the app (BUILT_IN): Classic Light and Classic Dark (the app's look before themes) and Dionysus Light and
Dark. People can add their own (the "themes" section of the settings, "custom"): a downloaded theme, edited and
uploaded again. Every value is checked here before it reaches a page (validate()): colours are #hex only and fonts and
decorations come from fixed lists, so a theme file can't carry anything but a look.

The page gets every theme's colours as CSS (css(), served at /theme.css) and which one to show as a small script
(script(), /theme.js), loaded before the page draws so it never flashes the wrong colours. "auto" shows the light theme
or the dark one by the device's own light or dark setting, and switches when that changes.
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
    "frame": "the vine round the whole app",
    "frame-leaf": "the leaves on the vine round the app",
    "menu-vine": "the vine between the menu and the page",
}
FONTS = ("IBM Plex Sans", "Source Sans 3", "Cinzel", "System")  # (Source Sans 3 and Cinzel ship with the app: web/fonts)
LOGOS = ("classic", "theatre")
DECOR = {
    "logo": "classic: the app's first logo; theatre: the Full house theatre, grapes facing a gold screen",
    "spots": "the spots round the app and on the menu: how strong, 0 (none) to 20 per cent",
    "vines": "true: a vine with a leaf now and then round each card and round the whole app",
    "film": "true: a reel of film under each page's title",
}

_CLASSIC_LIGHT = {
    "bg": "#F3F4F1", "panel": "#FFFFFF", "ink": "#1B1F1D", "muted": "#5A625D", "soft": "#4C544F", "line": "#DDE1DD", "line2": "#EDEFEC",
    "accent": "#0F5E57", "accent-ink": "#FFFFFF", "side": "#1B1F1D", "side-ink": "#C9CFCB",
    "ok-bg": "#DDEFE3", "ok": "#155C37", "warn-bg": "#FBE8CF", "warn": "#7F4205", "bad-bg": "#F8D9D6", "bad": "#8A1C14",
    "info-bg": "#E7EAE6", "info": "#4C544F", "run-bg": "#D8E9F4", "run": "#14476B",
    "guide-bg": "#F1F5F3", "guide-line": "#D6E2DD", "note-bg": "#FDF4E7", "note-line": "#F1D9B4", "note-ink": "#5E3404",
    "code-bg": "#F6F7F5", "c-movie": "#0F5E57", "c-tv": "#5FA79B", "c-audio": "#B9772A", "c-res": "#9AA39E", "c-idx": "#6E5BA8",
    "track": "#E7EAE6", "chart-a": "#008F7E", "chart-b": "#C2651A",
    "gold": "#E5A00D", "wine": "#8B3A62", "leaf": "#2E7D5B", "unripe": "#D3E4DA", "film": "#3A423E", "film-mark": "#F0AE1F",
    "spots": "#B07A00", "menu-spots": "#F0AE1F", "vine": "#DDE1DD", "vine-leaf": "#2E7D5B", "frame": "#AAACA7", "frame-leaf": "#AAACA7",
    "menu-vine": "#AAACA7",
}
_CLASSIC_DARK = {
    "bg": "#121614", "panel": "#1A1F1C", "ink": "#E6EAE7", "muted": "#9CA59F", "soft": "#B3BBB6", "line": "#2C332F", "line2": "#252B28",
    "accent": "#3FA596", "accent-ink": "#0B1311", "side": "#0C0F0E", "side-ink": "#C9CFCB",
    "ok-bg": "#16382A", "ok": "#8FD9AE", "warn-bg": "#3D2A12", "warn": "#F2C08A", "bad-bg": "#3E1B18", "bad": "#F2A49C",
    "info-bg": "#262C29", "info": "#B3BBB6", "run-bg": "#14304A", "run": "#9BC8EE",
    "guide-bg": "#18231F", "guide-line": "#2A3A34", "note-bg": "#2E2414", "note-line": "#4A3A20", "note-ink": "#F2D3A6",
    "code-bg": "#151A18", "c-movie": "#3FA596", "c-tv": "#8CC9BE", "c-audio": "#D9A15A", "c-res": "#6B746F", "c-idx": "#A797D8",
    "track": "#2A302D", "chart-a": "#159C8A", "chart-b": "#CF7A2A",
    "gold": "#F0AE1F", "wine": "#B5558A", "leaf": "#3FA676", "unripe": "#2B4136", "film": "#3A423E", "film-mark": "#F0AE1F",
    "spots": "#F0AE1F", "menu-spots": "#F0AE1F", "vine": "#2C332F", "vine-leaf": "#3FA676", "frame": "#2A6549", "frame-leaf": "#3FA676",
    "menu-vine": "#555957",
}
# Dionysus: the film's pale stone behind the cards, wine for the buttons and links, a vine round the cards and the app
_DIONYSUS_LIGHT = dict(_CLASSIC_LIGHT, **{
    "bg": "#E7E8E2", "line2": "#E9EAE5", "accent": "#8B3A62", "note-bg": "#FBF1DF", "note-line": "#E5C98F",
    "guide-bg": "#F3F4EF", "guide-line": "#D9DBD2", "code-bg": "#F5F5F1", "info-bg": "#E9EAE5",
    "c-movie": "#8B3A62", "c-tv": "#C98BAA", "chart-a": "#8B3A62", "chart-b": "#C2651A",
    "vine": "#97BEAD", "vine-leaf": "#2E7D5B", "frame": "#AAACA7", "frame-leaf": "#AAACA7", "menu-vine": "#AAACA7",
})
_DIONYSUS_DARK = dict(_CLASSIC_DARK, **{
    "bg": "#111513", "muted": "#A3ABA6", "accent": "#B5558A", "accent-ink": "#FFFFFF",
    "note-bg": "#2A2214", "note-line": "#5A4622", "guide-bg": "#1A201D", "guide-line": "#2E3632",
    "c-movie": "#B5558A", "c-tv": "#D99BBB", "chart-a": "#B5558A", "chart-b": "#CF7A2A",
    "vine": "#2D6249", "vine-leaf": "#3FA676", "frame": "#2A6549", "frame-leaf": "#3FA676", "menu-vine": "#555957",
})

_PLAIN = {"logo": "classic", "spots": 0, "vines": False, "film": False}
BUILT_IN = [
    {"id": "classic-light", "name": "Classic Light", "mode": "light", "colors": _CLASSIC_LIGHT,
     "fonts": {"text": "IBM Plex Sans", "headings": "IBM Plex Sans"}, "decor": _PLAIN},
    {"id": "classic-dark", "name": "Classic Dark", "mode": "dark", "colors": _CLASSIC_DARK,
     "fonts": {"text": "IBM Plex Sans", "headings": "IBM Plex Sans"}, "decor": _PLAIN},
    {"id": "dionysus-light", "name": "Dionysus Light", "mode": "light", "colors": _DIONYSUS_LIGHT,
     "fonts": {"text": "Source Sans 3", "headings": "Cinzel"}, "decor": {"logo": "theatre", "spots": 5, "vines": True, "film": True}},
    {"id": "dionysus-dark", "name": "Dionysus Dark", "mode": "dark", "colors": _DIONYSUS_DARK,
     "fonts": {"text": "Source Sans 3", "headings": "Cinzel"}, "decor": {"logo": "theatre", "spots": 6, "vines": True, "film": True}},
]
BUILT_IN_IDS = tuple(t["id"] for t in BUILT_IN)
DEFAULT = "dionysus-light"
DEFAULT_LIGHT, DEFAULT_DARK = "dionysus-light", "dionysus-dark"

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


def built_in(theme_id: str, mode: str = "light") -> dict:
    """A built-in theme by its id; Classic of the mode when there's none of that id."""
    return next((t for t in BUILT_IN if t["id"] == theme_id), None) or next(t for t in BUILT_IN if t["id"] == "classic-" + mode)


def validate(theme, custom_ids=()) -> dict:
    """A theme from a file or the settings, checked and filled in: only the keys a theme has, every colour #hex (any
    left out come from the theme it's based on, or the built-in of its mode), fonts and decorations from their lists.
    Its id comes from its name when it has none, and may not be a built-in's."""
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
    mode = theme.get("mode", "light")
    if mode not in ("light", "dark"):
        raise ThemeError(M("A theme’s mode is light or dark"))
    base = built_in(str(theme.get("based_on") or ""), mode)
    colors = theme.get("colors") or {}
    if not isinstance(colors, dict):
        raise ThemeError(M("A theme’s colors are an object of names and #hex colours"))
    out_colors = {}
    for key in COLORS:
        v = colors.get(key, base["colors"][key])
        if not isinstance(v, str) or not _HEX.fullmatch(v.strip()):
            raise ThemeError(M("The colour {name} must be a #hex colour, like #8B3A62", name=key))
        out_colors[key] = v.strip().upper()
    unknown = [k for k in colors if k not in COLORS]
    if unknown:
        raise ThemeError(M("Not a colour a theme sets: {names}", names=", ".join(map(str, unknown[:5]))))
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
        raise ThemeError(M("A theme’s decor is an object: logo, spots, vines and film"))
    d = dict(base["decor"], **{k: v for k, v in decor.items() if k in DECOR})
    if d["logo"] not in LOGOS:
        raise ThemeError(M("The logo must be one of {logos}", logos=", ".join(LOGOS)))
    if isinstance(d["spots"], bool) or not isinstance(d["spots"], int) or not 0 <= d["spots"] <= 20:
        raise ThemeError(M("The spots are a whole number from 0 to 20"))
    if not isinstance(d["vines"], bool) or not isinstance(d["film"], bool):
        raise ThemeError(M("The vines and film are true or false"))
    return {"dionysus_theme": FORMAT, "id": tid, "name": name, "mode": mode, "colors": out_colors, "fonts": out_fonts,
            "decor": {"logo": d["logo"], "spots": d["spots"], "vines": d["vines"], "film": d["film"]}}


def coerce(key: str, value, current):
    """The "themes" settings (config._coerce): which theme shows, the auto pair, and the uploaded themes."""
    if key == "custom":
        if not isinstance(value, dict) or len(value) > MAX_CUSTOM:
            raise ThemeError(M("themes.custom holds up to {n} themes, by id", n=str(MAX_CUSTOM)))
        out = {}
        for tid, theme in value.items():
            t = validate(dict(theme, id=tid) if isinstance(theme, dict) else theme)
            out[t["id"]] = t
        return out
    if not isinstance(value, str) or not (value in BUILT_IN_IDS or _ID.fullmatch(value) or (key == "selected" and value == "auto")):
        raise ThemeError(M("themes.{setting} must be a theme’s id", setting=key))
    return value


def everything(custom: dict | None) -> list:
    """The built-in themes, then the uploaded ones."""
    return [dict(t, dionysus_theme=FORMAT, builtin=True) for t in BUILT_IN] + \
           [dict(t, builtin=False) for t in (custom or {}).values() if isinstance(t, dict) and t.get("id") not in BUILT_IN_IDS]


def resolve(themes_settings: dict) -> dict:
    """Which theme shows: {"selected": id or "auto", "light": id, "dark": id}, each id one that exists (a deleted
    uploaded theme falls back to the default)."""
    s = themes_settings or {}
    ids = set(BUILT_IN_IDS) | set((s.get("custom") or {}).keys())
    pick = lambda v, d: v if v in ids else d  # noqa: E731
    sel = s.get("selected") or DEFAULT
    return {"selected": "auto" if sel == "auto" else pick(sel, DEFAULT),
            "light": pick(s.get("auto_light"), DEFAULT_LIGHT), "dark": pick(s.get("auto_dark"), DEFAULT_DARK)}


def css(themes_settings: dict) -> str:
    """Every theme's colours and fonts as CSS, each under :root[data-theme="<id>"] (the page sets data-theme)."""
    out = ["/* Dionysus themes (plexcache/themes.py): the page shows the one named by <html data-theme> */"]
    for t in everything((themes_settings or {}).get("custom")):
        props = [f"--{k}:{v}" for k, v in t["colors"].items()]
        props += [f"--sans:{_FONT_STACK[t['fonts']['text']]}", f"--head:{_FONT_STACK[t['fonts']['headings']]}",
                  f"--spots-a:{t['decor']['spots'] / 100:g}", f"color-scheme:{t['mode']}"]
        out.append(f':root[data-theme="{t["id"]}"]{{{";".join(props)}}}')
    return "\n".join(out) + "\n"


def script(themes_settings: dict) -> str:
    """The script that picks the theme before the page draws: sets data-theme, data-mode (light or dark) and the
    decorations (data-logo, data-vines, data-film, data-spots) on <html>, and follows the device's light or dark
    setting for "auto". app.js calls PCTHEME.use() with the settings after a change."""
    info = {t["id"]: {"mode": t["mode"], "decor": t["decor"]} for t in everything((themes_settings or {}).get("custom"))}
    return ("(function(){var INFO=" + json.dumps(info, separators=(",", ":")) + ",SEL=" + json.dumps(resolve(themes_settings), separators=(",", ":")) + ";\n"
            "var d=document.documentElement,mq=window.matchMedia?window.matchMedia(\"(prefers-color-scheme: dark)\"):null;\n"
            "function apply(){var id=SEL.selected===\"auto\"?(mq&&mq.matches?SEL.dark:SEL.light):SEL.selected,t=INFO[id]||INFO[\"" + DEFAULT + "\"],x=t.decor;\n"
            "d.setAttribute(\"data-theme\",INFO[id]?id:\"" + DEFAULT + "\");d.setAttribute(\"data-mode\",t.mode);d.setAttribute(\"data-logo\",x.logo);\n"
            "d.setAttribute(\"data-vines\",x.vines?\"on\":\"off\");d.setAttribute(\"data-film\",x.film?\"on\":\"off\");d.setAttribute(\"data-spots\",x.spots?\"on\":\"off\");\n"
            "if(window.PCDECOR)window.PCDECOR.themed();}\n"
            "apply();if(mq){if(mq.addEventListener)mq.addEventListener(\"change\",apply);else if(mq.addListener)mq.addListener(apply);}\n"
            "window.PCTHEME={apply:apply,use:function(sel,info){if(info)INFO=info;SEL=sel;apply();},current:function(){return d.getAttribute(\"data-theme\");}};})();\n")


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
    """An uploaded theme taken away; where it was in use, the theme new installs start on takes its place."""
    ts = settings.section("themes")
    patch: dict = {"custom": {k: v for k, v in (ts.get("custom") or {}).items() if k != theme_id}}
    for key, fallback in (("selected", DEFAULT), ("auto_light", DEFAULT_LIGHT), ("auto_dark", DEFAULT_DARK)):
        if ts.get(key) == theme_id:
            patch[key] = fallback
    settings.update({"themes": patch})


def download(theme: dict) -> str:
    """A theme as a file to keep or edit: the theme, then what each value does."""
    t = {k: theme[k] for k in ("dionysus_theme", "id", "name", "mode", "colors", "fonts", "decor")}
    if theme.get("builtin"):
        t = dict(t, id=theme["id"] + "-copy", name=theme["name"] + " Copy", based_on=theme["id"])
    t["about"] = {
        "how": "Change the values below, give the theme a new name, and upload it in Dionysus: System → Themes. "
               "Colours are #hex. Any colour left out comes from the theme named in based_on (or Classic Light or Dark, by mode).",
        "mode": "light or dark: which half of the Auto choice it can be, and the colours the browser uses for scrollbars and fields",
        "colors": COLORS,
        "fonts": "text and headings, each one of: " + ", ".join(FONTS),
        "decor": DECOR,
    }
    return json.dumps(t, indent=2, ensure_ascii=False) + "\n"
