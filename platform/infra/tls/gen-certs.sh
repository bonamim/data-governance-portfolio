#!/usr/bin/env bash
# Local certificate authority + server certificate for the platform's TLS endpoints (X3).
#
# Writes platform/infra/tls/out/ (gitignored): ca.crt/ca.key (the local CA) and server.crt/server.key
# with SANs for every name a client uses to reach Postgres — localhost (host tools, the restore
# drill), the compose service names (containers on the platform network) and host.docker.internal
# (containers in other compose projects). Idempotent: an existing, unexpired certificate is kept.
# Production uses certificates from your CA / cert-manager instead (production-readiness §4.1).
set -euo pipefail
OUT="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$OUT"
cd "$OUT"

if [ -f server.crt ] && openssl x509 -checkend 2592000 -noout -in server.crt >/dev/null 2>&1; then
	echo "tls: certificates present and valid for 30+ days ($OUT)"
	exit 0
fi

openssl req -x509 -newkey rsa:2048 -nodes -days 825 -sha256 \
	-subj "/CN=dgp-local-ca" -keyout ca.key -out ca.crt >/dev/null 2>&1

cat > server.ext <<EXT
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,IP:127.0.0.1,DNS:host.docker.internal,DNS:postgres-oltp,DNS:postgres-analytics,DNS:postgres-airflow,DNS:drill-oltp,DNS:drill-analytics,DNS:drill-meta
EXT
openssl req -newkey rsa:2048 -nodes -sha256 -subj "/CN=dgp-postgres" \
	-keyout server.key -out server.csr >/dev/null 2>&1
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -sha256 \
	-extfile server.ext -out server.crt >/dev/null 2>&1
rm -f server.csr server.ext ca.srl
chmod 600 ca.key server.key
echo "tls: generated local CA + server certificate ($OUT)"
