# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## What is DocsGPT (Aztec fork)

Open-source AI platform for document-grounded Q&A. Upstream is `arc53/DocsGPT`; this is an Aztec fork at `0.17.0+aztec`. Stack: Flask backend (Python 3.12), React 19 + TypeScript frontend (Vite), **PostgreSQL** (user data + vectors via pgvector), Redis (Celery broker), Celery workers.

Aztec-specific additions on top of upstream:
- **MCP key provisioning endpoint** at `POST /api/internal/create_mcp_key` (self-authenticated via `MCP_PROVISIONING_KEY`, not `INTERNAL_KEY`) that upserts one agent per Discord identity.
- **Discord `/mcp-key` command** (`extensions/discord/bot.py`) that calls the endpoint.
- **TypeScript MCP server** (`extensions/mcp-server/`) that exposes DocsGPT agents via MCP.
- **Chunking filter** (`application/parser/chunking.py`) that discards chunks with `token_count < 50`.
- **Custom settings**: `MCP_PROVISIONING_KEY`, `AZTEC_SOURCE_IDS`, `CORS_ALLOWED_ORIGINS`.

## Development environment

**Always run services via Docker.** Do not install Postgres, Redis, or app dependencies natively.

Two composes at `deployment/`:
- `docker-compose.yaml` — **dev compose**. Builds from source, exposes ports on localhost (frontend 5173, backend 7091, Redis 6379, Postgres 5432). Use this in dev / smoke-test.
- `docker-compose-hub.yaml` — **production compose**. Builds from source, publishes only Caddy's 80/443, uses Let's Encrypt, loads secrets from `.env`. Use this on the company server.

Configuration lives in `.env` at repo root. It is gitignored. `.env-template` holds placeholders and secret-generation instructions.

Before starting services, check if containers are already running (`docker compose ... ps`). If they show as exited, just bring them back up — do not recreate from scratch.

## Common commands

### Dev / smoke test (builds from source)
```bash
docker compose -f deployment/docker-compose.yaml up -d postgres
docker compose -f deployment/docker-compose.yaml run --rm backend python scripts/db/init_postgres.py
docker compose -f deployment/docker-compose.yaml up -d
docker compose -f deployment/docker-compose.yaml logs -f backend worker
docker compose -f deployment/docker-compose.yaml down
```

First-time Postgres bootstrap:
1. Start only `postgres` first so the initdb scripts (including `postgres-init/01-pgvector.sql` which enables the `vector` extension) can run.
2. Run `python scripts/db/init_postgres.py` — thin wrapper around `alembic upgrade head` that creates all tables including the Aztec `agents.mcp_*` columns.
3. Then bring up the rest.

### Production (company server)
Identical sequence but `-f deployment/docker-compose-hub.yaml` and `.env` must set `PUBLIC_HOSTNAME`, `ACME_EMAIL`, `POSTGRES_PASSWORD`, `JWT_SECRET_KEY`, `ENCRYPTION_SECRET_KEY`, `INTERNAL_KEY`, `MCP_PROVISIONING_KEY`, `AZTEC_SOURCE_IDS`. See `.env-template` for the full list and generation commands (`openssl rand -hex 32`).

### Tests & linting (from repo root)
```bash
python -m pytest                          # unit tests
python -m pytest -m integration           # integration tests (may need Postgres)
ruff check .                              # lint
ruff format .                             # format
```
Frontend (from `frontend/`): `npm run lint`, `npm run build`.

## Architecture

### Backend (`application/`)
- Entry points: `app.py` (Flask), `wsgi.py` (gunicorn), `worker.py` (Celery).
- Blueprints: `user` (`/api/user/*`), `answer` (`/api/answer`), `internal` (`/api/internal/*`, includes the MCP endpoint), `connector`, `v1`.
- Factory pattern (`LLMCreator`, `AgentCreator`, `VectorCreator`, `RetrieverCreator`, `StorageCreator`).
- **Storage layer**: `application/storage/db/` — SQLAlchemy Core with thin repositories per table. `session.py` provides `db_session()` context manager. `models.py` holds the schema. Migrations in `application/alembic/versions/NNNN_description.py` (hand-written SQL, not autogenerate). Add new migrations by creating the next-numbered file.
- **Auth**: `AUTH_TYPE` env var (`session_jwt`, `simple_jwt`). The built-in JWT is **not** a real access boundary — anyone can call `/api/generate_token`. Real auth lives at the reverse proxy (Cloudflare Access in our production deploy).
- **Vector store**: `VECTOR_STORE=pgvector` uses `application/vectorstore/pgvector.py`, which creates its own `documents` table with an IVFFlat cosine index in the same Postgres instance. `CREATE EXTENSION vector` runs both via the initdb script and inside `pgvector.py`'s init.

### Frontend (`frontend/`)
React 19 + TypeScript + Vite 8. Redux Toolkit (`store.ts`). Radix UI + Tailwind v4.

Vite bakes env vars at build time. Production uses `frontend/Dockerfile.prod` (multi-stage: Vite build → nginx static) with `VITE_API_HOST` passed as a build ARG matching `PUBLIC_HOSTNAME`. The upstream `frontend/Dockerfile` (running `npm run dev --host`) is dev-only.

### Deployment specifics
- Postgres image: `pgvector/pgvector:pg16` (not plain postgres). Holds agents, sources, conversations, etc., AND the vector embeddings.
- Reverse proxy: Caddy (`deployment/Caddyfile`). Terminates Let's Encrypt TLS, propagates `Cf-Access-Authenticated-User-Email` to the backend as `X-Auth-Email`, trusts Cloudflare as its upstream proxy.
- SSO: **Cloudflare Access** gates traffic before it reaches Caddy. The origin should be behind a Cloudflare Tunnel or IP-restricted to Cloudflare's ranges.
- No host port publishing for postgres/redis/backend/frontend in prod — only Caddy's 80/443 face the network.

## CI/CD (upstream)
`.github/workflows/`: `pytest.yml`, `lint.yml`, `bandit.yaml`, `docker-*-build.yml`, `ci.yml`, `zizmor.yml`. Our fork inherits these.

## Code style
- **Python:** Ruff, 120 char line length. PEP 8. Type hints expected. Google-style docstrings.
- **Frontend:** ESLint + Prettier, 80 char print width, single quotes, semicolons.

## PR readiness
Before opening a PR: run `ruff check .`, `python -m pytest`, `npm run lint && npm run build` in `frontend/`. Smoke-test the dev compose. Note any config, dependency, or deployment implications (especially anything affecting `.env` or the compose).

## Key files when working on the upgrade or deployment
- `deployment/docker-compose-hub.yaml` — production compose
- `deployment/Caddyfile` — TLS + reverse proxy + Cloudflare integration
- `deployment/postgres-init/01-pgvector.sql` — creates the `vector` extension
- `frontend/Dockerfile.prod` + `frontend/nginx.conf` — production frontend
- `application/alembic/versions/0003_mcp_provisioning.py` — adds MCP columns to `agents`
- `application/storage/db/repositories/agents.py:upsert_mcp_key` — the repo method for the MCP upsert
- `application/api/internal/routes.py:create_mcp_key` — the endpoint handler
- `.env-template` — all production secrets and their generation commands
