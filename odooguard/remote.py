"""Appels à l'agent OdooGuard installé sur les serveurs Odoo (server01, VM de test).

Côté serveur Odoo, la clé SSH d'OdooGuard est forcée sur
``/usr/local/sbin/odooguard-agent`` (``command=`` dans authorized_keys) :
seuls les verbes de la liste blanche de l'agent sont exécutables, avec des
arguments validés par expression régulière. Aucun shell n'est ouvert.

Sens des flux : OdooGuard -> serveur Odoo uniquement. Le serveur Odoo ne
détient aucune clé lui permettant d'écrire sur OdooGuard.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import IO

from odooguard.config import Target
from odooguard.util import CommandError, Result, run, ssh_base


class Agent:
    def __init__(self, target: Target):
        self.target = target

    def _cmd(self, verb: str, *args: str) -> list[str]:
        # La ligne de commande est transmise à l'agent via SSH_ORIGINAL_COMMAND ;
        # l'agent la redécoupe et valide chaque argument.
        remote = shlex.join([verb, *args])
        return ssh_base(self.target.host, self.target.ssh_user, self.target.ssh_key) + [remote]

    def call(self, verb: str, *args: str, timeout: float | None = 3600, check: bool = True) -> Result:
        return run(self._cmd(verb, *args), timeout=timeout, check=check)

    def call_json(self, verb: str, *args: str, timeout: float | None = 600) -> dict:
        res = self.call(verb, *args, timeout=timeout)
        lines = [l for l in res.stdout.splitlines() if l.startswith("{")]
        return json.loads(lines[-1]) if lines else {}

    def stream_in(self, verb: str, *args: str, source: IO[bytes]) -> Result:
        """Envoie un flux (pg_dump, tar) sur l'entrée standard de l'agent."""
        cmd = self._cmd(verb, *args)
        proc = subprocess.run(cmd, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = Result(proc.returncode, proc.stdout.decode(errors="replace"),
                     proc.stderr.decode(errors="replace"), 0)
        if proc.returncode != 0:
            raise CommandError(cmd, proc.returncode, res.stderr or res.stdout)
        return res

    def rsync_source(self, path: str) -> str:
        """Chemin source rsync (« root@hôte:/chemin »), lu via « rrsync -ro »."""
        return f"{self.target.ssh_user}@{self.target.host}:{path}"

    def rsync_ssh(self) -> str:
        return shlex.join(ssh_base(self.target.host, self.target.ssh_user, self.target.ssh_key)[:-1])

    def call_bytes(self, verb: str, *args: str, timeout: float = 120) -> bytes:
        cmd = self._cmd(verb, *args)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if proc.returncode != 0:
            raise CommandError(cmd, proc.returncode, proc.stderr.decode(errors="replace"))
        return proc.stdout
