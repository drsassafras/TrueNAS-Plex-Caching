/* Plex Cache translations — shared by the app and the sign-in page.
 *
 * T("Text {name}", {name: x})        plain text in the chosen language
 * H("<b>Text</b> {name}", {name: x}) HTML: the template is trusted, the values are escaped
 * P(n, "{n} file", "{n} files", {})  plural, plain text   (PH: the HTML version)
 * msg(v)                             a message from the server ({_t, _p, _one}) as plain text
 * N("Text")                          marks text for translation without translating it yet
 *
 * Catalogs (static/i18n/<code>.json) are keyed by the English text; see plexcache/i18n.py.
 * The language follows the browser unless one is picked from the menu next to Sign out.
 */
(function () {
  "use strict";

  var LANGUAGES = [
    ["en", "English (Canada)"], ["en-US", "English (US)"], ["en-GB", "English (UK)"],
    ["es", "Español"], ["fr", "Français"], ["de", "Deutsch"], ["pt-BR", "Português (Brasil)"],
    ["it", "Italiano"], ["nl", "Nederlands"], ["pl", "Polski"], ["ru", "Русский"],
    ["ja", "日本語"], ["zh-CN", "简体中文"], ["ko", "한국어"], ["zh-TW", "繁體中文"],
    ["tr", "Türkçe"], ["sv", "Svenska"], ["uk", "Українська"]
  ];
  var CODES = LANGUAGES.map(function (l) { return l[0]; });
  var KEY = "pc-lang";
  var REPO = "https://github.com/drsassafras/TrueNAS-Plex-Caching";

  // English with UK spelling: the UK and the countries that follow it.
  var UK = ["GB", "UK", "IE", "AU", "NZ", "ZA", "IN", "SG", "HK", "MT"];

  // A browser language tag ("de-AT", "zh-Hant-HK", "en-AU") -> one of CODES, or null.
  function match(tag) {
    if (!tag) return null;
    var parts = String(tag).replace(/_/g, "-").split("-"), base = parts[0].toLowerCase();
    var rest = parts.slice(1).map(function (p) { return p.toUpperCase(); });
    if (base === "en") {
      if (rest.indexOf("CA") >= 0) return "en";  // the original text is Canadian English
      if (rest.some(function (r) { return UK.indexOf(r) >= 0; })) return "en-GB";
      return "en-US";
    }
    if (base === "zh") return rest.some(function (r) { return r === "HANT" || r === "TW" || r === "HK" || r === "MO"; }) ? "zh-TW" : "zh-CN";
    if (base === "pt") return "pt-BR";
    for (var i = 0; i < CODES.length; i++) if (CODES[i].toLowerCase() === base) return CODES[i];
    return null;
  }

  function browserLang() {
    var tags = (navigator.languages && navigator.languages.length) ? navigator.languages : [navigator.language];
    for (var i = 0; i < tags.length; i++) { var m = match(tags[i]); if (m) return m; }
    return "en";
  }

  var choice = "auto";
  try { choice = localStorage.getItem(KEY) || "auto"; } catch (e) { /* storage unavailable */ }
  if (choice !== "auto" && CODES.indexOf(choice) < 0) choice = "auto";
  var lang = choice === "auto" ? browserLang() : choice;
  var locale = lang === "en" ? "en-CA" : lang;
  var cat = {};
  var plural = (window.Intl && Intl.PluralRules) ? new Intl.PluralRules(locale) : null;

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v) {
    try { return v.toLocaleString(locale, { maximumFractionDigits: 2 }); } catch (e) { return String(v); }
  }
  function value(v) {
    if (v == null) return "";
    if (typeof v === "number") return num(v);
    if (Array.isArray(v)) return v.map(value).join(/^(ja|zh)/.test(lang) ? "、" : ", ");
    if (typeof v === "object" && typeof v._t === "string") return msg(v);
    return String(v);
  }
  function fill(text, vars, escape) {
    vars = vars || {};
    return text.replace(/\{(\w+)\}/g, function (all, k) {
      if (!(k in vars)) return all;
      var s = value(vars[k]);
      return escape ? esc(s) : s;
    });
  }
  function category(n) {
    if (plural && typeof n === "number") return plural.select(n);
    return n === 1 ? "one" : "other";
  }
  function pick(other, one, n) {
    var e = cat[other];
    if (e && typeof e === "object" && (e[category(n)] || e.other)) return e[category(n)] || e.other;
    if (typeof e === "string") return e;
    return n === 1 && one != null ? one : other;
  }
  function T(s, vars) { var e = cat[s]; return fill(typeof e === "string" ? e : s, vars, false); }
  function H(s, vars) { var e = cat[s]; return fill(typeof e === "string" ? e : s, vars, true); }
  function P(n, one, other, vars) { vars = Object.assign({ n: n }, vars || {}); return fill(pick(other, one, n), vars, false); }
  function PH(n, one, other, vars) { vars = Object.assign({ n: n }, vars || {}); return fill(pick(other, one, n), vars, true); }
  function N(s) { return s; }
  // A message from the server: plain text, or {_t: template, _p: values, _one: English singular}.
  function msg(v) {
    if (v == null) return "";
    if (typeof v !== "object" || typeof v._t !== "string") return String(v);
    var p = v._p || {};
    if (v._one != null) return P(p.n, v._one, v._t, p);
    return T(v._t, p);
  }

  // Static text in the HTML pages: data-i18n (text) and data-i18n-html (trusted markup),
  // data-i18n-title / -label / -placeholder for attributes.
  function apply(root) {
    root.querySelectorAll("[data-i18n]").forEach(function (el) { el.textContent = T(el.textContent.trim().replace(/\s+/g, " ")); });
    root.querySelectorAll("[data-i18n-html]").forEach(function (el) { el.innerHTML = T(el.innerHTML.trim().replace(/\s+/g, " ")); });
    ["title", "aria-label", "placeholder"].forEach(function (a) {
      root.querySelectorAll("[data-i18n-" + a + "]").forEach(function (el) { el.setAttribute(a, T(el.getAttribute(a))); });
    });
    document.documentElement.lang = lang;
  }

  // ---- the language menu (next to Sign out, and on the sign-in page)
  var GLOBE = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M12 2a10 10 0 100 20 10 10 0 000-20zm6.93 6h-2.95a15.7 15.7 0 00-1.38-3.56A8.03 8.03 0 0118.93 8zM12 4.04c.83 1.2 1.48 2.53 1.91 3.96h-3.82c.43-1.43 1.08-2.76 1.91-3.96zM4.26 14a8.2 8.2 0 010-4h3.38a16.5 16.5 0 000 4H4.26zm.81 2h2.95c.32 1.25.78 2.45 1.38 3.56A7.99 7.99 0 015.07 16zm2.95-8H5.07a7.99 7.99 0 014.33-3.56A15.7 15.7 0 008.02 8zM12 19.96c-.83-1.2-1.48-2.53-1.91-3.96h3.82c-.43 1.43-1.08 2.76-1.91 3.96zM14.34 14H9.66a14.7 14.7 0 010-4h4.68a14.7 14.7 0 010 4zm.25 5.56c.6-1.11 1.06-2.31 1.38-3.56h2.95a8.03 8.03 0 01-4.33 3.56zM16.36 14a16.5 16.5 0 000-4h3.38a8.2 8.2 0 010 4h-3.38z"/></svg>';

  function nameOf(code) { for (var i = 0; i < LANGUAGES.length; i++) if (LANGUAGES[i][0] === code) return LANGUAGES[i][1]; return code; }

  function setChoice(c) {
    try { if (c === "auto") localStorage.removeItem(KEY); else localStorage.setItem(KEY, c); } catch (e) { /* storage unavailable */ }
    location.reload();
  }

  function picker() {
    var wrap = document.createElement("div");
    wrap.className = "langpick";
    var short = lang.split("-")[0].toUpperCase() + (lang === "en-US" ? "·US" : lang === "en-GB" ? "·UK" : lang === "zh-TW" ? "·TW" : "");
    var item = function (code, label, extra) {
      var on = code === choice;
      return '<li role="none"><button type="button" role="menuitemradio" aria-checked="' + on + '" data-lang="' + code + '"' +
        (code !== "auto" ? ' lang="' + code + '"' : "") + ">" + label + (extra || "") + "</button></li>";
    };
    var html = '<button type="button" class="langbtn" aria-haspopup="true" aria-expanded="false" title="' + esc(T("Language")) +
      '" aria-label="' + esc(T("Language: {name}", { name: nameOf(lang) })) + '">' + GLOBE + '<span class="code">' + short + "</span></button>" +
      '<div class="langmenu" hidden><ul role="menu" aria-label="' + esc(T("Language")) + '">' +
      item("auto", esc(T("Automatic (browser)")), ' <span class="muted">· ' + esc(nameOf(browserLang())) + "</span>") +
      '<li role="none" class="orig"><div class="orig-head">' + esc(T("Original")) + "</div><ul role=\"group\">" +
      LANGUAGES.slice(0, 3).map(function (l) { return item(l[0], esc(l[1])); }).join("") + "</ul></li>" +
      LANGUAGES.slice(3).map(function (l) { return item(l[0], esc(l[1])); }).join("") +
      '</ul><p class="langnote">' + esc(T("Other languages were translated with AI and may contain mistakes.")) +
      ' <a href="' + REPO + '/issues" target="_blank" rel="noopener">' + esc(T("Suggest a fix")) + "</a></p></div>";
    wrap.innerHTML = html;
    var btn = wrap.querySelector(".langbtn"), menu = wrap.querySelector(".langmenu");
    function open(show) {
      menu.hidden = !show;
      btn.setAttribute("aria-expanded", show ? "true" : "false");
      if (show) { var cur = menu.querySelector('[aria-checked="true"]'); if (cur) cur.focus(); }
    }
    btn.addEventListener("click", function () { open(menu.hidden); });
    menu.addEventListener("click", function (e) {
      var b = e.target.closest("[data-lang]");
      if (b) setChoice(b.dataset.lang);
    });
    menu.addEventListener("keydown", function (e) {
      var items = Array.prototype.slice.call(menu.querySelectorAll("[data-lang]")), i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
      } else if (e.key === "Escape") { open(false); btn.focus(); }
    });
    document.addEventListener("click", function (e) { if (!wrap.contains(e.target)) open(false); });
    return wrap;
  }

  // The page loads this file as i18n.js?v=<build>: ask for the translations of the same version
  var BUILD_QS = (((document.currentScript || {}).src || "").match(/\?v=[0-9a-f]+/) || [""])[0];
  var ready = (lang === "en" ? Promise.resolve() :
    fetch("/static/i18n/" + lang + ".json" + BUILD_QS, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (c) { cat = c || {}; })
      .catch(function () { cat = {}; }))
    .then(function () { apply(document); });

  window.PCI18N = {
    T: T, H: H, P: P, PH: PH, N: N, msg: msg, esc: esc, lang: lang, locale: locale, choice: choice,
    ready: ready, picker: picker, match: match, LANGUAGES: LANGUAGES
  };
})();
