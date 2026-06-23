# Aztec DocsGPT

RAG-backed knowledge-base assistant for the Aztec Network.
Live at **[aztec.adjacentpossible.dev](https://aztec.adjacentpossible.dev)**.
Corpus pinned to [`aztec-packages@v4.3.0`](https://github.com/AztecProtocol/aztec-packages/tree/v4.3.0).

This repo is a heavily-modified fork of [`arc53/DocsGPT`](https://github.com/arc53/DocsGPT)
starting from tag `0.17.0`; the upstream admin SPA, OAuth connectors,
STT/TTS, the FAISS/Mongo/Qdrant/Elasticsearch/LanceDB vector backends,
local SentenceTransformer embeddings, the docling parsers, the
never-provisioned agent tools (web search, telegram, notes/todos/memory,
etc.), and the LLM providers `LLMCreator` can't build (anthropic, google,
groq, novita, …) were removed. See [`CLAUDE.md`](./CLAUDE.md) for the
architectural diff and
[`AZTEC_SETUP.md`](./AZTEC_SETUP.md) for the production deploy walkthrough.

## Overview

### Access paths
- **Public /ask landing page** — `https://aztec.adjacentpossible.dev/ask`.
  Anonymous, no login. Single-page chat surface in the Aztec design system
  (parchment + chartreuse, hard-edged poster card) backed by the same RAG
  agent stack. Markdown rendering with syntax-highlighted code (Noir → Rust
  grammar). Top-5 cited sources appear under each answer with click-to-open
  popovers (links route to docs.aztec.network / noir-lang.org / GitHub via
  the same backend rewriter the widget uses). 18+ age gate on first visit.
  No conversation persistence (in-memory only). Backed by a dedicated
  hard-capped agent (10k requests/day, 5M tokens/day, no tools, no
  system-prompt override) provisioned via `scripts/db/create_ask_aztec_public_agent.py`.
- **Discord** — run `/mcp-key` in the Noir Discord (or any allowlisted
  guild — `NOIR_GUILD_IDS` is comma-separated) to provision a personal
  MCP API key. `@`-mention the bot — user-facing name **Honk AI** —
  in any channel, reply inline to one of its previous messages, or
  DM it to chat directly. The bot always replies in
  place: in the same channel for top-level guild mentions, inside the
  thread for thread mentions, and in the DM for DMs. In existing
  threads it also reads up to 30 prior thread messages so it can
  answer questions tagged on someone else's discussion. Responses are formatted for
  Discord (no Mermaid, no Markdown tables) via a custom system prompt
  and include a `-#` subtext "Sources" footer with up to 5 cited URLs.
  React with 👍 / 👎 on any of the bot's reply messages to record
  feedback — the bot forwards the reaction to the backend and stores
  it on the conversation row. `/forget-me` deletes all data stored
  under your Discord pseudonym.
- **MCP clients** (Claude Desktop, Claude Code, Codex) — paste the key from
  `/mcp-key` with `API_URL=https://aztec.adjacentpossible.dev`. The bot
  response includes ready-to-paste config snippets for each client and
  exposes a `search_aztec` tool backed by this deployment's `/api/search`.

### What this fork adds on top of upstream 0.17.0

- **MCP key provisioning** — `POST /api/internal/create_mcp_key`
  self-authenticates via `MCP_PROVISIONING_KEY` and upserts a per-Discord-user
  agent row (`agents.mcp_provider`, `mcp_provider_user_id`, `mcp_purpose`).
- **Pseudonymized user identifiers** (`application/pseudonyms.py`, migration
  `0005_pseudonymize_user_ids`) — Discord user IDs are HMAC-SHA256'd with a
  server-side `USER_ID_PEPPER` before being stored. `user_id` columns hold
  `discord_p_v1:<32hex>`; `agents.mcp_provider_user_id` holds the bare
  32-char hex; `agents.name` is the constant `"Aztec MCP"` for Discord-bound
  MCP rows (Discord display names never reach the DB).
  `USER_ID_PEPPER` is **required at boot and unrotatable** — generate once
  via `openssl rand -hex 32` and treat as you would
  `ENCRYPTION_SECRET_KEY`. `/api/internal/forget_discord_user`
  + the Discord `/forget-me` slash command satisfy GDPR right-to-erasure
  by computing the same pseudonym and deleting all matching rows.
  Post-deploy verification: `scripts/db/verify_pseudonymization.sql`.
- **`agents.surface` taxonomy** (migration `0009_agents_surface`) — explicit
  surface-attribution column, NOT NULL, CHECK-constrained to
  `('discord','widget','web_ask','mcp','eval')`. `agents.name` is a display
  label; `agents.surface` is the structural axis for per-surface
  analytics, dashboards, and rate limits. Three of the four prod agent
  names do NOT match their deployment surface (`Aztec 4.3.0` is the
  Discord bot, `docs.aztec.network` is the widget only by coincidence) —
  join reports on `surface`, not `name`. See `CLAUDE.md` for the full
  mapping table.
- **Discord bot** (`extensions/discord/`) — containerized and bundled into
  the production compose. Implements the `/mcp-key` slash command and
  chat passthrough triggered by @-mention, inline reply to a previous bot
  message, or DM. Reaches the backend on the internal compose network
  (bypasses Cloudflare Access).
- **Public /ask page** (`frontend-ask/`) — separate Vite/React/TS bundle
  served at `/ask` on the apex host. Anonymous, no login. Calls
  same-origin `POST /stream` with `save_conversation: false` so requests
  aren't persisted as conversations. Bearer key for the underlying agent
  is baked into the public JS at build time as `VITE_ASK_AZTEC_AGENT_KEY`
  (a known-public capability — the agent has hard request/token caps,
  no tools, and no system-prompt override). Caddy routes via the named
  matcher `path /ask /ask/*`. See `AZTEC_SETUP.md` for the provisioning
  + Cloudflare Access bypass steps required before the page goes live.
- **MCP server** — end users install
  [`@aztec/mcp-server`](https://www.npmjs.com/package/@aztec/mcp-server)
  ([source](https://github.com/AztecProtocol/mcp-server)) locally and
  point it at this backend's `/api/search` with the personal key the
  Discord bot provisioned. The MCP server itself is **not** in this
  repo; this repo only provides the key-provisioning and search HTTP
  endpoints it consumes.
- **Ingest deny-list** (`application/parser/file/bulk.py:_IGNORED_PATH_SEGMENTS`)
  skips `fixtures/`, `dumps/`, `node_modules/`, `target/`, `dist/`, `build/`,
  `_out/`, `__pycache__/`, and `.git/` directories during corpus ingest —
  prevents blockchain-state test fixtures from burning OpenAI embedding credit.
- **Chunking filter** (`application/parser/chunking.py`) drops chunks with
  `token_count < 50`.
- **Cloudflare Tunnel deployment topology** — Caddy
  (`deployment/Caddyfile`) sits in HTTP-only mode behind a Cloudflare
  Tunnel; no public IP required on the origin host. Handles path
  routing, streaming (`flush_interval -1` for SSE), security headers,
  and `Cf-Access-Authenticated-User-Email → X-Auth-Email`
  propagation. The Aztec deployment additionally anchors the tunnel on
  a separate bastion host in a CF-friendly region (us-east-2)
  reached over an outbound SSH reverse tunnel — see the
  "Bastion-anchored Cloudflare Tunnel" section of `AZTEC_SETUP.md`
  for why and how. Generic single-host
  CF-Tunnel-on-origin deployments still work with the same compose.
- **Slim backend image (1.34 GB)** — `application/Dockerfile` drops torch,
  transformers, sentence-transformers, docling, rapidocr, onnxruntime, and
  the bundled mpnet model in favor of remote OpenAI embeddings. The
  upstream image is ~12 GB.
- **Gunicorn + SSE hardening** — gthread workers (2 × 8), 75 s keep-alive,
  30 s graceful shutdown (paired with compose `stop_grace_period: 40s`), and
  a 15 s `: ping` heartbeat in `application/api/answer/routes/base.py` so
  Cloudflare doesn't idle-close long SSE streams during multi-hop retrieval.
- **Widget source URL rewriting** — the SSE `{type: "source"}` frame
  emitted on `/stream` rewrites each chunk's in-corpus path into a
  clickable public URL: rendered Developer Docs → `docs.aztec.network`,
  everything else (code, non-rendered docs) → GitHub blob at the
  corpus's release tag. The rewriter is version-aware (`_aztec_source_url`):
  it routes per the doc version in the source path (mainnet bare vs testnet
  `/testnet/`) and picks the release tag / noir pin by the request's active
  version. Sources are deduped by rewritten URL and capped at 10 per answer.
- **RAG context cap** (`RAG_MAX_DOC_TOKENS`, default 6k, prod 10k) —
  upstream feeds the model's full context window (~198k tokens) of
  retrieved docs on every query, which made answers take 60s+. The
  cap keeps generation to 10–20s while still grounding well. Bumped
  to 10k once the corpus grew to 12 sources to reduce late-source
  starvation.
- **Global rerank retrieval** — upstream `ClassicRAG` iterated source
  vectorstores FIFO, fetched k chunks per source, and broke once the
  token budget filled. With 12 sources and a 9k budget, sources #1–#2
  greedily consumed the entire budget and sources #4–#12 contributed
  zero docs to every answer. Our retriever embeds the question once,
  issues a single SQL query against pgvector with
  `WHERE source_id = ANY(%s)`, then greedy-packs the globally-sorted
  candidates. Retrieval is invariant to `AZTEC_SOURCE_IDS` ordering.
- **Rephrase auth fix** — `ClassicRAG` defaulted `api_key` to an agent
  UUID, which OpenRouter rejected with 401. The query rephrase step
  silently fell back to the raw question, so multi-turn follow-ups
  ("how does it differ?") went to retrieval without conversation
  context. Fix: default `api_key=None` so the LLM backend resolves
  the provider key.
- **Primary-source-first retrieval fix** — upstream dropped
  `agent.source_id` when `extra_source_ids` existed, so the "primary"
  source was never actually searched. Our `stream_processor` prepends
  it back onto `sources_list`.
- **Eval harness** (`scripts/eval/`) — 25 golden queries (tagged
  `identifier` / `concept` / `example`) with two run modes (direct
  retriever probe and end-to-end `/stream`) that assert source
  coverage, source diversity, banned identifiers, no Markdown tables,
  and per-query response-time SLAs. A second, manual variant-comparison
  workflow layered on top (`provision_test_agent.py` → `eval_retrieval.py
  --capture-answers` → `compare.py`) lets operators A/B a candidate
  prompt / source list / model against the live agent before shipping
  the change. See `scripts/eval/README.md`.
- **Reasoning-disable shim** — auto-injects
  `extra_body={"reasoning": {"enabled": false}}` for reasoning-mode
  OpenRouter models (e.g. `x-ai/grok-4.1-fast`) that would otherwise
  stream 100% chain-of-thought tokens with no visible answer. (`enabled:
  false` actually skips reasoning; `exclude: true` would still run — and
  bill — it, just hidden. See `llm/open_router.py`.)
- **CORS glob patterns** — `CORS_ALLOWED_ORIGINS` supports shell-style
  globs so Netlify preview URLs (`https://deploy-preview-*--aztec-docs-dev.netlify.app`)
  and local dev (`http://localhost:*`) don't have to be re-added per PR.
- **Widget embedding** — the live docs widget (in
  [`AztecProtocol/aztec-packages`](https://github.com/AztecProtocol/aztec-packages)
  under `docs/src/components/AztecDocsWidget/`) works from any origin in
  the CORS allowlist. Cloudflare Access must have **path-scoped Bypass
  applications** for the widget endpoints (`/stream`, `/api/search`,
  `/api/feedback`), otherwise browsers hit the SSO challenge inside an
  XHR and fail. The rest of the hostname stays SSO-gated.
- **No admin SPA** — the upstream React admin SPA at `frontend/` was
  removed in `chore/remove-unused-components`. All operator workflows
  are SQL or scripts under `scripts/`. Caddy at `/` returns a 200
  sentinel via `respond` so health checks don't 404.
- **`/api/version` endpoint** — public, unauthenticated `GET`/`POST`
  returning `{aztec_corpus_version, source_count}` from the
  `AZTEC_CORPUS_VERSION` setting (`unknown` when unset; production
  pins it to `v4.3.0`).
  [`@aztec/mcp-server`](https://github.com/AztecProtocol/mcp-server)
  reads this to gate against version drift between the MCP client and
  the deployed corpus. POST exists because Cloudflare Access gates GET
  on the apex hostname while letting POST `/api/*` through
  unauthenticated.
- **`/api/search` parity with `/stream`** — MCP search endpoint uses
  the same global-rerank algorithm as `/stream`, the same
  `_aztec_source_url` rewriting (so MCP consumers receive GitHub blob
  / `docs.aztec.network` URLs instead of internal corpus paths), the
  same primary-source-prepend fix, and an empty-apiref-chunk filter
  that drops apiref chunks whose body is just a path-only file
  heading.
- **Network-status agent tools**
  (`application/agents/tools/aztec_network.py`,
  `application/agents/tools/ethereum_network.py`) — give the Discord,
  widget, and Slack agents live read access to chain state.
  - **Aztec L2** via Aztecscan REST
    (`https://api.aztecscan.xyz/v1/{apiKey}/...`). Fifteen actions:
    latest height, latest block summary, a recent-block window
    (`/l2/blocks` — up to 20 blocks with timestamps for an inclusive
    `from_height`/`to_height` range, or the latest 20 with no range; use
    it to compute block intervals / average block time),
    blocks-by-finalization-stage,
    chain info (incl. L1 chain id + L1 contract addresses), validator
    totals, RPC node health, explorer search (`/l2/search` — resolve a
    block/tx/contract/account by hash, height, or address), contract
    instance lookup (`/l2/contract-instances/{address}`), L1 governance
    proposals (`/l1/governance/proposals`, optional `state` filter),
    chain finality tips (`/l2/tips` — proposed/checkpointed/proven/
    finalized heads), **transaction lookup by hash**
    (`/l2/tx-effects/{hash}` — full receipt for a mined tx: revert code,
    block, fee, effect counts; on a 404 it falls through to the pending
    pool `/l2/txs/{hash}` and the dropped list `/l2/dropped-txs/{hash}`,
    so one action reports mined / pending / dropped / not_found — the
    Aztecscan OpenAPI spec spells this route `/l2/txEffects/{hash}`, which
    404s in production; the kebab-case path is the live one), **block
    lookup by height or hash** (`/l2/blocks/{heightOrHash}` — trimmed
    header + the tx hashes it contains, capped at 10), **validator lookup
    by attester address** (`/l1/l2-validators/{attesterAddress}` —
    status decoded from the staking-contract enum ordinal, stake
    pre-divided by 10^18), and **recent reorgs** (`/l2/reorgs`, capped at
    10 — testnet has thousands of historical entries). The curated set
    was chosen by probing each endpoint live on both hosts.
    LLM-supplied identifiers are charset-
    validated before they reach the URL (hex ids must match
    `0x[0-9a-fA-F]{1,128}`; block ids may also be a decimal height) so a
    tool argument can never alter the request authority or escape its
    path segment. Each action takes a `network` arg —
    **`mainnet`** (the live network, anchored to Ethereum mainnet /
    `l1ChainId=1`, host `api.aztecscan.xyz`) or **`testnet`** (anchored
    to Sepolia / `l1ChainId=11155111`, host `api.testnet.aztecscan.xyz`)
    — **defaulting to `mainnet`**. Every result carries the `network` it
    came from, so testnet data is never mistaken for mainnet. The
    documented public placeholder key `temporary-api-key` works without
    signup; the key is a raw URL path segment so it must match
    `[A-Za-z0-9._-]{1,128}`. Per-network base URLs are configurable
    (`AZTECSCAN_MAINNET_BASE_URL` / `AZTECSCAN_TESTNET_BASE_URL`); config
    precedence: `user_tools.config` > env > default.
  - **Ethereum L1** via public JSON-RPC. Seven actions: block number, gas
    price, chain id, sync status, trimmed block header, transaction
    lookup by hash (`eth_getTransactionByHash` +
    `eth_getTransactionReceipt` combined — reports mined with
    success/reverted, pending, or not_found; useful for Aztec
    deposit/bridge and validator-staking txs), and address balance
    (`eth_getBalance` — e.g. checking an attester/proposer is funded).
    Each takes
    `network` = `"mainnet"` or `"sepolia"`; **Sepolia is the L1 the Aztec
    testnet anchors to** (`l1ChainId=11155111`). Defaults are
    publicnode.com endpoints; override via `ETHEREUM_RPC_URL` /
    `ETHEREUM_SEPOLIA_RPC_URL`. A literal Ethereum-MCP-server sidecar was
    rejected: the payload is identical JSON-RPC and `mcp_tool.py`'s SSRF
    guard blocks compose-internal hostnames; mirroring the MCP tool
    surface gives the same agent ergonomics without a sidecar.
  - **Security hardening shared by both tools**: SSRF `validate_url()`
    re-check per request (DNS-rebinding mitigation); env-derived config
    validated lazily at request time so a malformed env var degrades only
    these tools instead of breaking `ToolManager`'s eager
    instantiate-every-tool loop; `allow_redirects=False` (a malicious
    upstream could 302 to localhost or `169.254.169.254`); 512 KiB
    response cap; `network`/`block_tag` validated at the action boundary;
    transport-error messages are sanitized to the exception class name —
    `requests` exception strings embed the request URL, which can carry
    the Aztecscan key or an RPC provider token, so raw `str(exc)` never
    reaches logs or tool results.
  - **Provisioner** (`scripts/db/create_network_tools.py`) — idempotent
    upsert of the two `user_tools` rows under `user_id='local'`; with
    `--attach-to-agents` appends both UUIDs to `agents.tools` for every
    agent matching `surface IN ('discord','widget','slack')` (a
    re-run of `create_slack_chat_agent.py` preserves them). `--dry-run`
    previews without committing. Rollback SQL in the script docstring.
    Not attached to the public `/ask` agent (`tools='[]'` guardrail) or
    the per-user `Aztec MCP` agents (per-pseudonym `user_id` needs a
    `/api/internal/create_mcp_key` extension) — both are follow-ups.
- **Custom settings** — `MCP_PROVISIONING_KEY`, `USER_ID_PEPPER`,
  `AZTEC_SOURCE_IDS`, `AZTEC_CORPUS_VERSION`, `CORS_ALLOWED_ORIGINS`,
  `EMBEDDINGS_DIMENSION`, `RAG_MAX_DOC_TOKENS`, `VITE_ASK_AZTEC_AGENT_KEY`
  (build arg for the public `/ask` bundle), network-status tool vars
  `AZTECSCAN_MAINNET_BASE_URL`, `AZTECSCAN_TESTNET_BASE_URL`,
  `AZTECSCAN_API_KEY`, `ETHEREUM_RPC_URL`,
  `ETHEREUM_SEPOLIA_RPC_URL`, plus Discord-bot vars
  `DISCORD_TOKEN` and `NOIR_GUILD_IDS` (legacy single-guild
  `NOIR_GUILD_ID` is still honored).

### Repository map (fork-specific)

- [`CLAUDE.md`](./CLAUDE.md) — architecture and ops guide for this fork
  (read first for any code or deploy work).
- [`AZTEC_SETUP.md`](./AZTEC_SETUP.md) — end-to-end production deployment
  walkthrough (bootstrap, secrets, corpus ingest, verification, rollback).
- [`deployment/docker-compose-hub.yaml`](./deployment/docker-compose-hub.yaml) —
  production compose (cloudflared + Caddy + backend + worker + frontend +
  discord-bot + postgres/pgvector + redis).
- [`deployment/Caddyfile`](./deployment/Caddyfile) — reverse-proxy config,
  HTTP-only for the tunnel path.
- [`.env-template`](./.env-template) — every required env var with generation
  commands (`openssl rand -hex 32` for each `*_KEY`).
- [`extensions/discord/`](./extensions/discord/) — Discord bot source +
  Dockerfile.
- [`@aztec/mcp-server`](https://github.com/AztecProtocol/mcp-server) —
  the MCP server end users install locally; lives in a separate repo
  ([npm](https://www.npmjs.com/package/@aztec/mcp-server)). This repo
  only provides the HTTP endpoints (`/api/search`, `/api/version`,
  `/api/internal/create_mcp_key`) the MCP server consumes.
- The previous `extensions/mcp-server/` (standalone TS MCP server) and
  `extensions/react-widget/` (npm widget) directories were upstream
  artefacts not used by Aztec; both were removed. The live docs widget
  is in `aztec-packages` at `docs/src/components/AztecDocsWidget/`.

---

## Quickstart (dev compose)

> [!Note]
> Requires [Docker](https://docs.docker.com/engine/install/) with the
> compose plugin. The full production walkthrough — Postgres bootstrap,
> migrations, corpus ingest, agent provisioning, Cloudflare Access /
> bastion-relay setup — lives in [`AZTEC_SETUP.md`](./AZTEC_SETUP.md).

```bash
# 1. Provision .env from the template (then generate secrets per the comments)
cp .env-template .env

# 2. Start Postgres on its own so the pgvector init scripts run
docker compose -f deployment/docker-compose.yaml up -d postgres

# 3. Run migrations (creates agents/sources/conversations tables + Aztec MCP columns).
#    `scripts/` lives at the repo root, NOT inside the backend image, so bind-mount it.
docker compose -f deployment/docker-compose.yaml run --rm \
  -v $(pwd)/scripts:/app/scripts:ro \
  backend python scripts/db/init_postgres.py

# 4. Bring up the rest (backend on :7091, frontend-ask on :5174, redis, worker)
docker compose -f deployment/docker-compose.yaml up -d
docker compose -f deployment/docker-compose.yaml logs -f backend worker
```

Production uses `deployment/docker-compose-hub.yaml` instead — see
[`AZTEC_SETUP.md`](./AZTEC_SETUP.md).

## Project structure

- `application/` — Flask + Celery backend (Python 3.12). Migrations under `application/alembic/`.
- `extensions/discord/` — Discord bot (Honk AI).
- `frontend-ask/` — Public `/ask` chat surface (Vite + React 19).
- `scripts/` — Ingest, eval, and DB provisioning utilities.
- `deployment/` — Compose files (`docker-compose.yaml` dev, `docker-compose-hub.yaml` prod) and Caddy config.

## License

MIT. Original copyright © 2023 arc53; modifications © Aztec Labs. See [LICENSE](LICENSE).
