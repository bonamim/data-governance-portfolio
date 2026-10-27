#!/usr/bin/env bash
# Initialises the operational source database (shop_oltp).
# Runs once, on first container start, as the Postgres superuser.
# Creates a least-privilege loader (owns the data) and reader (platform ingestion).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE EXTENSION IF NOT EXISTS pgcrypto;

	-- Application roles (never use the superuser from an application, X5).
	CREATE ROLE oltp_loader LOGIN PASSWORD '${OLTP_LOADER_PASSWORD}';
	CREATE ROLE oltp_reader LOGIN PASSWORD '${OLTP_READER_PASSWORD}';

	-- The operational tables live in a dedicated schema owned by the loader.
	CREATE SCHEMA IF NOT EXISTS shop AUTHORIZATION oltp_loader;

	-- The platform reader gets read-only access, now and for future tables.
	GRANT USAGE ON SCHEMA shop TO oltp_reader;
	ALTER DEFAULT PRIVILEGES FOR ROLE oltp_loader IN SCHEMA shop
	    GRANT SELECT ON TABLES TO oltp_reader;

	-- Lock down connect: only our two roles, not PUBLIC.
	REVOKE CONNECT ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
	GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO oltp_loader, oltp_reader;
SQL

echo "shop_oltp initialised: schema 'shop', roles oltp_loader / oltp_reader."
