#!/usr/bin/env bash
# DR heartbeat table (proof P9): the restore drill commits a row here on the live source, waits for
# its WAL to be archived, then proves point-in-time recovery brings that row back — measuring the
# real data-loss window against the RPO. Owned by the superuser; no application role can see it.
# Idempotent: runs on a fresh database and via `make db-roles` on a running one.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'SQL'
	CREATE SCHEMA IF NOT EXISTS platform_dr;
	CREATE TABLE IF NOT EXISTS platform_dr.heartbeat (
	    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	    written_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
	    note        text
	);
SQL
echo "shop_oltp: platform_dr.heartbeat ensured."
