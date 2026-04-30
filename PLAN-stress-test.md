# Stress test plan — DocsGPT Aztec production

> Revised 2026-04-27 after Codex review. See "Revisions" at the bottom.

## 0. Objective

Find the concurrency ceiling of the live `aztec.adjacentpossible.dev`
deployment under realistic mixed traffic from the docs widget, the Discord
bot, and the MCP server. Identify the first-failing subsystem, validate
that prediction against measured behavior, and produce a ranked mitigation
list with cost/ROI.

### SLOs the test must defend

`/stream` (widget + Discord) and `/api/search` (MCP) have different
latency profiles, so the SLOs are tracked **per endpoint**.

| Endpoint | Metric | Target | Stop-test threshold |
| --- | --- | --- | --- |
| `/stream` | P95 first-token | < 3 s | > 6 s sustained 60 s |
| `/stream` | P95 total stream time | < 15 s | > 25 s sustained 60 s |
| `/api/search` | P95 total | < 4 s | > 8 s sustained 60 s |
| any | Error rate (5xx + timeout + disconnect) | < 1 % | > 2 % sustained 60 s |
| Postgres | Active connections | < 80 | > 95 of 100 default `max_connections` |

---

## 1. Stack inventory (audited 2026-04-27)

Numbers below were pulled from the codebase, not assumed.

### Backend / Flask
- Gunicorn `gthread`, **4 procs × 8 threads = 32 in-flight slots**
  (`application/Dockerfile`).
- `--timeout 120 --graceful-timeout 30 --keep-alive 75`,
  `stop_grace_period: 40s` on the backend service.
- SSE hot path: `application/api/answer/routes/base.py` →
  `_iter_with_heartbeat`, heartbeat every **15 s**, internal
  producer→consumer queue with `maxsize=64`.
- Each in-flight stream pins one gthread for the full lifetime of the
  upstream LLM call (typical 5–15 s).

### Retrieval — `/stream` (widget + Discord)
- `application/retriever/classic_rag.py:_get_data`.
- **One** sync OpenAI embedding call per request
  (`text-embedding-3-large`, 3072-dim).
- pgvector global rerank: **single** SQL with
  `WHERE source_id = ANY(%s)`, greedy-pack into 10 000-token budget.

### Retrieval — `/api/search` (MCP) ⚠ different code path
- `application/api/answer/routes/search.py:67-123` —
  **per-source loop**. For *each* `source_id` in `AZTEC_SOURCE_IDS`:
  - constructs a fresh `VectorCreator.create_vectorstore(...)` (one
    raw `psycopg.connect()`)
  - calls `docsearch.search(query, k=chunks_per_source * 2)`
    (one OpenAI embedding call + one SQL search)
- Early-stops when `len(results) >= chunks` (default chunks count).
  In the worst case (no source matches early), this is **N embeds +
  N seq scans + N raw psycopg connections** — and `N = 12` in prod.
- A single `/api/search` call can issue 1×–12× the upstream/DB load
  of a single `/stream` retrieval.

### Postgres
- `pgvector/pgvector:pg16`, single instance, **shared** by both the
  prod compose (`docsgpt-aztec-*`) and the dev compose
  (`docsgpt-oss-*`) on this host.
- SQLAlchemy engine pool `10 + 20 overflow` per process
  (`application/storage/db/engine.py`), `pool_pre_ping=True`,
  `pool_recycle=1800`, `statement_timeout=30 s`.
- **pgvector path uses its own `psycopg.connect()` — bypasses the
  SQLAlchemy pool.** Separate connection-pressure axis.
- Default `max_connections=100`. Across 4 backend procs × (10 + 20)
  + per-stream raw psycopg + Celery + dev compose, headroom is tight.
- 3072-dim ⇒ no IVFFlat (pgvector skips index for dims > 2000).
  Every retrieval is a **sequential scan** over `documents`.

### LLM provider
- OpenRouter, streaming. No client-side retry/backoff.
- Widget primary: `x-ai/grok-4.1-fast` (reasoning-disable shim).
- Discord/default: `z-ai/glm-4.6`.
- One LLM client instance per request (instantiated through
  `LLMCreator`).

### Discord bot
- `extensions/discord/bot.py`, async via `aiohttp.ClientSession`,
  one POST `/stream` per user message. 180 s client timeout. No
  internal concurrency cap.

### MCP server
- `extensions/mcp-server/`. Hits **`/api/search`** (JSON, not SSE).
  60 s request timeout, no concurrency cap on the client side.

### Edge / proxy
- Caddy: `flush_interval -1` on `/api/*` and `/stream` (no buffering).
  No explicit timeouts → Caddy defaults.
- Cloudflare Tunnel: outbound-only connector. Cloudflare's edge will
  502/524 long-idle streams — mitigated by the 15 s heartbeat.

### Resource caps
- **None.** No `mem_limit`, `cpus`, or `deploy.resources.limits` on any
  container in `docker-compose-hub.yaml`. Host RAM/CPU is the only
  ultimate cap, and the dev compose runs side-by-side on the same host.

### Persistence side-effects of a `/stream` request
- `conversations` table — written **only** if `save_conversation=True`
  (default). The harness sets `false`.
- `user_logs` — **unconditional** insert per request. Harness will
  add ~3000 append-only rows. Acceptable.
- `token_usage` — **unconditional** insert per LLM call. Same. Acceptable.

---

## 2. Predicted bottleneck order (to validate)

Ranked most-likely-first-to-fail, separated by traffic type.

### `/stream` traffic (widget, Discord)

1. **Gunicorn slot saturation (32)** — every SSE answer occupies one
   thread for full stream duration.
2. **OpenRouter latency / 429s on `grok-4.1-fast`** — wall-clock of the
   stream is mostly the upstream model.
3. **pgvector sequential scan CPU on Postgres** — 3072-dim, no index.
4. **OpenAI embedding rate limits / latency** — one call per request.
5. **Raw psycopg pgvector connections** — eats from `max_connections=100`.
6. **Host CPU/RAM** — Postgres seq scans and Python thread switching.
7. **Cloudflare Tunnel / Caddy** — unlikely to be first.

### `/api/search` traffic (MCP) — different ranking

1. **OpenAI embedding rate limits** — per-source loop fires up to
   12× the embeddings of a single `/stream` retrieval. If the
   embedding tier RPM is low, MCP load hits this *before* Postgres.
2. **pgvector seq-scan CPU** — multiplied by N sources per request.
3. **Raw psycopg connection burst** — one fresh connection per
   source per request. A 5-rps MCP burst can demand 60 short-lived
   pg connections in flight on top of the steady ones.
4. **Gunicorn slots** — `/api/search` is short (~1–4 s) so it
   releases slots faster than `/stream`.

### Predicted ceilings on a single VM, today's config

- ~24 concurrent `/stream` before P95 first-token slips past 3 s
- ~32 concurrent before total/error SLOs break
- `/api/search` ceiling more sensitive to OpenAI tier; predict
  ~10–15 concurrent before P95 > 8 s on a low embedding tier.

---

## 3. Realistic per-client volume envelope

| Client | Steady RPS | Burst RPS | Endpoint | Share of mix |
| --- | --- | --- | --- | --- |
| Widget (anon docs traffic) | 1.5–2.5 | 4–6 | `/stream` | 75 % |
| Discord (`/ask`) | 0.05–0.2 | 0.5 | `/stream` | 10 % |
| MCP | 0.1–0.5 | 0.5–1 | `/api/search` | 15 % |
| **Aggregate** | **~2–3** | **~5–7** | mix | 100 % |

---

## 4. Test design

### 4.1 Tooling — custom Python asyncio harness

`scripts/loadtest/run_stress.py` using `httpx.AsyncClient` + manual
SSE parsing. Reuses the SSE parser from `scripts/eval/eval_retrieval.py`
rather than starting from zero.

Per-request capture (timestamps, all monotonic):

- `t_request_start`
- `t_headers_received`
- `t_first_sse_data` — first non-comment frame (skip `: ping`)
- **`t_first_source_frame`** — `{"type": "source"}` arrives
  *after* retrieval completes and *before* the LLM stream starts.
  This is the clean phase boundary.
- **`t_first_answer_token`** — first `{"answer": …}` frame.
- `t_last_frame`
- HTTP status, disconnect reason, error payload, byte count

Derived metrics:

- `retrieval_ms = t_first_source_frame − t_headers_received`
- `llm_ttft_ms = t_first_answer_token − t_first_source_frame`
- `total_ms = t_last_frame − t_request_start`

This split is the single observation that distinguishes pgvector
slowness from OpenRouter slowness without needing server-side changes.

Three "client classes":

- `widget_client` → POST `/api/answer/stream`, anonymous payload,
  **`save_conversation=false`**
- `discord_client` → POST `/api/answer/stream`, history-as-string
  payload (matches the bot's wire format), **`save_conversation=false`**
- `mcp_client` → POST `/api/search` (JSON, not SSE)

### 4.2 Question pool — 60 prompts

Bootstrap from `scripts/eval/golden_queries.json` (15 questions),
expand to 60:

- 60 % paraphrases of the golden set
- 20 % "hot" repeats — top 5 questions used at 4× weight to mimic
  real widget traffic distribution
- 20 % multi-turn follow-ups (Discord/MCP only) to exercise the
  rephrase auth path

Manual sanity pass before commit: spot-check that paraphrases don't
all collapse to the same retrieval cluster (which would make the
embedding cache mitigation look better than reality).

### 4.3 Ramp profile — endpoint-isolated, then mixed

A single mixed ramp can't distinguish "gunicorn slots saturated"
from "Postgres got slow" from "OpenRouter got slow". So the test
runs in three sequential phases.

#### Phase A — `/stream` only (widget+Discord), ~30 min

Isolates the SSE path so retrieval/LLM behavior is clean.

| Phase | Concurrent `/stream` | Hold |
| --- | --- | --- |
| Warmup | 4 | 3 min |
| Step | 8 | 3 min |
| Step | 12 | 3 min |
| Step | 16 | 5 min |
| Step | 20 | 5 min |
| Step | 24 | 5 min |
| Step | 28 | 3 min |
| Step | 32 | 3 min |
| Burst | 36, 40 | 90 s each — only if 32 met SLO |

#### Phase B — `/api/search` only (MCP), ~10 min

Isolates the per-source loop path. Shorter requests, so steps go higher.

| Phase | Concurrent `/api/search` | Hold |
| --- | --- | --- |
| Warmup | 2 | 2 min |
| Step | 4 | 2 min |
| Step | 8 | 2 min |
| Step | 12 | 2 min |
| Step | 16 | 2 min |

#### Phase C — mixed (production-like), ~15 min

After A and B established each path's ceiling, run the realistic mix
at the highest concurrency that *both* phases passed at, and a soak
above the steady-state envelope.

| Phase | Mix (75 % widget / 10 % Discord / 15 % MCP) | Hold |
| --- | --- | --- |
| Steady | aggregate concurrency = 12 | 5 min |
| Steady | aggregate concurrency = 16 | 5 min |
| Soak | aggregate concurrency = 20 | 15 min |

Total wall-clock ≈ 75 minutes. Hard cap on total requests bounds
OpenRouter spend.

### 4.4 Metrics

Client-side (per-request, captured by harness, written to CSV):

- All timestamps from §4.1
- Phase split: `retrieval_ms`, `llm_ttft_ms`, `total_ms`
- Status, disconnect reason, byte count
- Phase tag (A/B/C), client class, prompt id

Server-side (separate observability run during the test):

- `docker stats` snapshot every 5 s → CSV (cpu %, mem, net, threads)
- `pg_stat_activity` snapshot every 5 s (count by state, longest
  active query duration, by query text — separates `documents`
  vector queries from relational ones)
- `pg_stat_statements` reset before run, top-20 by `total_exec_time`
  after — gives the per-query CPU split
- gunicorn access log — already captures req duration; tail to file
  for the window
- OpenRouter dashboard (manual): spend, p95, 429 count
- OpenAI usage dashboard (manual): embedding RPM and TPM during
  window
- Host: `top -b -d 5`, `iostat -x 5`, both writing to file for the
  full window

### 4.5 Safety guardrails

- Run on a low-traffic window. Confirm with operator before launch.
- **Hard cap on total requests** in the harness — `max_total_requests`
  config. Budget < $35 across all phases assuming ~$0.01/`/stream` req.
- **`save_conversation=false`** on every `/stream` request. The
  `conversations` table will be untouched; `user_logs` and
  `token_usage` get ~3000 append-only rows each, which is acceptable
  (matches normal usage shape).
- Pause Celery beat / pause any scheduled ingest jobs for the window.
- Stop-test triggers in the harness (rolling 60 s window):
  - Error rate > 2 % → abort
  - `/stream` P95 total > 25 s → abort
  - `/api/search` P95 > 8 s → abort
  - Postgres `numbackends > 95` (poll every 10 s) → abort
- No new `sources` rows. No agent edits. No schema changes. No DDL.
- **Stop the dev compose** (`docsgpt-oss-*`) for the test window so
  shared-host CPU doesn't pollute the result.

---

## 5. Mitigations — ranked by ROI

To be applied incrementally based on what the test actually shows.
Reordered after Codex review.

1. **Make `/api/search` use the single-embed global-rerank path.**
   The per-source loop in `routes/search.py` is a holdover; the
   global path already exists in `ClassicRAG._get_data`. Wiring
   `/api/search` to use it cuts MCP-traffic embedding load by up
   to 12× and pgvector connection burst by the same factor. Pure
   refactor, no schema or model change. **Likely the single
   biggest win for MCP load.**
2. **Embedding cache for repeat questions** — only worthwhile if
   the test confirms the workload actually has the hot-repeat
   pattern. Keyed by `sha256(normalized_question + sorted(source_ids))`,
   TTL 7 days, stored in Redis db 2 (cache namespace already exists).
   Single-digit lines in `classic_rag.py:_get_data`.
3. **Trim widget response length.** Widget-only `RAG_MAX_DOC_TOKENS`
   override (e.g. 6000) and a tighter prompt asking for shorter
   answers. Cuts upstream LLM wall-clock, frees gunicorn slots faster.
4. **Move pgvector off raw 3072-dim full vectors.** Either
   `halfvec(3072)` (drop-in pgvector type, half the storage,
   IVFFlat works), or 1536-dim Matryoshka truncation of
   `text-embedding-3-large`. Restores index-based retrieval.
   Biggest backend perf win, but requires re-ingest.
5. **Pool the raw `psycopg.connect()` path in `pgvector.py`** —
   either route through the SQLAlchemy engine or add a small
   `psycopg_pool`. Eliminates one connection-pressure axis.
6. **Container mem/CPU caps** so a runaway worker can't OOM the box
   and take the dev compose with it. Cheap, defensive, doesn't
   require waiting for a test result.
7. **Circuit-breaker for OpenRouter** (not blanket retry-with-backoff
   — retries on a streaming endpoint can pin gthread slots *longer*
   under overload, making the situation worse). On 429/5xx, fail
   fast and surface a friendly error rather than retry.
8. **Raise gunicorn workers (cautiously).** `4 × 8` → `5 × 8`, *not*
   `6 × 8` — more workers increase Postgres and provider pressure,
   so this only helps if the test shows slot saturation is the
   primary cause AND DB/provider headroom exists. Trivial change in
   `application/Dockerfile`.
9. **Split vectors onto a dedicated Postgres** — last resort, only
   if #4 + #5 aren't enough.

---

## 6. Open items

### Must resolve **before** the prod run
- [ ] Confirm OpenAI embedding tier RPM/TPM ceiling. If it's the
      free/low tier, MCP phase B will hit it before Postgres does
      and the bottleneck ranking shifts.
- [ ] Stop the dev compose (`docsgpt-oss-*`) for the test window.
- [ ] Confirm OpenRouter spend cap with operator (the $35 budget).
- [ ] Localhost dry-run of the harness at concurrency=2 against the
      *dev* compose to validate stop-triggers, SSE parsing, and CSV
      output — before pointing at prod.
- [ ] `save_conversation=false` confirmed in payload on every
      `/stream` request emitted by the harness.

### Can resolve after
- [ ] Polish prompt-pool diversity beyond the basic sanity pass.
- [ ] Decide whether to count `/api/search` errors against the
      same SLO budget as `/stream` (already split per-endpoint
      in §0, so this is largely resolved).

---

## 7. Deliverables

- `scripts/loadtest/run_stress.py` — the harness
- `scripts/loadtest/prompts.json` — 60-prompt pool
- `scripts/loadtest/results/<timestamp>/` — raw per-request CSV,
  docker stats CSV, pg_stat snapshots, summary markdown
- A short post-mortem comparing the predicted bottleneck order in §2
  against the observed one, plus the next mitigation to ship from §5.

---

## Revisions

**2026-04-27 (post-smoke)** — corrections from prod smoke run:
- §4.1 phase split is **not extractable client-side**. Sources arrive at
  end-of-stream, not after retrieval. Replaced `retrieval_ms`/`llm_ttft_ms`
  with single `ttft_ms = t_headers - t_request_start` (the prod backend's
  SSE producer doesn't flush headers until the first content chunk hits
  the queue, so headers ≈ first content).
- Harness now forces `model_id` per client class:
  `widget → x-ai/grok-4.1-fast`, `discord → z-ai/glm-4.6`. Without this,
  the loadtest agent inherits `LLM_NAME=z-ai/glm-4.6` and all "widget"
  traffic measures Discord-shape latency, blowing the SLO 3-4×.
- `TIMEOUT_SEARCH_S` bumped 30 → 60 s to match the real MCP server's
  client timeout — caught a tail-latency timeout on prompt P15b at
  c=2 that would have been a false signal.
- **Prod baseline at concurrency=2** (smoke, 60 s window):
  - widget P50 TTFT 1.3 s, P95 2.1 s; total P50 6.1 s, P95 7.2 s
  - mcp P50 1.1 s, P95 1.4 s

**2026-04-27** — Codex review pass:
- §1: Split MCP retrieval into its own subsection; the per-source
  loop in `routes/search.py` is a separate code path from the
  ClassicRAG global rerank, with N× the embedding and connection load.
- §1: Added persistence side-effects subsection — `save_conversation`
  flag controls `conversations`, but `user_logs` and `token_usage`
  are unconditional.
- §2: Split bottleneck ranking into `/stream` and `/api/search`
  paths.
- §4.1: Added per-phase timing metrics (`t_first_source_frame` is
  the clean retrieval/LLM boundary).
- §4.3: Replaced single mixed ramp with three phases — `/stream`
  only, `/api/search` only, then mixed soak.
- §4.5: Added `save_conversation=false` requirement; clarified
  unconditional log writes are acceptable.
- §5: Added "make `/api/search` use the global-rerank path" as #1.
  Demoted gunicorn-bump and reframed OpenRouter retry as a
  circuit-breaker, since blanket retries on streaming endpoints
  can amplify overload by pinning gthread slots longer.
- §6: Split open items into "must resolve before run" vs "can wait".
