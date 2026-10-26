"""Where the repository is, and how its YAML files are read.

Every tool runs against a checkout of this repository. ``DGP_REPO_ROOT`` names it explicitly (the
Airflow image mounts it at ``/opt/airflow/repo``; a test points it at a temporary copy); otherwise
it is the nearest directory, from the current one up, that holds ``governance/domains/`` — so a
command works from any folder inside the checkout, the way ``git`` does.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT_ENV = "DGP_REPO_ROOT"
_MARKER = Path("governance") / "domains"


def root() -> Path:
    """The repository checkout this process works on."""
    if configured := os.environ.get(ROOT_ENV):
        return Path(configured).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / _MARKER).is_dir():
            return candidate
    raise RuntimeError(
        f"not inside the repository (no {_MARKER}/ above {here}); run from the checkout or set "
        f"{ROOT_ENV}"
    )


def load_yaml(path: Path) -> dict:
    """A YAML mapping from ``path``; an empty file reads as ``{}``."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def relative(path: Path, base: Path | None = None) -> str:
    """``path`` as written in messages: relative to the repository, with forward slashes."""
    base = (base or root()).resolve()
    resolved = Path(path).resolve()
    return resolved.relative_to(base).as_posix() if resolved.is_relative_to(base) else str(path)


def domain_of(path: Path, base: Path | None = None) -> str:
    """The domain a file belongs to, from where it lives (``domains/<domain>/…``)."""
    parts = Path(path).resolve().relative_to((base or root()).resolve()).parts
    if len(parts) < 2 or parts[0] != "domains":
        raise ValueError(f"{path} is not inside domains/<domain>/")
    return parts[1]
