# Honk-ai-host MCP server

Host-side MCP server that exposes a **read-only** SQL / RAG / logs surface
on the production docsgpt host to remote claudebox sessions. Lives next
to the Flask backend in `deployment/docker-compose-hub.yaml` as the
`mcp` service.

**This is not the consumer-facing MCP.** The npm package
[`@aztec/mcp-server`](https://github.com/AztecProtocol/mcp-server) that
Discord users install via `/mcp-key` only sees `/api/search`. The server
documented here is for claudebox operator sessions and has direct read
access to the docsgpt Postgres database, the pgvector RAG store, and
recent container stdout.

| Surface | Consumers | Auth | Tools |
| --- | --- | --- | --- |
| `@aztec/mcp-server` | Claude Desktop / Cursor / Codex | Per-Discord-user bearer (`agents.key`) | `/api/search`, ripgrep |
| This server | claudebox operator sessions | CF Access service token **+** per-token bearer (`mcp_tokens`) | `honk_sql.*`, `honk_rag.*`, `honk_logs.*` |

## Tool surface

- `honk_sql.execute(query, timeout_seconds=30, row_cap=500)` — single
  SELECT/WITH statement against the docsgpt database. Multi-statement,
  DML, DDL, **and data-modifying CTEs** are rejected by an in-process
  guard; the `docsgpt_mcp_ro` Postgres role rejects writes server-side.
  Server clamps `row_cap ≤ 5000` and `timeout_seconds ≤ 120`.
- `honk_sql.list_tables(schema="public")` — bounded
  `information_schema.tables` listing.
- `honk_sql.describe(table, schema="public")` — column metadata.
- `honk_rag.search(query_text | query_vector, source_ids, k=8, include_full_text=False)` —
  pgvector similarity search wrapping the same helper the live
  retriever uses. **`source_ids` is required** — call `honk_rag.list_sources`
  first to discover available corpora. `query_text` is embedded
  server-side using `EMBEDDINGS_BASE_URL`. `include_full_text=true`
  needs the separate `rag:read_full_text` scope; without it, `text` is
  capped at 1 KiB per row.
- `honk_rag.list_sources()` — corpus inventory.
- `honk_logs.tail(service, lines=200, since=None, until=None, grep=None)` —
  recent stdout/stderr from a whitelisted service via
  `tecnativa/docker-socket-proxy` (`CONTAINERS=1 LOGS=1 POST=0`). 256
  KiB cap per call. `next_since` from the response paginates.
- `honk_logs.list_services()` — current container state.

## Per-call analytics for `/api/search`

`/api/search` (the endpoint `@aztec/mcp-server` calls — see the
consumer surface in the table above) writes one `user_logs` row per
authenticated call. Analytics fields live in
**`user_logs.metadata`** (jsonb, added by alembic 0007), which the
`docsgpt_mcp_ro` role has SELECT on. Bearer keys are *never* persisted
— attribution comes from `user_logs.user_id` (the agent owner's
pseudonym) and `metadata->>'agent_id'`.

`metadata` keys for `endpoint='api_search'` rows:
- `action` — constant `"api_search"`
- `agent_id` — UUID string of the agent whose key authenticated the call
- `question` — the query text (clipped to 10k chars)
- `chunks_requested` — the clamped `chunks` parameter
- `result_count` — number of results returned (0 if the agent has no sources)
- `sources` — list of public-URL-rewritten source URLs from the response
- `timestamp` — ISO-8601 UTC string captured at log-write time

`user_logs.data` is still the request-log column used by `/stream` and
carries bearer keys; it remains off-limits to `docsgpt_mcp_ro`. For
`/api/search` rows, `data` is NULL.

Example query — top 10 most-active MCP-key holders in the last 7 days:

```sql
SELECT user_id, COUNT(*) AS calls
FROM user_logs
WHERE endpoint = 'api_search'
  AND timestamp > now() - interval '7 days'
GROUP BY user_id
ORDER BY calls DESC
LIMIT 10;
```

## Defense-in-depth

1. **Cloudflare Access at the edge** — operator adds a service-token-only
   Access application gating both `/mcp` and `/mcp/*` on the apex
   hostname. SSO clients are rejected at the edge; only requests carrying
   `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers reach Caddy.
2. **Per-token bearer** — `mcp_tokens` stores SHA-256 hashes of
   operator-issued bearers, the granted scopes, expiration, and
   revocation timestamp. Plaintext is shown once at issuance.
3. **Least-privilege Postgres role** — `docsgpt_mcp_ro` is created by
   alembic 0006 with:
   - `default_transaction_read_only = on` at the role level,
   - hand-listed `GRANT SELECT` on non-secret-bearing tables in full,
   - **column-level `GRANT SELECT` on `agents`, `conversations`,
     `shared_conversations`, `token_usage`, `stack_logs`, and
     `connector_sessions`** — the bearer / OAuth / PII columns
     (`agents.key`, `conversations.api_key`,
     `connector_sessions.session_token`, …) are **not** granted to this
     role. A SQL guard bypass that reached the database would still hit
     `ERROR: permission denied for column key`.
   - `INSERT` on `mcp_audit` only. The audit-insert path explicitly
     issues `SET LOCAL transaction_read_only = off` for that single
     transaction so the role's read-only default doesn't block its own
     audit log.
4. **In-process SQL guard** (`sql_guard.py`) — rejects multi-statement
   input, anything whose first non-comment token is not SELECT/WITH,
   and any data-modifying keyword (`DELETE` / `INSERT` / `UPDATE` /
   `MERGE` / `TRUNCATE` / `COPY` / DDL) appearing outside string
   literals, identifier quotes, or dollar-quoted text. Belt and
   braces with the role layer; gives a clean operator-facing error
   instead of a Postgres permission failure 30s into the call.
5. **`tecnativa/docker-socket-proxy`** with `CONTAINERS=1 LOGS=1
   POST=0` only — the MCP container never touches `/var/run/docker.sock`,
   so a code-execution bug here cannot start, stop, or exec into
   containers. **The proxy is on its own compose network
   (`docker_proxy_net`)** with only the `mcp` service attached, so a
   compromise of `backend` or `worker` cannot reach the unauthenticated
   Docker API directly.
6. **Per-call audit row** — every tool invocation produces one
   `mcp_audit` row regardless of outcome (`ok` / `denied` / `error`).
   Records `token_id`, `tool`, SHA-256 of the JSON-encoded arguments
   (raw arguments are NOT stored), status, row/byte count, elapsed_ms,
   and exception class name on failure. The audit_call context wraps
   the scope check too, so `MissingScope` denials are visible in the
   log.

## Boot path

`python -m application.mcp_server` (`__main__.py`) →
`server.run(transport="http", host=MCP_HOST, port=MCP_PORT)`
where:

1. The Postgres connection string for the read-only role is resolved
   from `MCP_DB_URL` (preferred), `PGVECTOR_CONNECTION_STRING`, then
   `POSTGRES_URI`. In prod, `MCP_DB_URL` is set to the `docsgpt_mcp_ro`
   role and the others stay as the privileged `docsgpt` role used by
   the backend.
2. `TokenStore` opens a connection per `verify()` call to look up the
   bearer's SHA-256 hash in `mcp_tokens`. Constant-time compare,
   revoke/expiry check. Returns `TokenInfo(token_id, label, scopes)`.
3. `FastMCP` mounts the streamable-HTTP transport at the configured
   path. Caddy strips `/mcp/` before forwarding so the upstream sees
   the mount root.
4. Each tool handler enters an `audit_call` context first, then
   `require_scopes` (so scope failures show up in `mcp_audit`), then
   the actual work via `asyncio.to_thread` (psycopg / requests are
   sync).

## Deployment

See `AZTEC_SETUP.md` → "Honk-ai MCP server" for the full first-time
runbook. Short version:

```bash
# 1. Generate role password
echo "MCP_DB_PASSWORD=$(openssl rand -hex 32)" >> .env

# 2. Run alembic 0006 (creates mcp_tokens, mcp_audit, docsgpt_mcp_ro)
docker compose -f deployment/docker-compose-hub.yaml run --rm \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  backend python scripts/db/init_postgres.py

# 3. Enable the role (alembic creates it NOLOGIN)
. .env && docker compose -f deployment/docker-compose-hub.yaml exec postgres \
  psql -U docsgpt -d docsgpt -c \
  "ALTER ROLE docsgpt_mcp_ro WITH LOGIN PASSWORD '${MCP_DB_PASSWORD}';"

# 4. Bring up mcp + docker-proxy
docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d \
  mcp docker-proxy

# 5. Add the Caddy route (already in deployment/Caddyfile) + a
#    Cloudflare Access application gating /mcp and /mcp/* with a
#    service-token-only policy.

# 6. Issue at least one bearer for a claudebox group:
docker compose -f deployment/docker-compose-hub.yaml run --rm \
  -v "$(pwd)/scripts:/app/scripts:ro" -e PYTHONPATH=/app \
  backend python scripts/mcp/issue_token.py issue \
    --label cb-claudebox-rw --scopes db:read,rag:read,logs:read
```

The plaintext bearer prints **once**. Copy it into the claudebox
credentials store immediately; only the SHA-256 hash is persisted in
`mcp_tokens.token_hash`.

## Threat model — what a leaked `db:read` token can do

The Postgres role's grants are deliberately hand-listed, so a leaked
`db:read` token (or a SQL-guard bypass that reaches the database) is
bounded by what the migration explicitly granted:

| Can read | Cannot read |
| --- | --- |
| All tables in `_FULL_SELECT_TABLES` (users, prompts, sources, attachments, conversation_messages, workflows, documents, mcp_audit, …) | `agents.key`, `agents.shared_token`, `agents.incoming_webhook_token` |
| Most columns of `agents`, `conversations`, `shared_conversations`, `token_usage`, `stack_logs`, `connector_sessions` | `conversations.api_key`, `conversations.shared_token` |
| `mcp_tokens` (token hashes, scopes, metadata) | `connector_sessions.session_token`, `connector_sessions.token_info`, `connector_sessions.user_email` |
| `mcp_audit` (full history of MCP calls — args are SHA-256 hashed) | `shared_conversations.api_key`, `token_usage.api_key`, `stack_logs.api_key` |

Treat a `db:read` token as roughly equivalent to a read-only Postgres
shell on a non-secret-bearing subset of the schema. **It does NOT
elevate to "every Discord user's MCP bearer" or "every OAuth refresh
token"** — those columns were explicitly excluded by alembic 0006.

When you add a new column to one of the secret-bearing tables, audit
`application/alembic/versions/0006_mcp_admin_tables.py` at the same
time. New columns are unreadable until you explicitly add them to the
column allowlist.

## Why not `pg_read_all_data`?

The original draft of this PR used `GRANT pg_read_all_data TO
docsgpt_mcp_ro`. That role-attribute is a blanket SELECT on every
column of every table in every database, **including columns added by
future migrations**. The threat model assumed "`db:read` operator can
inspect the schema and counts"; with `pg_read_all_data` the actual
posture was "`db:read` operator can dump every Discord user's plaintext
bearer". The blanket grant was replaced with the hand-listed grants
described above before this PR shipped — see commit history.

## Tests

Unit tests live in `tests/mcp_server/`:

- `test_sql_guard.py` — accept/reject parametrisation including
  E-strings, dollar-quoted text, quoted identifiers, data-modifying
  CTEs, forbidden-keyword-inside-strings false-positive guards.
- `test_auth.py` — hash determinism, scope enforcement, env-var
  precedence in the connection-string resolver.
- `test_audit.py` — args hashing, `MissingScope` / `StatementRejected`
  → `status="denied"` mapping, exception status mapping.
- `test_logs_filter.py` — Docker logs frame demultiplexer, grep regex
  validation, service allowlist.
- `test_rag_arg_validation.py` — `source_ids` is required, exactly one
  of `query_text` / `query_vector` must be supplied.

Integration tests against a real Postgres + Docker proxy live under
`tests/mcp_server/integration/` (skipped by default; run with
`pytest -m integration`).

## File layout

```
application/mcp_server/
├── __init__.py        # package metadata
├── __main__.py        # `python -m application.mcp_server` entry point
├── server.py          # FastMCP wiring, audit + scope decorators, tool registry
├── auth.py            # TokenStore, hash_token, scope constants, MissingScope
├── audit.py           # audit_call context manager (per-call mcp_audit row)
├── sql_guard.py       # statement-level SELECT/WITH guard + write-keyword scan
└── tools/
    ├── sql.py         # honk_sql.{execute,list_tables,describe}
    ├── rag.py         # honk_rag.{search,list_sources}
    └── logs.py        # honk_logs.{tail,list_services}

scripts/mcp/
└── issue_token.py     # issue / revoke / list bearer tokens (privileged role)

application/alembic/versions/
└── 0006_mcp_admin_tables.py   # mcp_tokens, mcp_audit, docsgpt_mcp_ro role + grants
```
