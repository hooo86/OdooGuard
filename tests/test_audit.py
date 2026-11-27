import json

import pytest

from odooguard.audit import Auditor, confirm_destructive
from odooguard.history import History
from odooguard.util import OdooGuardError


def test_requires_force():
    with pytest.raises(OdooGuardError, match="--force"):
        confirm_destructive(action="x", expected="LNHC_Prod_2025", force=False, confirm=None,
                            description="d")


def test_wrong_confirmation():
    with pytest.raises(OdooGuardError, match="refusée"):
        confirm_destructive(action="x", expected="LNHC_Prod_2025", force=True,
                            confirm="LNHC_Prod", description="d")


def test_good_confirmation():
    confirm_destructive(action="x", expected="LNHC_Prod_2025", force=True,
                        confirm="LNHC_Prod_2025", description="d")


def test_non_interactive_without_confirm(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(OdooGuardError, match="--confirm"):
        confirm_destructive(action="x", expected="A", force=True, confirm=None, description="d")


def test_audit_written(tmp_path):
    h = History(str(tmp_path / "h.db"))
    a = Auditor(h, str(tmp_path / "audit.log"))
    a.record("restore-db", "odoo:TEST", "started", db="TEST")
    line = json.loads((tmp_path / "audit.log").read_text().strip())
    assert line["action"] == "restore-db" and line["outcome"] == "started"
