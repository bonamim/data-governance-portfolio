# Airflow 3 image for local orchestration.
# Installs the platform's ingestion + orchestration capabilities so the DAGs can
# import them. Build context is the repo root (see docker-compose.airflow.yml).
FROM apache/airflow:3.3.2-python3.12

# Copy only the two platform packages the DAGs depend on (not the whole repo).
COPY --chown=airflow:0 platform/ingestion    /opt/dgp/ingestion
COPY --chown=airflow:0 platform/orchestration /opt/dgp/orchestration

# Install them into Airflow's Python environment. --no-deps on orchestration so it
# does not try to pull a different Airflow; ingestion brings boto3/psycopg/pyyaml/kaggle.
RUN pip install --no-cache-dir /opt/dgp/ingestion \
 && pip install --no-cache-dir --no-deps /opt/dgp/orchestration

# Pre-create the shared data dir owned by airflow so the named volume mounted here
# inherits that ownership (Docker seeds a fresh named volume from the image path).
RUN mkdir -p /opt/airflow/data && chown airflow:0 /opt/airflow/data
