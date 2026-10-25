"""A semantic model request is recorded and the owning domain is told (ADR-0016 alert route)."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import semantic  # noqa: E402


@pytest.fixture
def requests_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "REQUESTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def webhook(monkeypatch):
    """A local webhook standing in for #customer-data; yields the bodies it received."""
    received: list[dict] = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server API)
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ALERT_WEBHOOK_CUSTOMER", f"http://127.0.0.1:{server.server_port}/hook")
    yield received
    server.shutdown()


def test_request_notifies_the_owner(requests_dir, webhook):
    out = semantic.request_semantic_model("customers", "Distinct people per state", "ana")

    assert out["owner_notification"] == {"notified": True, "channel": "#customer-data"}
    [body] = webhook
    assert body["domain"] == "customer"
    assert body["kind"] == "semantic_model_request"
    assert body["channel"] == "#customer-data"
    assert "Distinct people per state" in body["text"]
    assert body["detail"]["file"] == out["recorded"]


def test_second_request_keeps_the_first(requests_dir, webhook):
    semantic.request_semantic_model("customers", "first need", "ana")
    semantic.request_semantic_model("customers", "second need", "bo")

    needs = sorted(yaml.safe_load(p.read_text())["business_need"] for p in requests_dir.iterdir())
    assert needs == ["first need", "second need"]
    assert len(webhook) == 2


def test_no_webhook_is_reported_not_hidden(requests_dir, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_CUSTOMER", raising=False)

    out = semantic.request_semantic_model("customers", "need", "ana")

    assert out["owner_notification"]["notified"] is False
    assert "ALERT_WEBHOOK_CUSTOMER unset" in out["owner_notification"]["reason"]
    assert len(list(requests_dir.iterdir())) == 1  # still recorded


def test_unreachable_webhook_keeps_the_request(requests_dir, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_CUSTOMER", "http://127.0.0.1:9/unreachable")

    out = semantic.request_semantic_model("customers", "need", "ana")

    assert out["owner_notification"]["notified"] is False
    assert out["owner_notification"]["reason"].startswith("webhook failed")
    assert len(list(requests_dir.iterdir())) == 1
