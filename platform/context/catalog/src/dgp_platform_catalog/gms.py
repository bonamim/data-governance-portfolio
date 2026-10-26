"""A minimal client for DataHub's metadata service (GMS): search, aspect lookups and GraphQL.

The publishers write through the DataHub SDK's emitter; the few reads they need — and the proofs'
reads — go through this client instead, so it uses the standard library only and works where the
SDK isn't installed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30


def default_url() -> str:
    """``DATAHUB_GMS``, else GMS on its published host port (8081, beside Airflow's 8080)."""
    port = os.environ.get("DATAHUB_MAPPED_GMS_PORT", "8081")
    return os.environ.get("DATAHUB_GMS") or f"http://localhost:{port}"


class Gms:
    """Reads from one DataHub GMS."""

    def __init__(self, url: str | None = None) -> None:
        self.url = (url or default_url()).rstrip("/")

    def _request(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers)  # noqa: S310
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 — the configured GMS
            return json.load(resp)

    def search(
        self, entity: str = "dataset", platform: str | None = None, count: int = 500
    ) -> list[str]:
        """URNs of the ``entity`` entities, filtered to a data platform when given.

        GMS may ignore the platform filter (it has, on this version), so a caller that acts on the
        result — a soft-delete — must re-check each URN itself.
        """
        body: dict = {"input": "*", "entity": entity, "start": 0, "count": count}
        if platform:
            urn = f"urn:li:dataPlatform:{platform}"
            body["filter"] = {"or": [{"and": [{"field": "platform", "value": urn}]}]}
        found = self._request("/entities?action=search", body)
        return [e["entity"] for e in found["value"]["entities"]]

    def has_aspect(self, urn: str, aspect: str) -> bool:
        """True if the entity has ``aspect`` — e.g. ``schemaMetadata`` once it has been ingested,
        not merely referenced by an aspect written about it."""
        quoted = urllib.parse.quote(urn, safe="")
        try:
            self._request(f"/aspects/{quoted}?aspect={aspect}&version=0")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise
        return True

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        """The ``data`` of a GraphQL query."""
        return self._request("/api/graphql", {"query": query, "variables": variables or {}})["data"]
