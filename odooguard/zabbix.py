"""Indicateurs OdooGuard pour Zabbix (agent 2, UserParameter).

Lit uniquement la copie PUBLIQUE de l'état (``/var/lib/odooguard-public/status.json``,
0644, écrite toutes les 5 min par ``odooguard status --record``). Ce script est
autonome : il n'a besoin ni de la configuration (600), ni d'aucun secret, ni du
réseau, et répond en quelques millisecondes. L'agent Zabbix tourne donc sous
son propre compte sans aucun droit sur les sauvegardes.

Usage :
  odooguard-zabbix discovery                  -> découverte bas niveau (LLD) des composants
  odooguard-zabbix overall                    -> 0 ok, 1 avertissement, 2 critique, 3 état périmé/absent
  odooguard-zabbix age                        -> âge (s) de status.json (le contrôle tourne-t-il ?)
  odooguard-zabbix rpo db|files               -> perte de données max. actuelle (s)
  odooguard-zabbix component <nom> level|age|size|duration|message
"""

from __future__ import annotations

import json
import os
import sys
import time

DEFAULT = os.environ.get("ODOOGUARD_PUBLIC_STATUS", "/var/lib/odooguard-public/status.json")
LEVEL_NUM = {"ok": 0, "warning": 1, "critical": 2, "unknown": 3}
STALE_AFTER = 20 * 60  # le contrôle tourne toutes les 5 min


def load(path: str = DEFAULT) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _num(v) -> str:
    if v is None:
        return "-1"  # « jamais » / inconnu : valeur numérique explicite pour les déclencheurs
    return str(int(v)) if float(v).is_integer() else f"{float(v):.1f}"


def answer(args: list[str], status: dict | None, now: float | None = None) -> str:
    now = now or time.time()
    if not args:
        raise SystemExit(__doc__)
    cmd = args[0]
    if cmd == "age":
        return _num(now - status["generated_at"]) if status else "-1"
    if status is None or now - status.get("generated_at", 0) > STALE_AFTER:
        # État absent ou périmé : on ne renvoie jamais un « ok » trompeur.
        if cmd == "overall":
            return "3"
        if cmd == "discovery":
            return json.dumps({"data": []})
        if cmd == "component" and len(args) > 2 and args[2] == "level":
            return "3"
        return "-1"
    comps = {c["name"]: c for c in status.get("components", [])}
    if cmd == "discovery":
        return json.dumps({"data": [{"{#COMPONENT}": n, "{#LABEL}": c.get("label", n)}
                                    for n, c in comps.items()]}, ensure_ascii=False)
    if cmd == "overall":
        return str(LEVEL_NUM.get(status.get("overall"), 3))
    if cmd == "rpo":
        key = {"db": "db_seconds", "files": "files_seconds"}.get(args[1] if len(args) > 1 else "")
        if not key:
            raise SystemExit("rpo db|files")
        return _num((status.get("rpo") or {}).get(key))
    if cmd == "component":
        if len(args) < 3:
            raise SystemExit("component <nom> level|age|size|duration|message")
        c = comps.get(args[1])
        field = args[2]
        if c is None:
            return "3" if field == "level" else "-1"
        if field == "level":
            return str(LEVEL_NUM.get(c.get("level"), 3))
        if field == "message":
            return (c.get("message") or "")[:250]
        if field in ("age", "size", "duration"):
            return _num(c.get(field))
        raise SystemExit(f"champ inconnu : {field}")
    raise SystemExit(f"commande inconnue : {cmd}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = DEFAULT
    if argv[:1] == ["--file"]:
        path, argv = argv[1], argv[2:]
    print(answer(argv, load(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
