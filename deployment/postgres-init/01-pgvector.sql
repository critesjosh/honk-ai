-- Mounted at /docker-entrypoint-initdb.d/01-pgvector.sql so Postgres runs
-- it once, on first database initialization. IF NOT EXISTS keeps it
-- safe to re-run manually for schema repair.
CREATE EXTENSION IF NOT EXISTS vector;
