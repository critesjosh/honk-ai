# CLAUDE.md

Repo-specific instructions for Claude Code. Long-form architecture / operator content lives in `README.md`, `AZTEC_SETUP.md`, and per-feature READMEs (linked below) — this file is only for **rules that change how Claude works in this repo**.

## Read first

- **Docker only.** Do not install Postgres, Redis, or app deps natively. All services run via the composes in `deployment/`.
- **Hub vs dev compose.** Two stacks usually run side-by-side on this host. Before debugging a reported issue, run `docker ps` to see whether the symptom comes from `docsgpt-aztec-*` (prod, `deployment/docker-compose-hub.yaml`) or `docsgpt-oss-*` (dev, `deployment/docker-compose.yaml`). Iterating on the wrong one is silent and costs an hour.
- **Prod commands** use `docker compose -f deployment/docker-compose-hub.yaml --env-file .env …`.
- **`.env` changes need `up -d --force-recreate`**, not `restart` (restart re-uses the old container env).
- **Source edits don't cross composes.** Each builds its own images — rebuilding dev does NOT update hub and vice versa.
- **`scripts/` is at repo root and NOT copied into the backend image.** Bind-mount with `-v $(pwd)/scripts:/app/scripts:ro` when running scripts via `docker compose run --rm backend …`.

## Repo map

| Path | What's there |
|---|---|
| `application/` | Flask backend, RAG pipeline, blueprints (`user`, `answer`, `internal`, `connector`, `v1`), Celery worker, alembic migrations |
| `application/api/answer/CITATION_MARKER.md` | Citation-marker filter + inline scrubber + audit fields |
| `application/mcp_server/README.md` | Host-side operator MCP (`honk_sql.*` / `honk_rag.*` / `honk_logs.*`) design + threat model |
| `extensions/discord/README.md` | Honk AI Discord bot — guild allowlist, threads, spend cap, 429 breaker, feedback |
| `extensions/slack/README.md` | Honk AI Slack bot — Socket Mode, workspace allowlist, threads, per-workspace spend cap, Block Kit feedback |
| `frontend-ask/` | Public `/ask` Vite/React bundle |
| `scripts/ingest/README.md` | Corpus build / upload / swap workflow; `corpora.py` is single source of truth |
| `scripts/eval/README.md` | Retrieval + stream evals, variant comparison |
| `AZTEC_SETUP.md` | Operator runbook — prod deploy, secrets, ingress topology |
| `.env-template` | All env vars + generation commands |

The upstream React admin SPA at `frontend/` was removed (`chore/remove-unused-components`). All operator workflows are SQL or `scripts/`; there is no UI surface to edit agents, sources, or prompts. Caddy at `/` returns a one-line 200 sentinel so health checks don't 404.

## Non-obvious rules

These are gotchas that change what code Claude should produce. None are visible from the code alone.

- **Postgres is the source of truth for prompts.** `application/prompts/aztec_4_3_0_grounded{,_discord,_slack}.txt` are audit copies, not the live prompt. To roll an edit:
  ```bash
  docker cp application/prompts/<file>.txt docsgpt-aztec-postgres-1:/tmp/<file>.txt
  docker exec docsgpt-aztec-postgres-1 psql -U docsgpt -d docsgpt \
    -c "UPDATE prompts SET content = pg_read_file('/tmp/<file>.txt') WHERE id = '<prompt_id>';"
  ```
  Widget + `/ask`: prompt `0780959b-3c18-4ad9-8284-691665233a6f`. Discord: `4bfa9ddf-5d8e-4d5d-a94e-c9e52e1a9ba2`. Slack: `aed05256-9667-4ce2-bcc2-4ba5204c4af6` (NEVER point the Slack agent at the Discord prompt — it makes the bot introduce itself as a Discord bot). Run `scripts/eval/eval_retrieval.py --mode stream` against the affected agent before considering the change shipped.
- **`USER_ID_PEPPER` is required at boot AND unrotatable.** Rotation orphans every pseudonym. Settings validator + migration `0005_pseudonymize_user_ids` both fail-closed if missing / non-hex / `<16`-byte. See `application/pseudonyms.py`.
- **Use `agents.surface`, not `agents.name`, for attribution.** Three of four prod agents have display names that don't match their deployment surface. Canonical mapping (`discord` / `widget` / `web_ask` / `mcp` / `eval`) is enforced by the CHECK constraint in migration `0009_agents_surface`. Per-surface analytics example:
  ```sql
  SELECT a.surface, COUNT(DISTINCT c.id) AS conv, COUNT(cm.id) AS msg
  FROM conversation_messages cm JOIN conversations c ON c.id=cm.conversation_id
  JOIN agents a ON a.id=c.agent_id
  WHERE cm.timestamp >= NOW() - INTERVAL '7 days'
  GROUP BY a.surface;
  ```
- **`AZTEC_SOURCE_IDS` is consumed at agent creation only** (`/api/internal/create_mcp_key`). Reordering `.env` doesn't affect existing agents — use `UPDATE agents SET source_id=…, extra_source_ids=ARRAY[…]::uuid[]` instead.
- **Ingest deny-list lives in `_IGNORED_PATH_SEGMENTS`** (`application/parser/file/bulk.py`). Add new directory names there to skip during ingest. Chunking discards chunks with `token_count < 50` (`application/parser/chunking.py`) UNLESS `metadata.chunk_type == "apiref"`.
- **Do NOT default `ClassicRAG.api_key` to `settings.API_KEY`** in `application/retriever/classic_rag.py:__init__`. That's an agent UUID in prod and breaks `_rephrase_query()` with a 401. Default to `None` so the LLM backend's provider-key fallback resolves.
- **Agent-tool `__init__` must not raise on env-derived config and must not do network I/O.** `ToolManager(config={})` eagerly instantiates every `Tool` subclass in `application/agents/tools/` on every tool execution (`tool_executor._get_or_load_tool`), so one constructor raising on a bad optional env var breaks ALL tools for ALL agents mid-stream. Validate env/config lazily at request time and return an error dict instead (see `aztec_network.py` / `ethereum_network.py`). Also never put raw `str(exc)` from `requests` into logs or tool results — the exception string embeds the request URL, which can carry credentials (Aztecscan keys live in the URL path).
- **LLM-supplied args interpolated into a tool's request URL must be charset-validated before the request, not after.** In `aztec_network.py` the `{address}`/hash path segments go through `_validate_hex_id` (`0x[0-9a-fA-F]{1,128}`) and free-text search goes through `urlencode` as a query value — so a model-chosen argument can never alter the request authority or escape its path segment. `_request` only validates the *base* URL against SSRF, so the path it appends must be byte-safe by construction (paths are asserted free of `?`/`#`/control chars; only `_request(query=…)` may introduce a `?`). Adding a new action that takes an identifier? Validate it the same way.
- **An agent tool's LLM-visible action list is snapshotted into `user_tools.actions` by the provisioner, NOT read live from `get_actions_metadata()`.** `ToolExecutor.prepare_tools_for_llm` builds the function schemas from the DB `actions` column (`scripts/db/create_network_tools.py` writes it). So adding/renaming/editing an action in code requires **re-running the provisioner** after the image rebuild — a rebuild alone won't surface it to the model. Also: required params must carry a per-property `required: True` (top-level JSON-schema `required` lists are ignored by `_build_tool_parameters`).
- **OpenRouter reasoning shim uses `enabled: false`, not `exclude: true`** (`llm/open_router.py:_apply_reasoning_policy`). `exclude` still runs reasoning and just hides it (paying for hidden tokens). Extend `_REASONING_DISABLED_MODEL_PREFIXES` to disable more models. **Per-agent opt-IN overrides the disable:** agent UUIDs in `REASONING_ENABLED_AGENT_IDS` get reasoning ENABLED with a `REASONING_MAX_TOKENS` budget (`max_tokens` implies enabled; mutually exclusive with `enabled`/`effort`) — but ONLY on calls that offer tools. The policy reads the calling LLM's `self.agent_id` plus the call's `tools` arg, so it scopes per-surface AND skips no-tool utility calls (ClassicRAG query rephrase, history compression) that share the agent_id. **Caveat:** the usage frame estimates `generated_tokens` with tiktoken over streamed chunks (not OpenRouter's billed usage); reasoning streamed as `delta.reasoning` is counted as thought chunks, but reasoning conveyed only via `reasoning_details` isn't, so the Discord/Slack per-guild/-workspace spend cap can under-count cost while reasoning is on.
- **MCP key endpoint is auth'd by `MCP_PROVISIONING_KEY`, NOT `INTERNAL_KEY`.** `POST /api/internal/create_mcp_key` upserts one agent per `(mcp_provider, mcp_provider_user_id)`; the Discord `/mcp-key` and Slack `/aztec-mcp-key` slash commands both call it.
- **`create_mcp_key` / `forget_discord_user` are provider-aware via `_resolve_provider_identity`** (`application/api/internal/routes.py`). Legacy shape (no `provider` field) → `discord` and still requires `discord_user_id`/`discord_username`; generalized shape requires `provider ∈ {discord, slack}` + `provider_user_id` with **NO fallback** to `discord_user_id` (a Slack caller missing `provider_user_id` must 400, never silently mint a Discord pseudonym). Adding a provider means extending BOTH `_SUPPORTED_PROVIDERS` here and `_PROVIDER_PREFIXES` in `application/pseudonyms.py` (strict allowlist; `canonical_user_id` raises otherwise) — plus the `agents.surface` CHECK if it's a chat surface, plus the parity tests.
- **Bot chat is erasable via per-message requester tagging; `/forget-me` REDACTS in place, it does not DELETE.** To honour right-to-erasure for chat (which is stored under `user_id='local'`), the bots send `requester_provider` + `requester_provider_id` (the SAME raw identity their forget command sends) on every `/stream` call. The backend HMACs it once in `StreamProcessor.__init__` (`resolve_requester_pseudonym`, before the agent-identity overwrite; raw fields are popped so they never hit `user_logs.data`) and threads it through `complete_stream` → `save_conversation` → `conversation_messages.requester_user_id`, plus `user_logs`/`stack_logs` (via the agent attr → `@log_activity`). `forget_discord_user` then **redacts** matching `conversation_messages` (NULL content + `erased_at`, keeping `position` so the bots' `MAX(position)+1`/`answer_count`/feedback contract is intact — NEVER physically delete a tagged turn) and scrubs every off-message copy: conversation title + `compression_metadata`, synthetic compression-summary messages (marker `message_metadata.type='compression_summary'`), `pending_tool_state` for affected convos, and `user_logs`/`stack_logs` rows (deleted by `requester_user_id`). Migration `0011_requester_attribution`. **Parity is load-bearing:** the `/stream` tag and the forget identity must produce the same `canonical_user_id` (Slack must build `slack_raw_identity` with `enterprise_id` on BOTH paths). Only post-cutover turns are tagged; older turns age out via retention. Adding a chat surface means wiring its `/stream` payload + `_answer_question` plumbing too.
- **Slack identity is workspace-scoped: the raw id is the compound `team_id:user_id`** (`enterprise_id:team_id:user_id` on Grid), built by `slack_raw_identity` in `extensions/slack/bot.py`. A bare Slack `user_id` collides across workspaces and would make `/aztec-forget-me` ambiguous. Create AND forget must build it identically. Per-user Slack MCP keys are `surface='mcp'` (the structural `mcp_provider='slack'` distinguishes them); only the shared Slack *chat* agent is `surface='slack'` (`scripts/db/create_slack_chat_agent.py`, migration `0010_agents_surface_slack`). Bot *chat* turns are written under the chat agent's owner (`user_id='local'`) but are ALSO tagged per-message with the requester's pseudonym (`conversation_messages.requester_user_id`) so they ARE erasable by forget — see the requester-attribution rule below.
- **TWO-version KB (in flight, inert until cutover).** The KB is moving from single-version v4.3.0 to serving BOTH **v4.3.1 (mainnet)** and **v5.0.0-rc.1 (testnet)** from one agent per surface. Design of record: `PLAN-two-version-kb.md`. The mechanism is **version-scoped retrieval via source-id narrowing** (`application/retriever/version_scope.py`): a per-request `active_version` (query/history heuristic, default v5) narrows the agent's source set to `{active version} + {shared}` BEFORE retrieval, so one answer never mixes versions. **It is a NO-OP until the cutover stamps `sources.metadata.version`** — an unstamped source is always kept, so today's behavior is byte-identical; DB failure degrades to the full set. `active_version` is threaded through `complete_stream`/`_build_source_frame` (from `/stream`, `/api/answer`, `/api/search`, `/v1`) into `_aztec_source_url`. Scope: classic agents only (agentic/research + tool-action continuation are NOT version-scoped — documented in `version_scope.py`; doesn't affect the all-classic prod agents).
- **Widget URL rewriter `routes/base.py:_aztec_source_url` is version-AWARE, not keyed on a single prefix.** Rendered docs derive their version from the source path's `version-vX.Y.Z/` prefix → v4.3.1 (mainnet) = BARE site path, v5.0.0-rc.1 (testnet) = `/testnet/` infix; unknown versions (incl. legacy `version-v4.3.0/`) fall back to bare. Version-LESS corpora (code, rendered TS-API, noir-stdlib) take the request's `active_version` to pick the release tag / `next`-snapshot network folder / noir pin (maps `_DOCS_SITE_INFIX`/`_CODE_TAG`/`_TS_API_FOLDER`/`_NOIR_PIN`, `_DEFAULT_DOC_VERSION` at the top of `base.py`). The `id:`-slug override map (`aztec_doc_slugs.py`) is regenerated by `scripts/build_aztec_doc_slug_map.py` (auto-discovers every `version-v*` folder + retains frozen `version-v4.3.0` keys for pre-cutover safety — DELETE that block post-cutover). Bumping/adding a corpus version updates these maps + the slug regen + prompts + settings + tests.
- **`/stream` emits a `: ping\n\n` heartbeat every 15s** (`routes/base.py:_iter_with_heartbeat`) so Cloudflare / Caddy don't idle-close long answers. Preserve this when refactoring the SSE iterator.
- **`history` SSE field accepts BOTH a JSON-encoded string (Discord + Slack bots) and a native list (widget)** (`stream_processor._load_conversation_history`). Don't tighten the type to a single shape.
- **`/api/search` and `/stream` share the same global-rerank algorithm** (`ClassicRAG._get_data` and `routes/search.py:_search_global`). Changes to retrieval logic must land in both, plus the `_aztec_source_url` rewriter is applied to both paths. `/api/search` also drops empty-body apiref chunks via `_is_empty_apiref_chunk` (checks shape, not length — signature-only `pub fn foo(...)` chunks survive).
- **Literal-identifier guardrail verifies `0x` hex literals against `agent.retrieved_docs`, NOT `source_log_docs`** (`routes/base.py`, `_build_literal_allowlist`; design in `application/api/answer/CITATION_MARKER.md`). `source_log_docs` is empty mid-stream (classic agents yield `sources` AFTER the answer), so the allowlist is `retrieved_docs` + question + `chat_history` + **`tool_calls[*].result_full`** — that last is load-bearing: the `aztec_network` tool returns RPC/contract addresses NOT in the corpus, so omitting it scrubs live-data answers. Gated by `settings.LITERAL_GUARD_MODE` (`off`/`audit`/`enforce`, default `audit`). Adding a chat surface or identifier-emitting tool? Make sure its addresses reach the allowlist.
- **Content encryption at rest goes through the repository choke point — never write a classified column with raw SQL.** `application/security/content_registry.py` is the single source of truth (`CONTENT_FIELDS`: which columns hold user content + how each is encrypted: `text`/`json_blob`/`json_leaf`/`json_keep`). Writes call `encrypt_value(table, column, val)` (gated on `CONTENT_ENCRYPTION_ENABLED`, no-op when off); reads decrypt at the repo's `_row_to_dict` (read-both — legacy plaintext passes through). Wired in `conversations.py`/`user_logs.py`/`stack_logs.py`/`pending_tool_state.py`. Adding a content column → add it to the registry, wire the repo, add a `scan_plaintext.py` case; a raw `INSERT/UPDATE` that bypasses the repo silently leaks plaintext. The codec (`content_encryption.py`) is AES-256-GCM, HKDF-keyed off `ENCRYPTION_SECRET_KEY`, AAD=`table:column:kid:version`, envelope `honkenc:1:<kid>:g256:`. `conversation_messages.message_metadata` keeps structural keys (`type`/`citation_filter`/`is_clarification`) plaintext (forget reads `->>'type'`) and encrypts the rest. `user_logs.data->>'api_key'` is dead once `data` is encrypted → look up via the `api_key_fp` blind-index. **`api_key_fp` is dual-written even when the flag is OFF, so migration `0012` MUST be applied before the new code serves traffic** (a `user_logs` insert references the column). A decrypt failure never crashes the answer path — `content_registry.safe_decrypt_value` drops the value to a placeholder and `BaseAgent._build_messages`/`MessageBuilder` drop a turn whose prompt/response is `None` (PLAN §6). Verify/rollout: `scripts/db/encrypt_content_backfill.py` (one-time, idempotent), then `scripts/db/scan_plaintext.py` (exits nonzero on any plaintext content). The Redis LLM cache is OFF by default (`LLM_CACHE_ENABLED`); don't log prompt/response content (the `logging.*` calls in `llm/`+`agents/` were redacted). Full design + rollout: `PLAN-content-encryption.md`.

## Common commands

```bash
# Dev smoke test (see AZTEC_SETUP.md for full first-time bootstrap)
docker compose -f deployment/docker-compose.yaml up -d
docker compose -f deployment/docker-compose.yaml logs -f backend worker

# Production deploy/recreate after a code change
docker compose -f deployment/docker-compose-hub.yaml --env-file .env build backend worker frontend-ask
docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d --force-recreate backend worker frontend-ask

# Tests + lint
python -m pytest                          # unit
python -m pytest -m integration           # integration (needs Postgres)
ruff check .                              # lint
ruff format .                             # format
# Public /ask bundle:
cd frontend-ask && npm run lint && npm run build
```

## PR readiness

Before opening a PR:
- `ruff check .`
- `python -m pytest` on touched modules (with `.env` cleared so prod vars don't poison the test DB — pre-existing fixture errors in `tests/api/answer/services/` and parts of `tests/api/answer/routes/` are environmental and not introduced by your change)
- `cd frontend-ask && npm run lint && npm run build` if frontend touched
- Smoke-test the dev compose if behavior changed

**Update docs in the same change** when behavior, operator steps, env vars, or user-facing surfaces shift. Targets: `README.md`, `AZTEC_SETUP.md`, `.env-template`, the relevant per-feature README (`extensions/discord/README.md`, `application/api/answer/CITATION_MARKER.md`, `application/mcp_server/README.md`, `scripts/ingest/README.md`, `scripts/eval/README.md`). This file (`CLAUDE.md`) is only for rules that change how Claude works — don't accumulate feature documentation here.

## Code style

- **Python:** Ruff, 120 char line length, type hints expected, Google-style docstrings.
- **Frontend:** ESLint + Prettier, 80 char print width, single quotes, semicolons.
