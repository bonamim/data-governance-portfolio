#!/usr/bin/env python3
"""Generate the landing zone's object-store access policies from config (ADR-0024).

Reads every ``sources.yml`` and writes, into ``platform/access/landing/``:

  policies/landing-partner-<source>.json   PutObject (+ multipart) on <inbox>/<source>/* only
  policies/landing-loader.json             drain the inbox; write + read the archive (no delete)
  policies/landing-owner-<domain>.json     read the quarantine of the datasets the domain owns
  principals.tsv                           user <TAB> policy <TAB> env var holding its secret

The documents are standard IAM JSON: ``minio-init`` applies them locally (``make up``); in the
cloud the same statements become IAM / S3 Access Point policies. The output is committed, and
``--check`` (``make check``) fails when it drifts from the source config, so who-can-touch-what is
always reviewable in git.

Run: ``make landing-access`` (regenerate) · ``make check`` (drift guard).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "platform" / "access" / "landing"
INBOX = os.environ.get("LANDING_INBOX_BUCKET", "landing-inbox")
ARCHIVE = os.environ.get("LANDING_ARCHIVE_BUCKET", "landing-archive")

sys.path.insert(0, str(REPO_ROOT / "platform" / "ingestion" / "src"))
sys.path.insert(0, str(REPO_ROOT / "platform" / "quality" / "src"))
from dgp_platform_ingestion.config import load_datasets  # noqa: E402


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


def generate() -> dict[str, str]:
    """{relative path: file content} for everything under platform/access/landing/."""
    landing = [d for d in load_datasets(REPO_ROOT) if d.source_type == "landing"]
    sources = sorted({d.source_system for d in landing})
    owned: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for d in landing:
        owned[d.domain].append((d.source_system, d.name))

    files: dict[str, dict] = {"policies/landing-loader.json": loader_policy()}
    for src in sources:
        files[f"policies/landing-partner-{src}.json"] = partner_policy(src)
    for domain, ds in sorted(owned.items()):
        files[f"policies/landing-owner-{domain}.json"] = owner_policy(ds)

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

    wanted = generate()
    existing = {
        str(p.relative_to(OUT)): p.read_text(encoding="utf-8")
        for p in OUT.rglob("*")
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
        print(f"Landing access OK: {len(wanted) - 1} policies generated from sources.yml.")
        return 0

    for stale in set(existing) - set(wanted):
        (OUT / stale).unlink()
    for rel, content in wanted.items():
        path = OUT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"wrote {len(wanted)} files to {OUT.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
