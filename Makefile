# Data Governance Portfolio — developer entrypoints.
# Every command loads .env and targets the platform compose file.

SHELL := /bin/bash
ENV_FILE := .env
INFRA := platform/infra/docker-compose.yml
AIRFLOW := platform/orchestration/docker-compose.airflow.yml
COMPOSE := docker compose --env-file $(ENV_FILE) -f $(INFRA)
COMPOSE_ALL := docker compose --env-file $(ENV_FILE) -f $(INFRA) -f $(AIRFLOW)

# Load .env so recipes can use $$OLTP_PORT etc.
ifneq (,$(wildcard $(ENV_FILE)))
include $(ENV_FILE)
export
endif

# Host-side Postgres clients (dbt, ingestion, access scripts, proofs) verify the server certificate
# against the local CA (X3). Postgres refuses plain TCP outright (platform/infra/postgres/pg_hba.conf).
export PGSSLMODE ?= verify-full
export PGSSLROOTCERT ?= $(CURDIR)/platform/infra/tls/out/ca.crt

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available commands
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

$(ENV_FILE):
	@test -f $(ENV_FILE) || { cp .env.example $(ENV_FILE); echo "created .env from .env.example — review before running in shared settings"; }

.PHONY: certs
certs: ## Generate the local CA + Postgres server certificate (X3; idempotent, gitignored)
	@bash platform/infra/tls/gen-certs.sh

.PHONY: up
up: $(ENV_FILE) certs ## Start the platform (MinIO + 2 Postgres)
	@python3 platform/infra/preflight.py   # X2: warns locally; DGP_ENV=prod refuses default credentials
	$(COMPOSE) up -d --wait minio postgres-oltp postgres-analytics
	$(COMPOSE) run --rm minio-init   # create/verify buckets, then return
	$(COMPOSE) up -d s3-browser      # object-storage UI on :9001

.PHONY: pgadmin
pgadmin: $(ENV_FILE) certs ## Start pgAdmin (database UI) wired to shop_oltp, analytics and the Airflow DB
	$(COMPOSE) up -d pgadmin
	@echo "pgAdmin: http://localhost:$${PGADMIN_PORT:-5050}  (dev: no login; servers preloaded under governance-platform)"

.PHONY: down
down: ## Stop the platform (keep volumes)
	$(COMPOSE) down

.PHONY: destroy
destroy: ## Stop the platform and delete all data volumes
	$(COMPOSE) down -v

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Tail logs (use SVC=minio to narrow)
	$(COMPOSE) logs -f $(SVC)

.PHONY: psql-oltp
psql-oltp: ## Open psql on shop_oltp
	$(COMPOSE) exec postgres-oltp psql -U $(OLTP_SUPERUSER) -d $(OLTP_DB)

.PHONY: psql-analytics
psql-analytics: ## Open psql on the analytics warehouse
	$(COMPOSE) exec postgres-analytics psql -U $(ANALYTICS_SUPERUSER) -d $(ANALYTICS_DB)

.PHONY: preflight
preflight: ## List default/weak credentials (X2); DGP_ENV=prod make preflight fails on any
	@PREFLIGHT_VERBOSE=1 python3 platform/infra/preflight.py

.PHONY: db-roles
db-roles: ## Ensure least-privilege roles + the DR heartbeat on a RUNNING platform (fresh volumes get them at init)
	@for svc in postgres-oltp postgres-analytics; do \
		$(COMPOSE) exec -T $$(env | grep -E '^(ACCESS_ADMIN|CATALOG_READER|LANDING_EVENTS|SQLMESH_ROLE|AGENT_[A-Z_]+)_PASSWORD=' | sed 's/^/-e /') \
			$$svc bash /docker-entrypoint-initdb.d/10-hardening-roles.sh; \
	done
	$(COMPOSE) exec -T postgres-oltp bash /docker-entrypoint-initdb.d/20-dr-heartbeat.sh

.PHONY: encrypt-at-rest
encrypt-at-rest: ## One-time: rewrite objects stored before bucket encryption with SSE (X7; idempotent)
	uv run --no-project --with boto3 python platform/infra/encrypt_existing.py

.PHONY: verify-infra
verify-infra: ## Check buckets, schemas, roles and the ingestion ledger exist
	@bash platform/infra/verify.sh

.PHONY: sync-contracts
sync-contracts: ## Generate each layer's _ports.yml from the ODCS output-port contracts
	uv run python platform/transform/sync_contracts.py

.PHONY: transform
transform: ## Build all data products (dbt) in dependency order, applying consumer grants
	uv run --group dbt dbt deps --project-dir domains/customer/dbt --profiles-dir platform/transform
	uv run --group dbt dbt build --project-dir domains/customer/dbt --profiles-dir platform/transform
	uv run --group dbt dbt deps --project-dir domains/customer_experience/dbt --profiles-dir platform/transform
	uv run --group dbt dbt build --project-dir domains/customer_experience/dbt --profiles-dir platform/transform
	$(MAKE) mask grants
	cd domains/sales/dbt && uv run --group dbt dbt deps --profiles-dir ../../../platform/transform && uv run --group dbt dbt build --profiles-dir ../../../platform/transform
	$(MAKE) mask grants

.PHONY: mask
mask: ## Apply role-based PII masking views from the ODCS masking policies (ADR-0013, run before grants)
	uv run python platform/access/apply_masking.py

.PHONY: grants
grants: ## Apply consumer grants from data product definitions (output ports only; masked views for PII)
	uv run python platform/access/grant_consumers.py

.PHONY: publish
publish: ## Write-audit-publish per model: build to a candidate, audit (contract DQ), promote on pass
	uv run --group dbt python platform/transform/publish.py

.PHONY: slim-ci
slim-ci: ## Slim CI: build only the models a change touched (state:modified+) in <domain>_ci, deferring the rest to what is published
	uv run --group dbt python platform/transform/slim_ci.py

.PHONY: backfill
backfill: ## Reprocess a window of microbatch models: make backfill DOMAIN=sales START=2017-01-01 END=2017-03-01
	@test -n "$(DOMAIN)" -a -n "$(START)" -a -n "$(END)" || { echo "usage: make backfill DOMAIN=<d> START=YYYY-MM-DD END=YYYY-MM-DD"; exit 2; }
	uv run --group dbt python platform/transform/publish.py --domain $(DOMAIN) \
		--event-time-start $(START) --event-time-end $(END)

.PHONY: endpoints
endpoints: ## Print local service URLs and credentials (from .env)
	@echo "UIs"
	@echo "  S3 browser      http://localhost:$(MINIO_CONSOLE_PORT)   (no login; browses MinIO via the S3 API)"
	@echo "  Airflow         http://localhost:$(AIRFLOW_API_PORT)       (dev: no login)"
	@echo "  DataHub         http://localhost:9002                      user: datahub   pass: datahub"
	@echo "  Superset        http://localhost:$(SUPERSET_PORT)                      user: $(SUPERSET_ADMIN)   pass: $(SUPERSET_ADMIN_PASSWORD)"
	@echo "  pgAdmin         http://localhost:$(PGADMIN_PORT)                      (dev: no login; make pgadmin)"
	@echo "Databases / APIs"
	@echo "  MinIO S3 API    http://localhost:$(MINIO_API_PORT)   user: $(MINIO_ROOT_USER)   pass: $(MINIO_ROOT_PASSWORD)"
	@echo "  Postgres OLTP   localhost:$(OLTP_PORT)   db: $(OLTP_DB)   user: $(OLTP_SUPERUSER)   pass: $(OLTP_SUPERUSER_PASSWORD)"
	@echo "  Postgres WH     localhost:$(ANALYTICS_PORT)   db: $(ANALYTICS_DB)   user: $(ANALYTICS_SUPERUSER)   pass: $(ANALYTICS_SUPERUSER_PASSWORD)"

# --- Airflow 3 orchestration ------------------------------------------------
.PHONY: airflow-build
airflow-build: ## Build the Airflow image (installs ingestion + orchestration)
	$(COMPOSE_ALL) build

.PHONY: dbt-manifests
dbt-manifests: $(ENV_FILE) ## Parse every dbt project into target/manifest.json (Cosmos renders the transform DAGs from it; dbt-loom reads it)
	@# Dependency order: sales reads its upstream domains' manifests through dbt-loom.
	@for d in customer customer_experience sales; do \
		( cd domains/$$d/dbt && uv run --group dbt dbt deps --profiles-dir ../../../platform/transform >/dev/null \
			&& uv run --group dbt dbt parse --profiles-dir ../../../platform/transform >/dev/null ) \
			|| { echo "dbt parse failed for $$d"; exit 1; }; \
		echo "  manifest: domains/$$d/dbt/target/manifest.json"; \
	done

.PHONY: airflow-up
airflow-up: up airflow-build dbt-manifests ## Start Airflow (needs the platform up first)
	$(COMPOSE_ALL) up -d --wait airflow-apiserver airflow-scheduler airflow-dag-processor airflow-triggerer
	@echo "Airflow UI: http://localhost:$(AIRFLOW_API_PORT)  (dev: no login)"

.PHONY: airflow-down
airflow-down: ## Stop Airflow (keep the platform and volumes)
	$(COMPOSE_ALL) stop airflow-apiserver airflow-scheduler airflow-dag-processor airflow-triggerer airflow-init postgres-airflow

.PHONY: airflow-dags
airflow-dags: ## List the DAGs Airflow has parsed (one bundle per domain)
	$(COMPOSE_ALL) exec airflow-scheduler airflow dags list

.PHONY: airflow-logs
airflow-logs: ## Tail Airflow logs (use SVC=airflow-scheduler to narrow)
	$(COMPOSE_ALL) logs -f $(SVC)

# --- Python workspace -------------------------------------------------------
.PHONY: install
install: ## Create the uv workspace venv and install all packages
	uv sync --all-packages

.PHONY: lint
lint: ## Lint and format-check the Python code
	uv run ruff check .
	uv run ruff format --check .

.PHONY: test
test: ## Run the Python test suite
	uv run pytest -q

.PHONY: check-boundaries
check-boundaries: ## Enforce ADR-0008 module boundaries (governance policy)
	uv run python governance/policies/check_boundaries.py

.PHONY: landing-access
landing-access: ## Regenerate landing-zone access policies from sources.yml (ADR-0024; applied by `make up`)
	uv run python platform/access/landing_access.py

.PHONY: check-landing-access
check-landing-access: ## Fail if the committed landing access policies drift from sources.yml
	uv run python platform/access/landing_access.py --check

.PHONY: check-contracts
check-contracts: ## Enforce that every landing dataset has a valid ODCS contract
	uv run python governance/policies/check_contracts.py

.PHONY: check-data-products
check-data-products: ## Enforce the data product standard on every definition
	uv run python governance/policies/check_data_products.py

.PHONY: check-sqlmesh-contracts
check-sqlmesh-contracts: ## SQLMesh port models' schema + audits match their ODCS contracts (ADR-0026)
	uv run python portability/sqlmesh/sync_contracts.py --check

.PHONY: sqlmesh-sync
sqlmesh-sync: ## Regenerate the SQLMesh port models' schema + audits from the ODCS contracts
	uv run python portability/sqlmesh/sync_contracts.py

.PHONY: sqlmesh-plan
sqlmesh-plan: $(ENV_FILE) ## Build the data products with SQLMesh (portability check, ADR-0026)
	cd portability/sqlmesh && uv run --group sqlmesh sqlmesh plan --auto-apply --no-prompts

.PHONY: check-port-versions
check-port-versions: ## Versioned output ports: successor -> deprecated + sunset; fail after the sunset (ADR-0019)
	uv run python governance/policies/check_port_versions.py

.PHONY: check-output-ports
check-output-ports: ## Publishing guardrail: output-port contract <-> model <-> definition
	uv run python governance/policies/check_output_ports.py

# ---------------------------------------------------------------- reporting (M6) ---
BI := docker compose --env-file $(ENV_FILE) -p dgp-bi -f reporting/docker-compose.superset.yml

.PHONY: bi-up
bi-up: $(ENV_FILE) ## Start Superset (dgp-bi) in the current Colima VM
	$(BI) up -d

.PHONY: bi-down
bi-down: ## Stop Superset (keeps its volume)
	$(BI) down

.PHONY: bi-publish
bi-publish: $(ENV_FILE) ## Publish the Ossie semantic models to Superset (datasets + metrics)
	set -a; . ./$(ENV_FILE); set +a; \
	export SUPERSET_URL=http://localhost:$${SUPERSET_PORT:-8088}; \
	export SUPERSET_WAREHOUSE_URI="postgresql+psycopg2://marketplace_role:$${MARKETPLACE_ROLE_PASSWORD}@host.docker.internal:$${ANALYTICS_PORT}/$${ANALYTICS_DB}"; \
	uv run --no-project --with requests --with pyyaml python platform/semantics/generate_superset.py

.PHONY: bi-check-drift
bi-check-drift: $(ENV_FILE) ## Fail if Superset's metrics drifted from the Ossie models (one definition)
	set -a; . ./$(ENV_FILE); set +a; \
	export SUPERSET_URL=http://localhost:$${SUPERSET_PORT:-8088}; \
	uv run --no-project --with requests --with pyyaml python platform/semantics/generate_superset.py --check

# ---------------------------------------------------------- disaster recovery (M8) ---
# Backups of every store that can't be regenerated go to s3://$(BACKUP_BUCKET); the restore drill
# proves them in an isolated dgp-drill sandbox (docs/disaster-recovery.md, proof P9).
RECOVERY := uv run --no-project --with boto3 --with 'psycopg[binary]' --with pyyaml python

.PHONY: backup
backup: $(ENV_FILE) ## Back up shop_oltp, the ingestion ledger, Airflow, DataHub and Superset metadata
	$(RECOVERY) platform/recovery/backup.py

.PHONY: dr-status
dr-status: $(ENV_FILE) ## Alert check: fail if the latest backup is stale or no drill passed in 7 days (X10)
	$(RECOVERY) platform/recovery/status.py

.PHONY: restore-drill
restore-drill: $(ENV_FILE) certs ## Restore the latest backup into a sandbox, rebuild analytics, check, measure RTO
	$(RECOVERY) platform/recovery/restore_drill.py

# -------------------------------------------------------------- governed AI (M7) ---
# The governed semantic MCP server. Any MCP client (Claude, ChatGPT, Copilot, Cortex) connects to
# it; only each client's wiring differs (ai/README.md). stdio = local clients; http = remote clients.
AI_MCP := uv run --no-project --with mcp --with 'psycopg[binary]' --with pyyaml python ai/semantic-mcp/server.py
# The platform-wide view uses analytics_reader; each domain agent its own agent_<domain> role (M8).
AI_MCP_ENV = export SEMANTIC_WAREHOUSE_URI="postgresql://analytics_reader:$${ANALYTICS_READER_PASSWORD}@localhost:$${ANALYTICS_PORT}/$${ANALYTICS_DB}"; \
	for d in SALES CUSTOMER MARKETPLACE CUSTOMER_EXPERIENCE REFERENCE; do \
		pw=AGENT_$${d}_PASSWORD; role=agent_$$(echo $$d | tr '[:upper:]' '[:lower:]'); \
		export SEMANTIC_WAREHOUSE_URI_$$d="postgresql://$$role:$${!pw}@localhost:$${ANALYTICS_PORT}/$${ANALYTICS_DB}"; \
	done

.PHONY: ai-mcp
ai-mcp: $(ENV_FILE) ## Run the governed semantic MCP server over stdio (local clients: Claude, Copilot)
	set -a; . ./$(ENV_FILE); set +a; $(AI_MCP_ENV); \
	$(AI_MCP)

.PHONY: ai-mcp-http
ai-mcp-http: $(ENV_FILE) ## Run it as a remote MCP server over streamable-HTTP (ChatGPT connectors, Cortex)
	set -a; . ./$(ENV_FILE); set +a; $(AI_MCP_ENV); \
	export MCP_TRANSPORT=streamable-http MCP_HOST=$${MCP_HOST:-127.0.0.1} MCP_PORT=$${MCP_PORT:-8900}; \
	$(AI_MCP)

CLAUDE ?= claude

# Governed analyst session: Claude Code whose only data path is the MCP servers. env -i drops every
# .env secret (Make exports them all); the session gets just the consumer-role URI, which the semantic
# server needs. ai/analyst.settings.json denies the shell, edits, and reads of secrets and raw data.
.PHONY: ai-chat
ai-chat: $(ENV_FILE) ## Ask the governed data in Claude Code: MCP tools only, no shell, no secrets
	@env -i HOME="$$HOME" USER="$$USER" PATH="$$PATH" TERM="$$TERM" LANG="$${LANG:-en_US.UTF-8}" TMPDIR="$$TMPDIR" \
		$${ANTHROPIC_API_KEY:+ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY"} \
		$${CLAUDE_CONFIG_DIR:+CLAUDE_CONFIG_DIR="$$CLAUDE_CONFIG_DIR"} \
		DGP_REPO="$(CURDIR)" \
		SEMANTIC_WAREHOUSE_URI="postgresql://analytics_reader:$${ANALYTICS_READER_PASSWORD}@localhost:$${ANALYTICS_PORT}/$${ANALYTICS_DB}?sslmode=verify-full&sslrootcert=$(CURDIR)/platform/infra/tls/out/ca.crt" \
		$(CLAUDE) --settings ai/analyst.settings.json --mcp-config ai/analyst.mcp.json --strict-mcp-config $(ARGS)

.PHONY: ai-agent
ai-agent: $(ENV_FILE) ## Run ONE domain's federated agent: make ai-agent DOMAIN=sales (ADR-0021)
	@test -n "$(DOMAIN)" || { echo "set DOMAIN=<domain> (see make ai-agents)"; exit 2; }
	set -a; . ./$(ENV_FILE); set +a; $(AI_MCP_ENV); \
	export DGP_SEMANTIC_DOMAIN=$(DOMAIN); \
	$(AI_MCP)

.PHONY: ai-agents
ai-agents: ## List the federated domain agents (the mesh agent registry, derived from definitions)
	uv run --no-project --with pyyaml python ai/orchestrator/registry.py

.PHONY: ai-orchestrator
ai-orchestrator: $(ENV_FILE) ## Run the orchestrator MCP gateway (routes each question to the owning domain)
	set -a; . ./$(ENV_FILE); set +a; $(AI_MCP_ENV); \
	uv run --no-project --with mcp --with 'psycopg[binary]' --with pyyaml python ai/orchestrator/server.py

# ---------------------------------------------------------------- catalog (M4) ---
# DataHub runs as the dgp-catalog capability (own compose project, shared VM — ADR-0017),
# pinned by digest. Ingestion needs the DataHub CLI on PATH:
# `uv tool install 'acryl-datahub[postgres,dbt]'`. GMS is on $(DATAHUB_MAPPED_GMS_PORT).
CATALOG := docker compose --env-file $(ENV_FILE) -p dgp-catalog --profile quickstart -f platform/catalog/docker-compose.datahub.yml

.PHONY: catalog-up
catalog-up: $(ENV_FILE) ## Start DataHub (pinned dgp-catalog compose) in the current Colima VM
	$(CATALOG) up -d

.PHONY: catalog-down
catalog-down: ## Stop DataHub (keeps its volumes)
	$(CATALOG) down

.PHONY: catalog-docs
catalog-docs: $(ENV_FILE) ## Regenerate dbt manifest+catalog artifacts for ingestion
	@# dbt_packages/ is gitignored: install the shared dgp_dbt package first, as `make transform` does.
	uv run --group dbt dbt deps --project-dir domains/customer/dbt --profiles-dir platform/transform
	uv run --group dbt dbt docs generate --project-dir domains/customer/dbt --profiles-dir platform/transform
	uv run --group dbt dbt deps --project-dir domains/customer_experience/dbt --profiles-dir platform/transform
	uv run --group dbt dbt docs generate --project-dir domains/customer_experience/dbt --profiles-dir platform/transform
	cd domains/sales/dbt && uv run --group dbt dbt deps --profiles-dir ../../../platform/transform \
		&& uv run --group dbt dbt docs generate --profiles-dir ../../../platform/transform

.PHONY: catalog-ingest
catalog-ingest: catalog-docs ## Ingest the warehouse + dbt metadata (models, lineage, tests) into DataHub
	set -a; . ./$(ENV_FILE); set +a; \
	export DATAHUB_GMS=http://localhost:$${DATAHUB_MAPPED_GMS_PORT:-8081}; \
	datahub ingest -c platform/catalog/recipes/postgres_oltp.yml; \
	datahub ingest -c platform/catalog/recipes/postgres_analytics.yml; \
	for d in customer customer_experience sales; do \
		DBT_TARGET=$(CURDIR)/domains/$$d/dbt/target DBT_PROJECT=dgp_$$d \
			datahub ingest -c platform/catalog/recipes/dbt.yml; \
	done

.PHONY: catalog-govern
catalog-govern: $(ENV_FILE) ## Push mesh governance (domains, data products, ownership) into DataHub from definitions
	set -a; . ./$(ENV_FILE); set +a; \
	export DATAHUB_GMS=http://localhost:$${DATAHUB_MAPPED_GMS_PORT:-8081}; \
	uv run --no-project --python 3.11 --with 'acryl-datahub' --with pyyaml \
		python platform/catalog/generate_governance.py

.PHONY: catalog-quality
catalog-quality: $(ENV_FILE) ## Publish landing datasets + contract-gate results (assertions) into DataHub
	set -a; . ./$(ENV_FILE); set +a; \
	export DATAHUB_GMS=http://localhost:$${DATAHUB_MAPPED_GMS_PORT:-8081}; \
	uv run --no-project --python 3.11 --with 'acryl-datahub' --with pyyaml --with 'psycopg[binary]' \
		python platform/catalog/publish_landing_quality.py

.PHONY: check-domains
check-domains: ## Domain registry (governance/domains) matches the domain folders
	uv run python governance/policies/check_domains.py

.PHONY: check-breaking
check-breaking: ## Breaking-change gate (P5): fail if a changed output-port contract breaks consumers
	uv run --group dbt python governance/policies/check_breaking_changes.py

.PHONY: prove-p2
prove-p2: ## Proof P2 — a landing file that violates its contract is rejected by the quality gate
	uv run python proofs/p2_landing_gate.py

.PHONY: prove-p6
prove-p6: $(ENV_FILE) ## Proof P6 — a backfill rewrites exactly its window; reruns are idempotent (M9, live)
	uv run --group dbt python proofs/p6_backfill.py

.PHONY: prove-p15
prove-p15: $(ENV_FILE) ## Proof P15 — per-model write-audit-publish: in-place promote, quarantine, Airflow (M9, live)
	uv run --group dbt python proofs/p15_per_model_wap.py

.PHONY: prove-p16
prove-p16: $(ENV_FILE) ## Proof P16 — dbt state + Slim CI: a change builds only what it touched (M9, live)
	uv run --group dbt python proofs/p16_slim_ci.py

.PHONY: prove-p17
prove-p17: $(ENV_FILE) ## Proof P17 — an upload alone runs ingestion; a burst is one run (M10, live + Airflow)
	uv run python proofs/p17_event_driven.py

.PHONY: prove-p18
prove-p18: $(ENV_FILE) ## Proof P18 — batch completion signals, delivery deadlines, alerts to the owner (M10, live)
	uv run python proofs/p18_completion_and_sla.py

.PHONY: prove-p1
prove-p1: $(ENV_FILE) ## Proof P1 — a domain's broken code or model fails only that domain (live + Airflow)
	uv run python proofs/p1_domain_isolation.py

.PHONY: prove-p4
prove-p4: $(ENV_FILE) ## Proof P4 — a producer's new port version rebuilds its consumers via assets, nothing else (live + Airflow)
	uv run python proofs/p4_cross_domain.py

.PHONY: prove-p19
prove-p19: $(ENV_FILE) ## Proof P19 — a versioned port: v2 beside v1, sunset, migrate, retire with sign-off (live)
	uv run --group dbt python proofs/p19_versioned_port.py

.PHONY: prove-p20
prove-p20: $(ENV_FILE) ## Proof P20 — same contracts, products and numbers on SQLMesh as on dbt (live)
	uv run --group sqlmesh python proofs/p20_engine_portability.py

.PHONY: prove-p14
prove-p14: ## Proof P14 — landing lifecycle end to end: inbox -> accepted/quarantine -> DataHub (live)
	uv run python proofs/p14_landing_lifecycle.py

.PHONY: prove-p5
prove-p5: $(ENV_FILE) ## Proof P5 — breaking change blocked, then passes with a version bump + approval
	./proofs/p5_breaking_change.sh

.PHONY: prove-p12
prove-p12: ## Proof P12 — an output port can't be published undocumented (table, columns, semantic fields)
	./proofs/p12_port_completeness.sh

.PHONY: prove-p8
prove-p8: $(ENV_FILE) ## Proof P8 — consumers see PII masked and are denied raw/staging; compliance sees raw (M8)
	uv run --no-project --with 'psycopg[binary]' python proofs/p8_access_control.py

.PHONY: prove-p9
prove-p9: $(ENV_FILE) certs ## Proof P9 — back up, restore into a sandbox, rebuild, re-check, measure RTO (M8)
	./proofs/p9_restore_drill.sh

.PHONY: prove-p13
prove-p13: $(ENV_FILE) certs ## Proof P13 — TLS-only, least privilege, audit logging, encryption at rest, gates (M8)
	$(RECOVERY) proofs/p13_hardening.py

.PHONY: prove-p10
prove-p10: $(ENV_FILE) ## Proof P10 — BI and AI compute the same number from the one Ossie definition (M7)
	set -a; . ./$(ENV_FILE); set +a; \
	export SUPERSET_URL=http://localhost:$${SUPERSET_PORT:-8088}; \
	export SEMANTIC_WAREHOUSE_URI="postgresql://analytics_reader:$${ANALYTICS_READER_PASSWORD}@localhost:$${ANALYTICS_PORT}/$${ANALYTICS_DB}"; \
	uv run --no-project --with requests --with 'psycopg[binary]' --with pyyaml python proofs/p10_ai_bi_agreement.py

.PHONY: prove-p11
prove-p11: $(ENV_FILE) ## Proof P11 — federated domain agents: isolation, deterministic routing, same number (M7)
	set -a; . ./$(ENV_FILE); set +a; \
	$(AI_MCP_ENV); \
	uv run --no-project --with 'psycopg[binary]' --with pyyaml python proofs/p11_federated_routing.py

.PHONY: prove-all
prove-all: prove-p2 prove-p5 prove-p12 ## Run the self-contained proofs (P2, P5, P12); P3/P6/P7/P10/P11 need the live platform
	@echo "Self-contained proofs passed. P3/P6/P7/P10/P11 run against the live platform (see docs/proofs.md)."

.PHONY: check-semantics
check-semantics: ## Ossie semantic models build only on governed output-port columns (ADR-0010)
	uv run python governance/policies/check_semantics.py

.PHONY: check-pinned-images
check-pinned-images: ## Every compose image and Dockerfile base is pinned by digest (X8)
	uv run python governance/policies/check_pinned_images.py

.PHONY: check-ai-agents
check-ai-agents: ## AI agents obey the agent standard: domain-scoped, isolated, owned, no raw PII (ADR-0021)
	uv run python governance/policies/check_ai_agents.py

.PHONY: check
check: check-domains check-boundaries check-contracts check-landing-access check-data-products check-output-ports check-port-versions check-sqlmesh-contracts check-semantics check-ai-agents check-pinned-images lint test ## Run all governance + quality checks
