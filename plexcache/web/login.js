// The sign-in form (kept out of login.html so the page can forbid inline scripts).
var T = PCI18N.T;
PCI18N.ready.then(function () {
  document.title = T("Plex Cache – Sign In");
  document.getElementById("langslot").appendChild(PCI18N.picker());
});
function showHelp(open) {
  var b = document.getElementById("help-btn"), h = document.getElementById("help");
  h.hidden = !open;
  b.setAttribute("aria-expanded", open ? "true" : "false");
}
document.getElementById("help-btn").addEventListener("click", function () {
  showHelp(document.getElementById("help").hidden);
});
document.getElementById("login").addEventListener("submit", function (e) {
  e.preventDefault();
  var btn = e.target.querySelector("button[type=submit]"), err = document.getElementById("err");
  btn.disabled = true;
  err.hidden = true;
  fetch("/login", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
    body: JSON.stringify({ username: document.getElementById("u").value, password: document.getElementById("p").value }) })
    .then(function (r) {
      if (r.ok) { if (location.pathname === "/login") location.replace("/"); else location.reload(); return; }
      return r.json().catch(function () { return {}; }).then(function (data) {
        err.textContent = r.status === 401 ? T("That username and password don’t match.") :
          (r.status === 403 || r.status === 429) && data.error ? PCI18N.msg(data.error) : T("Couldn’t sign in ({status}).", { status: r.status });
        err.hidden = false;
        btn.disabled = false;
        if (r.status === 403) showHelp(true);
      });
    })
    .catch(function () { err.textContent = T("Can’t reach Plex Cache."); err.hidden = false; btn.disabled = false; });
});
