#!/usr/bin/env bash
# Proof P5: breaking-change gate. Simulates a PR that removes a column from the
# customer.customers output-port contract, shows the gate blocking it, then passing only after a
# major version bump AND the affected consumer's approval. Reverts everything on exit.
set -euo pipefail
cd "$(dirname "$0")/.."

CF=domains/customer/contracts/customers.odcs.yaml
AP=governance/approvals/customers-v2.yaml
BK="$(mktemp)"
OUT="$(mktemp)"
cp "$CF" "$BK"
cleanup() { cp "$BK" "$CF"; rm -f "$AP" "$BK" "$OUT"; }
trap cleanup EXIT

export BASE_REF=main # compare the working tree against local main (CI uses origin/<base>)

echo "### P5.1: remove a column (breaking change), no version bump  ->  expect BLOCKED"
python3 -c "import yaml;f='$CF';d=yaml.safe_load(open(f));d['schema'][0]['properties'].pop();yaml.safe_dump(d,open(f,'w'),sort_keys=False)"
if make -s check-breaking >"$OUT" 2>&1; then echo "FAIL: gate should have blocked"; cat "$OUT"; exit 1; fi
grep -q "MAJOR version bump" "$OUT" && echo "  OK: blocked: needs a major version bump"

echo "### P5.2: bump to 2.0.0 but no approval  ->  expect BLOCKED (names the consumer)"
python3 -c "import yaml;f='$CF';d=yaml.safe_load(open(f));d['version']='2.0.0';yaml.safe_dump(d,open(f,'w'),sort_keys=False)"
if make -s check-breaking >"$OUT" 2>&1; then echo "FAIL: gate should have blocked"; cat "$OUT"; exit 1; fi
grep -q "needs approval from: sales" "$OUT" && echo "  OK: blocked: needs approval from sales"

echo "### P5.3: sales approves  ->  expect PASS"
uv run python governance/policies/record_approval.py \
  --port customers --version 2 --team sales --user demo --pr 0 >/dev/null
if make -s check-breaking >"$OUT" 2>&1 && grep -q "APPROVED" "$OUT"; then
  echo "  OK: gate passes: breaking change approved by all consumers"
else
  echo "FAIL: gate should have passed"; cat "$OUT"; exit 1
fi

echo
echo "P5 proven: an output-port interface can't break silently, only with a major version bump"
echo "and approval from every affected consumer."
