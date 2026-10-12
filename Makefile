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

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available commands
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

$(ENV_FILE):
	@test -f $(ENV_FILE) || { cp .env.example $(ENV_FILE); echo "created .env from .env.example — review before running in shared settings"; }

.PHONY: up
up: $(ENV_FILE) ## Start the platform (MinIO + 2 Postgres)
	$(COMPOSE) up -d --wait minio postgres-oltp postgres-analytics
	$(COMPOSE) run --rm minio-init   # create/verify buckets, then return

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

.PHONY: verify-infra
verify-infra: ## Check buckets, schemas, roles and the ingestion ledger exist
	@bash platform/infra/verify.sh

.PHONY: sync-contracts
sync-contracts: ## Generate each layer's _ports.yml from the ODCS output-port contracts
	uv run python platform/transform/sync_contracts.py

.PHONY: transform
transform: ## Build all data products (dbt) in dependency order, applying consumer grants
	uv run --group dbt dbt build --project-dir domains/customer/dbt --profiles-dir platform/transform
	uv run --group dbt dbt build --project-dir domains/customer_experience/dbt --profiles-dir platform/transform
	$(MAKE) grants
	cd domains/sales/dbt && uv run --group dbt dbt build --profiles-dir ../../../platform/transform
	$(MAKE) grants

.PHONY: grants
grants: ## Apply consumer grants from data product definitions (output ports only)
	uv run python platform/access/grant_consumers.py

.PHONY: endpoints
endpoints: ## Print local service URLs and credentials (from .env)
	@echo "UIs"
	@echo "  MinIO console   http://localhost:$(MINIO_CONSOLE_PORT)   user: $(MINIO_ROOT_USER)   pass: $(MINIO_ROOT_PASSWORD)"
	@echo "  Airflow         http://localhost:$(AIRFLOW_API_PORT)       (dev: no login)"
	@echo "  DataHub         http://localhost:9002                      (planned — M4)"
	@echo "  Superset        http://localhost:8088                      (planned — M6)"
	@echo "Databases / APIs"
	@echo "  MinIO S3 API    http://localhost:$(MINIO_API_PORT)   user: $(MINIO_ROOT_USER)   pass: $(MINIO_ROOT_PASSWORD)"
	@echo "  Postgres OLTP   localhost:$(OLTP_PORT)   db: $(OLTP_DB)   user: $(OLTP_SUPERUSER)   pass: $(OLTP_SUPERUSER_PASSWORD)"
	@echo "  Postgres WH     localhost:$(ANALYTICS_PORT)   db: $(ANALYTICS_DB)   user: $(ANALYTICS_SUPERUSER)   pass: $(ANALYTICS_SUPERUSER_PASSWORD)"

# --- Airflow 3 orchestration ------------------------------------------------
.PHONY: airflow-build
airflow-build: ## Build the Airflow image (installs ingestion + orchestration)
	$(COMPOSE_ALL) build

.PHONY: airflow-up
airflow-up: up airflow-build ## Start Airflow (needs the platform up first)
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

.PHONY: check-contracts
check-contracts: ## Enforce that every landing dataset has a valid ODCS contract
	uv run python governance/policies/check_contracts.py

.PHONY: check-data-products
check-data-products: ## Enforce the data product standard on every definition
	uv run python governance/policies/check_data_products.py

.PHONY: check-output-ports
check-output-ports: ## Publishing guardrail: output-port contract <-> model <-> definition
	uv run python governance/policies/check_output_ports.py

.PHONY: marketplace
marketplace: ## Generate the internal data product marketplace (docs/marketplace.md)
	uv run python platform/marketplace/generate.py

.PHONY: check
check: check-boundaries check-contracts check-data-products check-output-ports lint test ## Run all governance + quality checks
