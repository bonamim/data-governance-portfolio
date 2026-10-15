#!/usr/bin/env bash
# Entrypoint for every platform Postgres: TLS-only network access + audit logging (X3, X9), then the
# stock image entrypoint. One file so oltp, analytics, Airflow metadata and the restore-drill
# sandbox all run the identical hardened configuration.
#
#   ssl=on, TLS >= 1.2, certificate from platform/infra/tls (make certs)
#   pg_hba: TLS + scram for every network client, plain TCP REJECTED; the in-container unix socket
#           (init scripts, healthchecks, `docker exec psql`) stays local-only
#   log_connections / log_disconnections / log_statement=ddl: who connected, and every schema/role
#           change, in the server log (production ships it to central logging and adds pgaudit)
set -euo pipefail

install -d -o postgres -g postgres -m 700 /etc/pgtls
install -o postgres -g postgres -m 600 /pgtls-src/server.key /etc/pgtls/server.key
install -o postgres -g postgres -m 644 /pgtls-src/server.crt /etc/pgtls/server.crt
install -o postgres -g postgres -m 644 /pgconf/pg_hba.conf /etc/pgtls/pg_hba.conf
# WAL archive volume (shop_oltp, point-in-time recovery): the server process must own it.
if [ -d /wal-archive ] && [ -w /wal-archive ]; then chown postgres:postgres /wal-archive; fi  # ro in the drill

exec docker-entrypoint.sh "$@" \
	-c ssl=on \
	-c ssl_cert_file=/etc/pgtls/server.crt \
	-c ssl_key_file=/etc/pgtls/server.key \
	-c ssl_min_protocol_version=TLSv1.2 \
	-c hba_file=/etc/pgtls/pg_hba.conf \
	-c log_connections=on \
	-c log_disconnections=on \
	-c log_statement=ddl
