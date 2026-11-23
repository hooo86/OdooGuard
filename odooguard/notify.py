"""Notifications.

Phase 1 : canal « journal » uniquement (journald + historique SQLite), branché
sur ``OnFailure=`` des unités systemd et sur ``odooguard status --check``.
Phase 2 : canaux e-mail et Telegram ajoutés ici, sans changer les appelants.
"""

from __future__ import annotations

import logging
import syslog

log = logging.getLogger("odooguard")

LEVEL_TO_SYSLOG = {"critical": syslog.LOG_CRIT, "warning": syslog.LOG_WARNING,
                   "ok": syslog.LOG_INFO}


def send(level: str, title: str, body: str = "") -> None:
    syslog.openlog("odooguard-alert", 0, syslog.LOG_DAEMON)
    syslog.syslog(LEVEL_TO_SYSLOG.get(level, syslog.LOG_ERR), f"[{level.upper()}] {title} {body}"[:4000])
    log.log(logging.ERROR if level == "critical" else logging.WARNING, "%s %s", title, body)
