#!/usr/bin/env bash
# Verifies the M1 platform came up correctly: buckets (versioned), warehouse
# schemas, roles and the ingestion ledger. Exit non-zero on the first problem.
set -euo pipefail

cd "$(dirname "$0")/../.."   # repo root
set -a; [ -f .env ] && . ./.env; set +a

COMPOSE="docker compose --env-file .env -f platform/infra/docker-compose.yml"
fail() { echo "FAIL: $1" >&2; exit 1; }
ok()   { echo "  ok: $1"; }

echo "== MinIO buckets =="
buckets="$($COMPOSE run --rm --no-deps -T --entrypoint bash minio-init -c \
  'mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls local' 2>/dev/null || true)"
for b in "$LANDING_BUCKET" "$STATE_BUCKET"; do
  echo "$buckets" | grep -q "$b/" && ok "bucket $b exists" || fail "bucket $b missing"
done

echo "== shop_oltp =="
oltp() { $COMPOSE exec -T postgres-oltp psql -U "$OLTP_SUPERUSER" -d "$OLTP_DB" -tAc "$1"; }
[ "$(oltp "SELECT 1 FROM information_schema.schemata WHERE schema_name='shop'")" = "1" ] \
  && ok "schema shop" || fail "schema shop missing"
for r in oltp_loader oltp_reader; do
  [ "$(oltp "SELECT 1 FROM pg_roles WHERE rolname='$r'")" = "1" ] && ok "role $r" || fail "role $r missing"
done

echo "== analytics =="
ana() { $COMPOSE exec -T postgres-analytics psql -U "$ANALYTICS_SUPERUSER" -d "$ANALYTICS_DB" -tAc "$1"; }
[ "$(ana "SELECT 1 FROM pg_roles WHERE rolname='platform_loader'")" = "1" ] \
  && ok "role platform_loader" || fail "role platform_loader missing"
[ "$(ana "SELECT to_regclass('platform_meta.ingestion_ledger') IS NOT NULL")" = "t" ] \
  && ok "platform_meta.ingestion_ledger" || fail "ingestion_ledger missing"
for d in sales customer marketplace customer_experience reference; do
  [ "$(ana "SELECT 1 FROM pg_roles WHERE rolname='${d}_role'")" = "1" ] && ok "role ${d}_role" || fail "role ${d}_role missing"
  for layer in "${d}_raw" "${d}_staging" "$d"; do
    [ "$(ana "SELECT 1 FROM information_schema.schemata WHERE schema_name='$layer'")" = "1" ] \
      && ok "schema $layer" || fail "schema $layer missing"
  done
done

echo
echo "M1 infrastructure verified."
