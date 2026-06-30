# PLAN — Application-layer content encryption at rest

**Status:** DESIGN — **APPROVED by Claude + Codex (gpt-5.5) after 6 review rounds** (2026-06-29).
Ready to implement; not started. **v6** — incorporates review rounds 1–5: non-Postgres sinks
(Redis cache, app logs, raw upload volume), scoped feature gates, `pending_tool_state`
completeness, `documents` read-path constraints, corrected forget tombstones, **a complete
25-table classification matrix** with a **programmatic credential sweep** backstop, the
**existing-log cutover purge**, and the `logging.info` grep fix. **Owner:** josh@aztec.foundation

**DECISIONS RESOLVED (2026-06-29):** path = **app-level** (infra-EBS not pursued); **D5 = derive**
the content key from `ENCRYPTION_SECRET_KEY` via HKDF (no new secret); **D6 = disable** the Redis
LLM cache (`LLM_CACHE_ENABLED=false`, default off; it was measured empty). D1 (encrypt
`tool_calls`)=yes, D2 (`/api/search metadata.question`)=yes, D3 (AAD no row-id)=yes, D4 (feedback
enum-only)=yes. **Implementation IN PROGRESS** on branch `feat/content-encryption-at-rest`.

**DONE + TESTED (279 tests, ruff clean):** codec (`content_encryption.py`), registry
(`content_registry.py`), settings + fail-closed boot validator, Redis cache gated off
(`LLM_CACHE_ENABLED`), content logs redacted (openai.py/research_agent.py), all 5 Class-A repos
wired at the choke point (encrypt on write / decrypt on read / read-both), `api_key_fp`
blind-index + migration `0012`, unit + integration tests (round-trip, ciphertext-at-rest,
fp-lookup, read-both), regression green incl. forget/erasure (33) + compression + continuation.
**REMAINING:** `scan_plaintext.py` verifier, `encrypt_content_backfill.py`, the cutover-time
CHECK-constraints migration, feature gates (UPLOADS/AGENTIC — low-priority, tables measured
empty), `decrypt_export.py` for honk-report, Class-C credential hardening, docs
(README/AZTEC_SETUP/.env-template/CLAUDE.md). **Note:** `ruff format` reflowed some pre-existing
multi-line SQL in the 3 touched repos (it's what the documented `ruff format .` produces; CI runs
`ruff check` only) — call out in the PR.

**What's required vs hardening:** the Discord "user data at rest" obligation is satisfied by
**Class A (message content)** + **Class B (PII)**. **Class C (system credentials/bearer keys)**
is defense-in-depth — several are already hashed (`mcp_tokens`/`mcp_audit`) or gated; do them
but they don't block the attestation. The matrix exists so **no table is left unclassified**
(the scan asserts this).

## 1. Goal & why

Discord's Developer Policy requires stored user data to be **encrypted at rest**. Honk AI stores
user content in Postgres, transiently in Redis, in **Docker/journald logs**, and (if uploads are
enabled) in a **raw-file volume** — all currently **plaintext** on an unencrypted EBS volume,
with no AWS access to enable disk/volume encryption. This plan encrypts/eliminates plaintext at
the application layer, keyed via env. Most footgun-prone option: a **missed sink** → false
attestation. Built to make that impossible-by-default and *provable*.

## 0. PREREQUISITE (D0) — enumerate enabled surfaces, and GATE OFF the rest

**D0 RESOLVED — measured on prod (`docsgpt-aztec-postgres-1`) 2026-06-29. The scope collapses
dramatically:**
- **Dormant DocsGPT surface is ENTIRELY EMPTY:** `attachments`, `memories`, `todos`, `notes`,
  `workflows`, `workflow_runs`, `connector_sessions`, `shared_conversations` = **0 rows**. So all
  of Class D / connectors / the upload+raw-file-volume / memories.path blind-index / documents
  private-ingest branches collapse to **"gate off + empty-guard"** — **no encryption needed**.
  (`/api/upload` is behind `decoded_token` auth, ingest/routes.py:43; not anonymous.)
- **Conversation sharing unused:** `conversations.shared_token` = 0, `shared_conversations` = 0.
  The "627" is just `conversations.api_key` bookkeeping on every row — 626 are 36-char **agent
  UUIDs**, 1 is a 64-char key (low sensitivity → Class C fingerprint). One `agents.shared_token`
  set (1 of 14), no webhook tokens.
- **`users` has NO PII** — wrapped column is `agent_preferences` (UI prefs). → Class E confirmed;
  **Class B is effectively empty** (connector_sessions, the only PII table, is gated-off-empty).
- **`sources` (66) / `user_tools` (2) / `prompts` (4) / `agents` (14) all operator-owned**
  (`sources.user_id ∈ {local, docsgpt}`, no user-authored) → Class D guards pass trivially.
- **Class A content is tiny:** ~978 `conversation_messages`, 1070 `user_logs`, 981 `stack_logs`,
  **0 `pending_tool_state`**, 0 erased → backfill is ~3k rows, fast and low-risk.
- **Redis LLM cache (db 2) is EMPTY** (DBSIZE 0) → disabling it (D6) costs nothing. **Celery
  result backend (db 1) has 1441 keys** — verify these hold no user content (synchronous answer
  path doesn't use Celery; ingest is operator-only) and add a TTL/flush note.

**Net: the real work is Class A (5 tables, ~3k rows) + disable Redis cache + redact/purge logs +
the verification scan/guards + empty-guards for the dormant tables.** The plan's worst-case
branches (encrypt documents/attachments/memories/workflows, blind-index memories.path) are all
moot in this deployment. Re-confirm before each release that the dormant tables are still empty
(the scan enforces it).

Honk AI uses a slice of upstream DocsGPT. Surfaces reachable by any user
(Discord/Slack/widget/`/ask`/MCP): **file uploads / private ingest** (`/api/upload`), **agentic**
features (tools / memories / notes / todos / workflows), the **Redis LLM cache**.

**For every surface not used: add a fail-closed runtime gate** (config flag → 403/404 at the
route, e.g. `UPLOADS_ENABLED=false`, `AGENTIC_ENABLED=false`) so plaintext can't reappear after
attestation (closes the Tier-2 TOCTOU hole — verify-empty alone is insufficient because a
later-enabled feature would silently write plaintext). Run the §12 scan **after** the gates are
live. Surfaces that ARE used move to Tier 1 with the per-field treatment noted.

## 2. Scope registry — complete 25-table matrix (`application/security/content_registry.py`)

The verification scan (§12) enumerates **every** table; an unclassified one fails the build.
Mechanisms: **A=content-encrypt**, **B=PII-encrypt**, **C=credential blind-index/fingerprint**,
**D=gate-off + scoped/empty guard**, **E=out of scope (rationale)**. Three crypto modes:
**text**=envelope string; **json-blob**=`{"__enc__":"honkenc:…"}`; **json-leaf**=named leaf→envelope,
structure + allowlisted keys plaintext.

### Class A — message content (REQUIRED; the bot's actual write surface)

| Table | Field(s) | Mode |
|---|---|---|
| `conversation_messages` | `prompt`, `response`, `thought` | text |
| `conversation_messages` | `tool_calls` | json-blob (D1) |
| `conversation_messages` | `message_metadata` | **structural, NOT encrypted** — prod holds only `citation_filter` + `type`; scan asserts the `STRUCTURAL_METADATA_ALLOWLIST` so a future content key fails the build (refined from json-leaf after measuring prod) |
| `conversations` | `name` | text |
| `conversations` | `compression_metadata` | json-leaf (`*.compressed_summary`) |
| `pending_tool_state` | `messages`,`pending_tool_calls`,`agent_config`,`client_tools`,`tools_dict`,`tool_schemas` | json-blob (all 6) |
| `user_logs` | `data` | json-blob |
| `user_logs` | `metadata.question` | json-leaf (D2) |
| `stack_logs` | `query` | text |
| `stack_logs` | `stacks` | json-blob |

`conversation_messages.feedback`: stores `{"text":<lowercased>,"timestamp":…}`, value normalized
to the `like`/`dislike` enum (feedback.py:61-68). Out of scope **while enum-only**; scan asserts
`feedback IS NULL OR feedback->>'text' IN ('like','dislike')`, **plus write-time enum validation**
in `set_feedback` so free text can't land (→ json-leaf if ever needed, D4).
`conversation_messages.sources` = public-doc chunks → out of scope unless private ingest enabled.

### Class B — PII (REQUIRED if the surface is enabled)

| Table | Field(s) | Treatment |
|---|---|---|
| `connector_sessions` | `user_email` | encrypt (PII; migration 0006:201 flags it). **Gate off** (connectors unused by the bots) → empty-guard; if enabled, encrypt. |
| `users` | (only `user_id` pseudonym + a wrapped col — **verify no email/PII in the wrapped column at models.py:41**) | E if pseudonym-only; B if it carries PII |

### Class C — secrets / bearer credentials (HARDENING; equality-lookup → fingerprint, not plaintext)

| Table | Field | Treatment |
|---|---|---|
| `user_logs` | `data->>'api_key'` | `api_key_fp` (§8) — already in plan |
| `stack_logs` | `api_key` | `api_key_fp` sibling column (same mechanism) |
| `token_usage` | `api_key` | actively written for spend tracking; equality-queried (token_usage.py:57) → `api_key_fp`, drop plaintext |
| `agents` | `key` (CITEXT unique, `find_by_key` agents.py:122), `shared_token` (agents.py:206), `incoming_webhook_token` (agents.py:223) | bearer credentials, equality lookup → blind-index/`*_fp`, or scoped NULL-guard if sharing/webhook surfaces disabled. **Plus** `name`/`description`/`tools`/`json_schema` user-/operator-authored → encrypt the **per-user MCP-key agent** rows (`create_mcp_key` writes one per Discord/Slack user); operator prod agents low-content |
| `conversations` | `api_key`, `shared_token` (models.py:306; written conversations.py:153, `set_shared_token` 319; 0006:154 flags both bearers) | `*_fp` blind-index, or scoped NULL-guard if conversation-sharing disabled |
| `shared_conversations` | `api_key` (models.py:368; written/dedup-queried shared_conversations.py:58,106,164) | `api_key_fp` + rewrite the dedup index/query, or assert `api_key IS NULL` if promptable shared links disabled |
| `connector_sessions` | `session_token`,`token_info`,`session_data` | OAuth tokens → encrypt; gated off (unused) → empty-guard |
| `mcp_tokens`, `mcp_audit` | `token_hash`,`args_hash` | **already SHA-256 hashed at rest** (0006:228,251) — no action; classified E-by-hashing |

**Systematic Class-C rule (so this isn't hand-enumeration whack-a-mole):** the registry marks
every column whose name matches `*token*`/`*api_key*`/`*secret*`/`*webhook*` (minus the
already-hashed `*_hash` and the `*_fp` fingerprints themselves) as a credential needing **either**
a fingerprint/blind-index (if equality-looked-up) **or** a scoped NULL-guard (if its feature is
gated off). The §12 scan **enumerates these columns programmatically from `information_schema`**
and fails on any plaintext bearer — so a credential column added later (or one missed in this
table) is caught by the scan, not by re-reading the schema. The rows above are the known
instances; the scan is the backstop.

### Class D — dormant upstream surface (gate-off + scoped/empty guard; encrypt if enabled)

Guards are **scoped predicates on user/private rows**, not bare row-counts — these tables are
legitimately non-empty (public corpus, operator-provisioned tools/agents).

| Table | Content field(s) | Scoped guard / if-enabled treatment |
|---|---|---|
| `documents` (pgvector) | `text`,`metadata` | public corpus expected; guard = only approved public `source_id`s. If `/api/upload` enabled (ingest/routes.py:118→pgvector.py:243; MCP rag.py:128): `text` ILIKE (apiref_resolver.py:376) + structural `metadata` keys (search.py:278) break under random enc → structural-keys-plaintext + deterministic/blind-index for apiref text, or keep private ingest disabled |
| `sources` | `name`,`file_path`,`directory_structure`,`file_name_map` | public-corpus rows expected (internal/routes.py:252); guard = fail on user-ingest rows; encrypt those |
| `user_tools` | `name`,`custom_name`,`display_name`,`description`,`config_requirements`,`actions` | operator network tools expected (create_network_tools.py); guard = fail on user-authored; `config.encrypted_credentials` already encrypted (tool_executor.py:444) |
| `agent_folders` | `name`,`description` | gate off (agentic unused) → empty-guard; encrypt user rows if enabled |
| `attachments` | `filename`,`content`,`metadata` | uploads gated off → empty-guard; else encrypt + handle the raw-upload volume (§3E) |
| `memories` | `content`; **`path`** | `path` UNIQUE + `LIKE`-prefix (memories.py:40,51,249) → deterministic/blind-index/plaintext; `content` normal enc. Empty-guard if agentic off |
| `notes` | `title`,`content` | empty-guard / encrypt (no `metadata` col) |
| `todos` | `title` | empty-guard / encrypt (no `metadata` col) |
| `workflows`/`workflow_nodes`/`workflow_edges`/`workflow_runs` | names/descriptions/`config`/`source_handle`/`target_handle`/`inputs`/`result`/`steps` (models.py:393-457) | empty-guard / json-blob encrypt |
| `prompts` | `content`,`name` | operator system prompts → not user data; guard = fail on user-authored prompts if ever enabled |

### Class E — out of scope (structurally non-sensitive; rationale)
`app_metadata` (only `key`/`value`, models.py:133 — singleton instance state/flags; the scan's
Class-C column-name rule covers any future credential stored as a `value`), `token_usage`
(counters; `api_key` → C), `users` (pseudonym — pending the models.py:41 wrapped-column
verification → else B), `mcp_tokens`/`mcp_audit` (already hashed → C-by-hashing).
`shared_conversations` is **C** (has `api_key`), not out-of-scope. Each registry-listed with
rationale so the "every-table-classified" assertion passes.

## 3. Sinks beyond Postgres tables

**3A. Threat model / caveat** — protects against theft of the data/snapshot **in isolation**;
not against full host access (key in env on unencrypted root). Sufficient for the Discord
attestation; not a substitute for eventual EBS encryption. Accepted.

**3B. Redis LLM cache (MUST).** `gen_cache` (cache.py:47/79) + `stream_cache` (cache.py:112)
store responses in Redis db 2 for 1800s; they skip only when `tools is not None`, so no-tool
calls (answer stream, title, compression, rephrase) cache plaintext. **Disable via new
`LLM_CACHE_ENABLED=false`** (rec, D6) or encrypt the payload with the codec. Scan asserts no
plaintext answer payloads in Redis.

**3C. Celery (Redis db 0 broker / db 1 result backend; JSON-serialized, celeryconfig.py:6).** The
synchronous answer path is **not** queued — no chat prompt/response flows through Celery. Tasks:
**ingest** enqueues only user/job/filename/path **metadata** (not file bodies), result =
job/filename/user/source-id (workers/ingest.py:212) — user metadata only **if uploads enabled**
(D0: gated off, empty); **periodic** returns counts only (periodic.py:117); **`mcp_oauth_task`**
returns tool metadata + writes OAuth status/auth URLs to the Redis cache db (workers/mcp_oauth.py:64)
— covered by the agentic/connectors gate. db1 (1441 keys today) is a general plaintext-at-rest sink
→ flush/TTL at cutover (§13). All Celery exposure is bounded by the upload + agentic/connectors gates.

**3D. Application logs (MUST — always applies).** `OpenAILLM` logs full cleaned messages at INFO
(openai.py:240 and the stream variant ~276 — note: `logging.info`, not `logger.info`) — prompts,
history, retrieved snippets; research mode logs LLM output (research_agent.py:322,400). These hit
stdout → **Docker json-file / journald logs persisted on the unencrypted volume**. Fix:
**redact/drop content from these statements** (lower to DEBUG and/or truncate to non-content),
audit every `logging.info`/`logger.info` across `application/llm/*` and `application/agents/*`,
and add a CI grep guard matching **both** `logging.info(` and `logger.info(` with content args.
**Cutover (MUST):** the redaction only stops *future* leakage — also **purge existing plaintext
logs** at rollout: truncate the backend/worker Docker json-file logs
(`/var/lib/docker/containers/*/*-json.log`) and any journald entries, and `FLUSHDB` the Redis
cache db. Add a verification guard (grep the live log tail for content patterns). Note the
container log driver / rotation in `AZTEC_SETUP.md`.

**3E. Raw upload volume (MUST if uploads enabled).** `/api/upload` writes original file bytes to
`uploads:/app/application/inputs`, a persistent named volume (compose-hub.yaml:86-87,127,330),
before Celery parses them. If uploads enabled: encrypt-at-write or purge-after-parse that volume.
If gated off (rec): add an **empty-volume guard** to the scan.

## 4. Crypto codec
`application/security/content_encryption.py` (separate from the credential helper; do NOT reuse
AES-CBC). **AES-256-GCM**, 96-bit random nonce/value. **Key:** one app key per `kid`, derived
once at startup via HKDF-SHA256, cached (no PBKDF2-per-row). **AAD:**
`f"{table}:{column}:{kid}:{ENVELOPE_VERSION}"`, no row-id (D3; accepts same-column ciphertext
replay — content is legitimately re-emitted in a column, conversation_service.py:233).
**Envelope:** `honkenc:1:<kid>:g256:<b64url(nonce‖ct‖tag)>`; json-blob wraps `{"__enc__":…}`.
API: `encrypt_text/decrypt_text`, `encrypt_json_blob/decrypt_json_blob`,
`encrypt_json_leaves/decrypt_json_leaves(…,leaf_paths,keep_plaintext)`, `is_encrypted`;
`encrypt_*` idempotent.

## 5. Key management & rotation
Keyring (mirrors `pseudonyms.py`). Settings: `CONTENT_ENCRYPTION_ENABLED`,
`CONTENT_ENCRYPTION_ACTIVE_KID`, `CONTENT_ENCRYPTION_KEYS` (csv `kid:secret`),
`CONTENT_ENCRYPTION_LEGACY_READ`, `LLM_CACHE_ENABLED`, the feature gates (§0). v1 key (D5) MAY be
`HKDF(ENCRYPTION_SECRET_KEY, info="honk-content-encryption-v1")`. **Fail-closed boot validator**
(model on USER_ID_PEPPER, settings.py:170): reject if enabled and (active kid absent | <32B |
source is the default `"default-docsgpt-encryption-key"` settings.py:140 | low entropy).
Rotation: readers hold old+new → flip ACTIVE_KID → backfill re-encrypts → scan old kid → drop.

## 6. Read path: read-both → strict
`is_encrypted`→decrypt; `None`/tombstone→`None`; plaintext+`LEGACY_READ`→plaintext;
plaintext+strict→log class-only+`(table,column,id)`, return `None`. **Decrypt failure** never
crashes the answer path, never logs decrypted bytes/exception message (class only); history drops
the turn/placeholder (same as the `erased_at` filter, stream_processor.py:239).

## 7. Choke point: repository layer (verified)
`conversations.py`: `append_message`(514→540), `update_message_at`(583→590) encrypt
prompt/response/thought/tool_calls + leaf message_metadata; `get_messages`(486)/`get_message_at`
(497)/`_message_row_to_dict`(27) decrypt (route ALL message reads here); `create`(125)/`rename`
(247)/`get*`/`list_for_user`(195-246) for `name`; `update_compression_metadata`(333)/
`append_compression_point`(405)/`get_compressed_context` for leaf summary. `user_logs.py`:
`insert`(31→58); `list_paginated`(81)/`find_by_api_key`(113) via `api_key_fp` (§8).
`stack_logs.py`: `insert`(25→41) for query+stacks. `pending_tool_state.py`: `save_state`(31→62)/
`load_state`(101) for all 6 JSONB cols. Repo-bypassing reads to cover/prove-clean: `/api/search`
assembly + global-rerank, evals, MCP `rag.py`, and **all raw writes under `scripts/db/`** (§11).

## 8. Breakages
1. **`user_logs.data->>'api_key'`** (user_logs.py:99,128) breaks when `data` is opaque. Add
   `api_key_fp=HMAC-SHA256(api_key)` (dedicated namespace), **partial non-unique** index. Ordering
   (§13): add col + dual-write + reads via fp **with `data->>'api_key'` fallback** + backfill fp
   **before** encrypting `data` + drop fallback.
2. **MCP role** `docsgpt_mcp_ro` has full SELECT on conversation/prompt/attachment/memory/
   workflow tables (0006) → **reads ciphertext post-encryption** (acceptable; **never give it the
   key**). MCP content analytics move to non-content columns.

## 9. Compatibility (verified)
- `/forget-me` redacts by IDs/timestamps. Exact tombstones (round-3 corrected): **message
  redaction** sets `prompt/response/thought=NULL`, `tool_calls/sources=[]`, `attachments={}`,
  `message_metadata={}`, `feedback=NULL`, **`requester_user_id=NULL`, `erased_at=now()`**
  (routes.py:469); **compression-summary tombstone** is a separate update clearing only
  `prompt`, `response`, `message_metadata` + `erased_at` (routes.py:495); conversations clear
  `name`, `compression_metadata=NULL` (routes.py:486). Leaf encryption preserves `->>'type'`. ✅
- Retention by timestamp (periodic.py:89). ✅ Single app key → shared-thread/cross-user
  decryptable. ✅ Triggers (0020 user_id, attachment AFTER DELETE) touch only user_id/UUID array,
  no conflict. ✅

## 10. Operator tooling
`honk-report` (raw psql sampling, SKILL.md:93)→ciphertext: replace with
`scripts/db/decrypt_export.py`. `scripts/eval/` needs the codec. Plain psql still does
counts/timestamps/feedback-enum/model-ids/surfaces.

## 11. Backfill & import re-entry
`scripts/db/encrypt_content_backfill.py`: batched, idempotent, per Tier-1 table; `api_key_fp`
before `data`; ends with "0 plaintext" assert. **All raw `scripts/db` writers** (`backfill.py`
Mongo→PG import: user_logs 438, attachments 1220, workflow_runs 2415, prompts/conversations/
pending) re-introduce plaintext → codec-aware or **disable post-rollout**; static guard covers all
`scripts/db/`. Rejected: aging-out / lazy-on-write.

## 12. Verification (defensibility)
1. **Repo round-trip tests** per classified field (raw DB encrypted; repo read plaintext).
2. **`scripts/db/scan_plaintext.py`** (registry-driven): text (`LIKE 'honkenc:%'`), json-blob
   (`? '__enc__'`), json-leaf (walk JSON, check content leaves — DB CHECK can't see these).
   Also: **Class-D scoped-predicate guards** (fail on user/private rows), **feedback enum assert**,
   **"every table classified"**, **Redis scan** (no plaintext payloads), **live-log-tail scan**
   (no content patterns), **uploads-volume empty-guard** (if gated off), **feature-gate assert**
   (gated endpoints return 403/404), and the **programmatic Class-C credential sweep**: enumerate
   `*token*`/`*api_key*`/`*secret*`/`*webhook*` columns from `information_schema`, fail on any
   plaintext bearer (catches credential columns not hand-listed in §2 Class C). **Allowlist
   non-bearer matches** to avoid false positives: `token_limit`, `limited_token_mode`,
   `prompt_tokens`, `generated_tokens` (counters/limits) and the `*_fp`/`*_hash` columns. Include
   the explicit JSON credential paths (`user_logs.data->>'api_key'`, `connector_sessions.token_info`).
3. **Static guard** (Semgrep/CI grep): reject raw `INSERT/UPDATE` to classified tables/cols
   outside allowlist (repos, encrypting backfill, forget eraser, tests) incl. all `scripts/db/`;
   **+ reject content in `logger.info` across `application/llm/*` & `application/agents/*`** (3D).
4. **DB CHECK** (`NOT VALID` → validate post-backfill), per-table (only `conversation_messages`
   has `erased_at`):
   - `conversation_messages` text: `col IS NULL OR erased_at IS NOT NULL OR col LIKE 'honkenc:%'`.
   - `conversation_messages.tool_calls`: `… OR erased_at IS NOT NULL OR tool_calls='[]'::jsonb OR tool_calls ? '__enc__'`.
   - `conversations.name`: `name IS NULL OR name LIKE 'honkenc:%'`.
   - `stack_logs.query`: `query IS NULL OR query LIKE 'honkenc:%'`; `stack_logs.stacks`: `stacks='[]'::jsonb OR stacks ? '__enc__'`.
   - `user_logs.data`: `data IS NULL OR data ? '__enc__'`.
   - `pending_tool_state` (all 6 JSONB): `col ? '__enc__'` (no erased_at on this table).
   - json-leaf fields (`message_metadata`, `compression_metadata`, `metadata.question`): **no
     column CHECK** — scan-only.
5. **MCP-role smoke test** — `docsgpt_mcp_ro` content SELECTs return ciphertext.

## 13. Rollout (zero-downtime)
1. Ship codec+registry+repo wiring (read-both/write-encrypted); add `api_key_fp` (dual-write,
   reads via fp+fallback); set `LLM_CACHE_ENABLED=false`; **redact content logs (3D)**; **enable
   feature gates (§0)**; make/flag `scripts/db` writers codec-aware. Rebuild backend+worker.
2. Backfill: `api_key_fp` first, then encrypt Tier-1 fields. Re-run until clean.
3. Drop the `data->>'api_key'` fallback. Deploy.
4. Scan → 0 plaintext (PG + Redis + uploads vol + **live log tail**); Tier-2/Class-D scoped
   guards; feature-gate + MCP-role smoke; swap honk-report to decrypt tool.
   **Cutover purge (§3D):** truncate existing backend/worker Docker json-file logs + journald,
   and `FLUSHDB` the Redis cache db (db 2) **and the Celery result backend (db 1, 1441 stale keys)**,
   so pre-change plaintext is gone.
5. Flip `CONTENT_ENCRYPTION_LEGACY_READ=false`; `up -d --force-recreate`.
6. VALIDATE CHECK constraints; enable CI static guard.

## 14. Docs to update
`README.md`, `AZTEC_SETUP.md` (vars, boot validator, `LLM_CACHE_ENABLED`, log driver, feature
gates), `.env-template`, `CLAUDE.md` (choke-point rule; raw `scripts/db` writes registry-aware;
no content in logs; Redis-cache-off), `honk-report` SKILL.md, `scripts/eval/README.md`,
`application/mcp_server/README.md` (MCP sees ciphertext).
