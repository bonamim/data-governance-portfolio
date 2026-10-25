"""Production landing-bucket controls generated from sources.yml (ADR-0016 §5, D11/D12)."""

import sys
from pathlib import Path

ACCESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACCESS))
import generate_landing_access as la  # noqa: E402

FILES = la.generate()


def _load(name: str) -> dict:
    import json

    return json.loads(FILES[f"production/{name}"])


def _statements(doc: dict, sid: str) -> list[dict]:
    return [s for s in doc["Statement"] if s["Sid"] == sid]


def test_both_buckets_refuse_plain_http_and_unencrypted_uploads():
    for name in ("inbox-bucket-policy.json", "archive-bucket-policy.json"):
        doc = _load(name)
        (tls,) = _statements(doc, "DenyInsecureTransport")
        assert tls["Effect"] == "Deny" and tls["Condition"] == {
            "Bool": {"aws:SecureTransport": "false"}
        }
        (sse,) = _statements(doc, "DenyUnencryptedUploads")
        assert sse["Condition"]["StringNotEquals"]["s3:x-amz-server-side-encryption"] == "aws:kms"


def test_each_partner_prefix_requires_its_own_key():
    doc = _load("inbox-bucket-policy.json")
    sources = sorted({d.source_system for d in la.load_datasets(la.REPO_ROOT)
                      if d.source_type == "landing"})  # fmt: skip
    keys = {k["alias"]: k for k in _load("kms-keys.json")["keys"]}
    for s in sources:
        (rule,) = _statements(doc, f"Deny{s.title()}OtherKey")
        assert rule["Resource"] == [la._arn(la.INBOX, f"{s}/*")]
        key = rule["Condition"]["StringNotEquals"]["s3:x-amz-server-side-encryption-aws-kms-key-id"]
        assert key.endswith("${KEY_ID_LANDING_" + s.upper() + "}")
        assert keys[f"alias/dgp-landing-{s}"]["encrypt"] == [la.ROLE_ARN + f"landing-partner-{s}"]


def test_accepted_evidence_cannot_be_deleted():
    (deny,) = _statements(_load("archive-bucket-policy.json"), "DenyDeleteAcceptedEvidence")
    assert deny["Resource"] == [la._arn(la.ARCHIVE, "accepted/*")]
    assert "s3:BypassGovernanceRetention" in deny["Action"]


def test_every_retention_class_has_a_lifecycle_with_valid_transitions():
    rules = {r["ID"]: r for r in _load("archive-lifecycle.json")["Rules"]}
    for days in la.RETENTION_CLASSES:
        rule = rules[f"accepted-{days}d"]
        assert rule["Expiration"]["Days"] == days
        steps = [t["Days"] for t in rule.get("Transitions", [])]
        assert steps == sorted(steps) and all(d < days for d in steps)
        # S3 minimums: STANDARD_IA after >= 30 days, and 30 days in it before the next class.
        assert all(b - a >= 30 for a, b in zip(steps, steps[1:], strict=False))
        assert not steps or steps[0] >= 30
    assert rules["quarantine-90d"]["Expiration"]["Days"] == 90


def test_every_principal_has_a_rotated_secret_and_none_is_in_a_file():
    secrets = _load("secrets.json")["secrets"]
    assert {s["principal"] for s in secrets} >= {"landing-loader"}
    assert all(s["secret"].startswith("dgp/landing/") and s["rotation_days"] <= 90 for s in secrets)
    assert not any("SECRET" in content and "=" in content for content in FILES.values()
                   if content.lstrip().startswith("{"))  # fmt: skip
