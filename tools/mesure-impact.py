#!/usr/bin/env python3
"""Mesure de l'impact des sauvegardes sur Odoo (critère d'acceptation).

Interroge /web/login toutes les N secondes pendant la durée demandée et note
si une sauvegarde OdooGuard est en cours à cet instant (verrous de
/run/odooguard). Produit un CSV et un résumé : temps de réponse médian et
95e centile, avec et sans sauvegarde en cours.

Usage (sur OdooGuard, une journée ouvrée complète) :
  python3 tools/mesure-impact.py --url http://192.168.8.80:8069/web/login \
      --duree 10h --intervalle 10 --csv /var/lib/odooguard/impact-$(date +%F).csv
"""
import argparse
import csv
import fcntl
import os
import statistics
import time
import urllib.request

LOCKS = ("db", "files")


def backup_running(lock_dir):
    for name in LOCKS:
        path = os.path.join(lock_dir, f"{name}.lock")
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                return name
    return ""


def probe(url, timeout):
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
            code = r.status
    except Exception as exc:  # noqa: BLE001
        code = f"ERR {type(exc).__name__}"
    return (time.monotonic() - t0) * 1000, code


def duration(s):
    unit = s[-1]
    n = float(s[:-1])
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def pct(values, p):
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--duree", default="1h")
    ap.add_argument("--intervalle", type=float, default=10)
    ap.add_argument("--lock-dir", default="/run/odooguard")
    ap.add_argument("--csv", default="impact.csv")
    a = ap.parse_args()
    end = time.time() + duration(a.duree)
    rows = []
    with open(a.csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["horodatage", "ms", "code", "sauvegarde_en_cours"])
        while time.time() < end:
            ms, code = probe(a.url, 30)
            running = backup_running(a.lock_dir)
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), f"{ms:.0f}", code, running])
            fh.flush()
            rows.append((ms, code, running))
            time.sleep(a.intervalle)
    for label, sel in (("sans sauvegarde", [r for r in rows if not r[2]]),
                       ("pendant une sauvegarde", [r for r in rows if r[2]])):
        ok = [r[0] for r in sel if r[1] == 200]
        errors = len(sel) - len(ok)
        print(f"{label:<24} n={len(sel):5d}  médiane={statistics.median(ok) if ok else float('nan'):7.0f} ms"
              f"  p95={pct(ok, 95):7.0f} ms  erreurs={errors}")


if __name__ == "__main__":
    main()
