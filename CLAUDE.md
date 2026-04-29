# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## What is DocsGPT (Aztec fork)

Open-source AI platform for document-grounded Q&A. Upstream is `arc53/DocsGPT`; this is an Aztec fork at `0.17.0+aztec`. Stack: Flask backend (Python 3.12), React 19 + TypeScript frontend (Vite), **PostgreSQL** (user data + vectors via pgvector), Redis (Celery broker), Celery workers.

Aztec-specific additions on top of upstream:
- **MCP key provisioning endpoint** at `POST /api/internal/create_mcp_key` (self-authenticated via `MCP_PROVISIONING_KEY`, not `INTERNAL_KEY`) that upserts one agent per Discord identity.
- **Discord `/mcp-key` command** (`extensions/discord/bot.py`) that calls the endpoint.
- **TypeScript MCP server** (`extensions/mcp-server/`) that exposes DocsGPT agents via MCP.
- **Chunking filter** (`application/parser/chunking.py`) that discards chunks with `token_count < 50`.
- **Path ignore list** (`application/parser/file/bulk.py` → `_IGNORED_PATH_SEGMENTS`) that skips any file under a `fixtures/`, `dumps/`, `node_modules/`, `target/`, `dist/`, `build/`, `_out/`, `__pycache__/` or `.git/` directory during ingest. Prevents blockchain-state test fixtures and build artefacts from burning embedding credits. Add new deny-list directory names here.
- **Custom settings**: `MCP_PROVISIONING_KEY`, `AZTEC_SOURCE_IDS`, `CORS_ALLOWED_ORIGINS`, `EMBEDDINGS_DIMENSION`, `RAG_MAX_DOC_TOKENS`, `VITE_DISABLE_AGENT_EDIT`.
- **RAG context cap** (`settings.RAG_MAX_DOC_TOKENS`, default 6000, set to **10000** in prod `.env`) — caps `calculate_doc_token_budget()` in `utils.py`. Upstream defaults to the full model window (~198k for Claude Sonnet 4.6), which makes every answer slow; the cap keeps generation well under the 15s ceiling while still grounded. Bumped from 6000 → 10000 when the corpus grew to 12 sources to reduce late-source starvation (ClassicRAG iterates sources FIFO and breaks when budget fills, so later sources contribute 0 docs under tight budgets).
- **CORS glob patterns** (`application/app.py:after_request`) — `CORS_ALLOWED_ORIGINS` supports `fnmatch`-style globs. Used for Netlify preview URLs (`https://deploy-preview-*--aztec-docs-dev.netlify.app`) and localhost dev (`http://localhost:*`).
- **Widget source rewriting** (`api/answer/routes/base.py:_aztec_source_url`) — remaps corpus paths in the emitted `{type: "source"}` SSE frame into public URLs. Developer Docs → `https://docs.aztec.network/developers/docs/...`; everything else → GitHub at `v4.2.0`. Strips both `.txt` (body-bearing code corpora — `Token.nr.txt` → `Token.nr`) AND `.md` (apiref corpora — `hash.nr.md` → `hash.nr`) before producing the GitHub blob URL, so users never see the parser-friendliness extension hack in citations. Sources are deduped by rewritten public URL before emission, then capped at 10 (`_MAX_SOURCES_EMITTED`). Bumped from 5 → 10 because a single answer typically references chunks across 6–10 distinct files; the old cap dropped citations from the long tail.
- **Global rerank retrieval** (`application/retriever/classic_rag.py:_get_data`, `application/vectorstore/pgvector.py:search_by_vector_with_score`) — replaces the upstream FIFO per-source loop. Embeds the question **once**, then issues a single SQL query with `WHERE source_id = ANY(%s)` against pgvector and greedy-packs the globally-sorted candidates into the token budget. Dedup key is `(source_path, leading 200 chars)` so adjacent near-identical chunks don't starve other sources. Background: with 12 sources and a 9 k budget, the FIFO version had sources #1–#2 consume the entire budget (29 chunks) and sources #4–#12 contribute zero. Direct probe of `std::hash::poseidon2` retrieved 0 chunks from the noir-stdlib corpus despite that being the canonical home of that identifier. Properties asserted by tests (`tests/test_retriever.py`, `tests/vectorstore/test_pgvector.py`): question is embedded exactly once, retrieval is invariant to `AZTEC_SOURCE_IDS` ordering, results are packed in ascending distance order, near-identical chunks are deduped, and backends without `search_by_vector_with_score` fail loudly instead of silently degrading.
- **Rephrase auth fix** (`application/retriever/classic_rag.py:__init__`) — `ClassicRAG` previously defaulted `api_key` to `settings.API_KEY`, which in prod is an agent UUID, not an OpenRouter key. `_rephrase_query()` got 401 from OpenRouter on every multi-turn query, silently fell back to the raw question, and follow-ups like "how does it differ?" went to retrieval without conversation context. Fix: default `api_key=None` so the LLM backend's own provider-key fallback (`OPEN_ROUTER_API_KEY` / `OPENAI_API_KEY`) resolves. One-line change with outsized impact on multi-turn answer quality.
- **Agent sources_list ordering fix** (`stream_processor._get_data_from_api_key`) — the upstream code dropped `agent.source_id` (primary) when `extra_source_ids` existed. Our version prepends the primary to `sources_list` so it's searched first. **Bug pattern**: editing an agent via the DocsGPT UI can re-clear `source_id` and rewrite `extra_source_ids` in a default order (often with E2E tests first) and can also swap `prompt_id`. If retrieval degrades after a UI edit, re-apply the canonical order + prompt via SQL. Prefer `VITE_DISABLE_AGENT_EDIT=true` in prod.

  **Canonical source order** (as of 2026-04-24, 12 sources total, developer-question-weighted): Dev Docs → Aztec.nr Framework → Noir Language Docs → Example Contracts → aztec.js SDK → TypeScript API → Noir stdlib → CLI → Network Docs → E2E Tests → Protocol Circuits → L1 Contracts. Source-of-truth is the `AZTEC_SOURCE_IDS` comment block in `.env`. Note: `AZTEC_SOURCE_IDS` is only consumed at **agent creation** time (`/api/internal/create_mcp_key`) — reordering `.env` does NOT affect existing agents; they must be updated via `UPDATE agents SET source_id=…, extra_source_ids=ARRAY[…]::uuid[]`.
- **SSE heartbeat** (`api/answer/routes/base.py:_iter_with_heartbeat`) — emits `: ping\n\n` every 15s on silent generator gaps so Cloudflare/Caddy don't idle-close long answers.
- **SSE `history` shape tolerance** (`stream_processor._load_conversation_history`) — accepts both a JSON-encoded string (Discord bot) and a native list (widget / spec-compliant clients). Upstream only accepted the string.
- **Gunicorn gthread workers** (`application/Dockerfile`) — 4×8 (32 concurrent slots) with `--timeout 120 --graceful-timeout 30 --keep-alive 75`. Paired with `stop_grace_period: 40s` on the backend compose service so SIGTERM drains SSE streams cleanly.
- **Reasoning-disable shim for OpenRouter** (`llm/openai.py:_should_disable_reasoning`) — for models like `x-ai/grok-4.1-fast` that emit chain-of-thought tokens by default and stream no answer content, injects `extra_body={"reasoning": {"exclude": true}}`. Extend via `_REASONING_DISABLED_MODEL_PREFIXES`.
- **Agent edit feature flag** (`VITE_DISABLE_AGENT_EDIT=true` build ARG, `frontend/Dockerfile.prod`) — hides the agent create/edit form in the UI (`NewAgent.tsx`, `AgentsList.tsx`, `AgentCard.tsx`). Replaces the form with a notice directing admins to manage agents via SQL or `/api/internal/create_mcp_key`. Use in prod to prevent UI-side config drift.
- **Canonical Aztec system prompt** (`application/prompts/aztec_4_2_0_grounded.txt`) — the production system prompt for the `Aztec 4.2.0` / `docs.aztec.network` agents. **Source of truth lives in the `prompts` table in Postgres**, the file is the audit-tracked copy. Iterated through 6 versions to fight identifier-fabrication on weaker models (the widget primary is `x-ai/grok-4.1-fast` for cost; faithfulness rules in the prompt compensate). To update: edit the file, `docker cp` into postgres, then `UPDATE prompts SET content = pg_read_file('/tmp/<file>') WHERE id='0780959b-3c18-4ad9-8284-691665233a6f';`. A sync script in `scripts/db/` would be a sensible follow-up.
- **Eval harness** (`scripts/eval/eval_retrieval.py`, `scripts/eval/golden_queries.json`) — 25 golden queries, each tagged with a `bucket` of `identifier` / `concept` / `example`, and two run modes:
  - `--mode retriever` probes `ClassicRAG._get_data()` directly. Identifier queries assert the expected `.nr` apiref file appears in the **top-3** retrieved chunks; concept queries guard against an over-eager apiref bias by asserting the first hit comes from the configured concept prefix.
  - `--mode stream` hits `/stream` end-to-end and asserts banned-identifier absence, no Markdown tables (Discord-format rule), source diversity, `max_response_time_s`, and (for identifier queries) that the **first cited source** is from the apiref corpus.
  - Includes a multi-turn follow-up query that exercises the rephrase auth fix. `--bucket {identifier,concept,example,all}` filters which queries to run. See `scripts/eval/README.md` for invocation. Run before merging any retrieval-path or system-prompt change.

## Development environment

**Always run services via Docker.** Do not install Postgres, Redis, or app dependencies natively.

Two composes at `deployment/`:
- `docker-compose.yaml` — **dev compose**. Builds from source, exposes ports on localhost (frontend 5173, backend 7091, Redis 6379, Postgres 5432). Use this in dev / smoke-test.
- `docker-compose-hub.yaml` — **production compose**. Builds from source, runs an outbound-only `cloudflared` connector (Cloudflare Tunnel), Caddy in HTTP-only mode (TLS lives at the CF edge), plus the Discord bot as a container. **No host ports are published** — nothing on this host faces the public internet. Secrets loaded from `.env`. Use this on the company server.

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

## Current production deployment (josh-box)

**Prod compose is live** on this host and reachable externally via Cloudflare Tunnel. Both composes run side-by-side — check container names to know which backend you're hitting:

| Compose | Project / container prefix | Images | Purpose |
|---|---|---|---|
| `deployment/docker-compose-hub.yaml` | `docsgpt-aztec-*` | `aztec/docsgpt:0.17.0-aztec.1`, `aztec/docsgpt-fe:0.17.0-aztec.1` | **production** — Cloudflare Tunnel → Caddy → frontend/backend; discord-bot container; no host ports |
| `deployment/docker-compose.yaml` | `docsgpt-oss-*` | `docsgpt-oss-backend:latest`, `docsgpt-oss-worker:latest`, `docsgpt-oss-frontend:latest` | dev smoke-test — publishes 5173/7091/5432/6379 on localhost |

Production config (in `.env`, shared by both composes):
- `PUBLIC_HOSTNAME=aztec.adjacentpossible.dev`
- `CLOUDFLARE_TUNNEL_TOKEN=<set>` — cloudflared connector registered to the Zero Trust dashboard; tunnel ingress routes `$PUBLIC_HOSTNAME` → `caddy:80`
- `LLM_PROVIDER=openrouter`, `LLM_NAME=z-ai/glm-4.6` — default model served by both composes
- `POSTGRES_PASSWORD=docsgpt` (TODO: rotate to `openssl rand -hex 32` before opening to real users; the `.env` comment flags this)
- `EMBEDDINGS_*` via OpenAI `text-embedding-3-large` (3072-dim)
- `AZTEC_SOURCE_IDS` points at all 12 v4.2.0 corpora (see "Data sources" below for the canonical order)
- `UPLOAD_FOLDER=/app/application/inputs` — MUST be absolute; the `uploads` named volume is mounted there, but `LocalStorage` computes its `base_dir` as `/app`, so a relative `inputs` resolves to an unmounted `/app/inputs` and every `POST /api/upload` fails with `PermissionError` on `os.makedirs`.

### Critical gotcha — source edits don't cross composes

Each compose builds its **own** images. Editing `application/*.py` and rebuilding the dev compose does NOT update the hub compose, and vice-versa. To apply source changes to production:

```bash
docker compose -f deployment/docker-compose-hub.yaml --env-file .env build backend worker frontend
docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d --force-recreate backend worker frontend
```

Before iterating on a bug someone's reporting, run `docker ps` and check whether the bug repro path is `docsgpt-aztec-*` (hub) or `docsgpt-oss-*` (dev). Iterating on the wrong compose is silent and wastes an hour.

### `.env` change propagation

`.env` is read at container startup via `env_file:` in each compose. A plain `docker compose restart` re-uses the old container's env — edits to `.env` only take effect after `up -d --force-recreate`. This bit us with `AZTEC_SOURCE_IDS` and `LLM_NAME` during the initial deploy.

## Data sources (knowledge base corpus)

> **Reproducible ingest:** the full re-ingest workflow (build all 12 corpora, upload, swap the agent's source list) lives under `scripts/ingest/` — see `scripts/ingest/README.md` for the version-bump checklist. `scripts/ingest/corpora.py` is the single source of truth for corpus paths, extensions, and transforms.

All indexed content comes from the sibling **`aztec-packages` repo** pinned at the `v4.2.0` git tag. On the dev host the repo lives at `/mnt/user-data/josh/aztec-packages/`; the worktree used for ingest is typically checked out at `/tmp/aztec-v4.2.0`:

```bash
git -C ../aztec-packages worktree add --detach /tmp/aztec-v4.2.0 v4.2.0
```

**Twelve corpora** are ingested into the `sources` table (one row per corpus, UUID auto-generated) and `documents` table (one row per chunk, pgvector 3072-dim embeddings via OpenAI `text-embedding-3-large`). UUIDs for the ones the MCP bot should serve go into `AZTEC_SOURCE_IDS` in `.env`:

| Source (display name) | Path in aztec-packages | Transform | Excludes | File ext |
|---|---|---|---|---|
| **Aztec Developer Docs v4.2.0 (clean)** | `docs/developer_versioned_docs/version-v4.2.0/` | passthrough | `docs/resources/migration_notes.*` | .md .mdx .json |
| **Aztec Network Docs v4.2.0 (clean)** | `docs/network_versioned_docs/version-v4.2.0/` | passthrough | `operators/reference/changelog/*`, `reference/changelog/*` | .md |
| **Aztec.nr Framework v4.2.0 (apiref)** | `noir-projects/aztec-nr/` | **noir_apiref** | — | .nr → .nr.md |
| Aztec Example Contracts v4.2.0 | `noir-projects/noir-contracts/contracts/` | rename_code_to_txt | — | .nr → .nr.txt |
| Aztec Protocol Circuits v4.2.0 | `noir-projects/noir-protocol-circuits/` | rename_code_to_txt | — | .nr → .nr.txt |
| aztec.js SDK v4.2.0 | `yarn-project/aztec.js/src/` | rename_code_to_txt | — | .ts → .ts.txt |
| Aztec CLI v4.2.0 | `yarn-project/cli/src/` + `yarn-project/cli-wallet/src/` | rename_code_to_txt | — | .ts → .ts.txt |
| Aztec E2E Tests v4.2.0 | `yarn-project/end-to-end/src/` | rename_code_to_txt | — | .ts → .ts.txt |
| Aztec L1 Contracts v4.2.0 | `l1-contracts/` | rename_code_to_txt | — | .sol → .sol.txt |
| Aztec TypeScript API v4.2.0 | `docs/static/typescript-api/testnet/` | passthrough | — | .md .txt |
| Noir Language Docs v4.2.0 | `noir-lang/noir` @ `842974fcf…`: `docs/docs/` | passthrough | — | .md .mdx |
| **Noir stdlib v4.2.0 (apiref)** | `noir-lang/noir` @ `842974fcf…`: `noir_stdlib/src/` | **noir_apiref** | — | .nr → .nr.md |

### Three transforms, three intents

The 12 corpora split into three buckets by transform:

- **`passthrough`** — markdown corpora ingested as-is. The DocsGPT parser allowlist accepts these natively.
- **`rename_code_to_txt`** — body-bearing source code with `.txt` appended so the parser allowlist accepts it. `Token.nr` → `aztec-nr/token/Token.nr.txt`. Used for examples / circuits / TS / Solidity, where the body IS the answer the user wants (how to write a token contract, how to call this RPC method, etc.). The original path is preserved in each chunk's `metadata.source`.
- **`noir_apiref`** — `.nr` files run through `scripts/ingest/noir_apiref.py` to produce a Markdown view of just the public surface (doc comments + signatures, no bodies, no `//` line comments, no `#[test]` items). Output is `foo.nr.md`. Used for `aztec-nr` and `noir-stdlib`, where users asking about identifier signatures need to see the API ref, not implementation noise. (See `PLAN-rag-apiref.md` for the why.) The chunker (`application/parser/chunking.py`) detects these by file extension via `application/parser/file/bulk.py` and tags them `chunk_type=apiref`, exempting them from the global `<50` token discard so signature-only chunks survive.

### Per-corpus exclusions (`exclude_paths`)

`SourceTree.exclude_paths` (in `scripts/ingest/corpora.py`) is a tuple of fnmatch patterns evaluated against each file's path relative to the source tree. Used for surgical removal of transitional content that mentions every renamed identifier in both spellings — those files embed well for identifier queries but are never the canonical answer. Currently:

- **Aztec Developer Docs** excludes `docs/resources/migration_notes.*` (was 285 chunks at v4.2.0 — biggest single source of off-target citations on the production widget).
- **Aztec Network Docs** excludes `operators/reference/changelog/*` and `reference/changelog/*` (~60 chunks of release notes).

If migration content becomes a recurring user need, the cleaner long-term fix is a separate "migration" corpus that's only included when the query is migration-shaped (Phase 4 of `PLAN-rag-apiref.md`).

### How ingest runs
The full reproducible workflow is in `scripts/ingest/README.md`. Short version:

1. `python -m scripts.ingest.build --aztec-pkg /tmp/aztec-vNEW --noir /tmp/noir-vNEW --out /tmp/build` builds zips for all 12 corpora.
2. `python -m scripts.ingest.upload --build-dir /tmp/build --base-url ... --token "$INTERNAL_KEY" --out /tmp/build/upload_manifest.json` uploads each zip to `POST /api/upload`, polls `GET /api/task_status?task_id=...` until `SUCCESS`, captures the resulting `sources.id` per corpus.
3. `python -m scripts.ingest.swap_sources --upload-manifest ... --agent-id $PROD_AGENT_ID --out /tmp/swap.sql` generates SQL (does NOT execute) to point the agent's `source_id` + `extra_source_ids` at the new corpora, plus the canonical-order `AZTEC_SOURCE_IDS` block to paste into `.env`.
4. Run `psql -f /tmp/swap.sql`, update `.env`, then `docker compose … up -d --force-recreate backend worker`.

Worker internals: walks the extracted tree, chunks each file, drops chunks with `token_count < 50` (UNLESS `metadata.chunk_type == "apiref"` — see `application/parser/chunking.py`), embeds survivors via OpenAI, writes to `documents`.

Re-ingest: today the endpoint has **no idempotency**. Re-uploading the same zip with the same name creates a duplicate `sources` row and duplicate `documents` chunks — burning OpenAI credits. To cleanly re-ingest, first wipe the old source: `DELETE FROM sources WHERE name = '...';` then `PGVectorStore.delete_index()` (or `DELETE FROM documents WHERE source_id = '<uuid>';`) before POSTing again. An idempotent `?replace=true` path is scoped in `TODO.md` post-deploy items.

### What is NOT indexed
Intentionally excluded (per MCP resource scope): current-unversioned docs under `docs/docs-developers/`, Barretenberg (`barretenberg/` C++/Rust/TS, ~2.4k files), patterns/howto guides that don't yet exist as a distinct folder in v4.2.0.

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
