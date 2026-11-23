import pytest

from odooguard import cli


def test_parser_commands():
    p = cli.build_parser()
    a = p.parse_args(["backup", "db", "--type", "full"])
    assert a.what == "db" and a.type == "full"
    a = p.parse_args(["restore", "db", "--db", "LNHC_Prod_2025", "--as", "TEST",
                      "--time", "hier 14:30", "--target", "testvm"])
    assert a.as_name == "TEST" and a.neutralize == "auto"
    a = p.parse_args(["status", "--check", "--record"])
    assert a.check and a.record


def test_restore_cluster_requires_target():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["restore", "cluster"])


def test_main_config_error(tmp_path, capsys):
    rc = cli.main(["--config", str(tmp_path / "absent.conf"), "status"])
    assert rc == 3
    assert "configuration" in capsys.readouterr().err


def test_restore_cluster_without_force_is_refused(cfg, capsys):
    rc = cli.main(["--config", cfg.path, "restore", "cluster", "--target", "testvm"])
    assert rc == 3
    assert "--force" in capsys.readouterr().err
