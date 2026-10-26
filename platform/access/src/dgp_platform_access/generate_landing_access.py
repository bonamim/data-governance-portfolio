"""Generate the landing zone's object-store access policies from config (ADR-0024).

Reads every ``sources.yml`` and writes, into ``platform/access/landing/``:

  policies/landing-partner-<source>.json   PutObject (+ multipart) on <inbox>/<source>/* only
  policies/landing-loader.json             drain the inbox; write + read the archive (no delete)
  policies/landing-owner-<domain>.json     read the quarantine of the datasets the domain owns
  principals.tsv                           user <TAB> policy <TAB> env var holding its secret
  production/                              the production bucket controls (ADR-0016 §5), as
                                           templates: ${AWS_ACCOUNT_ID} / ${AWS_REGION} are filled
                                           at deploy time
    inbox-bucket-policy.json               TLS only; uploads must be SSE-KMS with the uploading
                                           partner's own key
    archive-bucket-policy.json             TLS only; SSE-KMS only; nobody deletes accepted/
    inbox-lifecycle.json / archive-lifecycle.json
                                           expiry + storage-class transitions per retention class
    kms-keys.json                          one key per partner (encrypt) the loader may decrypt
    secrets.json                           where each principal's credential lives in the secrets
                                           manager, and its rotation

The policies are standard IAM JSON: ``minio-init`` applies ``policies/`` locally (``make up``); in
the cloud the same statements become IAM / S3 Access Point policies. ``production/`` needs what a
laptop can't host (TLS on the object store, a KMS, a remote storage tier, a secrets manager), so it
is generated and reviewed here and applied by the production deployment (production-readiness
D11/D12). The output is committed, and
``--check`` (``make check``) fails when it drifts from the source config, so who-can-touch-what is
always reviewable in git.

Run: ``make landing-access`` (regenerate) · ``make check`` (drift guard). Module:
``python -m dgp_platform_access.generate_landing_access [--check]``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from dgp_governance.repo import relative, root
from dgp_platform_ingestion.config import load_datasets
from dgp_platform_ingestion.layout import RETENTION_CLASSES

INBOX = os.environ.get("LANDING_INBOX_BUCKET", "landing-inbox")
ARCHIVE = os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive")

KEY_ARN = "arn:aws:kms:${AWS_REGION}:${AWS_ACCOUNT_ID}:key/"
ROLE_ARN = "arn:aws:iam::${AWS_ACCOUNT_ID}:role/"
# Storage-class transitions for accepted evidence, per retention class (days -> class). The 90-day
# class stays in STANDARD: an early transition would cost more than it saves.
TRANSITIONS = {
    90: [],
    365: [(30, "STANDARD_IA"), (90, "GLACIER_IR")],
    2555: [(30, "STANDARD_IA"), (90, "GLACIER_IR"), (365, "DEEP_ARCHIVE")],
}


def _arn(bucket: str, path: str = "") -> str:
    return f"arn:aws:s3:::{bucket}" + (f"/{path}" if path else "")


def _doc(*statements: dict) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def partner_policy(source: str) -> dict:
    # Write-only to its own prefix: a partner can neither read back nor list what others sent.
    return _doc(
        {
            "Sid": "DeliverToOwnPrefix",
            "Effect": "Allow",
            "Action": [
                "s3:PutObject",
                "s3:AbortMultipartUpload",
                "s3:ListMultipartUploadParts",
            ],
            "Resource": [_arn(INBOX, f"{source}/*")],
        }
    )


def loader_policy() -> dict:
    return _doc(
        {
            "Sid": "DrainInbox",
            "Effect": "Allow",
            "Action": [
                "s3:ListBucket",
                "s3:ListBucketVersions",
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
            ],
            "Resource": [_arn(INBOX), _arn(INBOX, "*")],
        },
        {
            # File accepted/quarantined deliveries (lock + retention tag) and read them back for
            # replay. Deliberately no Delete* and no BypassGovernanceRetention on the archive.
            "Sid": "FileToArchive",
            "Effect": "Allow",
            "Action": [
                "s3:ListBucket",
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:GetObjectTagging",
                "s3:GetObjectRetention",
                "s3:PutObject",
                "s3:PutObjectTagging",
                "s3:PutObjectRetention",
                "s3:AbortMultipartUpload",
                "s3:ListMultipartUploadParts",
            ],
            "Resource": [_arn(ARCHIVE), _arn(ARCHIVE, "*")],
        },
    )


def owner_policy(datasets: list[tuple[str, str]]) -> dict:
    """Triage access: the owning domain reads the quarantine of its own datasets, nothing else."""
    prefixes = [f"quarantine/{src}/{name}/" for src, name in sorted(datasets)]
    return _doc(
        {
            "Sid": "ListOwnQuarantine",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [_arn(ARCHIVE)],
            "Condition": {"StringLike": {"s3:prefix": [p + "*" for p in prefixes]}},
        },
        {
            "Sid": "ReadOwnQuarantine",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
            "Resource": [_arn(ARCHIVE, p + "*") for p in prefixes],
        },
    )


def _deny_insecure(bucket: str) -> list[dict]:
    return [
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [_arn(bucket), _arn(bucket, "*")],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        },
        {
            "Sid": "DenyUnencryptedUploads",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:PutObject",
            "Resource": [_arn(bucket, "*")],
            "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"}},
        },
    ]


def inbox_bucket_policy(sources: list[str]) -> dict:
    """TLS only; every upload SSE-KMS; each partner's prefix only under its own key."""
    return _doc(
        *_deny_insecure(INBOX),
        *[
            {
                "Sid": f"Deny{s.title()}OtherKey",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": [_arn(INBOX, f"{s}/*")],
                "Condition": {
                    "StringNotEquals": {
                        "s3:x-amz-server-side-encryption-aws-kms-key-id": KEY_ARN
                        + "${KEY_ID_LANDING_"
                        + s.upper()
                        + "}"
                    }
                },
            }
            for s in sources
        ],
    )


def archive_bucket_policy() -> dict:
    """TLS only; SSE-KMS only; accepted evidence can't be deleted (object lock is the backstop)."""
    return _doc(
        *_deny_insecure(ARCHIVE),
        {
            "Sid": "DenyDeleteAcceptedEvidence",
            "Effect": "Deny",
            "Principal": "*",
            "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention"],
            "Resource": [_arn(ARCHIVE, "accepted/*")],
        },
    )


def inbox_lifecycle() -> dict:
    return {
        "Rules": [
            {
                "ID": "inbox-safety-net",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "Expiration": {"Days": 30},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }
        ]
    }


def archive_lifecycle() -> dict:
    rules = [
        {
            "ID": f"accepted-{days}d",
            "Status": "Enabled",
            "Filter": {"Tag": {"Key": "retention-class", "Value": f"{days}d"}},
            **(
                {"Transitions": [{"Days": d, "StorageClass": c} for d, c in TRANSITIONS[days]]}
                if TRANSITIONS[days]
                else {}
            ),
            "Expiration": {"Days": days},
        }
        for days in RETENTION_CLASSES
    ]
    rules.append(
        {
            "ID": "quarantine-90d",
            "Status": "Enabled",
            "Filter": {"Prefix": "quarantine/"},
            "Expiration": {"Days": 90},
        }
    )
    return {"Rules": rules}


def kms_keys(sources: list[str]) -> dict:
    return {
        "keys": [
            {
                "alias": f"alias/dgp-landing-{s}",
                "key_id_var": f"KEY_ID_LANDING_{s.upper()}",
                "rotation": "yearly (automatic)",
                "encrypt": [ROLE_ARN + f"landing-partner-{s}"],
                "decrypt": [ROLE_ARN + "landing-loader"],
            }
            for s in sources
        ]
    }


def secrets_inventory(sources: list[str]) -> dict:
    principals = ["landing-loader", *[f"partner-{s}" for s in sources]]
    return {
        "secrets": [
            {
                "principal": p,
                "secret": f"dgp/landing/{p}",
                "rotation_days": 90,
                "read_by": "landing-loader workload role"
                if p == "landing-loader"
                else "the partner, through its onboarding portal (never the platform)",
            }
            for p in principals
        ]
    }


def output_dir() -> Path:
    """Where the generated files are committed: ``platform/access/landing/``."""
    return root() / "platform" / "access" / "landing"


def generate() -> dict[str, str]:
    """{relative path: file content} for everything under platform/access/landing/."""
    landing = [d for d in load_datasets(root()) if d.source_type == "landing"]
    sources = sorted({d.source_system for d in landing})
    owned: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for d in landing:
        owned[d.domain].append((d.source_system, d.name))

    files: dict[str, dict] = {"policies/landing-loader.json": loader_policy()}
    for src in sources:
        files[f"policies/landing-partner-{src}.json"] = partner_policy(src)
    for domain, ds in sorted(owned.items()):
        files[f"policies/landing-owner-{domain}.json"] = owner_policy(ds)

    files["production/inbox-bucket-policy.json"] = inbox_bucket_policy(sources)
    files["production/archive-bucket-policy.json"] = archive_bucket_policy()
    files["production/inbox-lifecycle.json"] = inbox_lifecycle()
    files["production/archive-lifecycle.json"] = archive_lifecycle()
    files["production/kms-keys.json"] = kms_keys(sources)
    files["production/secrets.json"] = secrets_inventory(sources)

    out = {path: json.dumps(doc, indent=2) + "\n" for path, doc in files.items()}
    # Principals created locally. Owner policies attach to the domain's SSO role in production
    # (no local user: humans do not get static keys).
    rows = ["# user\tpolicy\tenv var holding the secret (generated — do not edit)"]
    rows.append("landing-loader\tlanding-loader\tLANDING_LOADER_SECRET_KEY")
    rows += [
        f"partner-{s}\tlanding-partner-{s}\tLANDING_PARTNER_{s.upper()}_SECRET" for s in sources
    ]
    out["principals.tsv"] = "\n".join(rows) + "\n"
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed files drift")
    args = parser.parse_args()

    out = output_dir()
    wanted = generate()
    existing = {
        str(p.relative_to(out)): p.read_text(encoding="utf-8")
        for p in out.rglob("*")
        if p.is_file()
    }
    if args.check:
        drift = sorted(set(wanted) ^ set(existing)) + sorted(
            k for k in wanted.keys() & existing.keys() if wanted[k] != existing[k]
        )
        if drift:
            print("Landing access drift (run `make landing-access`):")
            for k in drift:
                print(f"  - platform/access/landing/{k}")
            return 1
        print(f"Landing access OK: {len(wanted)} files generated from sources.yml.")
        return 0

    for stale in set(existing) - set(wanted):
        (out / stale).unlink()
    for rel, content in wanted.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"wrote {len(wanted)} files to {relative(out)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
