// Thème clair / sombre : appliqué avant l'affichage (pas de flash), choix mémorisé
// dans ce navigateur uniquement. Par défaut : réglage du système.
(function () {
  var KEY = "odooguard-theme";
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* stockage indisponible */ }
  if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
  function effective() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function refreshIcon(btn) {
    var use = btn.querySelector("use");
    if (use) use.setAttribute("href", "/static/icons.svg#i-" + (effective() === "dark" ? "light_mode" : "dark_mode"));
  }
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    refreshIcon(btn);
    btn.addEventListener("click", function () {
      var next = effective() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* sans mémorisation */ }
      refreshIcon(btn);
    });
  });
})();
