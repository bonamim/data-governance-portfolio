#!/usr/bin/env bash
# Proof P9 — disaster recovery. Takes a fresh backup of every store that can't be regenerated, then
# runs the automated restore drill: restore into an isolated sandbox, rebuild the warehouse from git
# + the landing zone, run every contract check, re-check access control, and measure recovery time
# against each RTO (docs/disaster-recovery.md §5). Needs the live platform (`make up`).
set -euo pipefail
cd "$(dirname "$0")/.."

RECOVERY=(uv run python)

echo "### P9.1 — back up every non-regenerable store"
"${RECOVERY[@]}" platform/core/recovery/backup.py

echo
echo "### P9.2 — restore drill against that backup  ->  expect every check passed and every RTO met"
"${RECOVERY[@]}" platform/core/recovery/restore_drill.py
