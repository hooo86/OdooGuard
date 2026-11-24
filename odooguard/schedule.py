"""Planification des sauvegardes depuis le tableau de bord.

Les sauvegardes restent déclenchées par les minuteurs systemd (indépendants du
tableau de bord). Ce module :
* LIT la planification actuelle (unité d'origine + fichier complémentaire), l'état
  activé/suspendu et la prochaine exécution — sans aucun droit particulier ;
* construit une expression systemd (OnCalendar) à partir d'un formulaire simple ;
* APPLIQUE un changement via odooguard-schedule-helper (root par sudo, liste blanche).
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from odooguard.util import OdooGuardError, run

HELPER = "/usr/local/sbin/odooguard-schedule-helper"
UNIT_DIR = "/etc/systemd/system"
DROPIN = "odooguard-planification.conf"

# minuteur -> (libellé, description, rôle pour modifier)
TIMERS = {
    "odooguard-backup-db-full.timer": ("Base : complète", "copie intégrale de la base (référence de restauration)"),
    "odooguard-backup-db-diff.timer": ("Base : différentielle", "changements depuis la dernière complète"),
    "odooguard-backup-db-incr.timer": ("Base : incrémentale", "changements depuis la dernière sauvegarde (raccourcit les restaurations)"),
    "odooguard-backup-files.timer": ("Fichiers", "filestore, modules, configuration (perte max. = intervalle)"),
    "odooguard-copy.timer": ("Copie vers les NAS", "recopie vérifiée des dépôts sur chaque NAS"),
    "odooguard-verify-quick.timer": ("Vérification rapide", "contrôle des dépôts et échantillon de relecture"),
    "odooguard-verify-full.timer": ("Vérification complète", "relecture intégrale (longue)"),
    "odooguard-maintenance.timer": ("Maintenance", "rétention restic (suppression des anciens instantanés)"),
    "odooguard-restore-test.timer": ("Test de restauration", "restauration complète contrôlée sur la VM de test"),
}
DAYS = (("Mon", "lun."), ("Tue", "mar."), ("Wed", "mer."), ("Thu", "jeu."), ("Fri", "ven."),
        ("Sat", "sam."), ("Sun", "dim."))
INTERVALS = {"15min": "*-*-* *:0/15:00", "20min": "*-*-* *:0/20:00", "30min": "*-*-* *:0/30:00",
             "1h": "*-*-* *:{m:02d}:00", "2h": "*-*-* 0/2:{m:02d}:00", "3h": "*-*-* 0/3:{m:02d}:00",
             "4h": "*-*-* 0/4:{m:02d}:00", "6h": "*-*-* 0/6:{m:02d}:00", "12h": "*-*-* 0/12:{m:02d}:00"}
SPEC_RE = re.compile(r"^[A-Za-z0-9 ,.:/*~-]{1,100}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _oncalendar(path: str) -> list[str] | None:
    """Expressions OnCalendar d'un fichier (None si le fichier n'en définit pas)."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return None
    specs: list[str] | None = None
    for line in lines:
        line = line.strip()
        if line.startswith("OnCalendar="):
            val = line.split("=", 1)[1].strip()
            specs = [] if not val else (specs or []) + [val]
    return specs


def current(timer: str, unit_dir: str = UNIT_DIR) -> dict:
    base = _oncalendar(os.path.join(unit_dir, timer)) or []
    override = _oncalendar(os.path.join(unit_dir, timer + ".d", DROPIN))
    specs = override if override else base
    enabled = os.path.exists(os.path.join(unit_dir, "timers.target.wants", timer))
    return {"timer": timer, "label": TIMERS[timer][0], "description": TIMERS[timer][1],
            "specs": specs, "default_specs": base, "customized": bool(override),
            "enabled": enabled, "installed": os.path.exists(os.path.join(unit_dir, timer)),
            "next": next_run(specs)}


def next_run(specs: list[str]) -> str | None:
    """Prochaine échéance (texte systemd) parmi plusieurs expressions."""
    best = None
    for spec in specs:
        try:
            r = subprocess.run(["systemd-analyze", "calendar", spec], capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        m = re.search(r"Next elapse:\s*\w+\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", r.stdout)
        if m and (best is None or m.group(1) < best):
            best = m.group(1)
    return best


def all_timers(unit_dir: str = UNIT_DIR) -> list[dict]:
    return [current(t, unit_dir) for t in TIMERS]


def build_specs(p: dict) -> list[str]:
    """Formulaire -> expressions OnCalendar."""
    mode = p.get("mode")
    days = [d for d, _ in DAYS if p.get(f"day_{d}")]
    if mode in ("times", "hourly") and not days:
        raise OdooGuardError("choisir au moins un jour", "usage")
    prefix = "" if len(days) == 7 else ",".join(days) + " "
    if mode == "times":
        times = [t.strip() for t in re.split(r"[,;\s]+", str(p.get("times", ""))) if t.strip()]
        if not 1 <= len(times) <= 8:
            raise OdooGuardError("indiquer de 1 à 8 heures, ex. 01:00, 13:30", "usage")
        specs = []
        for t in times:
            m = TIME_RE.match(t)
            if not m:
                raise OdooGuardError(f"heure invalide : {t} (format HH:MM)", "usage")
            specs.append(f"{prefix}*-*-* {int(m.group(1)):02d}:{m.group(2)}:00")
        return specs
    if mode == "hourly":
        try:
            h1, h2, mi = int(p.get("from_hour")), int(p.get("to_hour")), int(p.get("minute", 0))
        except (TypeError, ValueError):
            raise OdooGuardError("plage horaire invalide", "usage") from None
        if not (0 <= h1 <= h2 <= 23 and 0 <= mi <= 59):
            raise OdooGuardError("plage horaire invalide (0 ≤ début ≤ fin ≤ 23)", "usage")
        return [f"{prefix}*-*-* {h1:02d}..{h2:02d}:{mi:02d}:00"]
    if mode == "interval":
        key = p.get("interval")
        if key not in INTERVALS:
            raise OdooGuardError("intervalle invalide", "usage")
        try:
            mi = int(p.get("minute", 0))
        except (TypeError, ValueError):
            mi = 0
        return [INTERVALS[key].format(m=max(0, min(59, mi)))]
    if mode == "advanced":
        specs = [s.strip() for s in str(p.get("advanced", "")).splitlines() if s.strip()]
        if not 1 <= len(specs) <= 8:
            raise OdooGuardError("de 1 à 8 expressions, une par ligne", "usage")
        for s in specs:
            if not SPEC_RE.match(s):
                raise OdooGuardError(f"expression refusée : {s}", "usage")
        return specs
    raise OdooGuardError("mode de planification inconnu", "usage")


def validate_specs(specs: list[str]) -> list[str]:
    for s in specs:
        r = subprocess.run(["systemd-analyze", "calendar", s], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise OdooGuardError(f"expression invalide pour systemd : {s}", "usage")
    return specs


def helper(*args: str) -> dict:
    res = run(["sudo", "-n", HELPER, *args], check=False, timeout=120)
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("{")]
    data = json.loads(lines[-1]) if lines else {}
    if res.returncode != 0 or not data.get("ok"):
        raise OdooGuardError("planification : " + (data.get("error") or (res.stderr or res.stdout)[-300:]),
                             "command")
    return data


def job_schedule(params: dict) -> dict:
    timer = params.get("timer")
    if timer not in TIMERS:
        raise OdooGuardError("minuteur inconnu", "usage")
    label = TIMERS[timer][0]
    action = params.get("action")
    if action == "set":
        specs = validate_specs([str(s) for s in params.get("specs", [])])
        helper("set", timer, *specs)
        if params.get("enable"):
            helper("enable", timer)
        return {"timer": timer, "specs": specs, "next": next_run(specs),
                "message": f"{label} : planification enregistrée ({' ; '.join(specs)})"}
    if action == "reset":
        helper("reset", timer)
        return {"timer": timer, "message": f"{label} : planification d'origine rétablie"}
    if action in ("enable", "disable"):
        helper(action, timer)
        return {"timer": timer, "message": f"{label} : {'activée' if action == 'enable' else 'suspendue'}"}
    raise OdooGuardError("action inconnue", "usage")
