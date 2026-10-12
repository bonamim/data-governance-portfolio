#!/usr/bin/env bash
# Initialises the analytics warehouse.
# Runs once, on first container start, as the Postgres superuser.
#
# Creates, per the data mesh design (docs/data-mesh.md section 5):
#   - one Postgres role per domain (least privilege)
#   - three schemas per domain: <domain>_raw, <domain>_staging, <domain>
#   - a platform_loader role that writes raw schemas and the ingestion ledger
#   - the platform_meta.ingestion_ledger table (pipeline state, ADR-0006)
#
# Cross-domain consumer grants (consumers read output ports only) are generated
# from data product definitions in milestone M3 — this sets up the base roles.
set -euo pipefail

DOMAINS="sales customer marketplace customer_experience reference"

# Map each domain to its role password env var.
domain_password() {
	case "$1" in
		sales)               echo "$SALES_ROLE_PASSWORD" ;;
		customer)            echo "$CUSTOMER_ROLE_PASSWORD" ;;
		marketplace)         echo "$MARKETPLACE_ROLE_PASSWORD" ;;
		customer_experience) echo "$CUSTOMER_EXPERIENCE_ROLE_PASSWORD" ;;
		reference)           echo "$REFERENCE_ROLE_PASSWORD" ;;
	esac
}

run_sql() { psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" "$@"; }

# --- Platform-wide objects -------------------------------------------------
run_sql <<-SQL
	-- Platform loader: writes raw schemas and the ingestion ledger. Not a superuser.
	CREATE ROLE platform_loader LOGIN PASSWORD '${PLATFORM_LOADER_PASSWORD}';

	REVOKE CONNECT ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
	GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO platform_loader;

	-- Pipeline state: the ingestion ledger (ADR-0006, data state for files + tables).
	CREATE SCHEMA IF NOT EXISTS platform_meta AUTHORIZATION platform_loader;

	CREATE TABLE IF NOT EXISTS platform_meta.ingestion_ledger (
	    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	    source_type          text        NOT NULL CHECK (source_type IN ('file', 'table')),
	    source_identifier    text        NOT NULL,   -- object key (files) or qualified table name
	    object_etag          text,                   -- MinIO ETag / checksum; NULL for table sources
	    contract_version     text,
	    target_schema        text        NOT NULL,
	    target_table         text        NOT NULL,
	    row_count            bigint,
	    status               text        NOT NULL CHECK (status IN ('loaded', 'quarantined', 'skipped')),
	    run_id               text,                   -- Airflow run id
	    data_interval_start  timestamptz,
	    data_interval_end    timestamptz,
	    message              text,
	    loaded_at            timestamptz NOT NULL DEFAULT now()
	);

	-- Idempotency: the same file (same ETag) is loaded at most once. A changed file
	-- has a new ETag and is therefore a new row. Enforced in proof P6.
	CREATE UNIQUE INDEX IF NOT EXISTS ingestion_ledger_file_once
	    ON platform_meta.ingestion_ledger (source_identifier, object_etag)
	    WHERE status = 'loaded' AND source_type = 'file';

	CREATE INDEX IF NOT EXISTS ingestion_ledger_target
	    ON platform_meta.ingestion_ledger (target_schema, target_table, loaded_at DESC);

	-- The table is created by the superuser running this init script, so hand it
	-- (and its identity sequence) to platform_loader, which writes it at runtime.
	ALTER TABLE platform_meta.ingestion_ledger OWNER TO platform_loader;
SQL

echo "analytics: platform_loader role + platform_meta.ingestion_ledger created."

# --- Per-domain roles and schemas ------------------------------------------
for domain in $DOMAINS; do
	password="$(domain_password "$domain")"
	run_sql <<-SQL
		CREATE ROLE ${domain}_role LOGIN PASSWORD '${password}';
		GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${domain}_role;

		-- Medallion layers (internal, engineering DQ). raw is loaded by the platform;
		-- bronze/silver/gold are the domain's progression. Output ports are models
		-- deliberately tagged `output_port` that live in one of these layer schemas
		-- (ADR-0012) — there is no separate output schema; publishing is a tag, not a
		-- location. Consumer access to a port is granted per-model.
		CREATE SCHEMA IF NOT EXISTS ${domain}_raw    AUTHORIZATION platform_loader;
		CREATE SCHEMA IF NOT EXISTS ${domain}_bronze AUTHORIZATION ${domain}_role;
		CREATE SCHEMA IF NOT EXISTS ${domain}_silver AUTHORIZATION ${domain}_role;
		CREATE SCHEMA IF NOT EXISTS ${domain}_gold   AUTHORIZATION ${domain}_role;

		-- The domain reads its own raw (loaded by the platform) and owns the rest.
		GRANT USAGE ON SCHEMA ${domain}_raw TO ${domain}_role;
		GRANT SELECT ON ALL TABLES IN SCHEMA ${domain}_raw TO ${domain}_role;
		ALTER DEFAULT PRIVILEGES FOR ROLE platform_loader IN SCHEMA ${domain}_raw
		    GRANT SELECT ON TABLES TO ${domain}_role;
	SQL
	echo "  domain '${domain}': role + raw/bronze/silver/gold schemas created."
done

echo "analytics warehouse initialised."
