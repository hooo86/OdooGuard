import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odooguard import config as config_mod  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    """Configuration d'exemple pointant vers un répertoire temporaire."""
    src = os.path.join(ROOT, "conf", "odooguard.conf.example")
    text = open(src, encoding="utf-8").read()
    for old in ("/srv/odooguard", "/var/lib/odooguard", "/var/log/odooguard", "/run/odooguard",
                "/mnt/nas-odoo", "/mnt/nas-backups"):
        text = text.replace(old, str(tmp_path) + old)
    path = tmp_path / "odooguard.conf"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    c = config_mod.load(str(path))
    for d in (c.repo_path, c.wal_spool, c.mirror_dir, c.staging_dir, c.state_dir, c.restic_repo):
        os.makedirs(d, exist_ok=True)
    return c


@pytest.fixture
def fixture_path():
    return lambda name: os.path.join(ROOT, "tests", "fixtures", name)


def have(binary):
    return shutil.which(binary) is not None
