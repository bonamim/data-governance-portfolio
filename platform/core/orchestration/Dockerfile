# Airflow 3 image for local orchestration.
# Installs the platform's ingestion + orchestration capabilities so the DAGs can
# import them. Build context is the repo root (see docker-compose.airflow.yml).
FROM apache/airflow:3.3.2-python3.12@sha256:9df9c8be4096b9cc626bd7cb1f2b8c712eef66c59c615f8c7e6200871ca10bd1

# Heavy, stable tooling first so iterating on the platform packages (below) stays a fast rebuild.
# Transform tooling so the transform DAGs can run dbt build + write-audit-publish and generate
# dbt metadata from ODCS contracts. Cosmos renders each dbt project as one task per model
# (ADR-0015), from the manifest, pinned, as it shapes every transform DAG.
RUN pip install --no-cache-dir \
      "dbt-core>=1.9" "dbt-postgres>=1.9" "dbt-loom>=0.7" "datacontract-cli>=1.2" \
      "astronomer-cosmos==1.15.1"

# DataHub lineage: the plugin emits DataFlow (DAG) / DataJob (task) entities and task->dataset
# lineage to the catalog (ADR-0018). Compatible with Airflow 3 via the OpenLineage provider.
RUN pip install --no-cache-dir "acryl-datahub-airflow-plugin==1.7.0.14"

# Pre-create the shared data dir owned by airflow so the named volume mounted here
# inherits that ownership (Docker seeds a fresh named volume from the image path).
RUN mkdir -p /opt/airflow/data && chown airflow:0 /opt/airflow/data

# The platform capabilities the DAGs depend on (volatile, so kept last for fast rebuilds).
# The ingestion DAG's publish_quality task runs the catalog publisher, and the transform DAG's grant
# task runs the access tools, and both read the definitions through the governance package.
COPY --chown=airflow:0 platform/core/contracts     /opt/dgp/contracts
COPY --chown=airflow:0 platform/quality            /opt/dgp/quality
COPY --chown=airflow:0 platform/core/ingestion     /opt/dgp/ingestion
COPY --chown=airflow:0 governance/pyproject.toml   /opt/dgp/governance/pyproject.toml
COPY --chown=airflow:0 governance/src              /opt/dgp/governance/src
COPY --chown=airflow:0 platform/context/semantics/pyproject.toml /opt/dgp/semantics/pyproject.toml
COPY --chown=airflow:0 platform/context/semantics/src            /opt/dgp/semantics/src
COPY --chown=airflow:0 platform/context/catalog/pyproject.toml   /opt/dgp/catalog/pyproject.toml
COPY --chown=airflow:0 platform/context/catalog/src              /opt/dgp/catalog/src
COPY --chown=airflow:0 platform/access/pyproject.toml            /opt/dgp/access/pyproject.toml
COPY --chown=airflow:0 platform/access/src                       /opt/dgp/access/src
COPY --chown=airflow:0 platform/core/orchestration /opt/dgp/orchestration

# Install them into Airflow's Python environment, each after what it depends on. --no-deps on
# orchestration so it does not pull a different Airflow, and the catalog's DataHub SDK comes with the
# DataHub plugin above. ingestion brings boto3/psycopg/pyyaml/kaggle.
RUN pip install --no-cache-dir /opt/dgp/contracts \
 && pip install --no-cache-dir /opt/dgp/quality \
 && pip install --no-cache-dir /opt/dgp/ingestion \
 && pip install --no-cache-dir /opt/dgp/governance \
 && pip install --no-cache-dir /opt/dgp/semantics \
 && pip install --no-cache-dir /opt/dgp/catalog \
 && pip install --no-cache-dir /opt/dgp/access \
 && pip install --no-cache-dir --no-deps /opt/dgp/orchestration
