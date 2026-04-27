# Aztec DocsGPT eval harness

Regression suite for the RAG retrieval and answer pipeline. Run before merging
any change that touches `application/retriever/`, `application/vectorstore/`,
the system prompt (`application/prompts/aztec_4_2_0_grounded.txt`), or the
`AZTEC_SOURCE_IDS` corpus configuration.

## Layout

- `eval_retrieval.py` — runner. Two modes: `retriever` (direct probe of
  `ClassicRAG._get_data()`) and `stream` (full SSE roundtrip via `/stream`).
- `golden_queries.json` — 15 hand-written queries that exercise each indexed
  corpus plus a multi-turn follow-up that exercises the rephrase path.

## Running

Both modes need access to the backend's Python environment (settings,
SQLAlchemy session, vectorstore). The simplest way is to exec into a running
backend container.

### Retriever mode (no `/stream`, no LLM call)

Asserts that retrieval pulls chunks from the expected source prefixes and
hits a minimum source-diversity floor. Cheap (no model spend) — run this on
every retrieval-path change.

```bash
docker compose -f deployment/docker-compose-hub.yaml exec backend \
    python scripts/eval/eval_retrieval.py --mode retriever
```

### Stream mode (full pipeline)

Hits `POST /stream` with a real agent API key, parses the SSE stream, and
asserts:

- No banned identifiers in the answer (catches model fabrication of
  function/type names that don't exist in the corpus).
- No Markdown tables (Discord format rule — pipe-separated tables don't
  render in Discord).
- Source diversity ≥ `min_distinct_sources` per query.
- Wall time ≤ `max_response_time_s` (default 15s).
- Non-empty answer.

```bash
docker compose -f deployment/docker-compose-hub.yaml exec backend \
    python scripts/eval/eval_retrieval.py \
        --mode stream \
        --api-key <agent_key> \
        --base-url http://backend:7091
```

The `--api-key` is an agent API key (the kind `/mcp-key` issues), **not** a
JWT. From outside the container, point `--base-url` at
`https://aztec.adjacentpossible.dev` (Cloudflare Access bypass required for
`/stream`) or at the host-published dev compose port.

### Structured output

```bash
python scripts/eval/eval_retrieval.py --mode stream --api-key … \
    --json-out /tmp/eval-$(date +%Y%m%d-%H%M).json
```

Writes per-query results (pass/fail flags, elapsed time, source tallies,
banned-identifier hits) for diffing across runs. Exit code is 0 only if every
query passes.

## Adding a query

Each entry in `golden_queries.json` is:

```json
{
  "tag": "noir-stdlib-poseidon2",
  "query": "How do I use std::hash::poseidon2 in Noir?",
  "expected_source_prefixes": ["noir-stdlib/"],
  "min_distinct_sources": 1,
  "banned_identifiers": ["poseidon::hash_internal"],
  "max_response_time_s": 15
}
```

Field meanings:

| Field | Mode | Purpose |
|---|---|---|
| `tag` | both | Short identifier shown in PASS/FAIL output. |
| `query` | both | The user question. |
| `history` | stream | Optional conversation history (list of `[user, assistant]` pairs) for multi-turn tests. |
| `expected_source_prefixes` | retriever | Path prefixes that must appear in retrieved docs. Buckets are normalized to one of `noir-docs/`, `noir-stdlib/`, `typescript-api/`, `aztec-nr/`, `aztec.js/`, `cli/`, `cli-wallet/`, `end-to-end/`, `l1-contracts/`, `noir-contracts/`, `noir-protocol-circuits/`, `version-v4.2.0/`. |
| `min_distinct_sources` | both | Floor on bucket count. Cross-source queries set this ≥ 2. |
| `banned_identifiers` | stream | Substrings that must NOT appear in the answer. Use to lock down hallucinated APIs (e.g. an old method name that was renamed in v4.2.0). |
| `max_response_time_s` | stream | Wall-time SLA. Defaults to 15. |

Pick a `tag` that names the corpus + concept (`aztec-nr-private-storage`,
`l1-contracts-rollup`). One query per indexed corpus is the floor; add more
when a real-world failure surfaces a gap.

## When a query fails

- **`missing: [<prefix>]`** (retriever mode) — the global rerank didn't
  surface any chunk from that corpus. First check it's actually indexed
  (`SELECT id, name FROM sources;`), then check whether the query terms
  match the corpus's token distribution. If a corpus is genuinely the right
  home for an answer but isn't being pulled, the embedding distance is
  probably losing to lexically-similar chunks elsewhere — consider
  rephrasing the test query, not lowering the bar.
- **`banned: [<id>]`** (stream mode) — the model fabricated an identifier.
  Either tighten the system prompt (`aztec_4_2_0_grounded.txt`) or, if the
  banned name is genuinely valid in some context but wrong here, narrow the
  query's banned list.
- **`slow:<n>s`** (stream mode) — answer exceeded the time budget. Check
  `RAG_MAX_DOC_TOKENS` (10k in prod) and the model latency curve — Grok and
  GLM are sub-15s on this budget; reasoning-mode models are not.
- **`low-diversity`** (stream mode) — fewer distinct sources cited than
  required. Often a sign of the FIFO starvation regression returning;
  re-run retriever mode to localize.
