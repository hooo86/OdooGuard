import os

import pytest

from odooguard import config as config_mod


def test_example_config_loads(cfg):
    assert cfg.source.host == "192.168.8.80"
    assert cfg.source.is_production is True
    assert cfg.databases == ["LNHC_Prod_2025", "LNHC_08_07_26"]
    assert "/etc/odoo" in cfg.file_sources
    assert cfg.stanza == "lnhc"
    assert cfg.max_age["wal"] == 600
    assert set(cfg.destinations) == {"nas1", "nas2"}
    assert cfg.target("testvm").is_production is False
    assert cfg.target("odoo") is cfg.source


def test_unknown_target(cfg):
    with pytest.raises(config_mod.ConfigError):
        cfg.target("inconnue")


def test_permissions_enforced(cfg):
    os.chmod(cfg.path, 0o644)
    with pytest.raises(config_mod.ConfigError, match="chmod 600"):
        config_mod.load(cfg.path)


def test_env_override(cfg, monkeypatch):
    monkeypatch.setenv("ODOOGUARD_ODOO_HOST", "10.0.0.9")
    monkeypatch.setenv("ODOOGUARD_DESTINATION_NAS2_ENABLED", "no")
    c = config_mod.load(cfg.path)
    assert c.source.host == "10.0.0.9"
    assert c.destinations["nas2"].enabled is False


def test_missing_file():
    with pytest.raises(config_mod.ConfigError):
        config_mod.load("/nexiste/pas.conf")


def test_no_secret_in_example():
    root = os.path.dirname(os.path.dirname(__file__))
    for dirpath, _, files in os.walk(os.path.join(root, "conf")):
        for f in files:
            text = open(os.path.join(dirpath, f), encoding="utf-8").read().lower()
            assert "password=" not in text.replace(" ", "")
            if "cipher-pass" in text:
                assert "@cipher_pass@" in text
