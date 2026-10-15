#!/usr/bin/env bash
# Proof P12 — output-port completeness. An output port is an interface people, BI and AI agents
# read without asking the owner, so it can't be published undocumented. Strips one piece of
# documentation at a time from the real customer.customers contract and the sales Ossie model,
# runs the real CI gates, and shows each one blocking. Reverts everything on exit.
set -euo pipefail
cd "$(dirname "$0")/.."

CF=domains/customer/contracts/customers.odcs.yaml
SM=domains/sales/semantics/sales_performance.semantic_model.yaml
BK_CF="$(mktemp)"
BK_SM="$(mktemp)"
OUT="$(mktemp)"
cp "$CF" "$BK_CF"
cp "$SM" "$BK_SM"
cleanup() { cp "$BK_CF" "$CF"; cp "$BK_SM" "$SM"; rm -f "$BK_CF" "$BK_SM" "$OUT"; }
trap cleanup EXIT

# Apply a Python edit to the YAML document `d` loaded from file $1.
edit() {
  uv run python -c "import yaml,sys;f=sys.argv[1];d=yaml.safe_load(open(f));$2;yaml.safe_dump(d,open(f,'w'),sort_keys=False)" "$1"
}
restore() { cp "$BK_CF" "$CF"; cp "$BK_SM" "$SM"; }

# Expect the gate (make target $1) to block, with $2 in its output.
expect_blocked() {
  if make -s "$1" >"$OUT" 2>&1; then echo "FAIL: $1 should have blocked"; cat "$OUT"; exit 1; fi
  if ! grep -q "$2" "$OUT"; then echo "FAIL: $1 blocked for the wrong reason"; cat "$OUT"; exit 1; fi
  echo "  OK — blocked: $(grep -m1 "$2" "$OUT" | sed 's/^ *- //')"
}

echo "### P12.1 — the documented ports  ->  expect PASS"
make -s check-output-ports >"$OUT" 2>&1 || { echo "FAIL: baseline should pass"; cat "$OUT"; exit 1; }
make -s check-semantics >>"$OUT" 2>&1 || { echo "FAIL: baseline should pass"; cat "$OUT"; exit 1; }
echo "  OK — both gates pass on the fully documented ports"

echo "### P12.2 — a column with no description  ->  expect BLOCKED"
edit "$CF" "[p.pop('description') for p in d['schema'][0]['properties'] if p['name']=='customer_city']"
expect_blocked check-output-ports "column 'customer_city' has no description"
restore

echo "### P12.3 — a column with no classification  ->  expect BLOCKED"
edit "$CF" "[p.pop('classification') for p in d['schema'][0]['properties'] if p['name']=='customer_state']"
expect_blocked check-output-ports "column 'customer_state' has no classification"
restore

echo "### P12.4 — a table with no description  ->  expect BLOCKED"
edit "$CF" "d['schema'][0].pop('description')"
expect_blocked check-output-ports "table 'customers' has no description"
restore

echo "### P12.5 — a semantic field the AI reads with no description  ->  expect BLOCKED"
edit "$SM" "[f.pop('description') for f in d['datasets'][0]['fields'] if f['name']=='num_customers']"
expect_blocked check-semantics "field 'num_customers' has no description"
restore

echo
echo "P12 proven: an output port can't be published undocumented — every table, column and"
echo "semantic field a consumer or AI agent reads says what it means."
