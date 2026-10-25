#!/usr/bin/env python3
"""No-default-credential guard — production-readiness X2.

Every default admin user/password must be replaced at deploy time, and startup must FAIL if a known
default is detected. This inspects the deployment's configuration and reports every secret that is:

  * unset (empty),
  * still the value shipped in ``.env.example``,
  * a well-known default (``admin``, ``postgres``, ``datahub``, ``minioadmin``, …),
  * a dev placeholder (``local_…``, ``dev_only…``), or
  * shorter than 16 characters,

plus default admin user names and any password hard-coded as a literal in a compose file (the
DataHub quickstart compose carries ``datahub``/``datahub`` — a dev artefact; production deploys
DataHub with its Helm chart and secrets from the secrets manager).

Two modes:
  * local (default): prints the findings as a warning and exits 0 — dev defaults are expected on a
    laptop. ``make up`` runs it so they are never invisible.
  * production (``DGP_ENV=prod`` or ``--prod``): any finding exits 1 — the deploy pipeline runs this
    before rollout, so the platform refuses to start with a default credential.

Values come from ``.env`` overlaid by the process environment (where a secrets manager injects
them).
Stdlib only, so it runs before anything is installed. Run: ``make preflight``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET_KEY = re.compile(r"(PASSWORD|SECRET|_KEY|TOKEN|SALT)")
KNOWN_DEFAULTS = {
    "admin", "password", "postgres", "datahub", "minioadmin", "airflow", "superset",
    "changeme", "change_me", "secret", "root", "test", "johnsnowknowsnothing",
}  # fmt: skip
DEV_MARKERS = ("local_", "dev_only")
MIN_LENGTH = 16
DEFAULT_USERS = {"SUPERSET_ADMIN": {"admin"}, "MINIO_ROOT_USER": {"minioadmin", "minio_admin"}}
# Optional integrations: unset is fine (the feature is just off), but a set value must be strong.
OPTIONAL = {"KAGGLE_KEY"}
COMPOSE_FILES = [
    "platform/core/infra/docker-compose.yml",
    "platform/core/orchestration/docker-compose.airflow.yml",
    "platform/context/catalog/docker-compose.datahub.yml",
    "reporting/docker-compose.superset.yml",
]
LITERAL_SECRET = re.compile(
    r"^\s*-?\s*([A-Z0-9_]*(?:PASSWORD|SECRET)[A-Z0-9_]*)\s*[:=]\s*(\S+)\s*$"
)


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def secret_findings(config: dict[str, str], example: dict[str, str]) -> list[str]:
    """Every weak/default secret or default admin user name in ``config``."""
    findings: list[str] = []
    for key in sorted(k for k in example if SECRET_KEY.search(k)):
        value = config.get(key, "")
        if not value:
            if key not in OPTIONAL:
                findings.append(f"{key}: unset")
            continue
        if example.get(key) and value == example[key]:
            findings.append(f"{key}: still the .env.example value")
        elif value.lower() in KNOWN_DEFAULTS:
            findings.append(f"{key}: a well-known default ({value!r})")
        elif value.lower().startswith(DEV_MARKERS):
            findings.append(f"{key}: a dev placeholder")
        elif len(value) < MIN_LENGTH:
            findings.append(f"{key}: shorter than {MIN_LENGTH} characters")
    for key, defaults in DEFAULT_USERS.items():
        if config.get(key, "").lower() in defaults:
            findings.append(f"{key}: default admin user name ({config[key]!r})")
    return findings


def compose_findings(root: Path = REPO_ROOT) -> list[str]:
    """Passwords/secrets written as literals (not ${VAR}) in a compose file."""
    findings: list[str] = []
    for rel in COMPOSE_FILES:
        path = root / rel
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = LITERAL_SECRET.match(line)
            if m and "$" not in m.group(2):
                findings.append(f"{rel}:{n}: {m.group(1)} is a literal in the compose file")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prod", action="store_true", help="fail on any finding (or DGP_ENV=prod)")
    args = parser.parse_args(argv)
    prod = args.prod or os.environ.get("DGP_ENV", "").lower() in {"prod", "production"}

    example = parse_env_file(REPO_ROOT / ".env.example")
    config = {**parse_env_file(REPO_ROOT / ".env"), **os.environ}
    findings = secret_findings(config, example) + compose_findings()

    if not findings:
        print("preflight: no default or weak credentials detected (X2)")
        return 0
    if prod:
        print(
            f"preflight FAILED — refusing to start with {len(findings)} "
            "default/weak credential(s) (X2):"
        )
        for f in findings:
            print(f"  - {f}")
        return 1
    print(
        f"preflight: {len(findings)} dev default credential(s) in use — fine on a laptop; "
        "DGP_ENV=prod refuses to start with any of them (X2, `make preflight` lists them)."
    )
    if os.environ.get("PREFLIGHT_VERBOSE"):
        for f in findings:
            print(f"  - {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
