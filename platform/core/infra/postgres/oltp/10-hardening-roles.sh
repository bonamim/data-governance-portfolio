#!/usr/bin/env bash
# Read-only metadata role for DataHub ingestion (X5) — replaces ingesting as the superuser.
# Idempotent: runs on a fresh database after 00-init.sh and on a running one via `make db-roles`.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
	-v pw="$CATALOG_READER_PASSWORD" <<-'SQL'
	SELECT format('CREATE ROLE catalog_reader LOGIN NOSUPERUSER PASSWORD %L', :'pw')
	WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catalog_reader') \gexec
	SELECT format('ALTER ROLE catalog_reader LOGIN NOSUPERUSER PASSWORD %L', :'pw') \gexec
	SELECT format('GRANT CONNECT ON DATABASE %I TO catalog_reader', current_database()) \gexec
	GRANT pg_read_all_data TO catalog_reader;
SQL

echo "shop_oltp: catalog_reader ensured."
