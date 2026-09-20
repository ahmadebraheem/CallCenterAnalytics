FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DBT_PROFILES_DIR=/workspace/analytics/dbt
WORKDIR /workspace
COPY analytics/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && dbt --version
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY analytics /workspace/analytics
WORKDIR /workspace/analytics/dbt
ENTRYPOINT ["dbt"]
CMD ["debug"]
