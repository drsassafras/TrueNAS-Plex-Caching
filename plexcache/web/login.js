// The sign-in form (kept out of login.html so the page can forbid inline scripts).
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
        err.textContent = r.status === 401 ? "That username and password don't match." :
          r.status === 403 && data.error ? data.error : "Couldn't sign in (" + r.status + ").";
        err.hidden = false;
        btn.disabled = false;
        if (r.status === 403) showHelp(true);
      });
    })
    .catch(function () { err.textContent = "Can't reach Plex Cache."; err.hidden = false; btn.disabled = false; });
});
