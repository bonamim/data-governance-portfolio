# Airflow 3 image for local orchestration.
# Installs the platform's ingestion + orchestration capabilities so the DAGs can
# import them. Build context is the repo root (see docker-compose.airflow.yml).
FROM apache/airflow:3.3.2-python3.12

# Copy the platform packages the DAGs depend on (not the whole repo).
COPY --chown=airflow:0 platform/quality       /opt/dgp/quality
COPY --chown=airflow:0 platform/ingestion     /opt/dgp/ingestion
COPY --chown=airflow:0 platform/orchestration /opt/dgp/orchestration

# Install them into Airflow's Python environment. quality first (ingestion depends on
# it); --no-deps on orchestration so it does not pull a different Airflow. ingestion
# brings boto3/psycopg/pyyaml/kaggle.
RUN pip install --no-cache-dir /opt/dgp/quality \
 && pip install --no-cache-dir /opt/dgp/ingestion \
 && pip install --no-cache-dir --no-deps /opt/dgp/orchestration

# Transform tooling so the transform DAGs can run dbt build + write-audit-publish
# and generate dbt metadata from ODCS contracts.
RUN pip install --no-cache-dir \
      "dbt-core>=1.9" "dbt-postgres>=1.9" "dbt-loom>=0.7" "datacontract-cli>=1.2"

# Pre-create the shared data dir owned by airflow so the named volume mounted here
# inherits that ownership (Docker seeds a fresh named volume from the image path).
RUN mkdir -p /opt/airflow/data && chown airflow:0 /opt/airflow/data
