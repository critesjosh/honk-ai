# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## What is DocsGPT (Aztec fork)

Open-source AI platform for document-grounded Q&A. Upstream is `arc53/DocsGPT`; this is an Aztec fork at `0.17.0+aztec`. Stack: Flask backend (Python 3.12), public `/ask` React/TS bundle (Vite, in `frontend-ask/`), **PostgreSQL** (user data + vectors via pgvector), Redis (Celery broker), Celery workers, Discord bot. The upstream admin SPA, OAuth connectors, STT/TTS, and the FAISS/Mongo/Qdrant/Elasticsearch vector backends were removed in `chore/remove-unused-components`.

Aztec-specific additions on top of upstream:
- **MCP key provisioning endpoint** at `POST /api/internal/create_mcp_key` (self-authenticated via `MCP_PROVISIONING_KEY`, not `INTERNAL_KEY`) that upserts one agent per Discord identity.
- **Discord `/mcp-key` command** (`extensions/discord/bot.py`) that calls the endpoint.
- **MCP server consumers**: end users query DocsGPT via [`@aztec/mcp-server`](https://github.com/AztecProtocol/mcp-server) (separate repo, npm package). The standalone TS MCP server that previously lived at `extensions/mcp-server/` was removed; that path now contains a README pointer.
- **Host-side MCP server** (`application/mcp_server/`, prod compose service `mcp`) — separate from the consumer-facing `@aztec/mcp-server` above. Exposes a read-only SQL / RAG / logs surface to remote claudebox operator sessions: `honk_sql.*`, `honk_rag.*`, `honk_logs.*`. **Production runs in network-trust mode**: the `mcp` container publishes `127.0.0.1:7092:7092` and `MCP_AUTH_REQUIRED=false` (FastMCP bearer middleware off, no Caddy `/mcp` route). The trust boundary is the SSH-tunnel chain (`claudebox-deploy → ci-bastion → josh-box`) plus the least-privilege Postgres role `docsgpt_mcp_ro` — **column-level grants that exclude `agents.key` / `conversations.api_key` / `connector_sessions.session_token` and other bearer/PII columns** (`pg_read_all_data` deliberately NOT used). To prevent compose-network co-mingling making the bearer-less server reachable to traffic-serving services, **mcp is isolated on its own compose network `mcp_internal`** shared only with postgres; backend/worker/discord-bot/frontend-ask/caddy are on `default` only and cannot reach `mcp:7092`. The `docker-socket-proxy` sidecar (`CONTAINERS=1 LOGS=1`) lives on a separate `docker_proxy_net` for the same reason. Per-call `mcp_audit` row regardless of outcome — `token_id` is null in network-trust mode. To switch to bearer mode (per-issuer audit, public URL access via `mcp_tokens`): flip `MCP_AUTH_REQUIRED=true` + restore the Caddy `@mcp` block + add a Cloudflare Access app + add bearer-injection to the claudebox side (cloxy strips inbound Authorization headers, so a host-side proxy or cloxy patch is required for the bearer to reach the upstream). Design + threat model: `application/mcp_server/README.md`. Operator runbook: `AZTEC_SETUP.md` → "Honk-ai MCP server".
- **Chunking filter** (`application/parser/chunking.py`) that discards chunks with `token_count < 50`.
- **Path ignore list** (`application/parser/file/bulk.py` → `_IGNORED_PATH_SEGMENTS`) skips any file under `fixtures/`, `dumps/`, `node_modules/`, `target/`, `dist/`, `build/`, `_out/`, `__pycache__/` or `.git/` during ingest. Add new deny-list directory names here.
- **Custom settings**: `MCP_PROVISIONING_KEY`, `USER_ID_PEPPER`, `AZTEC_SOURCE_IDS`, `AZTEC_CORPUS_VERSION`, `CORS_ALLOWED_ORIGINS`, `EMBEDDINGS_DIMENSION`, `RAG_MAX_DOC_TOKENS`, `VITE_ASK_AZTEC_AGENT_KEY` (build arg for the public /ask bundle), plus Discord-bot vars `DISCORD_TOKEN` and `NOIR_GUILD_IDS` (comma-separated, supersedes legacy `NOIR_GUILD_ID`).
- **Pseudonymized user identifiers** (`application/pseudonyms.py`, migration `0005_pseudonymize_user_ids`) — every Discord user_id is HMAC-SHA256(`USER_ID_PEPPER`, raw_id), prefixed `discord_p_v1:` in `user_id` columns, bare 32-hex in `agents.mcp_provider_user_id`. `agents.name` for Discord agents is the constant `"Aztec MCP"`. **`USER_ID_PEPPER` is required at boot AND unrotatable** (rotation orphans every pseudonym). Settings validator + migration both fail-closed if missing/non-hex/`<16`-byte. The pepper lives only in backend env; the Discord bot sends raw IDs over the internal compose network and the backend pseudonymizes at the request boundary (`/forget-me`, `/mcp-key`). Post-deploy check: `scripts/db/verify_pseudonymization.sql`.
- **RAG context cap** (`settings.RAG_MAX_DOC_TOKENS`, default 6000, **10000** in prod `.env`) — caps `calculate_doc_token_budget()` in `utils.py` (upstream defaulted to the full model window).
- **CORS glob patterns** (`application/app.py:after_request`) — `CORS_ALLOWED_ORIGINS` supports `fnmatch`-style globs. Used for Netlify preview URLs (`https://deploy-preview-*--aztec-docs-dev.netlify.app`) and localhost dev (`http://localhost:*`).
- **Widget source rewriting** (`api/answer/routes/base.py:_aztec_source_url`) — remaps corpus paths in the emitted `{type: "source"}` SSE frame into public URLs. Routing rules (in match order):
  - `version-v4.2.0/operators/<rest>` → `https://docs.aztec.network/operate/operators/<rest>` (the rendered network/operator docs live under `/operate/` on the site, NOT under `/developers/` or GitHub).
  - `version-v4.2.0/docs/<rest>` → `https://docs.aztec.network/developers/docs/<rest>` (rendered developer docs).
  - `version-v4.2.0/<file>` → `https://docs.aztec.network/developers/<file>` (top-level dev docs like `overview`, `getting_started_on_local_network` — NOT under `/developers/docs/`).
  - `aztec-site/<rest>` → `https://docs.aztec.network/<rest>` (unversioned site-root pages from aztec-packages `docs/docs/` — today just `networks.md`, the canonical L1 contract address table including the **testnet** GSE/Rollup/Registry that the versioned operator docs explicitly defer to).
  - `noir-docs/<rest>` → `https://noir-lang.org/docs/<rest>` (rendered Noir docs, NOT GitHub).
  - `noir-stdlib/<rest>` → `noir-lang/noir` GitHub blob at the pinned commit (apiref of real `.nr` source files).
  - Everything else (code corpora — aztec.js, aztec-nr, noir-contracts, l1-contracts, typescript-api, etc.) → `aztec-packages` GitHub blob at `v4.2.0`.

  Rendered-docs targets `_strip_doc_ext` (`.md`/`.mdx`) and `_strip_index_suffix` (Docusaurus serves `foo/index.md` at `/foo`). Code targets strip both `.txt` (`Token.nr.txt` → `Token.nr`) AND `.md` (`hash.nr.md` → `hash.nr`). No GitHub fallback for `version-v4.2.0/...` — that folder doesn't exist at the literal v4.2.0 git tag. Sources deduped by rewritten URL, capped at 10 (`_MAX_SOURCES_EMITTED`). Property-tested in `tests/api/answer/routes/test_source_url_rewrite.py`.
- **Global rerank retrieval** (`application/retriever/classic_rag.py:_get_data`, `application/vectorstore/pgvector.py:search_by_vector_with_score`) — embeds the question once, single SQL with `WHERE source_id = ANY(%s)`, greedy-packs globally-sorted candidates into the token budget. Dedup key: `(source_path, leading 200 chars)`. Backends without `search_by_vector_with_score` fail loudly. Tests: `tests/test_retriever.py`, `tests/vectorstore/test_pgvector.py`.
- **Rephrase auth fix** (`application/retriever/classic_rag.py:__init__`) — `ClassicRAG` defaults `api_key=None` so the LLM backend's provider-key fallback (`OPEN_ROUTER_API_KEY` / `OPENAI_API_KEY`) resolves. Do NOT default to `settings.API_KEY` — that's an agent UUID in prod and breaks `_rephrase_query()` with a 401.
- **Agent sources_list ordering fix** (`stream_processor._get_data_from_api_key`) — prepends `agent.source_id` to `sources_list` (upstream dropped the primary when `extra_source_ids` existed). With the admin SPA removed there is no UI surface that could re-clear `source_id` or swap `prompt_id` — drift can only come from direct SQL edits. Canonical 12-source order is in the `AZTEC_SOURCE_IDS` comment block in `.env`. **`AZTEC_SOURCE_IDS` is only consumed at agent creation** (`/api/internal/create_mcp_key`); reordering `.env` does not affect existing agents — `UPDATE agents SET source_id=…, extra_source_ids=ARRAY[…]::uuid[]`.
- **SSE heartbeat** (`api/answer/routes/base.py:_iter_with_heartbeat`) — emits `: ping\n\n` every 15s on silent generator gaps so Cloudflare/Caddy don't idle-close long answers.
- **SSE `history` shape tolerance** (`stream_processor._load_conversation_history`) — accepts both a JSON-encoded string (Discord bot) and a native list (widget).
- **Gunicorn gthread workers** (`application/Dockerfile`) — 4×8 (32 concurrent slots), `--timeout 120 --graceful-timeout 30 --keep-alive 75`. Paired with `stop_grace_period: 40s` on the backend compose service so SIGTERM drains SSE streams cleanly.
- **Reasoning-disable shim for OpenRouter** (`llm/open_router.py:_should_disable_reasoning`) — for models that emit chain-of-thought by default (currently `x-ai/grok-4.1-fast` and `qwen/qwen3.6-flash`), injects `extra_body={"reasoning": {"enabled": False}}`. Extend via `_REASONING_DISABLED_MODEL_PREFIXES`. Reason `enabled: false` is used rather than `exclude: true`: the former turns the feature off entirely so the model emits normal answer tokens instead of CoT, the latter still *runs* reasoning and just hides it (paying for hidden tokens; verified on `stepfun/step-3.5-flash`, which without the shim spent ~10x latency on hidden CoT).
- **Empty-response telemetry** (`llm/openai.py:_raw_gen_stream`) — when a stream produces zero content deltas, the wrapping `finally` emits a `llm.empty_response model=… finish_reason=… provider_error=…` warn log if the final `finish_reason` is anything other than `stop`/`tool_calls`/`None`, or if any OpenRouter `error` payload was surfaced on a stream line. Some providers return HTTP 200 with an empty body on safety-filter trips (`finish_reason=content_filter`) and the OpenAI SDK does NOT raise — these used to be silent (DB row saved with `response=''`, `message_metadata={}`). Grep backend logs for `llm.empty_response` to surface them. Tests: `tests/llm/test_openai.py::TestRawGenStreamEmptyResponseTelemetry`.
- **No admin SPA** — the upstream React admin SPA at `frontend/` was removed in `chore/remove-unused-components`. All operator workflows are SQL or `scripts/`. The previous `VITE_DISABLE_AGENT_EDIT` build flag is therefore moot and the env var has been retired. Caddy at `/` returns a one-line 200 sentinel via `respond` so health checks don't 404.
- **Canonical Aztec system prompts** — two prompts live in the `prompts` table; both are audit-tracked as `.txt` files but **Postgres is the source of truth**, NOT the files.
  - `application/prompts/aztec_4_2_0_grounded.txt` ↔ prompt row `0780959b-3c18-4ad9-8284-691665233a6f` ("Aztec Docs (widget) — Aztec 4.2.0 grounded") — used by the `docs.aztec.network` agent (widget) and the `Ask Aztec — public web` agent (`/ask` page).
  - `application/prompts/aztec_4_2_0_grounded_discord.txt` ↔ prompt row `4bfa9ddf-5d8e-4d5d-a94e-c9e52e1a9ba2` ("Honk AI (Discord bot) — Aztec 4.2.0 grounded") — used by the `Aztec 4.2.0` agent (Honk AI Discord bot's `@`-mention path; the bot's `API_KEY` resolves to this agent). Has a Discord-specific Length section (target ≤1500 chars, no section-header padding) so chat replies stay compact.
  - To update either: edit the file, `docker cp` into postgres, then `UPDATE prompts SET content = pg_read_file('/tmp/<file>') WHERE id = '<prompt_id>';`. Verify with `SELECT LEFT(content, 200) FROM prompts WHERE id = '<prompt_id>';`. Run the eval harness (`scripts/eval/eval_retrieval.py --mode stream --api-key <agent_key>`) against the relevant agent before considering the change shipped.
- **Eval harness** (`scripts/eval/eval_retrieval.py`, `scripts/eval/golden_queries.json`) — 25 golden queries tagged `identifier` / `concept` / `example`. Two run modes: `--mode retriever` probes `ClassicRAG._get_data()` directly (identifier queries assert top-3 contains the expected `.nr` apiref file; concept queries assert first hit comes from the configured concept prefix); `--mode stream` hits `/stream` end-to-end and asserts banned-identifier absence, no Markdown tables, source diversity, response-time ceiling, and apiref-first citation for identifier queries. `--bucket {identifier,concept,example,all}` filters. See `scripts/eval/README.md`. Run before merging any retrieval-path or prompt change.
- **Eval-variant comparison** (`scripts/eval/provision_test_agent.py` + `scripts/eval/eval_retrieval.py --capture-answers` + `scripts/eval/compare.py`) — manual A/B harness layered on top of the regression suite. The provisioner upserts a throwaway agent + prompt under `user_id = 'eval-variant'` (race-safe via the `prompts_eval_variant_name_uniq` / `agents_eval_variant_name_uniq` partial unique indexes from migration `0008_eval_variant_unique`; rate limits are explicitly cleared on every update so cap drift can't skew timing). `eval_retrieval.py --capture-answers --label X --json-out` writes a snapshot envelope (`{label, mode, ran_at, summary, results}`) including the full answer text and ordered cited URLs per query; `compare.py` (stdlib-only, runs on the host) diffs two snapshots into a Markdown report with per-bucket Δ, regress/fix lists, citation diff, and unified answer-text diff. Snapshots written before this PR were a bare list — `compare.py` accepts both shapes. Operator runbook: `scripts/eval/README.md` → "Variant comparison workflow". Not in CI; trigger manually before merging prompt / source-list / model changes.
- **`/api/search` parity with `/stream`** (`application/api/answer/routes/search.py:_search_global`) — MCP search endpoint uses the same global-rerank algorithm as `ClassicRAG._get_data`, the same `_aztec_source_url` rewriting, and the same primary-source-prepend fix. Drops empty-body apiref chunks via `_is_empty_apiref_chunk` (filter checks shape, not length, so signature-only chunks like `pub fn poseidon(...)` survive).
- **`GET`/`POST` `/api/version`** (`application/api/answer/routes/version.py`) — public, unauthenticated. Returns `{aztec_corpus_version, source_count}` from `settings.AZTEC_CORPUS_VERSION` (default `v4.2.0`). Read by `@aztec/mcp-server` for version-drift gating. Both verbs exist because Cloudflare Access gates GET on the apex but lets POST `/api/*` through unauthenticated.
- **Discord multi-guild allowlist + in-place reply** (`extensions/discord/bot.py`) — `NOIR_GUILD_IDS` (comma-separated) replaces legacy `NOIR_GUILD_ID` (still honored); current prod allowlist is `1399477876461404252` (**Aztec Bot Testing**, owner: josh), `1113924620781883405` (**Noir**, the public community server), and `1144692727120937080` (3rd authorized guild — confirmed intentional 2026-05-14). Slash-command sync is per-guild with per-guild error isolation. The user-facing bot name is **Honk AI**; "Aztec DocsGPT" is the project, "Aztec MCP" is the service the `/mcp-key` key grants access to. The bot always replies in place: same channel for top-level guild mentions, inside the thread for thread mentions, in the DM for DMs. **Threads are no longer auto-created on top-level mentions** — removed 2026-05-12 because `POST /channels/.../messages/.../threads` was the only Discord write route reliably tripping shared-bucket 40062 (anti-abuse heuristics flag rapid repeated thread creation from a single author in small/low-trust servers — the Aztec Bot Testing server consistently hit this during dev iteration; Noir never did). Plain `send`/`typing` aren't throttled the same way.
- **Discord reply-to-bot trigger** (`extensions/discord/bot.py:_is_reply_to_bot`) — in guild channels the bot also responds when a message is a Discord inline reply to one of its previous messages, not only on explicit `@HonkAI` mentions. Lets users follow up on an answer without re-typing the tag (the natural Discord UX). Resolution uses `message.reference.resolved` populated by the gateway's `referenced_message` field — no `channel.fetch_message` fallback on cache miss, since uncached replies are rare and the user can still @-mention. **Non-reply rejection is gated on `message.type == discord.MessageType.reply`** (the parent ``Message.type``, value 19), NOT on `MessageReferenceType.reply` — in discord.py 2.5+ that enum value is an alias for `.default` and covers pins/crossposts/channel-follow/thread-created/poll-result, all of which would otherwise pass an `enum`-only check. `MessageReferenceType.forward` is rejected as a secondary belt-and-braces filter on 2.5+. ``on_message`` also bails on any bot-authored input (`getattr(message.author, "bot", False)`) so the new reply trigger can't drag Honk into a bot-to-bot loop. DM behaviour is unchanged (DMs always trigger).
- **Discord thread-context awareness** (`extensions/discord/bot.py`) — when @-mentioned inside a `discord.Thread`, the bot fetches the thread starter + up to `DISCORD_THREAD_CONTEXT_LIMIT` (default 30) most-recent messages before the trigger and bundles them as a labelled context block AFTER the user's question (question-first ordering). History fetch uses `before=triggering_message, oldest_first=False` then reverses — `oldest_first=True` returns the wrong end of the thread. Per-thread state lives in LRU `thread_conversation_histories` (cap 500), separate from per-user `conversation_histories`; an `asyncio.Lock` per thread. **Privacy**: forwards other users' message bodies + display names to the LLM provider. Knobs: `DISCORD_THREAD_CONTEXT_LIMIT` (`0` disables), `DISCORD_THREAD_CONTEXT_MAX_CHARS` (default 12000).
- **Discord citation footer** (`extensions/discord/bot.py:_format_sources_footer`) — appends a `-#` (subtext) "Sources" block listing up to 5 URLs (`_DISCORD_FOOTER_SOURCE_LIMIT`); URLs wrapped in `<...>` to suppress auto-embed cards. **Merged into the last answer chunk when `last_chunk + "\n\n" + footer <= 2000` chars**; otherwise sent as a separate `target.send(...)` after all chunks. Goes through the same `_aztec_source_url` rewriter as the widget. The chunker (`chunk_string`) also packs to ≥80% of the 2000-char limit so most answers ship in 1–2 sends instead of 3–4 (added 2026-05-12 after a per-channel write 429 fired on a 3-chunk + footer reply into a fresh thread).
- **Discord shared-bucket 429 breaker** (`extensions/discord/bot.py`) — per-guild circuit breaker on Discord `code: 40062` / `discord.RateLimited` (any `X-RateLimit-Scope: shared` 429). **Single-hit**: one observed 40062 trips it; no threshold or counter. While open, the bot refuses to call `/stream` for that guild and replies to silenced @-mentions with a deduped (per-channel, per-cooldown-window) "rate-limited, try again in about N minute(s)" notice — falling back to a ⏳ reaction if the notice itself 429s. Cooldown defaults to **60s** (`DISCORD_SHARED_429_COOLDOWN_SECONDS`, min 10s); rationale + sizing evidence in the comment block at the constant. Backed by `bot.http.max_ratelimit_timeout = 2.0` which kills discord.py's default 5-retry loop so 40062s surface as observable exceptions rather than amplifying — that is the actual structural defense against the 2026-05-08 Noir anti-abuse flagging incident; the cooldown is belt-and-suspenders. Cooldown is decoupled from Discord's returned `retry_after` (which only describes when *Discord* will accept our next write, not when external shared-bucket contention clears). Four bail-out sites all funnel through `_signal_breaker_silenced`: front-door on_message, `typing`, pre-`/stream` recheck, post-`/stream` answer/footer send (the previous `create_thread` site was removed with auto-thread creation on 2026-05-12).
- **Discord reaction → feedback** (`extensions/discord/bot.py:on_raw_reaction_add`, `submit_feedback`, `_register_feedback_target`) — 👍/👎 on Honk AI replies POSTs `/api/feedback` with `{conversation_id, question_index, feedback}` → `conversation_messages.feedback` (JSONB). The bot mirrors the backend's per-conversation position counter locally (`answer_count`); backend allocates atomically via `MAX(position)+1`, one row per `/stream`. Increment + `conversation_id` persistence happens IMMEDIATELY after `/stream` (before any Discord send). Per-thread + per-user `asyncio.Lock` serializes concurrent same-target mentions. Non-200 (`conversation_id=None`) early-returns without consuming a position. `message_id → (conversation_id, question_index)` in LRU `feedback_targets` (cap 1000); restart loses the cache. Auth: `/api/feedback` accepts anonymous because `AUTH_TYPE` is unset → `user_id="local"` matches the bot agent. REMOVE events not handled. Read via `GET /api/get_feedback_analytics`. **Known limitation**: when `ENABLE_CONVERSATION_COMPRESSION=true` (default in `application/core/settings.py:208`), compression appends an extra `conversation_messages` row (`base.py:635`) and silently no-ops post-compression feedback. Mitigation: set `ENABLE_CONVERSATION_COMPRESSION=false` in prod `.env`. Real fix (TODO): bot reads backend-assigned position via `position` SSE event.
- **Public /ask page** (`frontend-ask/`, served at `/ask` on the apex) — anonymous, RAG-only chat. Separate Vite/React/TS bundle from `frontend/`. Calls same-origin `POST /stream` with `save_conversation: false` so anonymous traffic isn't persisted (backend `user_logs` still records). `noir`/`nr` code-fence language routes to Rust grammar in `prism-react-renderer` (Noir has no Prism grammar). Citations render as a numbered `Sources` block (top 5, popover); inline `[n]` markers are NOT post-processed (stream isn't span-aligned to chunks). 18+ age gate uses the SHARED `aztecDocsGPTAgeAck` localStorage key (same as the docs widget). Disclaimer wording is kept in sync with the docs widget on aztec-packages. Caddy matcher: `path /ask /ask/*` (NOT `/ask*` — that matches `/asky`) → `frontend-ask:80`. Build args: `VITE_ASK_AZTEC_AGENT_KEY` (required, known-public bearer baked into bundle; fail-closed in prod compose), `VITE_ASK_AZTEC_API_BASE` (cross-origin dev only), `ASK_CONNECT_SRC` (CSP `connect-src`; dev adds `http://localhost:7091`).
- **Public-web agent provisioner** (`scripts/db/create_ask_aztec_public_agent.py`) — idempotent SQL upsert keyed on `(user_id='public-web', name='Ask Aztec — public web')`. Forces guardrails on the agent row: `limited_request_mode=true` (10k/day), `limited_token_mode=true` (5M/day), `tools='[]'`, `allow_system_prompt_override=false`, canonical 12-source order from `AZTEC_SOURCE_IDS`. Re-running refreshes sources/caps/prompt but **preserves the bearer key** (rotation requires a deliberate UPDATE). Script isn't in the backend image — bind-mount via `-v $(pwd)/scripts:/app/scripts:ro` with `-e PYTHONPATH=/app` when running `docker compose run --rm backend`.

## Development environment

**Always run services via Docker.** Do not install Postgres, Redis, or app dependencies natively.

Two composes at `deployment/`:
- `docker-compose.yaml` — **dev compose**. Builds from source, exposes ports on localhost (backend 7091, frontend-ask 5174, Redis 6379, Postgres 5432). Use this in dev / smoke-test.
- `docker-compose-hub.yaml` — **production compose**. Builds from source, Caddy in HTTP-only mode (TLS at the CF edge), Discord bot container. CF Tunnel anchor lives on `ci-bastion.aztecprotocol.com` (us-east-2); Caddy publishes only `127.0.0.1:5080:80` for an outbound SSH reverse tunnel to bastion's cloudflared. Nothing on this host faces the public internet directly. See `PLAN-bastion-relay.md` for full topology + the systemd-user unit on josh-box.

Configuration lives in `.env` at repo root. It is gitignored. `.env-template` holds placeholders and secret-generation instructions.

Before starting services, check if containers are already running (`docker compose ... ps`). If they show as exited, just bring them back up — do not recreate from scratch.

## Common commands

### Dev / smoke test (builds from source)
```bash
docker compose -f deployment/docker-compose.yaml up -d postgres
# scripts/ is at the repo root and NOT copied into the backend image
# (Dockerfile only copies application/), so bind-mount it for the bootstrap.
docker compose -f deployment/docker-compose.yaml run --rm \
  -v $(pwd)/scripts:/app/scripts:ro \
  backend python scripts/db/init_postgres.py
docker compose -f deployment/docker-compose.yaml up -d
docker compose -f deployment/docker-compose.yaml logs -f backend worker
docker compose -f deployment/docker-compose.yaml down
```

First-time Postgres bootstrap:
1. Start only `postgres` first so the initdb scripts (including `postgres-init/01-pgvector.sql` which enables the `vector` extension) can run.
2. Run `init_postgres.py` (with the `-v $(pwd)/scripts:/app/scripts:ro` mount above) — thin wrapper around `alembic upgrade head` that creates all tables including the Aztec `agents.mcp_*` columns.
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
Public `/ask` bundle (from `frontend-ask/`): `npm run lint`, `npm run build`.

## Architecture

### Backend (`application/`)
- Entry points: `app.py` (Flask), `wsgi.py` (gunicorn), `worker.py` (Celery).
- Blueprints: `user` (`/api/user/*`), `answer` (`/api/answer`), `internal` (`/api/internal/*`, includes the MCP endpoint), `connector`, `v1`.
- Factory pattern (`LLMCreator`, `AgentCreator`, `VectorCreator`, `RetrieverCreator`, `StorageCreator`).
- **Storage layer**: `application/storage/db/` — SQLAlchemy Core with thin repositories per table. `session.py` provides `db_session()` context manager. `models.py` holds the schema. Migrations in `application/alembic/versions/NNNN_description.py` (hand-written SQL, not autogenerate). Add new migrations by creating the next-numbered file.
- **Auth**: `AUTH_TYPE` env var (`session_jwt`, `simple_jwt`). The built-in JWT is **not** a real access boundary — anyone can call `/api/generate_token`. Real auth lives at the reverse proxy (Cloudflare Access in our production deploy).
- **Vector store**: `VECTOR_STORE=pgvector` uses `application/vectorstore/pgvector.py`, which creates its own `documents` table with an IVFFlat cosine index in the same Postgres instance. `CREATE EXTENSION vector` runs both via the initdb script and inside `pgvector.py`'s init.

### Frontends
The fork ships exactly one frontend: **`frontend-ask/`**, the public anonymous chat surface served at `/ask`. Vite + React 19 + TypeScript, prod-built into the `aztec/docsgpt-ask` nginx image. The upstream admin SPA at `frontend/` was removed.

### Deployment specifics
- Postgres image: `pgvector/pgvector:pg16` (not plain postgres). Holds agents, sources, conversations, etc., AND the vector embeddings.
- Reverse proxy: Caddy (`deployment/Caddyfile`). Terminates Let's Encrypt TLS, propagates `Cf-Access-Authenticated-User-Email` to the backend as `X-Auth-Email`, trusts Cloudflare as its upstream proxy.
- SSO: **Cloudflare Access** gates traffic before it reaches Caddy. The origin should be behind a Cloudflare Tunnel or IP-restricted to Cloudflare's ranges.
- No host port publishing for postgres/redis/backend/frontend in prod — only Caddy's 80/443 face the network.

## Current production deployment (josh-box)

**Prod compose is live** on this host. Public ingress reaches Caddy via a Cloudflare Tunnel anchored on the company bastion (`ci-bastion.aztecprotocol.com`, AWS us-east-2), bridged to josh-box by an outbound SSH reverse tunnel. Both composes run side-by-side — check container names to know which backend you're hitting:

| Compose | Project / container prefix | Images | Purpose |
|---|---|---|---|
| `deployment/docker-compose-hub.yaml` | `docsgpt-aztec-*` | `aztec/docsgpt:0.17.0-aztec.1`, `aztec/docsgpt-ask:0.17.0-aztec.1` | **production** — CF Tunnel (anchored on bastion) → SSH reverse tunnel → Caddy → frontend-ask (public `/ask`) / backend; discord-bot container; only Caddy on `127.0.0.1:5080` (loopback) |
| `deployment/docker-compose.yaml` | `docsgpt-oss-*` | `docsgpt-oss-backend:latest`, `docsgpt-oss-worker:latest`, `docsgpt-oss-frontend:latest`, `docsgpt-oss-frontend-ask:latest` | dev smoke-test — publishes 5173/7091/5432/6379 on localhost; `frontend-ask` on 5174 (built prod-style nginx with `ASK_CONNECT_SRC` relaxed to allow `http://localhost:7091` cross-origin) |

Ingress path: `CF edge → CF Tunnel → cloudflared on ci-bastion → bastion localhost:5080 → SSH reverse tunnel (systemd-user `aztec-docs-tunnel.service`) → josh-box 127.0.0.1:5080 → Caddy → backend`. Full topology in `PLAN-bastion-relay.md`.

Production config (in `.env`, shared by both composes):
- `PUBLIC_HOSTNAME=aztec.adjacentpossible.dev`
- `CLOUDFLARE_TUNNEL_TOKEN=<set>` — cloudflared runs on bastion, not in this compose. CF dashboard's Public Hostname Service URL is `http://localhost:5080`. Token-based tunnels ignore `--url`; dashboard config is the source of truth.
- `LLM_PROVIDER=openrouter`, `LLM_NAME=qwen/qwen3.6-flash` — env-level fallback. **Per-agent `default_model_id` can override this**: `Aztec 4.2.0` (Honk AI Discord) and `Ask Aztec — public web` (the /ask page) both pin `qwen/qwen3.6-flash` explicitly (so a future LLM_NAME change doesn't silently move them); `docs.aztec.network` (widget) pins `x-ai/grok-4.1-fast`. The per-Discord-user `Aztec MCP` agents have NULL `default_model_id` and fall back to `LLM_NAME`, so they're also on qwen. **Why qwen**: eval comparison vs the previous default `z-ai/glm-4.6` showed 20/25 vs 14/25 PASS, 2.5x faster total wall time, zero regressions — see PR #118. To re-enforce the /ask agent's model via the provisioner, set `ASK_AZTEC_DEFAULT_MODEL_ID=qwen/qwen3.6-flash` when running `scripts/db/create_ask_aztec_public_agent.py`.
- `POSTGRES_PASSWORD=docsgpt` (TODO: rotate before opening to real users)
- `EMBEDDINGS_*` via OpenAI `text-embedding-3-large` (3072-dim)
- `AZTEC_SOURCE_IDS` points at all 12 v4.2.0 corpora
- `UPLOAD_FOLDER=/app/application/inputs` — MUST be absolute. `LocalStorage` computes `base_dir=/app`, so a relative `inputs` resolves to an unmounted path and every `POST /api/upload` fails with `PermissionError`.

### Critical gotcha — source edits don't cross composes

Each compose builds its **own** images. Editing `application/*.py` and rebuilding the dev compose does NOT update the hub compose, and vice-versa. To apply source changes to production:

```bash
docker compose -f deployment/docker-compose-hub.yaml --env-file .env build backend worker frontend
docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d --force-recreate backend worker frontend
```

Before iterating on a bug someone's reporting, run `docker ps` and check whether the bug repro path is `docsgpt-aztec-*` (hub) or `docsgpt-oss-*` (dev). Iterating on the wrong compose is silent and wastes an hour.

### `.env` change propagation

`docker compose restart` re-uses the old container's env. `.env` edits only take effect after `up -d --force-recreate`.

## Data sources (knowledge base corpus)

> **Reproducible ingest:** the full re-ingest workflow (build all 13 corpora, upload, swap the agent's source list) lives under `scripts/ingest/` — see `scripts/ingest/README.md` for the version-bump checklist. `scripts/ingest/corpora.py` is the single source of truth for corpus paths, extensions, and transforms.

All indexed content comes from the sibling **`aztec-packages` repo** pinned at the `v4.2.0` git tag. On the dev host the repo lives at `/mnt/user-data/josh/aztec-packages/`; the worktree used for ingest is typically checked out at `/tmp/aztec-v4.2.0`:

```bash
git -C ../aztec-packages worktree add --detach /tmp/aztec-v4.2.0 v4.2.0
```

**Thirteen corpora** are ingested into the `sources` table (one row per corpus, UUID auto-generated) and `documents` table (one row per chunk, pgvector 3072-dim embeddings via OpenAI `text-embedding-3-large`). UUIDs for the ones the MCP bot should serve go into `AZTEC_SOURCE_IDS` in `.env`:

| Source (display name) | Path in aztec-packages | Transform | Excludes / Includes | File ext |
|---|---|---|---|---|
| **Aztec Developer Docs v4.2.0 (clean)** | `docs/developer_versioned_docs/version-v4.2.0/` | passthrough | excludes `docs/resources/migration_notes.*` | .md .mdx .json |
| **Aztec Network Docs v4.2.0 (clean)** | `docs/network_versioned_docs/version-v4.2.0/` | passthrough | excludes `operators/reference/changelog/*`, `reference/changelog/*` | .md |
| **Aztec Site Networks Page v4.2.0** | `docs/docs/` | passthrough | **includes only** `networks.md` (the L1 contract address table — mainnet vs. testnet GSE/Rollup/Registry that the versioned operator docs defer to) | .md .mdx |
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

The 13 corpora split into three buckets by transform:

- **`passthrough`** — markdown ingested as-is.
- **`rename_code_to_txt`** — source code with `.txt` appended so the parser allowlist accepts it (`Token.nr` → `Token.nr.txt`). Used where the body IS the answer (examples, circuits, TS, Solidity). Original path preserved in `metadata.source`.
- **`noir_apiref`** — `.nr` files run through `scripts/ingest/noir_apiref.py` to produce a Markdown view of just the public surface (doc comments + signatures, no bodies, no `//` comments, no `#[test]`). Output `foo.nr.md`. Used for `aztec-nr` and `noir-stdlib`. The chunker tags these `chunk_type=apiref` (via file extension in `application/parser/file/bulk.py`), exempting them from the `<50` token discard. See `PLAN-rag-apiref.md`.

### Per-corpus exclusions / inclusions (`exclude_paths` / `include_paths`)

`SourceTree.exclude_paths` (`scripts/ingest/corpora.py`) is a tuple of fnmatch patterns relative to the source tree. Currently:
- **Aztec Developer Docs** excludes `docs/resources/migration_notes.*`.
- **Aztec Network Docs** excludes `operators/reference/changelog/*` and `reference/changelog/*`.

`SourceTree.include_paths` is an optional allowlist with the same semantics; when non-empty only matching files are kept. Used by:
- **Aztec Site Networks Page** — `include_paths=("networks.md",)` over `docs/docs/`, so the corpus contains exactly one file. Honored by the `passthrough` and `rename_code_to_txt` transforms; `noir_apiref` fails loud if used.

### How ingest runs
The full reproducible workflow is in `scripts/ingest/README.md`. Short version:

1. `python -m scripts.ingest.build --aztec-pkg /tmp/aztec-vNEW --noir /tmp/noir-vNEW --out /tmp/build` builds zips for all 13 corpora.
2. `python -m scripts.ingest.upload --build-dir /tmp/build --base-url ... --token "$INTERNAL_KEY" --out /tmp/build/upload_manifest.json` uploads each zip to `POST /api/upload`, polls `GET /api/task_status?task_id=...` until `SUCCESS`, captures the resulting `sources.id` per corpus.
3. `python -m scripts.ingest.swap_sources --upload-manifest ... --agent-id $PROD_AGENT_ID --out /tmp/swap.sql` generates SQL (does NOT execute) to point the agent's `source_id` + `extra_source_ids` at the new corpora, plus the canonical-order `AZTEC_SOURCE_IDS` block to paste into `.env`.
4. Run `psql -f /tmp/swap.sql`, update `.env`, then `docker compose … up -d --force-recreate backend worker`.

Worker internals: walks the extracted tree, chunks each file, drops chunks with `token_count < 50` (UNLESS `metadata.chunk_type == "apiref"` — see `application/parser/chunking.py`), embeds survivors via OpenAI, writes to `documents`.

Re-ingest: the upload endpoint has **no idempotency** — re-uploading the same zip duplicates the `sources` row and all chunks. Wipe first: `DELETE FROM sources WHERE name = '...';` then `DELETE FROM documents WHERE source_id = '<uuid>';` before POSTing again.

### What is NOT indexed
Intentionally excluded (per MCP resource scope): current-unversioned docs under `docs/docs-developers/`, Barretenberg (`barretenberg/` C++/Rust/TS, ~2.4k files), patterns/howto guides that don't yet exist as a distinct folder in v4.2.0.

## CI/CD
Active: `.github/workflows/` `pytest.yml`, `lint.yml`, `vale.yml`, `zizmor.yml`. `bandit.yaml` and `labeler.yml` are upstream-gated (skip here). Production builds happen via `docker compose build` on josh-box; this fork never pushes to a registry.

## Code style
- **Python:** Ruff, 120 char line length, type hints expected, Google-style docstrings.
- **Frontend:** ESLint + Prettier, 80 char print width, single quotes, semicolons.

## PR readiness
Before opening a PR: `ruff check .`, `python -m pytest`, `npm run lint && npm run build` in `frontend-ask/`. Smoke-test the dev compose.

**Update docs in the same change** when behaviour, operator steps, env vars, or user-facing surfaces shift. Targets: this file, `README.md`, `AZTEC_SETUP.md`, `.env-template`, plus per-feature READMEs (`scripts/ingest/`, `scripts/eval/`). Stale docs here have caused real operator mistakes — treat the doc sweep as part of the task.

## Deployment files
- `deployment/docker-compose-hub.yaml`, `deployment/docker-compose.yaml` — prod / dev composes
- `deployment/Caddyfile` — `@ask path /ask /ask/*` proxies to `frontend-ask:80`
- `deployment/postgres-init/01-pgvector.sql` — enables the `vector` extension
- `frontend-ask/Dockerfile.prod` + `frontend-ask/nginx.conf` — public /ask bundle (CSP `connect-src` build-arg-substituted via `__ASK_CONNECT_SRC__`)
- `application/alembic/versions/` — `0003_mcp_provisioning`, `0004_sources_is_public`, `0005_pseudonymize_user_ids`
- `.env-template` — all production secrets + generation commands
