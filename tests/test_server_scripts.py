"""Tests des scripts installés sur server01 : filtrage des commandes SSH."""
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "server", "odooguard-pgbr-gate")
AGENT = os.path.join(ROOT, "server", "odooguard-agent")

OK_PG = ("pgbackrest --config=/etc/pgbackrest/pgbackrest.conf --exec-id=2219-f28da5a6 "
         "--log-level-console=off --log-level-file=off --log-level-stderr=error "
         "--pg1-path=/var/lib/postgresql/15/main --process=0 --remote-type=pg --stanza=lnhc backup:remote")


def gate(role, cmd):
    env = {"PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": cmd}
    return subprocess.run([GATE, role], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=20)


@pytest.mark.parametrize("cmd", [
    "bash -i",
    "pgbackrest --stanza=lnhc --remote-type=pg expire:remote",           # commande non autorisée
    "pgbackrest --stanza=lnhc --remote-type=pg stanza-delete:remote",
    OK_PG + "; rm -rf /",
    OK_PG.replace("backup:remote", "backup:remote && id"),
    OK_PG.replace("--remote-type=pg", "--remote-type=repo"),              # mauvais rôle
    "pgbackrest --stanza=$(id) --remote-type=pg backup:remote",
    "pgbackrest --stanza=lnhc --remote-type=pg backup:remote\nid",
    "",
])
def test_gate_refuses(cmd):
    r = gate("pg", cmd)
    assert r.returncode != 0
    assert "refusé" in r.stderr


def test_gate_repo_role_refuses_backup():
    r = gate("repo", OK_PG.replace("--remote-type=pg", "--remote-type=repo"))
    assert r.returncode != 0 and "refusé" in r.stderr


def test_gate_unknown_role():
    assert gate("root", OK_PG).returncode != 0


@pytest.mark.skipif(not os.path.exists("/usr/bin/pgbackrest"), reason="pgbackrest absent")
def test_gate_accepts_pgbackrest_remote():
    env = {"PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": OK_PG}
    r = subprocess.run([GATE, "pg"], env=env, capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=20)
    # Transmis à pgBackRest, qui répond avec son protocole distant
    assert b"refus" not in r.stderr
    assert b'"service":"remote"' in r.stdout


def agent(cmd):
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SSH_ORIGINAL_COMMAND": cmd}
    return subprocess.run(["bash", AGENT], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=20)


@pytest.mark.parametrize("cmd,msg", [
    ("db-exists LNHC;drop", "caractères interdits"),
    ("db-exists $(id)", "caractères interdits"),
    ("db-exists 'x'", "caractères interdits"),
    ("shell", "verbe inconnu"),
    ("db-import -bad", "nom de base invalide"),
    ("pg-control 15/../.. main", "version PostgreSQL invalide"),
    ("pg-control 15 ../main", "nom d'instance invalide"),
    ("fs-import /etc/shadow", "chemin non autorisé"),
    ("pg-restore-cluster 15 main lnhc 2026-11-22", "horodatage invalide"),
])
def test_agent_refuses(cmd, msg):
    r = agent(cmd)
    assert r.returncode != 0
    assert msg in r.stderr


def test_scripts_syntax():
    for f in ("odooguard-agent", "odooguard-pgbr-gate", "odooguard-export"):
        subprocess.run(["bash", "-n", os.path.join(ROOT, "server", f)], check=True)
