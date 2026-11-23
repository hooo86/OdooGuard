// OdooGuard - survol des graphiques (réticule + infobulle). Aucune dépendance.
(function () {
  function fmt(v) {
    if (v < 120) return Math.round(v) + " s";
    if (v < 7200) return Math.round(v / 60) + " min";
    return (v / 3600).toFixed(1) + " h";
  }
  function setup(svg) {
    var pts = JSON.parse(svg.dataset.points || "[]");
    if (pts.length < 2) return;
    var pad = svg.dataset.pad.split(",").map(Number);
    var x0 = +svg.dataset.x0, x1 = +svg.dataset.x1, vmax = +svg.dataset.vmax;
    var vb = svg.viewBox.baseVal, iw = vb.width - pad[0] - pad[1], ih = vb.height - pad[2] - pad[3];
    var cross = svg.querySelector(".crosshair"), dot = svg.querySelector(".hoverdot");
    var tip = svg.parentNode.querySelector(".tooltip");
    function sx(t) { return pad[0] + (t - x0) / Math.max(x1 - x0, 1) * iw; }
    function sy(v) { return pad[2] + ih - v / vmax * ih; }
    svg.addEventListener("mousemove", function (e) {
      var r = svg.getBoundingClientRect();
      var px = (e.clientX - r.left) / r.width * vb.width;
      var t = x0 + (px - pad[0]) / iw * (x1 - x0), best = pts[0];
      for (var i = 1; i < pts.length; i++) if (Math.abs(pts[i][0] - t) < Math.abs(best[0] - t)) best = pts[i];
      var cx = sx(best[0]), cy = sy(best[1]);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
      var d = new Date(best[0] * 1000);
      tip.textContent = d.toLocaleString("fr-FR", {weekday: "short", day: "2-digit", month: "2-digit",
        hour: "2-digit", minute: "2-digit"}) + " · " + fmt(best[1]);
      tip.hidden = false;
      var left = cx / vb.width * r.width + 14;
      if (left + tip.offsetWidth > r.width) left -= tip.offsetWidth + 28;
      tip.style.left = left + "px";
      tip.style.top = (cy / vb.height * r.height) + "px";
    });
    svg.addEventListener("mouseleave", function () {
      cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("svg.linechart").forEach(setup);
  });
})();
