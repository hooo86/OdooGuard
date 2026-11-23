"""Graphiques SVG générés côté serveur (aucune bibliothèque JavaScript externe).

Une seule forme : la courbe d'évolution (une série par graphique, avec la
ligne de seuil de l'objectif). Le survol (réticule + infobulle) est assuré par
``static/app.js`` à partir des attributs ``data-*`` ; un tableau équivalent
est toujours proposé sous le graphique (accessibilité).
"""

from __future__ import annotations

import datetime as dt
import json
from html import escape

W, H = 640, 180
PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 14, 28


def _fmt_s(v: float) -> str:
    if v < 120:
        return f"{v:.0f} s"
    if v < 7200:
        return f"{v / 60:.0f} min"
    return f"{v / 3600:.1f} h"


def line_chart(series: list[tuple[float, float]], *, title: str, threshold: float | None,
               threshold_label: str, chart_id: str) -> str:
    """Courbe temps -> secondes. ``series`` : [(horodatage, valeur)]."""
    if len(series) < 2:
        return (f'<figure class="chart"><figcaption>{escape(title)}</figcaption>'
                '<p class="muted">Pas encore assez de mesures (une toutes les 5 min).</p></figure>')
    # Sous-échantillonnage : max d'un intervalle (on ne masque jamais un pic)
    if len(series) > 300:
        step = len(series) / 300
        buckets: list[tuple[float, float]] = []
        for i in range(300):
            chunk = series[int(i * step): int((i + 1) * step)] or [series[-1]]
            buckets.append(max(chunk, key=lambda p: p[1]))
        series = buckets
    t0, t1 = series[0][0], series[-1][0]
    vmax = max(max(v for _, v in series), threshold or 0) * 1.1 or 1
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def x(t):
        return PAD_L + (t - t0) / max(t1 - t0, 1) * iw

    def y(v):
        return PAD_T + ih - v / vmax * ih

    pts = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in series)
    grid = []
    for i in range(4):
        v = vmax * i / 3
        grid.append(f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                    f'<text class="axis" x="{PAD_L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{_fmt_s(v)}</text>')
    days = []
    d = dt.datetime.fromtimestamp(t0).replace(hour=0, minute=0, second=0, microsecond=0)
    while d.timestamp() <= t1:
        if d.timestamp() >= t0:
            xx = x(d.timestamp())
            days.append(f'<text class="axis" x="{xx:.1f}" y="{H - 8}" text-anchor="middle">'
                        f'{d.strftime("%d/%m")}</text>')
        d += dt.timedelta(days=1)
    thr = ""
    if threshold:
        thr = (f'<line class="threshold" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(threshold):.1f}" '
               f'y2="{y(threshold):.1f}"/><text class="axis" x="{W - PAD_R}" '
               f'y="{y(threshold) - 5:.1f}" text-anchor="end">{escape(threshold_label)}</text>')
    data = json.dumps([[round(t), round(v, 1)] for t, v in series])
    last_t, last_v = series[-1]
    return f"""
<figure class="chart" id="{chart_id}">
  <figcaption>{escape(title)} <span class="muted">· max {_fmt_s(max(v for _, v in series))}, actuel {_fmt_s(last_v)}</span></figcaption>
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}" class="linechart"
       data-points='{escape(data)}' data-x0="{t0}" data-x1="{t1}" data-vmax="{vmax}"
       data-pad="{PAD_L},{PAD_R},{PAD_T},{PAD_B}">
    {''.join(grid)}
    {''.join(days)}
    {thr}
    <polyline class="series" fill="none" points="{pts}"/>
    <circle class="endpoint" cx="{x(last_t):.1f}" cy="{y(last_v):.1f}" r="4"/>
    <line class="crosshair" x1="0" x2="0" y1="{PAD_T}" y2="{H - PAD_B}" visibility="hidden"/>
    <circle class="hoverdot" r="5" cx="0" cy="0" visibility="hidden"/>
    <rect class="hitarea" x="{PAD_L}" y="{PAD_T}" width="{iw}" height="{ih}" fill="transparent"/>
  </svg>
  <div class="tooltip" hidden></div>
</figure>"""
