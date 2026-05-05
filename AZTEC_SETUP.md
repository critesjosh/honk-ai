# DocsGPT for Aztec — Deployment Guide

Aztec fork of DocsGPT, pinned to upstream tag `0.17.0` with Aztec customizations on top. This document covers both local dev and production deployment.

## Architecture (post-0.17.0)

- **PostgreSQL** holds all user data — agents, sources, conversations, prompts, attachments, workflows, logs, token usage.
- **pgvector** (same Postgres instance, different table) stores document embeddings. Upstream's FAISS index directory is no longer used.
- **Redis** is the Celery broker and cache.
- **Caddy** (production only) terminates TLS via Let's Encrypt and reverse-proxies to the backend / frontend.
- **Cloudflare Access** is the authentication boundary on the public deployment — DocsGPT's built-in JWT is **not** a real auth gate.

No MongoDB. No FAISS files. Source metadata is a row in the Postgres `sources` table; embeddings live in the pgvector `documents` table.

## Quick start — local dev on this machine

Prerequisites: Docker and Docker Compose.

Minimum `.env` for dev (copy from `.env-template` and edit):

```
API_KEY=<your LLM provider key>
LLM_NAME=gpt-4o                 # or whatever your provider expects
INTERNAL_KEY=<any random string>
POSTGRES_URI=postgresql://docsgpt:docsgpt@postgres:5432/docsgpt
VECTOR_STORE=pgvector
PGVECTOR_CONNECTION_STRING=postgresql://docsgpt:docsgpt@postgres:5432/docsgpt
```

Bootstrap sequence (first run only — Postgres must come up before the app imports):

```bash
cd /workspaces/sandbox/DocsGPT
docker compose -f deployment/docker-compose.yaml up -d postgres
# initdb.d runs postgres-init/01-pgvector.sql, enabling the vector extension.
docker compose -f deployment/docker-compose.yaml run --rm backend python scripts/db/init_postgres.py
# init_postgres.py is `alembic upgrade head`. Creates all tables including the Aztec agents.mcp_* columns.
docker compose -f deployment/docker-compose.yaml up -d
```

After that, `docker compose -f deployment/docker-compose.yaml {up,down,logs,restart}` works as normal.

UI: http://localhost:5173 · API: http://localhost:7091 · Postgres: `localhost:5432` (docsgpt/docsgpt).

## Production deployment — company server

Do **not** reuse the dev compose on the public internet. Use `deployment/docker-compose-hub.yaml`.

### 1. Provision secrets

Generate once, treat as unrotatable (especially `ENCRYPTION_SECRET_KEY` and `USER_ID_PEPPER`). Populate `.env` on the server:

```bash
openssl rand -hex 32   # POSTGRES_PASSWORD
openssl rand -hex 32   # JWT_SECRET_KEY
openssl rand -hex 32   # ENCRYPTION_SECRET_KEY
openssl rand -hex 32   # INTERNAL_KEY
openssl rand -hex 32   # MCP_PROVISIONING_KEY
openssl rand -hex 32   # USER_ID_PEPPER          # required at boot; HMAC pepper for pseudonymized Discord user IDs
```

`USER_ID_PEPPER` is **required at backend boot** — the settings validator
fail-closes if it's missing, non-hex, or decodes to <16 bytes. It's also
required by migration `0005_pseudonymize_user_ids`, which aborts before
any UPDATE if the env var isn't right. **Do not rotate it** — every
existing pseudonym is derived from it, so rotation orphans every
Discord user's stored row and forces them to re-provision via
`/mcp-key`.

Minimum prod `.env`:

```
PUBLIC_HOSTNAME=docs.yourcompany.com
ACME_EMAIL=ops@yourcompany.com
POSTGRES_PASSWORD=<generated>
POSTGRES_URI=postgresql://docsgpt:${POSTGRES_PASSWORD}@postgres:5432/docsgpt
PGVECTOR_CONNECTION_STRING=${POSTGRES_URI}
VECTOR_STORE=pgvector
JWT_SECRET_KEY=<generated>
ENCRYPTION_SECRET_KEY=<generated>
INTERNAL_KEY=<generated>
MCP_PROVISIONING_KEY=<generated>
USER_ID_PEPPER=<generated>
AUTO_MIGRATE=false
AUTO_CREATE_DB=false
VERSION_CHECK=false
CORS_ALLOWED_ORIGINS=https://${PUBLIC_HOSTNAME}
CONNECTOR_REDIRECT_BASE_URI=https://${PUBLIC_HOSTNAME}/api/connectors/callback
VITE_API_HOST=https://${PUBLIC_HOSTNAME}
IMAGE_TAG=0.17.0-aztec.1
API_KEY=<your LLM provider key>
LLM_NAME=<model>
```

See `.env-template` for the full list and comments.

### 2. Build custom images

```bash
docker compose -f deployment/docker-compose-hub.yaml build
```

Tag via `IMAGE_TAG` in `.env`. Push to a private registry if the production server is different from the build host.

### 3. First-boot bootstrap (explicit, not auto-migrate)

```bash
docker compose -f deployment/docker-compose-hub.yaml up -d postgres
docker compose -f deployment/docker-compose-hub.yaml run --rm backend python scripts/db/init_postgres.py
docker compose -f deployment/docker-compose-hub.yaml up -d
```

Subsequent releases apply migrations the same way — never rely on `AUTO_MIGRATE=true` in prod:

```bash
docker compose -f deployment/docker-compose-hub.yaml --env-file .env exec backend \
  alembic -c application/alembic.ini upgrade head

# After migration 0005 (privacy / pseudonymization) ran, verify post-conditions:
docker cp scripts/db/verify_pseudonymization.sql docsgpt-aztec-postgres-1:/tmp/
docker compose -f deployment/docker-compose-hub.yaml --env-file .env exec postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/verify_pseudonymization.sql
# Every "negative sentinel" must read 0; positive sentinels >0 if any
# Discord users have provisioned an MCP key.
```

### 4. Put Cloudflare Access in front

1. Add the public hostname to Cloudflare.
2. Create an **Access application** covering `https://$PUBLIC_HOSTNAME/*` with the SSO policy of your choice.
3. Either provision a **Cloudflare Tunnel** from the server (`cloudflared`) — preferred, no public IP — or add a firewall rule allowing only Cloudflare's IP ranges to reach port 443. If your origin sits in a region where CF backbone routing has been flaky (we hit this with a LHR-anchored tunnel), the same compose can be run with cloudflared on a separate bastion in a CF-friendly region, fronted by an SSH reverse tunnel. See `PLAN-bastion-relay.md` for the topology, decisions, and a step-by-step migration runbook.
4. Caddy already trusts Cloudflare as its upstream proxy (see `deployment/Caddyfile`) and propagates `Cf-Access-Authenticated-User-Email` as `X-Auth-Email` to the backend.

For programmatic clients (the Discord bot, CI), issue a **Service Token** in Cloudflare Access so they can pass the `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers without SSO.

### 5. Re-ingest the Aztec corpus

This is a greenfield install — no Mongo data is carried forward. Re-ingest via the stock upload API:

```bash
# Upload one file
curl -X POST https://$PUBLIC_HOSTNAME/api/upload \
  -H "CF-Access-Client-Id: $CF_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_CLIENT_SECRET" \
  -F "user=system" \
  -F "name=Aztec Developer Docs" \
  -F "file=@./aztec-dev-docs.zip"

# Response contains task_id — poll until the Celery worker finishes.
curl -s "https://$PUBLIC_HOSTNAME/api/task_status?task_id=<TASK_ID>" \
  -H "CF-Access-Client-Id: $CF_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_CLIENT_SECRET"
```

DocsGPT's supported extensions: `.rst .md .mdx .pdf .txt .docx .csv .epub .html .json .xlsx .pptx`. Source code (`.nr .ts .sol .hpp .cpp` …) must be renamed to `.txt` before zipping — see the rename snippet at the bottom of this doc.

The legacy `reingest_all.py` / `reingest_batch.py` scripts (Mongo-bound) were retired at the 0.17.0 upgrade. If you need batch re-ingest tooling, write a new script against the Postgres `SourcesRepository`.

### 6. Get source UUIDs for AZTEC_SOURCE_IDS

After the corpus lands, fetch the UUIDs of the sources that should back the Discord-provisioned MCP agents:

```bash
docker compose -f deployment/docker-compose-hub.yaml exec postgres \
  psql -U docsgpt -d docsgpt \
  -c "SELECT id, name FROM sources ORDER BY created_at;"
```

Copy the relevant IDs into `.env` as `AZTEC_SOURCE_IDS=<uuid-1>,<uuid-2>,…` and restart backend + worker. The first UUID becomes `agents.source_id`; the rest go into `agents.extra_source_ids`.

### 7. Staging → production cutover

Run the full deployment on a staging hostname first (e.g., `docs-staging.yourcompany.com`). Verify:

- [ ] `docker compose ... ps` — all services healthy.
- [ ] `psql $POSTGRES_URI -c '\dx'` — lists `vector`.
- [ ] `psql $POSTGRES_URI -c '\dt'` — all app tables present.
- [ ] Cloudflare Access challenges unauthenticated visitors.
- [ ] After login, UI loads, an agent can be created, a row lands in `agents`.
- [ ] A doc uploaded via `/api/upload` creates a `sources` row and embeddings land in the `documents` table.
- [ ] Discord `/mcp-key` command returns a key; a corresponding `agents` row has the expected `mcp_provider = 'discord'`.
- [ ] `nmap` from an external host: only 443 open (+ 80 if Caddy does HTTP→HTTPS redirect).
- [ ] Retrieval quality check: ask a question you know the answer to from the Aztec corpus, confirm a sensible answer.

Only after all green: flip DNS / firewall / Cloudflare Access to the production hostname.

## Development workflow (for code changes)

```bash
# Lint / format Python
ruff check . && ruff format .

# Unit tests
python -m pytest

# Integration tests (require Postgres)
python -m pytest -m integration

# Frontend
cd frontend && npm run lint && npm run build
```

Dev containers volume-mount `application/core/model_configs.py` so changes to model metadata take effect on container restart without rebuilding.

## MCP server (LLM clients)

End users query the Aztec knowledge base from Claude Desktop, Claude
Code, Cursor, Codex, etc. via the official Aztec MCP server:

- **npm:** [`@aztec/mcp-server`](https://www.npmjs.com/package/@aztec/mcp-server)
- **source:** [`AztecProtocol/mcp-server`](https://github.com/AztecProtocol/mcp-server)

This repo provides the **key-provisioning** and **search** endpoints
that MCP server hits; the MCP server itself lives in
`aztecprotocol/mcp-server`.

### Getting a key (Discord `/mcp-key` flow)

1. Join the Noir Discord: https://discord.com/invite/JtqzkdeQ6G
2. Run `/mcp-key` in any channel.
3. The bot replies with a personal API key in an ephemeral message,
   plus a copy-pasteable config snippet for each major MCP client.

Under the hood, the bot calls `POST /api/internal/create_mcp_key` with `X-Provisioning-Key: $MCP_PROVISIONING_KEY`. The endpoint upserts an agent keyed by `(mcp_provider='discord', mcp_provider_user_id=<discord id>, mcp_purpose='aztec_mcp')` — one agent per Discord user per purpose. Returns `{api_key, created}`.

### Configuring an MCP client

No local install required — `npx -y @aztec/mcp-server` pulls the
package on demand. Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "aztec-docs": {
      "command": "npx",
      "args": ["-y", "@aztec/mcp-server"],
      "env": {
        "API_URL": "https://docs.yourcompany.com",
        "API_KEY": "your-key-from-discord"
      }
    }
  }
}
```

See the [`@aztec/mcp-server` README](https://github.com/AztecProtocol/mcp-server#readme) for Cursor / Claude Code / Codex configs. The Discord `/mcp-key` follow-up emits the same snippets pre-filled with your `API_URL`.

> **Note on semantic search.** DocsGPT-backed semantic search in
> `@aztec/mcp-server` is gated on `API_KEY` and is being added in
> [AztecProtocol/mcp-server#18](https://github.com/AztecProtocol/mcp-server/pull/18).
> Until that PR merges and a new version publishes, the MCP server
> runs in ripgrep-only mode over locally cloned Aztec docs (`API_KEY`
> is harmlessly ignored). Once it lands, semantic search activates
> automatically with no client-side config change.

## Renaming source code for ingest

```bash
# Convert .nr/.ts/.sol to .txt preserving directory structure, then zip.
mkdir -p /tmp/my-source
find /path/to/noir/code -name "*.nr" | while read f; do
  dir=$(dirname "$f")
  base=$(basename "$f")
  mkdir -p "/tmp/my-source/$dir"
  cp "$f" "/tmp/my-source/$dir/${base}.txt"
done
cd /tmp/my-source && zip -r /tmp/my-upload.zip .
```

Then upload the zip via `/api/upload` as shown in step 5 above.

## File layout

```
DocsGPT/
  .env                                  # secrets + config (gitignored)
  .env-template                         # template + generation commands
  CLAUDE.md                             # dev/architecture guide
  AZTEC_SETUP.md                        # this file
  deployment/
    docker-compose.yaml                 # dev (build from source, expose ports)
    docker-compose-hub.yaml             # prod (Caddy, pgvector, internal-only)
    Caddyfile                           # TLS + Cloudflare Access integration
    postgres-init/01-pgvector.sql       # enables vector extension on initdb
  frontend/
    Dockerfile                          # dev (Vite dev server)
    Dockerfile.prod                     # prod (Vite build + nginx static)
    nginx.conf                          # SPA fallback + cache headers
  application/
    Dockerfile                          # backend + worker image (production-grade)
    inputs/                             # raw uploaded files (named volume in prod)
    alembic/versions/
      0001_initial.py                   # upstream
      0002_app_metadata.py              # upstream
      0003_mcp_provisioning.py          # Aztec: agents.mcp_* columns
      0004_sources_is_public.py         # Aztec: source visibility model
      0005_pseudonymize_user_ids.py     # Aztec: HMAC-pseudonymized Discord IDs
    storage/db/                         # SQLAlchemy Core models + repositories
    api/internal/routes.py              # create_mcp_key endpoint
  extensions/
    discord/bot.py                      # /mcp-key, /forget-me, @-mention, thread-context
    mcp-server/                         # README pointer to @aztec/mcp-server (in-repo TS server was removed)
```
