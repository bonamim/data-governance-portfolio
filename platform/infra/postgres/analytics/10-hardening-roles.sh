#!/usr/bin/env bash
# Least-privilege roles for the admin and AI planes (production-readiness X5, M8). Idempotent.
#
# Runs on a fresh warehouse after 00-init.sh, and against a running one via `make db-roles`
# (the init directory only executes on first start). Creates or updates:
#   access_admin    applies masking views + consumer grants (platform/access) instead of the
#                   superuser: member of every domain role (so it can GRANT on the ports the domains
#                   own) + CREATE on the database (for the <domain>_masked schemas). Not a superuser.
#   catalog_reader  DataHub metadata ingestion: read-only everywhere (pg_read_all_data), no writes.
#   agent_<domain>  one read-only role per federated domain agent (ADR-0021); granted only its own
#                   domain's output ports by grant_consumers.py, so isolation holds in the database
#                   too, not just in the agent's code.
set -euo pipefail

DOMAINS="sales customer marketplace customer_experience reference"
run_sql() { psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" "$@"; }

# Create-or-update a LOGIN role with a password (passwords come from the environment, quoted by psql).
ensure_login() {
	run_sql -v role="$1" -v pw="$2" <<-'SQL'
		SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'role', :'pw')
		WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
		SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER PASSWORD %L', :'role', :'pw') \gexec
		SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'role') \gexec
	SQL
}

agent_password() {
	local var="AGENT_$(echo "$1" | tr '[:lower:]' '[:upper:]')_PASSWORD"
	echo "${!var}"
}

ensure_login access_admin "$ACCESS_ADMIN_PASSWORD"
ensure_login catalog_reader "$CATALOG_READER_PASSWORD"
for domain in $DOMAINS; do
	ensure_login "agent_${domain}" "$(agent_password "$domain")"
	run_sql -c "GRANT ${domain}_role TO access_admin"
	# Slim CI sandbox (ADR-0015 §3): changed models are built here, unchanged ones deferred to the
	# published schemas. Owned by the domain role; never granted to consumers.
	run_sql -c "CREATE SCHEMA IF NOT EXISTS ${domain}_ci AUTHORIZATION ${domain}_role"
done

run_sql <<-SQL
	GRANT CREATE ON DATABASE ${POSTGRES_DB} TO access_admin;
	GRANT pg_read_all_data TO catalog_reader;
	-- Masking views were first created by the superuser (M8 slice 1): hand them to access_admin.
	DO \$\$
	DECLARE r record;
	BEGIN
	  FOR r IN SELECT nspname FROM pg_namespace WHERE nspname LIKE '%\_masked' LOOP
	    EXECUTE format('ALTER SCHEMA %I OWNER TO access_admin', r.nspname);
	  END LOOP;
	  FOR r IN SELECT schemaname, viewname FROM pg_views WHERE schemaname LIKE '%\_masked' LOOP
	    EXECUTE format('ALTER VIEW %I.%I OWNER TO access_admin', r.schemaname, r.viewname);
	  END LOOP;
	END
	\$\$;
SQL

echo "analytics: access_admin, catalog_reader and agent_<domain> roles ensured."
