-- Vector embeddings
CREATE EXTENSION IF NOT EXISTS vector;

-- Query observability
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Text search / fuzzy matching
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;

-- Indexing helpers
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Data types & utilities
CREATE EXTENSION IF NOT EXISTS hstore;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS tablefunc;

-- Scheduled jobs (requires shared_preload_libraries)
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Hypothetical indexes for EXPLAIN tuning
CREATE EXTENSION IF NOT EXISTS hypopg;
