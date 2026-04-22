# TODO — Deploy 0.17.0 Aztec fork to production server

This is the handoff checklist. The code work is done (branch `upgrade/0.17.0`, merged 0.17.0 + Aztec customizations, ported MCP endpoint to Postgres, reworked compose for public deploy). What's below is operator work on the target server.

Supporting docs already in the repo:
- **[CLAUDE.md](./CLAUDE.md)** — dev/architecture guide (read first).
- **[AZTEC_SETUP.md](./AZTEC_SETUP.md)** — end-to-end deployment guide with exact commands.
- **[.env-template](./.env-template)** — every production env var with generation commands.

## Pre-flight (before anything else)

- [ ] Push `upgrade/0.17.0` branch and the `pre-017-snapshot` tag to the private repo.
- [ ] On the server: clone the private repo, `git checkout upgrade/0.17.0`.
- [ ] Confirm Docker + Docker Compose plugin installed (`docker compose version`).
- [ ] Confirm server time is synced (NTP) — Let's Encrypt TLS issuance needs accurate time.
- [ ] Pick `PUBLIC_HOSTNAME` (e.g., `docs.yourcompany.com`) and DNS-point it at the server (A record, or behind a Cloudflare Tunnel — see step 4 below).

## 1. Generate production secrets

Generate fresh values on the server — **do not** reuse the local-dev `.env` from the codespace; those secrets have been on a shared disk and need rotating regardless.

```bash
cd /path/to/DocsGPT
cp .env-template .env
# Then fill in .env with output from these:
openssl rand -hex 32   # POSTGRES_PASSWORD
openssl rand -hex 32   # JWT_SECRET_KEY
openssl rand -hex 32   # ENCRYPTION_SECRET_KEY   ← UNROTATABLE. Pick once.
openssl rand -hex 32   # INTERNAL_KEY
openssl rand -hex 32   # MCP_PROVISIONING_KEY
```

Required `.env` values for the production compose (see `.env-template` for the full list + comments):

```env
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
AUTO_MIGRATE=false
AUTO_CREATE_DB=false
VERSION_CHECK=false
CORS_ALLOWED_ORIGINS=https://${PUBLIC_HOSTNAME}
CONNECTOR_REDIRECT_BASE_URI=https://${PUBLIC_HOSTNAME}/api/connectors/callback
VITE_API_HOST=https://${PUBLIC_HOSTNAME}
IMAGE_TAG=0.17.0-aztec.1
API_KEY=<your LLM provider key, e.g. OpenRouter>
LLM_NAME=<model id>
EMBEDDINGS_BASE_URL=https://api.openai.com    # or whichever embeddings API
EMBEDDINGS_KEY=<embeddings API key>
EMBEDDINGS_NAME=text-embedding-3-large         # or your model
DISCORD_TOKEN=<new Discord bot token>          # rotated
```

**Leave `AZTEC_SOURCE_IDS` unset until step 7** (the re-ingest produces the new UUIDs). The Mongo ObjectId values in the old local `.env` (24-char hex) will not work against Postgres.

## 2. Build the custom images

```bash
docker compose -f deployment/docker-compose-hub.yaml build
```

First build takes 5–15 min (downloads the mpnet-base-v2 embeddings model, installs Python deps, builds the frontend). If building on the same host you'll deploy to, that's it. If building elsewhere, push to a private registry and pull on the server. Image tags come from `IMAGE_TAG` in `.env`.

## 3. First-boot bootstrap (explicit DB init, not auto-migrate)

With `AUTO_MIGRATE=false` and `AUTO_CREATE_DB=false` set, Postgres schema creation is an operator step.

```bash
# a) Bring up Postgres alone first so the initdb script can run
docker compose -f deployment/docker-compose-hub.yaml up -d postgres

# b) Wait for health, then confirm pgvector is enabled
docker compose -f deployment/docker-compose-hub.yaml exec postgres \
  psql -U docsgpt -d docsgpt -c '\dx'    # expect 'vector' listed

# c) Apply Alembic migrations. NOTE: scripts/db/init_postgres.py is NOT in the
#    built image (the Dockerfile only copies application/). Use alembic directly —
#    it's what the script wraps. The migration runs through 0003_mcp_provisioning.
docker compose -f deployment/docker-compose-hub.yaml run --rm \
  -w /app/application backend alembic upgrade head

# d) Verify schema
docker compose -f deployment/docker-compose-hub.yaml exec postgres \
  psql -U docsgpt -d docsgpt -c '\dt'
docker compose -f deployment/docker-compose-hub.yaml exec postgres \
  psql -U docsgpt -d docsgpt -c "SELECT version_num FROM alembic_version;"
# expect version_num = 0003_mcp_provisioning

# e) Bring up the rest
docker compose -f deployment/docker-compose-hub.yaml up -d
```

**Optional improvement for future maintainers:** fold `scripts/db/init_postgres.py` into the backend image by changing the compose's `backend.build.context` from `../application` to `..` and the Dockerfile's `COPY . /app/application` to `COPY application /app/application` plus `COPY scripts /app/scripts`. Then `python scripts/db/init_postgres.py` works as documented upstream. Not required to ship.

## 4. Cloudflare Access (the auth boundary)

DocsGPT's built-in JWT is **not** a real auth gate — the public internet must never reach the backend unauthenticated.

- [ ] Add `$PUBLIC_HOSTNAME` to your Cloudflare zone.
- [ ] Zero Trust → Access → Applications → **Add an application (Self-hosted)** covering `https://$PUBLIC_HOSTNAME/*`.
- [ ] Create an Access **policy** on that application (e.g., Include → Emails ending in `@yourcompany.com`, or your SSO IdP).
- [ ] Choose one of:
  - **(Preferred) Cloudflare Tunnel** — install `cloudflared` on the server, connect it to the Zero Trust dashboard, route `$PUBLIC_HOSTNAME` → `http://caddy:80` (or `http://localhost:80` on the host). No public IP needed on the server. Server firewall can block all inbound.
  - **IP allowlist** — in the server firewall (ufw / cloud provider), allow 443 (and 80 for ACME HTTP-01) **only** from Cloudflare's IP ranges. Cloudflare publishes them at `https://www.cloudflare.com/ips-v4` and `ips-v6`. Keep the list fresh.
- [ ] For programmatic clients (the Discord bot on its own host, CI, monitoring), create a **Service Token** in Access. The bot presents `CF-Access-Client-Id` + `CF-Access-Client-Secret` headers to bypass the SSO browser flow. Update `extensions/discord/bot.py`'s HTTP calls to include these headers.

Caddy is already configured (`deployment/Caddyfile`) to trust Cloudflare as its upstream proxy and propagate `Cf-Access-Authenticated-User-Email` → `X-Auth-Email`.

## 5. Verify the stack is healthy

```bash
docker compose -f deployment/docker-compose-hub.yaml ps
# Expect: caddy, frontend, backend, worker, redis, postgres — all "Up" (postgres "Up (healthy)")

docker compose -f deployment/docker-compose-hub.yaml logs backend worker | grep -iE 'error|traceback'
# Should be clean (ignore any "pymongo not installed" — that's expected since VECTOR_STORE != mongodb)

# External reachability (from your laptop, not the server):
curl -I https://$PUBLIC_HOSTNAME/     # expect 200 (after SSO) via Caddy's LE cert
nmap -Pn $SERVER_IP                    # should show ONLY 443 (and 80 if you kept ACME HTTP-01)
```

Open `https://$PUBLIC_HOSTNAME/` in a browser → should hit Cloudflare Access → SSO challenge → UI loads after login.

## 6. Re-ingest the Aztec corpus (greenfield)

There is no Mongo data carried over. The old `reingest_all.py` / `reingest_batch.py` scripts were retired during the upgrade. Use the stock upload API.

Supported extensions: `.rst .md .mdx .pdf .txt .docx .csv .epub .html .json .xlsx .pptx`. Source code (`.nr .ts .sol .hpp .cpp` …) needs `.txt` rename before zipping — see the snippet at the bottom of `AZTEC_SETUP.md`.

Auth calls via Cloudflare Access service token:

```bash
export CF_CLIENT_ID=<from CF Access service token>
export CF_CLIENT_SECRET=<from CF Access service token>
export API_URL=https://$PUBLIC_HOSTNAME

curl -X POST "$API_URL/api/upload" \
  -H "CF-Access-Client-Id: $CF_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_CLIENT_SECRET" \
  -F "user=system" \
  -F "name=Aztec Developer Docs" \
  -F "file=@./aztec-dev-docs.zip"

# Poll task_status until done.
curl -s "$API_URL/api/task_status?task_id=<TASK_ID>" \
  -H "CF-Access-Client-Id: $CF_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_CLIENT_SECRET"
```

## 7. Set AZTEC_SOURCE_IDS to the new Postgres UUIDs

After each corpus source finishes ingesting, fetch its UUID:

```bash
docker compose -f deployment/docker-compose-hub.yaml exec postgres \
  psql -U docsgpt -d docsgpt \
  -c "SELECT id, name, created_at FROM sources ORDER BY created_at;"
```

Copy the UUIDs of the sources the MCP bot should serve into `.env`:

```env
AZTEC_SOURCE_IDS=<uuid-1>,<uuid-2>,<uuid-3>,<uuid-4>
```

Restart backend + worker so they reload settings:

```bash
docker compose -f deployment/docker-compose-hub.yaml restart backend worker
```

The first UUID becomes `agents.source_id`; the rest go into `agents.extra_source_ids` when an MCP agent is provisioned.

## 8. Smoke-test the MCP endpoint end-to-end

From the server (local, bypassing CF Access for the internal endpoint — it self-authenticates via `MCP_PROVISIONING_KEY`):

```bash
docker compose -f deployment/docker-compose-hub.yaml exec backend \
  curl -s -X POST http://localhost:7091/api/internal/create_mcp_key \
  -H "X-Provisioning-Key: $MCP_PROVISIONING_KEY" \
  -H "Content-Type: application/json" \
  -d '{"discord_user_id":"test-user-123","discord_username":"SmokeTest"}'
```

Expect `{"api_key":"<uuid>","created":true}` (first call) or `{"...","created":false}` (second call — upsert hit existing row).

Verify the row landed:

```bash
docker compose -f deployment/docker-compose-hub.yaml exec postgres \
  psql -U docsgpt -d docsgpt \
  -c "SELECT id, name, mcp_provider, mcp_provider_user_id, mcp_purpose, key FROM agents WHERE mcp_provider = 'discord';"
```

Run `/mcp-key` in the Noir Discord → same flow but over the network → confirm it returns a key.

## 9. Run the full test suite

The codespace couldn't run the full test suite (pytest-postgresql needs Postgres server binaries, apt was restricted). On the server:

```bash
# Install test dependencies into a venv (outside Docker) OR run inside a test container
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r application/requirements.txt
pip install pytest pytest-cov pytest-postgresql mongomock
# Need Postgres binaries on the host for pytest-postgresql (pg_ctl).
sudo apt install -y postgresql-client-16 postgresql-16

python -m pytest                  # unit tests
python -m pytest -m integration   # integration — if any require live services
ruff check .                      # already passes on our edits; runs clean on merge
```

Frontend:

```bash
cd frontend
npm ci
npm run lint
npm run build
```

Both should complete without errors.

## 10. Staging → production cutover (if you're doing a staged rollout)

Run the full deploy on a staging hostname first (e.g., `docs-staging.yourcompany.com`) behind a separate CF Access application. Work through steps 1–9. Checklist:

- [ ] All containers healthy.
- [ ] `\dt` shows all tables; `alembic_version = 0003_mcp_provisioning`.
- [ ] CF Access challenges unauthenticated requests.
- [ ] Agent creation in UI works → row lands in `agents`.
- [ ] Doc upload → Celery processes → `documents` table (pgvector) grows → retrieval returns results.
- [ ] Discord `/mcp-key` → row in `agents` with `mcp_provider='discord'`.
- [ ] External port-scan: only 443 (+ 80 if using ACME HTTP-01) open.
- [ ] Retrieval quality: ask a question you know the answer to, confirm a sensible answer.

Only once all green: flip DNS / CF Access / firewall to the production hostname.

## Known issues + gotchas

- **`application/core/model_configs.py` is mounted read-only** in the production compose. Edits require a container restart to take effect (no hot-reload in prod).
- **CLAUDE.md is tracked in our fork but gitignored in upstream 0.17.0.** The .gitignore still ignores it for untracked copies; the tracked version is preserved. If you edit it on the server, commit with `git add -f CLAUDE.md`.
- **`VITE_API_HOST` is baked at build time**, not runtime. If you change the public hostname after deploy, you need to rebuild the frontend image.
- **`ENCRYPTION_SECRET_KEY` cannot be rotated** without re-encrypting all encrypted columns (none currently, but upstream may add encrypted fields). Set it once, back it up, treat it like a root key.
- **`VERSION_CHECK=false`** in `.env` is important — the worker otherwise phones home to `gptcloud.arc53.com` on every start.
- **Codespace-specific workaround that should NOT be in the production instructions**: if you ever need to build the backend image in a constrained-network dev environment, use `DOCKER_BUILDKIT=1 docker build --network=host ...`. Not applicable to your company server.

## Rollback

If the upgrade goes sideways on the server before DNS cuts over:

```bash
docker compose -f deployment/docker-compose-hub.yaml down -v    # -v wipes the postgres volume
git checkout main                                                # back to bd03a513
# Restore FAISS/inputs from backup-pre-017.tgz if you had kept anything there.
# Bring up the old stack however it was running pre-upgrade.
```

The old MongoDB data (if any was kept) lives on the pre-upgrade machine's volume — untouched by this upgrade. `pre-017-snapshot` tag points at the last commit before the 0.17.0 merge.

## Post-deploy TODO (not blocking)

- [ ] Move `scripts/db/init_postgres.py` into the backend image (see §3's optional improvement) so the documented upstream command works.
- [ ] Add a pytest run to CI for the fork (upstream has `pytest.yml` workflow; it should still work).
- [ ] Consider an Alembic `0004_` migration that makes `ENCRYPTION_SECRET_KEY` usage explicit on an `agents.encrypted_metadata` column or similar — today the key is unused; it only matters going forward.
- [ ] Write a small batch-ingest script against `SourcesRepository` to replace the retired `reingest_all.py`/`reingest_batch.py` if you need programmatic re-ingest.
- [ ] Update the Discord bot HTTP calls to pass `CF-Access-Client-Id` + `CF-Access-Client-Secret` headers (bot hits `/api/internal/create_mcp_key` which is behind CF Access in prod).

## Key files (quick reference)

- `deployment/docker-compose-hub.yaml` — production compose
- `deployment/docker-compose.yaml` — dev compose (builds from source, exposes ports — for local smoke-test only)
- `deployment/Caddyfile` — TLS + Cloudflare trust + reverse proxy
- `deployment/postgres-init/01-pgvector.sql` — enables `vector` extension on initdb
- `frontend/Dockerfile.prod` + `frontend/nginx.conf` — production frontend
- `application/alembic/versions/0003_mcp_provisioning.py` — adds MCP columns
- `application/storage/db/repositories/agents.py::upsert_mcp_key` — the upsert
- `application/api/internal/routes.py::create_mcp_key` — the endpoint
- `.env-template` — all production secrets and generation commands
