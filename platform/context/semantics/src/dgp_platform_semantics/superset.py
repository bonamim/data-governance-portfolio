"""A small Superset REST client: just what the semantic layer's BI adapter and its proof need.

Superset is where BI users meet the governed metrics; ``publish_superset.py`` writes them there from
the Ossie models, and proof P10 reads them back to show BI and AI agree. Both talk to Superset
through this client, so login and lookups are written once.
"""

from __future__ import annotations

import os

import requests

TIMEOUT = 30


class Superset:
    """An authenticated session against the Superset API (``SUPERSET_URL``, admin credentials)."""

    def __init__(
        self,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.url = (url or os.environ.get("SUPERSET_URL", "http://localhost:8088")).rstrip("/")
        self.session = requests.Session()
        r = self.session.post(
            f"{self.url}/api/v1/security/login",
            json={
                "username": username or os.environ.get("SUPERSET_ADMIN", "admin"),
                "password": password or os.environ.get("SUPERSET_ADMIN_PASSWORD", "admin"),
                "provider": "db",
                "refresh": True,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        self.session.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        csrf = self.get("security/csrf_token/")["result"]
        self.session.headers.update({"X-CSRFToken": csrf, "Referer": self.url})

    def get(self, path: str, **params: object) -> dict:
        r = self.session.get(f"{self.url}/api/v1/{path}", params=params or None, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict, timeout: int = TIMEOUT) -> dict:
        r = self.session.post(f"{self.url}/api/v1/{path}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def put(self, path: str, body: dict, timeout: int = TIMEOUT) -> dict:
        r = self.session.put(f"{self.url}/api/v1/{path}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def find(self, kind: str, key: str, value: str) -> int | None:
        """The id of the first ``kind`` (``database``, ``dataset``…) whose ``key`` is ``value``."""
        for item in self.get(f"{kind}/", page_size=100).get("result", []):
            if item.get(key) == value:
                return item["id"]
        return None

    def dataset(self, dataset_id: int) -> dict:
        """One dataset, with its metrics."""
        return self.get(f"dataset/{dataset_id}")["result"]

    def dataset_by_table(self, table: str) -> dict | None:
        dataset_id = self.find("dataset", "table_name", table)
        return self.dataset(dataset_id) if dataset_id is not None else None
