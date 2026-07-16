FROM pgvector/pgvector:pg18

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        postgresql-18-cron \
        postgresql-18-hypopg \
 && rm -rf /var/lib/apt/lists/*
