"""Confirmation et journalisation des opérations destructives.

Règle : toute opération qui écrase, renomme ou supprime des données (base,
filestore, sauvegarde) doit :
  1. être demandée explicitement avec ``--force`` ;
  2. être confirmée en retapant le nom exact de l'objet visé (mode interactif),
     ou en passant ``--confirm <nom exact>`` (mode non interactif) ;
  3. être tracée AVANT et APRÈS exécution dans le journal d'audit
     (fichier ``audit.log`` en ajout seul + table SQLite ``audit``) et le journal système.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import sys
import syslog
import time

from odooguard.history import History
from odooguard.util import OdooGuardError

log = logging.getLogger("odooguard")


def actor_name() -> str:
    """Auteur réel : utilisateur web (travaux), sinon utilisateur sudo, sinon compte courant."""
    web = os.environ.get("ODOOGUARD_ACTOR")
    if web:
        return f"web:{web}"
    return os.environ.get("SUDO_USER") or getpass.getuser()


class Auditor:
    def __init__(self, history: History, audit_log: str):
        self.history = history
        self.audit_log = audit_log

    def record(self, action: str, target: str, outcome: str, /, **details) -> None:
        actor = actor_name()
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "actor": actor,
            "action": action,
            "target": target,
            "outcome": outcome,
            **details,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            os.makedirs(os.path.dirname(self.audit_log), exist_ok=True)
            # Fichier protégé par « chattr +a » à l'installation : ajout seul.
            with open(self.audit_log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            log.error("impossible d'écrire le journal d'audit %s : %s", self.audit_log, exc)
        syslog.openlog("odooguard-audit", 0, syslog.LOG_AUTH)
        syslog.syslog(syslog.LOG_NOTICE, line)
        self.history.audit(action, target, outcome, details)


def confirm_destructive(*, action: str, expected: str, force: bool, confirm: str | None,
                        description: str) -> None:
    """Bloque tant que l'exploitant n'a pas confirmé explicitement.

    ``expected`` est le texte exact à retaper (nom de la base, étiquette...).
    """
    if not force:
        raise OdooGuardError(
            f"{description}\nOpération destructive : relancer avec --force.", "usage"
        )
    if confirm is not None:
        if confirm != expected:
            raise OdooGuardError(
                f"confirmation refusée : « {confirm} » ≠ « {expected} »", "usage"
            )
        return
    if not sys.stdin.isatty():
        raise OdooGuardError(
            f"{description}\nMode non interactif : ajouter --confirm '{expected}'.", "usage"
        )
    print(f"\n⚠  {description}", file=sys.stderr)
    print(f"Pour confirmer « {action} », retapez exactement : {expected}", file=sys.stderr)
    answer = input("> ").strip()
    if answer != expected:
        raise OdooGuardError("confirmation incorrecte : opération annulée", "usage")
