"""X2 guard: default/weak credentials are detected, strong ones pass, prod mode refuses to start."""

from __future__ import annotations

import preflight

EXAMPLE = {"DB_PASSWORD": "local_db", "API_SECRET_KEY": "", "KAGGLE_KEY": "", "DB_PORT": "5432"}
STRONG = "q7Vb2xK9pLm4Zt8RwY3n"


def test_example_values_and_defaults_are_flagged():
    config = {"DB_PASSWORD": "local_db", "API_SECRET_KEY": "admin", "SUPERSET_ADMIN": "admin"}
    found = preflight.secret_findings(config, EXAMPLE)
    assert any("DB_PASSWORD: still the .env.example value" in f for f in found)
    assert any("API_SECRET_KEY: a well-known default" in f for f in found)
    assert any("SUPERSET_ADMIN: default admin user name" in f for f in found)


def test_unset_short_and_placeholder_are_flagged():
    found = preflight.secret_findings({"DB_PASSWORD": "dev_only_x" * 3}, EXAMPLE)
    assert "DB_PASSWORD: a dev placeholder" in found
    assert "API_SECRET_KEY: unset" in found
    assert preflight.secret_findings(
        {"DB_PASSWORD": "short", "API_SECRET_KEY": STRONG}, EXAMPLE
    ) == [f"DB_PASSWORD: shorter than {preflight.MIN_LENGTH} characters"]


def test_strong_config_passes_and_optional_may_be_unset():
    assert (
        preflight.secret_findings({"DB_PASSWORD": STRONG, "API_SECRET_KEY": STRONG[::-1]}, EXAMPLE)
        == []
    )


def test_compose_literals_are_flagged(tmp_path):
    f = tmp_path / preflight.COMPOSE_FILES[0]
    f.parent.mkdir(parents=True)
    f.write_text(
        "    environment:\n      MYSQL_PASSWORD: datahub\n      POSTGRES_PASSWORD: ${PW}\n",
        encoding="utf-8",
    )
    found = preflight.compose_findings(tmp_path)
    assert len(found) == 1 and "MYSQL_PASSWORD is a literal" in found[0]


def test_prod_mode_refuses_the_shipped_defaults(monkeypatch):
    monkeypatch.setattr(
        preflight, "parse_env_file", lambda p: {"OLTP_SUPERUSER_PASSWORD": "local_x"}
    )
    monkeypatch.setenv("DGP_ENV", "prod")
    assert preflight.main([]) == 1
    monkeypatch.setenv("DGP_ENV", "local")
    assert preflight.main([]) == 0
