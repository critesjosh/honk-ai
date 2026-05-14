# DocsGPT for Aztec — Deployment Guide

Aztec fork of DocsGPT, pinned to upstream tag `0.17.0` with Aztec customizations on top. This document covers both local dev and production deployment.

## Architecture (post-0.17.0)

- **PostgreSQL** holds all user data — agents, sources, conversations, prompts, attachments, workflows, logs, token usage.
- **pgvector** (same Postgres instance, different table) stores document embeddings. Upstream's FAISS index directory is no longer used.
- **Redis** is the Celery broker and cache.
- **Caddy** (production only) reverse-proxies to the backend (`/api/*`, `/stream`) and the public `/ask` page (`frontend-ask`). TLS lives at the Cloudflare edge; Caddy itself runs HTTP-only behind the tunnel.
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
# scripts/ lives at the repo root, NOT inside the backend image (the Dockerfile
# only copies application/), so bind-mount it for this one-shot bootstrap.
docker compose -f deployment/docker-compose.yaml run --rm \
  -v $(pwd)/scripts:/app/scripts:ro \
  backend python scripts/db/init_postgres.py
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
# scripts/ isn't in the backend image — bind-mount it for the one-shot bootstrap.
docker compose -f deployment/docker-compose-hub.yaml run --rm \
  -v $(pwd)/scripts:/app/scripts:ro \
  backend python scripts/db/init_postgres.py
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
3. Either provision a **Cloudflare Tunnel** from the server (`cloudflared`) — preferred, no public IP — or add a firewall rule allowing only Cloudflare's IP ranges to reach port 443. If the origin's region has flaky CF backbone routing (we hit this on LHR), anchor `cloudflared` on a bastion in a healthier region and reverse-SSH Caddy to it; this is the current production topology on `aztec.adjacentpossible.dev`. Runbook: **"Bastion-anchored Cloudflare Tunnel"** below.
4. Caddy already trusts Cloudflare as its upstream proxy (see `deployment/Caddyfile`) and propagates `Cf-Access-Authenticated-User-Email` as `X-Auth-Email` to the backend.

For programmatic clients (the Discord bot, CI), issue a **Service Token** in Cloudflare Access so they can pass the `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers without SSO.

**If you'll deploy the public `/ask` page** (step 7 below), add an Access **Bypass** policy on the same application matching path `/ask` and `/ask/*` (Action: Bypass, Include: Everyone). Without that bypass, anonymous visitors hitting `https://$PUBLIC_HOSTNAME/ask/` are redirected to SSO. The existing POST `/api/*` and `/stream` bypass that already lets the docs widget call the bot covers the API surface — no further bypass needed there.

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

### 7. (Optional) Provision the public `/ask` page

The `/ask` page is a separate Vite/React bundle (`frontend-ask/`) served at `/ask` on the apex host as an anonymous, RAG-only chat surface. It needs (a) a dedicated agent with hard guardrails and (b) the agent's bearer key baked into the build at compile time.

```bash
# 7a. Mint (or refresh) the public-web agent. The script is not in
# the backend image; bind-mount the host scripts/ directory + put
# /app on PYTHONPATH so it can import application.core.settings.
docker compose --env-file .env -f deployment/docker-compose-hub.yaml run --rm \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  -e PYTHONPATH=/app \
  -e ASK_AZTEC_AGENT_KEY="$(openssl rand -hex 32)" \
  -e ASK_AZTEC_PROMPT_ID="0780959b-3c18-4ad9-8284-691665233a6f" \
  backend python scripts/db/create_ask_aztec_public_agent.py
# Captures the printed key. The script is idempotent on
# (user_id='public-web', name='Ask Aztec — public web') — re-running
# refreshes source list / caps / prompt but PRESERVES the existing
# key so deployed bundles keep working.

# 7b. Add the printed key to .env. The hub compose's frontend-ask
# build arg is required-var-checked, so a missing key fails the build.
echo 'VITE_ASK_AZTEC_AGENT_KEY=<paste key here>' >> .env

# 7c. Build + recreate the frontend-ask service. Caddy must also be
# recreated so it picks up the @ask matcher block on first deploy.
docker compose --env-file .env -f deployment/docker-compose-hub.yaml build frontend-ask
docker compose --env-file .env -f deployment/docker-compose-hub.yaml up -d --force-recreate --no-deps frontend-ask caddy
```

Operator follow-ups (out-of-band, in the Cloudflare Zero Trust dashboard):

1. **Access Bypass for `/ask`** — see step 4 above. Without it, `GET /ask/*` redirects to SSO.
2. **WAF rate limits on `POST /stream`** — the bearer key is public, so daily DB caps (10k req / 5M tok) won't stop fast burn. Recommended:
   - ~6 req/min per `ip.src` (Managed Challenge or 10-min block).
   - ~30–50 req/hour per `ip.src` (1-hour block).
   - 32–64 KB body cap to bound history payload abuse.
3. **Rotate the key** by editing `agents.key` in Postgres directly (the provisioner deliberately preserves the existing key on re-run; rotation is a deliberate UPDATE so an accidental script invocation doesn't break every deployed bundle).

### 8. Staging → production cutover

Run the full deployment on a staging hostname first (e.g., `docs-staging.yourcompany.com`). Verify:

- [ ] `docker compose ... ps` — all services healthy.
- [ ] `psql $POSTGRES_URI -c '\dx'` — lists `vector`.
- [ ] `psql $POSTGRES_URI -c '\dt'` — all app tables present.
- [ ] Cloudflare Access challenges unauthenticated visitors.
- [ ] After login, UI loads, an agent can be created, a row lands in `agents`.
- [ ] A doc uploaded via `/api/upload` creates a `sources` row and embeddings land in the `documents` table.
- [ ] Discord `/mcp-key` command returns a key; a corresponding `agents` row has the expected `mcp_provider = 'discord'`.
- [ ] Discord `@`-mention reply includes a `-#` "Sources" footer with up to 5 cited URLs.
- [ ] (If `/ask` is deployed) `https://$PUBLIC_HOSTNAME/ask/` loads anonymously without SSO redirect; age gate appears on first visit; a starter prompt streams an answer with rendered markdown and source chips.
- [ ] `nmap` from an external host: only 443 open (+ 80 if Caddy does HTTP→HTTPS redirect).
- [ ] Retrieval quality check: ask a question you know the answer to from the Aztec corpus, confirm a sensible answer.

Only after all green: flip DNS / firewall / Cloudflare Access to the production hostname.

## Bastion-anchored Cloudflare Tunnel

`aztec.adjacentpossible.dev` ingresses through `cloudflared` on `ci-bastion.aztecprotocol.com` (AWS us-east-2), not the origin. Path: `CF edge → cloudflared on bastion → bastion 127.0.0.1:5080 → SSH -R → origin 127.0.0.1:5080 → Caddy:80 → backend/frontend-ask`. The origin's Caddy is already bound loopback-only in `deployment/docker-compose-hub.yaml` (`127.0.0.1:5080:80`); this section assumes that and only documents what's outside the compose.

**Trust expansion**: anyone with a shell on bastion can reach the origin via `curl -H "Host: $PUBLIC_HOSTNAME" http://localhost:5080/...`, bypassing Cloudflare Access. Accepted because bastion is employees-only. `/api/internal/*` still requires `INTERNAL_KEY` / `MCP_PROVISIONING_KEY`.

### Origin side

1. Mint a dedicated SSH key (don't reuse a CI key — keeps ingress revocable independently):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/aztec_docs_relay -N "" -C "aztec-docs-relay@$(hostname)"
   ```
2. Pin the bastion host key; cross-check the fingerprint against a trusted source:
   ```bash
   ssh-keyscan -t ed25519 ci-bastion.aztecprotocol.com >> ~/.ssh/known_hosts
   ssh-keygen -l -F ci-bastion.aztecprotocol.com -f ~/.ssh/known_hosts
   ```
3. Install `~/.config/systemd/user/aztec-docs-tunnel.service`:
   ```ini
   [Unit]
   Description=SSH reverse tunnel origin → ci-bastion
   After=network-online.target
   Wants=network-online.target
   [Service]
   # -R 5080: public Caddy ingress for the docs frontend / API.
   # -R 7092: honk-ai MCP container (docker-compose-hub.yaml publishes
   #   127.0.0.1:7092:7092 for the ``mcp`` service). Reached over the
   #   bastion by the claudebox-deploy host, which mirrors it back via
   #   its own ``cloxy-tunnel.service``. The MCP server runs with
   #   ``MCP_AUTH_REQUIRED=false`` in this mode; the trust boundary is
   #   the SSH-key chain itself.
   ExecStart=/usr/bin/ssh -N \
     -R localhost:5080:localhost:5080 \
     -R localhost:7092:localhost:7092 \
     -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
     -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o BatchMode=yes \
     -i %h/.ssh/aztec_docs_relay ubuntu@ci-bastion.aztecprotocol.com
   Restart=always
   RestartSec=5
   [Install]
   WantedBy=default.target
   ```
   `ExitOnForwardFailure=yes` is load-bearing: without it the SSH session can outlive a dead remote bind. Then enable:
   ```bash
   loginctl enable-linger "$(whoami)"   # one-time
   systemctl --user daemon-reload && systemctl --user enable --now aztec-docs-tunnel.service
   ```

### Bastion side

1. The bastion admin adds the origin's public key to `~ubuntu/.ssh/authorized_keys` restricted to the agreed ports and nothing else:
   ```
   restrict,permitlisten="localhost:5080",permitlisten="localhost:7092",from="<origin egress IP>" ssh-ed25519 AAAA… aztec-docs-relay@origin
   ```
   `permitlisten="localhost:7092"` is what lets josh-box reverse-forward port 7092 so the claudebox-deploy host can reach the honk-ai MCP server without a public hostname. The `-R` fails-fast under `ExitOnForwardFailure=yes` if the clause is missing.

   Separately, the claudebox-deploy host's outbound key (the one used by `cloxy-tunnel.service`) needs **`permitopen="127.0.0.1:7092"`** — *not* `permitlisten`. OpenSSH uses two different allowlists: `permitlisten` gates `-R` (which port the client may bind on the server side), `permitopen` gates `-L` / dynamic forwarding (which `host:port` the client may forward to from the server side). Mixing them up surfaces as "administratively prohibited" channel-open errors at first use, not at SSH-handshake time, so test once after each edit.
2. Install `cloudflared`, store `CF_TOKEN` in `/etc/cloudflared/aztec-docs.env` (mode `0600`), then `/etc/systemd/system/cloudflared-aztec-docs.service`:
   ```ini
   [Service]
   EnvironmentFile=/etc/cloudflared/aztec-docs.env
   ExecStart=/usr/bin/cloudflared tunnel run --token ${CF_TOKEN}
   Restart=always
   ```
   No `--url` — token-managed tunnels ignore it. The upstream URL is dashboard config and is the source of truth.
3. **In the CF Zero Trust dashboard**, set the tunnel's public hostname Service URL to `http://localhost:5080` **before** starting the bastion service. This applies to *all* connectors, so any pre-existing origin-anchored connector starts failing at the same instant — that IS the cutover moment.

### Validate, cut over, roll back

```bash
# From origin, prove the tunnel reaches Caddy. Caddy is Host-matched
# on $PUBLIC_HOSTNAME — the explicit header is mandatory.
ssh -i ~/.ssh/aztec_docs_relay ubuntu@ci-bastion.aztecprotocol.com -- \
  curl -is -H "Host: $PUBLIC_HOSTNAME" http://localhost:5080/api/health
```

Roll back by reverting the dashboard Service URL. The prod compose has no `cloudflared` service today, so if you need the old origin-anchored path back, run `cloudflared tunnel run` on the origin directly against the same token.

| Failure | Mitigation |
| --- | --- |
| Bastion down / rebooted | Reanchor: `cloudflared tunnel run` on origin against the same token, revert dashboard URL. |
| `permitlisten` doesn't include a port you tried to add | `authorized_keys` edit on bastion; the failing `-R` already restart-loops so detection is automatic. |
| CF backbone fails to us-east-2 | Provision a second relay in a different region. Today this is a single-anchor topology. |

## Capacity & SLOs (production)

| Subsystem | Limit | Source |
| --- | --- | --- |
| Gunicorn | 4 procs × 8 `gthread` = **32 in-flight slots**; each `/stream` pins a slot for the whole 5–15 s answer | `application/Dockerfile` |
| Postgres | Default `max_connections=100`. SQLAlchemy pool 10+20 per backend proc; pgvector retrieval uses its own raw `psycopg.connect()` (bypasses the pool) | `application/storage/db/engine.py` |
| pgvector | 3072-dim ⇒ **every retrieval is a seq scan** (pgvector skips IVFFlat above 2000 dims). CPU-bound on Postgres. | `application/vectorstore/pgvector.py` |
| Container caps | None set in compose. Dev compose (`docsgpt-oss-*`) competes for host RAM/CPU if up alongside prod. | `deployment/docker-compose-hub.yaml` |

SLO targets:

| Endpoint | Metric | Target |
| --- | --- | --- |
| `/stream` | P95 first-token / total | < 3 s / < 15 s |
| `/api/search` | P95 total | < 4 s |
| Any | 5xx + timeout + disconnect | < 1 % |
| Postgres | Active connections | < 80 of 100 |

**Observed ceiling**: ~10–20 concurrent `/stream` answers under current OpenRouter quotas, well below the 32-slot floor. First-failing subsystem is upstream LLM latency, not the stack. Next bottlenecks if OpenRouter is no longer the constraint: pgvector seq-scan CPU, then Postgres `max_connections` (bump in `deployment/postgres-init/` along with `shared_buffers`).

**Re-running the stress test**: `scripts/loadtest/run_stress.py` (`httpx` + manual SSE). Captures `ttft_ms` and `total_ms` per request (a client-side retrieval-vs-LLM phase split is *not* extractable — the `{type:"source"}` SSE frame is emitted at end-of-stream). Results land in `scripts/loadtest/results/<timestamp>/`. Run from outside the origin to exercise the full edge path.

## Development workflow (for code changes)

```bash
# Lint / format Python
ruff check . && ruff format .

# Unit tests
python -m pytest

# Integration tests (require Postgres)
python -m pytest -m integration

# Public /ask bundle
cd frontend-ask && npm run lint && npm run build
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

### Reading user feedback

When users react with 👍 / 👎 to one of Honk AI's reply messages, the bot
POSTs `/api/feedback` and the value lands in `conversation_messages.feedback`
(JSONB, e.g. `{"text": "like", "timestamp": "..."}`). Inspect with:

```sql
SELECT cm.timestamp, cm.feedback, LEFT(cm.prompt, 80) AS prompt
FROM conversation_messages cm
WHERE cm.feedback IS NOT NULL
ORDER BY cm.timestamp DESC
LIMIT 50;
```

Or call `GET /api/get_feedback_analytics` for aggregated counts.

The bot's in-memory `feedback_targets` map only tracks reply messages
sent **after** the bot last started, capped at the most recent 1000
answers. Reactions on older messages are silently ignored — that's by
design (cap is per-bot-process, no DB-backed persistence). Set
`AUTH_TYPE` to `simple_jwt` / `session_jwt` will break this path because
`/api/feedback` becomes a JWT-required endpoint at that point;
production currently runs anonymous (`AUTH_TYPE` unset) and the bot
relies on it.

> **⚠ Recommended:** set `ENABLE_CONVERSATION_COMPRESSION=false` in
> `.env` while this feature is live. The backend's compression path
> (`application/api/answer/routes/base.py:635`) appends an extra
> `conversation_messages` summary row when a long conversation
> crosses the compression threshold, which shifts subsequent rows'
> positions by +1 and silently desyncs the bot's `answer_count`
> counter. The bot's `set_feedback` UPDATE then matches no row,
> `/api/feedback` still 200s, and feedback for post-compression
> turns is silently dropped. The real fix (read the position the
> backend actually assigned via an SSE event or follow-up GET) is
> tracked but not yet shipped.

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

## Honk-ai MCP server

The honk-ai-host MCP server runs as a sibling Compose service
(`mcp` + `docker-proxy`) on the same machine as the Flask backend.
It exposes a **read-only** SQL / RAG / logs surface to remote
claudebox sessions over MCP-over-HTTP. The current production
deployment uses a **network-only trust model**: the MCP container's
port 7092 is bound to josh-box's loopback (`127.0.0.1:7092`) and
reached exclusively through an SSH-tunnel chain (claudebox-deploy host
→ bastion → josh-box). There is **no public Caddy route** and the
bearer middleware is **off** (`MCP_AUTH_REQUIRED=false`). Access is
gated by SSH-key possession on each hop plus the column-level Postgres
grants on the `docsgpt_mcp_ro` role; `mcp_audit` rows are still
written but `token_id` is null in this mode.

This is distinct from the public `@aztec/mcp-server` consumer-facing
MCP described above:

| Surface | Consumers | Auth | Tools |
|---|---|---|---|
| `@aztec/mcp-server` | Claude Desktop, Cursor, etc. | Per-Discord-user bearer (`agents.key`, via Discord `/mcp-key`) | `/api/search`, ripgrep |
| `application/mcp_server/` (this section) | claudebox operator sessions | SSH key possession + `docsgpt_mcp_ro` Postgres role grants | `honk_sql.*`, `honk_rag.*`, `honk_logs.*` |

To re-introduce per-issuer auth (e.g. multiple claudebox installs that
should be distinguishable in `mcp_audit`, or to re-open the public
Caddy route), flip `MCP_AUTH_REQUIRED=true` in the `mcp` service env
and restore the Caddy `@mcp` block plus a Cloudflare Access
service-token application. The bearer-issuance script
(`scripts/mcp/issue_token.py`) and the `mcp_tokens` table are
deliberately kept intact for that path.

> **Design + threat model:** `application/mcp_server/README.md` is the
> primary spec. This section is the operator runbook; the README has
> the rationale behind each tool, the role permission model, what a
> `db:read` token can and cannot read, and why `pg_read_all_data` is
> not used.

### Tool surface

- `honk_sql.execute(query, timeout_seconds, row_cap)` — single
  SELECT/WITH statement. The in-process guard rejects DML/DDL,
  multi-statement input, and **data-modifying CTEs**
  (`WITH x AS (DELETE FROM …) SELECT * FROM x`); the
  `docsgpt_mcp_ro` Postgres role rejects writes server-side via
  `default_transaction_read_only = on` and hand-listed grants.
- `honk_sql.list_tables(schema='public')` — bounded `information_schema.tables` listing.
- `honk_sql.describe(table, schema='public')` — column metadata.
- `honk_rag.search(query_text | query_vector, source_ids, k, include_full_text)` —
  pgvector similarity search wrapping the same helper the live
  retriever uses. **`source_ids` is required** — call
  `honk_rag.list_sources` first. `query_text` is embedded server-side
  using `EMBEDDINGS_BASE_URL`. `include_full_text=true` requires the
  `rag:read_full_text` scope.
- `honk_rag.list_sources()` — corpus inventory.
- `honk_logs.tail(service, lines, since, until, grep)` — recent
  stdout/stderr from a whitelisted service via
  `tecnativa/docker-socket-proxy` (`CONTAINERS=1 LOGS=1 POST=0`). 256
  KiB cap per call. The proxy lives on its own compose network
  (`docker_proxy_net`); only the `mcp` service is attached.
- `honk_logs.list_services()` — current container state for the
  whitelisted services.

### What a `db:read` token can / cannot read

The `docsgpt_mcp_ro` role does **not** have `pg_read_all_data`. It has
hand-listed `GRANT SELECT` on non-secret-bearing tables in full, and
column-level `GRANT SELECT` on the secret-bearing tables that
**excludes** bearer / OAuth / PII columns:

- excluded: `agents.{key,shared_token,incoming_webhook_token}`,
  `conversations.{api_key,shared_token}`,
  `shared_conversations.api_key`, `token_usage.api_key`,
  `stack_logs.api_key`, `connector_sessions.{session_token,token_info,user_email}`.

A leaked `db:read` token is bounded by these grants — it does NOT
elevate to "dump every Discord user's plaintext MCP bearer". When you
add a new column to one of these tables, audit
`application/alembic/versions/0006_mcp_admin_tables.py` to decide
whether to include it in the allowlist.

### First-time setup

1. Add `MCP_DB_PASSWORD` to `.env`:
   ```bash
   echo "MCP_DB_PASSWORD=$(openssl rand -hex 32)" >> .env
   ```
2. Apply the migration that creates `mcp_tokens`, `mcp_audit`, and
   the `docsgpt_mcp_ro` role (idempotent — re-running is safe):
   ```bash
   docker compose -f deployment/docker-compose-hub.yaml run --rm \
     -v "$(pwd)/scripts:/app/scripts:ro" \
     backend python scripts/db/init_postgres.py
   ```
3. Set the role's password (the migration creates the role with
   `NOLOGIN`; this enables it):
   ```bash
   . .env && docker compose -f deployment/docker-compose-hub.yaml exec postgres \
     psql -U docsgpt -d docsgpt -c \
     "ALTER ROLE docsgpt_mcp_ro WITH LOGIN PASSWORD '${MCP_DB_PASSWORD}';"
   ```
4. Bring up the new services:
   ```bash
   docker compose -f deployment/docker-compose-hub.yaml --env-file .env \
     up -d mcp docker-proxy
   ```
5. **Network-trust mode (current production):** no Caddy route, no
   Cloudflare Access application. The `mcp` service publishes
   `127.0.0.1:7092:7092` (see `deployment/docker-compose-hub.yaml`)
   and the path in is the SSH tunnel chain — josh-box's
   `aztec-docs-tunnel.service` reverse-forwards port 7092 to bastion,
   and the claudebox-deploy host's `cloxy-tunnel.service`
   local-forwards bastion's 7092 back to its own loopback. The
   bastion-side `authorized_keys` entries need different OpenSSH
   options for the two sides:
   - josh-box's key (used by `-R`): `permitlisten="localhost:7092"`
   - claudebox-deploy's key (used by `-L`): `permitopen="127.0.0.1:7092"`
   Wrong/missing option fails-fast under `ExitOnForwardFailure=yes`
   for `permitlisten`, but surfaces as channel-open errors at first
   use for `permitopen`. Test once after each edit.

   **Bearer mode (re-enable if you need per-issuer audit or want
   external access):** four moving parts must change together —
     1. Flip `MCP_AUTH_REQUIRED=true` in the `mcp` service env.
     2. Restore the Caddy `@mcp` block (see git history of
        `deployment/Caddyfile` before this PR).
     3. Create a Cloudflare Access application with both `/mcp` and
        `/mcp/*` as path matchers, service-token-only policy.
     4. Issue a bearer with `scripts/mcp/issue_token.py` — see
        "Issuing a bearer token" below.
   And then, on the claudebox side: cloxy's MCP sidecar strips
   inbound `Authorization` headers
   (`cloxy/cmd/sidecar/main.go:stripForwardedSecurityHeaders`), so a
   bearer can't ride session-container requests through to the MCP
   server. Bearer-mode revert needs a bearer-injecting local proxy on
   the claudebox-deploy host (or a cloxy patch adding per-upstream
   auth-header config) to land at the same time. Without that,
   flipping `MCP_AUTH_REQUIRED=true` breaks every honk_* tool call
   from claudebox. The two modes share the same migration; you can
   flip between them without re-bootstrapping the database.

6. **After the first ingest completes** (the worker creates the
   `documents` table on first upload), grant `docsgpt_mcp_ro` read
   access to it:
   ```bash
   docker compose -f deployment/docker-compose-hub.yaml exec postgres \
     psql -U docsgpt -d docsgpt -c \
     "GRANT SELECT ON documents TO docsgpt_mcp_ro;"
   ```
   The alembic migration grants `documents` conditionally inside a
   `DO` block, so on a fresh DB before first ingest the GRANT is a
   no-op — without this manual step `honk_rag.search` returns
   ``permission denied for table documents``. Re-running the migration
   does not retry the GRANT; this is a one-time operator step on
   first-deploy. Idempotent; safe to re-run.

### Issuing a bearer token for a claudebox group

Only applies in bearer mode (`MCP_AUTH_REQUIRED=true`). In the current
network-trust default, this step is unnecessary — the SSH tunnel chain
is the auth.

```bash
docker compose -f deployment/docker-compose-hub.yaml run --rm \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  -e PYTHONPATH=/app \
  backend python scripts/mcp/issue_token.py issue \
    --label cb-claudebox-rw \
    --scopes db:read,rag:read,logs:read
```

The plaintext is shown **once**. Copy it into the claudebox
credentials store immediately. Only the SHA-256 hash is persisted in
`mcp_tokens.token_hash`. Revoke with:

```bash
docker compose -f deployment/docker-compose-hub.yaml run --rm \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  -e PYTHONPATH=/app \
  backend python scripts/mcp/issue_token.py revoke --label cb-claudebox-rw
```

### Inspecting the audit log

Every tool call — including scope rejections (`MissingScope`) and SQL
guard rejections (`StatementRejected`) — writes one row to `mcp_audit`.
The audit context manager wraps `require_scopes` so denials show up in
the log with `status='denied'` rather than vanishing.

The role-level `default_transaction_read_only = on` would normally
block `INSERT INTO mcp_audit`; the audit path opts out with
`SET LOCAL transaction_read_only = off` for that one transaction. If
you ever see "Auditing must never crash the request" in the backend
logs paired with a Postgres permission error, the alembic 0006
migration didn't run cleanly — re-apply it.
Useful queries:

```sql
-- Recent tool activity by token
SELECT t.label, a.tool, a.status, a.row_count, a.elapsed_ms, a.ts
FROM mcp_audit a
LEFT JOIN mcp_tokens t ON t.id = a.token_id
ORDER BY a.ts DESC
LIMIT 50;

-- Denied (scope-rejection or guard-rejection) calls
SELECT a.tool, a.error, a.elapsed_ms, a.ts
FROM mcp_audit a
WHERE a.status = 'denied'
ORDER BY a.ts DESC
LIMIT 50;
```

`args_hash` is a SHA-256 over the JSON-encoded arguments — useful for
spotting repeated identical calls. The raw arguments are not stored
because SQL queries can include operator-pasted secrets / document
fragments.

### Why these specific tools

- **SQL**: claudebox sessions answering operator questions about
  conversation feedback, agent counts, source health, etc. need
  ad-hoc SELECTs without the round-trip of opening a SQL shell.
- **RAG**: claudebox audits and research sessions periodically need
  to retrieve grounded context the same way the production retriever
  does, with the same source filtering and embedding model.
- **Logs**: claudebox debugging sessions need to see recent backend /
  worker / discord-bot output without the human pasting it in. The
  logs tool is bounded by service whitelist + per-call byte cap.

The MCP server **never writes**. Mutations go through the existing
backend HTTP API or direct `psql`, both of which already have their
own access controls.

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
  frontend-ask/
    Dockerfile.prod                     # prod public /ask bundle (Vite + nginx)
    nginx.conf                          # SPA fallback under /ask + CSP
    src/                                # React/TS, parchment+chartreuse design
    public/favicon.png                  # Aztec symbol favicon
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
    discord/bot.py                      # /mcp-key, /forget-me, @-mention + reply-to-bot triggers, thread-context, citation footer
    mcp-server/                         # README pointer to @aztec/mcp-server (in-repo TS server was removed)
  scripts/db/
    init_postgres.py                    # alembic upgrade head wrapper
    create_ask_aztec_public_agent.py    # provisioner for the /ask bearer-key agent
    verify_pseudonymization.sql         # post-migration sentinels for Discord pseudonyms
```
