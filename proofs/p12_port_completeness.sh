#!/usr/bin/env bash
# Proof P12: output-port completeness. An output port is an interface people, BI and AI agents
# read without asking the owner, so it can't be published undocumented. Strips one piece of
# documentation at a time from the real customer.customers contract and the sales Ossie model,
# runs the real CI gates, and shows each one blocking. The landing file every domain reads first is
# held to the same documentation rule. Reverts everything on exit.
set -euo pipefail
cd "$(dirname "$0")/.."

CF=domains/customer/contracts/customers.odcs.yaml
SM=domains/sales/semantics/sales_performance.semantic_model.yaml
# The definitions customer_city and customer_state inherit their meaning from (ADR-0027): a column
# is undocumented only when neither it nor its definition says what it means.
CITY=domains/reference/contracts/definitions/city_name.odcs.yaml
STATE=domains/reference/contracts/definitions/state_code.odcs.yaml
LANDING=domains/customer_experience/contracts/landing/reviews.odcs.yaml
FILES=("$CF" "$SM" "$CITY" "$STATE" "$LANDING")
BACKUP="$(mktemp -d)"
OUT="$(mktemp)"
for f in "${FILES[@]}"; do mkdir -p "$BACKUP/$(dirname "$f")"; cp "$f" "$BACKUP/$f"; done
restore() { for f in "${FILES[@]}"; do cp "$BACKUP/$f" "$f"; done; }
cleanup() { restore; rm -rf "$BACKUP" "$OUT"; }
trap cleanup EXIT

# Apply a Python edit to the YAML document `d` loaded from file $1.
edit() {
  uv run python -c "import yaml,sys;f=sys.argv[1];d=yaml.safe_load(open(f));$2;yaml.safe_dump(d,open(f,'w'),sort_keys=False)" "$1"
}

# Expect the gate (make target $1) to block, with $2 in its output.
expect_blocked() {
  if make -s "$1" >"$OUT" 2>&1; then echo "FAIL: $1 should have blocked"; cat "$OUT"; exit 1; fi
  if ! grep -q "$2" "$OUT"; then echo "FAIL: $1 blocked for the wrong reason"; cat "$OUT"; exit 1; fi
  echo "  OK: blocked: $(grep -m1 "$2" "$OUT" | sed 's/^ *- //')"
}

echo "### P12.1: the documented ports  ->  expect PASS"
make -s check-output-ports >"$OUT" 2>&1 || { echo "FAIL: baseline should pass"; cat "$OUT"; exit 1; }
make -s check-semantics >>"$OUT" 2>&1 || { echo "FAIL: baseline should pass"; cat "$OUT"; exit 1; }
make -s check-contracts >>"$OUT" 2>&1 || { echo "FAIL: baseline should pass"; cat "$OUT"; exit 1; }
echo "  OK: the gates pass on the fully documented ports and landing files"

echo "### P12.2: a column with no description, here or in its definition  ->  expect BLOCKED"
edit "$CF" "[p.pop('description') for p in d['schema'][0]['properties'] if p['name']=='customer_city']"
edit "$CITY" "d.pop('description')"
expect_blocked check-output-ports "column 'customer_city' has no description"
restore

echo "### P12.3: a column with no classification, here or in its definition  ->  expect BLOCKED"
edit "$CF" "[p.pop('classification', None) for p in d['schema'][0]['properties'] if p['name']=='customer_state']"
edit "$STATE" "d.pop('classification')"
expect_blocked check-output-ports "column 'customer_state' has no classification"
restore

echo "### P12.4: a table with no description  ->  expect BLOCKED"
edit "$CF" "d['schema'][0].pop('description')"
expect_blocked check-output-ports "table 'customers' has no description"
restore

# A field that is a contract column takes its description from the contract (ADR-0027). What the
# model itself must describe is what the contract can't: its metrics and derived fields.
echo "### P12.5: a metric the AI reads with no description  ->  expect BLOCKED"
edit "$SM" "[m.pop('description') for m in d['metrics'] if m['name']=='customer_count']"
expect_blocked check-semantics "metric 'customer_count' has no description"
restore

echo "### P12.6: a landing column with no description and no definition  ->  expect BLOCKED"
edit "$LANDING" "[p.pop('authoritativeDefinitions') for p in d['schema'][0]['properties'] if p['name']=='review_comment_title']"
expect_blocked check-contracts "column 'review_comment_title' has no description"
restore

echo
echo "P12 proven: an output port can't be published undocumented, nor a landing file contracted"
echo "undocumented: every table, column and metric a consumer or AI agent reads says what it"
echo "means, here or in its definition."
