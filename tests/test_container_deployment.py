"""Guard the acceptance runner's isolation from real deployment data."""
import json
import sys

import pytest

from scripts import check_container_deployment as check


def source_config():
    return {"services": {
        "backend": {"image": "original", "build": {}, "env_file": [".env"],
                    "ports": ["8010:8010"], "environment": {"ORDER_DB_DSN": "mysql://production.example/real"},
                    "volumes": [{"type": "bind", "source": "real-data", "target": "/app/data"},
                                {"type": "bind", "source": "real-model", "target": "/app/model", "read_only": True}]},
        "frontend": {"build": {"args": {}}, "ports": ["5173:80"]},
        "mysql": {"environment": {}, "volumes": [
            {"type": "bind", "source": "real-db", "target": "/var/lib/mysql"}]},
        "optional": {"profiles": ["monitoring"]},
    }}


def test_acceptance_never_mounts_real_data_or_loads_env_file(monkeypatch):
    commands = []

    def capture(*args, **kwargs):
        commands.append(args)
        return json.dumps(source_config())

    monkeypatch.setattr(check, "run", capture)
    config = check.isolated_config("isolated", "built-image", True)
    assert "--no-env-resolution" in commands[0] and "--no-interpolate" in commands[0]
    assert "optional" not in config["services"]
    for service in config["services"].values():
        assert "env_file" not in service and "ports" not in service
        assert all(mount["type"] == "volume" for mount in service.get("volumes", []))
    backend = config["services"]["backend"]
    assert backend["image"] == "built-image"
    assert backend["environment"]["ORDER_DB_DSN"] == "mysql://ecom:ecom_dev_password@mysql:3306/ecom_agent_orders"
    assert backend["environment"]["COMMERCE_INVOICE_ROOT"] == "/app/data/demo-invoices"
    assert set(config["volumes"]) == {"app", "mysql"}


def test_acceptance_refuses_unrecognized_writable_bind(monkeypatch):
    config = source_config()
    config["services"]["backend"]["volumes"].append(
        {"type": "bind", "source": "important", "target": "/unknown"})
    monkeypatch.setattr(check, "run", lambda *args, **kwargs: json.dumps(config))
    with pytest.raises(RuntimeError, match="Unrecognized writable mount"):
        check.isolated_config("isolated")


@pytest.mark.parametrize("applied", [False, True])
def test_migration_check_exit_code_matches_status(monkeypatch, capsys, applied):
    from scripts import migrate_mysql_business_schema as migration

    monkeypatch.setattr(sys, "argv", ["migration", "--dsn", "mysql://unused/db", "--check"])

    def migrate(dsn, *, check):
        assert dsn == "mysql://unused/db" and check
        return applied

    monkeypatch.setattr(migration, "migrate", migrate)
    if applied:
        migration.main()
    else:
        with pytest.raises(SystemExit) as error:
            migration.main()
        assert error.value.code == 1
    assert ("applied" if applied else "pending") in capsys.readouterr().out
