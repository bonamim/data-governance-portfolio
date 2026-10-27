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
# from data product definitions in milestone M3. This sets up the base roles.
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

	-- Least-privilege CONSUMER role for the AI/BI consumers (output ports only, never raw/staging;
	-- sees PII masked unless it holds an unmask role). Replaces reusing a domain role as the
	-- consumer (ADR-0013/M8). Per-domain scoping is enforced in the agent (ADR-0021); this is the
	-- defence-in-depth warehouse grant.
	CREATE ROLE analytics_reader LOGIN PASSWORD '${ANALYTICS_READER_PASSWORD}';

	-- Unmask role: a session holding it sees raw PII through the masking views (ADR-0013). LOGIN
	-- here is a LOCAL convenience so the proof can connect as it; in production compliance is a
	-- NOLOGIN group GRANTed to the specific users allowed to unmask.
	CREATE ROLE compliance LOGIN PASSWORD '${COMPLIANCE_PASSWORD}';

	REVOKE CONNECT ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
	GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO platform_loader;
	GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO analytics_reader;
	GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO compliance;

	-- Pipeline state: the ingestion ledger (ADR-0006, data state for files + tables).
	CREATE SCHEMA IF NOT EXISTS platform_meta AUTHORIZATION platform_loader;

	CREATE TABLE IF NOT EXISTS platform_meta.ingestion_ledger (
	    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	    source_type          text        NOT NULL CHECK (source_type IN ('file', 'table')),
	    source_identifier    text        NOT NULL,   -- inbox object key (files) or qualified table name
	    object_etag          text,                   -- MinIO ETag; NULL for table sources
	    content_sha256       text,                   -- SHA-256 of the file content (dedup, P6)
	    dataset              text,                   -- <source>/<dataset> for files (ADR-0024)
	    partition            text,                   -- archive partition, e.g. dt=2026-10-15
	    object_version       text,                   -- the pinned inbox object version processed
	    archive_key          text,                   -- accepted/... or quarantine/... key it was filed to
	    archive_version      text,                   -- the archive object version filed (accepted/)
	    gate_id              bigint,                 -- load rows: the 'accepted' gate row loaded (ADR-0025)
	    checks               jsonb,                  -- per-rule landing-gate results (published to DataHub)
	    contract_version     text,
	    target_schema        text        NOT NULL,
	    target_table         text        NOT NULL,
	    row_count            bigint,
	    status               text        NOT NULL
	                         CONSTRAINT ingestion_ledger_status_check
	                         CHECK (status IN ('accepted', 'loaded', 'quarantined', 'skipped', 'superseded')),
	    run_id               text,                   -- Airflow run id
	    data_interval_start  timestamptz,
	    data_interval_end    timestamptz,
	    message              text,
	    loaded_at            timestamptz NOT NULL DEFAULT now()
	);

	-- Idempotency (proof P6): a delivery whose content equals the dataset's latest accepted file
	-- is skipped by the gate (ADR-0024); this index serves that lookup.
	CREATE INDEX IF NOT EXISTS ingestion_ledger_dataset
	    ON platform_meta.ingestion_ledger (dataset, loaded_at DESC);

	-- The gate -> load hand-off (ADR-0025): an 'accepted' row no load row points at is pending.
	CREATE INDEX IF NOT EXISTS ingestion_ledger_gate
	    ON platform_meta.ingestion_ledger (gate_id);

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
		-- (ADR-0012). There is no separate output schema: publishing is a tag, not a
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
