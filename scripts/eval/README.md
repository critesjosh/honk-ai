# Aztec DocsGPT eval harness

Regression suite for the RAG retrieval and answer pipeline. Run before merging
any change that touches `application/retriever/`, `application/vectorstore/`,
the system prompt (`application/prompts/aztec_4_3_0_grounded.txt`), or the
`AZTEC_SOURCE_IDS` corpus configuration.

Two workflows live here:

1. **Regression suite** (`eval_retrieval.py` alone) — pass/fail on the 25
   golden queries. Run on the current prod agent before merging.
2. **Variant comparison** (`provision_test_agent.py` → `eval_retrieval.py
   --capture-answers` → `compare.py`) — see how an experimental prompt,
   source list, or model differs from the live baseline before shipping
   it. Manual trigger; not part of CI.

## Layout

- `eval_retrieval.py` — runner. Two modes: `retriever` (direct probe of
  `ClassicRAG._get_data()`) and `stream` (full SSE roundtrip via `/stream`).
  `--capture-answers` records the full answer + cited URLs into the JSON
  output so `compare.py` can diff two runs.
- `golden_queries.json` — 25 hand-written queries (each tagged `bucket` of
  `identifier` / `concept` / `example`) that exercise each indexed corpus
  plus a multi-turn follow-up that exercises the rephrase path. Filter
  with `--bucket {identifier,concept,example,all}`.
- `provision_test_agent.py` — upserts a throwaway agent + prompt row under
  `user_id = 'eval-variant'` with `surface = 'eval'` (the
  `agents.surface` taxonomy keeps these out of per-surface prod
  analytics — see `CLAUDE.md` for the column). Lets you point the
  harness at a candidate prompt / source list / model without editing
  the live agent.
- `compare.py` — pure-stdlib host script. Reads two snapshot JSONs and
  emits a Markdown diff (pass/fail flips, latency, cited URLs, unified
  diff of the answer text).

## Running

Both modes need access to the backend's Python environment (settings,
SQLAlchemy session, vectorstore). The `application/` image does NOT include
the repo `scripts/` directory — bind-mount it via `docker compose run` (the
same pattern `init_postgres.py` uses; see the project-level CLAUDE.md). A
plain `docker compose exec backend python scripts/eval/...` will fail with
`No such file or directory` because scripts aren't in the image.

### Retriever mode (no `/stream`, no LLM call)

Asserts that retrieval pulls chunks from the expected source prefixes and
hits a minimum source-diversity floor. Cheap (no model spend) — run this on
every retrieval-path change.

```bash
docker compose -f deployment/docker-compose-hub.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/eval_retrieval.py --mode retriever
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
docker compose -f deployment/docker-compose-hub.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/eval_retrieval.py \
        --mode stream \
        --api-key <agent_key> \
        --base-url http://backend:7091
```

The `--api-key` is an agent API key (the kind `/mcp-key` issues), **not** a
JWT. `http://backend:7091` resolves over the compose-internal network from
the one-off `run --rm` container. From the host, point `--base-url` at the
dev compose's published port (`http://localhost:7091`) or at
`https://aztec.adjacentpossible.dev` (Cloudflare Access bypass required for
`/stream`).

### Structured output

```bash
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/eval_retrieval.py \
        --mode stream --api-key … \
        --json-out /tmp/eval-$(date +%Y%m%d-%H%M).json \
        --label baseline
```

Writes a snapshot envelope (`{label, mode, ran_at, summary, results}`) with
per-query results (pass/fail flags, elapsed time, source tallies,
banned-identifier hits) for diffing across runs. Exit code is 0 only if every
query passes.

Add `--capture-answers` (stream mode only) to also embed the full answer text
and ordered cited URLs in each result. This is what `compare.py` consumes —
without it, the diff report can only show pass/fail and latency, not answer
text or citation deltas.

## Variant comparison workflow

Goal: see how a change to the system prompt, source list, agent settings,
or LLM model affects answers before pushing it to a prod agent.

The shape is **baseline ↔ candidate**. Take a snapshot of each, diff
them with `compare.py`.

### 1. Provision a candidate agent in the dev compose

Bring up dev Postgres if it isn't already running, then upsert a
throwaway agent row under `user_id = 'eval-variant'`:

```bash
docker compose -f deployment/docker-compose.yaml up -d postgres backend
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/provision_test_agent.py \
        --name variant-new-prompt \
        --prompt-file /app/application/prompts/aztec_4_3_0_grounded.txt \
        --source-ids-from-env \
        --model qwen/qwen3.6-flash
```

The provisioner prints a single JSON line on stdout — capture the key
(use `run -T` to disable TTY allocation so `jq` can parse the pipe):

```bash
CANDIDATE_KEY=$(docker compose -f deployment/docker-compose.yaml run --rm -T \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/provision_test_agent.py \
        --name variant-new-prompt \
        --prompt-file /app/application/prompts/aztec_4_3_0_grounded.txt \
        --source-ids-from-env \
        | jq -r .key)
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--name` | Variant name. Used as the `agents.name` and `prompts.name`. Re-running with the same name updates the existing rows in place (so you can iterate on a prompt file and re-provision). |
| `--prompt-file PATH` | Reads a system prompt from disk. Mount your scripts dir (`-v $(pwd)/scripts:/app/scripts:ro`) and point at e.g. `/app/scripts/eval/variants/experimental.txt`. |
| `--prompt-id UUID` | Reuse an existing `prompts.id` instead of inserting a new row. Useful when comparing two source lists against the same prompt. |
| `--source-ids UUID,UUID,…` | Custom source list. The first id is the primary, rest become `extra_source_ids`. |
| `--source-ids-from-env` | Use the same list (`AZTEC_SOURCE_IDS`) the prod agents use — good when you're varying prompt/model only. |
| `--model` | LLM id (e.g. `qwen/qwen3.6-flash`, `x-ai/grok-4.1-fast`). NULL → falls back to `LLM_NAME` at request time. |
| `--key` | Set the bearer key explicitly. Default: 64 random hex chars. The key is preserved on re-runs (rotation requires `--delete` then re-provision). |
| `--list` | Print every `eval-variant` agent + its key + sources as JSON. |
| `--delete --name X` | Remove the agent + prompt rows for variant `X`. |
| `--delete-all` | Wipe every `eval-variant` row. Requires `EVAL_VARIANT_CONFIRM=yes` in the env. |

### 2. Capture a baseline snapshot

Snapshot the **current prod agent** so the diff has a stable reference.
Run from a host that can reach the prod base URL (or `localhost:7091`
if you're testing against the dev compose). Write the JSON envelope to
a host-readable path under `application/inputs/` so it survives the
ephemeral container (that path is the only host-mounted dir on the
backend service):

```bash
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/eval_retrieval.py \
        --mode stream \
        --api-key "$BASELINE_KEY" \
        --base-url http://backend:7091 \
        --capture-answers \
        --label baseline \
        --json-out /app/application/inputs/eval-baseline.json
# Host-side path: application/inputs/eval-baseline.json
```

### 3. Capture a candidate snapshot

Same harness, pointed at the candidate's bearer:

```bash
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/eval_retrieval.py \
        --mode stream \
        --api-key "$CANDIDATE_KEY" \
        --base-url http://backend:7091 \
        --capture-answers \
        --label variant-new-prompt \
        --json-out /app/application/inputs/eval-candidate.json
```

### 4. Diff the snapshots

Pure-stdlib host script, no Docker required. The snapshots written above
land at `application/inputs/eval-{baseline,candidate}.json` on the host:

```bash
python scripts/eval/compare.py \
    application/inputs/eval-baseline.json \
    application/inputs/eval-candidate.json \
    --out /tmp/report.md
```

The report contains:

- Summary table with per-bucket pass/total and the delta.
- Status-change lists (regressed / fixed / still failing / added / removed).
- Per-query table with latency, answer-length, and citation-count deltas.
- A detailed section (default: flips only) with the query, per-side flag
  list, citation set diff, and a unified diff of the answer text.

Flags:

| Flag | Purpose |
|---|---|
| `--only flips` | (default) Detail only queries that flipped pass↔fail. |
| `--only changed` | Detail every query whose answer text differs. Useful when checking that "no answer changed" — empty section means clean run. |
| `--only all` | Detail every query. Verbose; mostly for one-shot full-corpus reviews. |
| `--only none` | Summary tables only. |
| `--max-answer-diff-chars N` | Truncate each side of the unified diff to N chars (default 4000). `0` disables truncation. |

### 5. Clean up

When you're done iterating on a variant:

```bash
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/provision_test_agent.py \
        --delete --name variant-new-prompt
```

`--delete --name X` removes the agent first, then deletes the prompt
only if no other agent still references it — so a prompt reused across
variants (the `--prompt-id` recipes below) is preserved until the last
referring agent is removed.

Or nuke every eval-variant row at once:

```bash
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    -e EVAL_VARIANT_CONFIRM=yes \
    backend python /app/scripts/eval/provision_test_agent.py --delete-all
```

## Recipes by what you're changing

The workflow above (provision → baseline → candidate → diff) is the
same in every case; only the provisioner flags differ. The recipes
below show which knobs to turn for each kind of change.

### A. System prompt change

Edit a copy of the live prompt, save it next to `scripts/eval/`, and
provision with `--prompt-file` pointing at it. Keep
`--source-ids-from-env` so the source list matches prod — that
isolates the prompt as the only variable.

```bash
cp application/prompts/aztec_4_3_0_grounded.txt \
    scripts/eval/variants/grounded_terser.txt
# edit scripts/eval/variants/grounded_terser.txt
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/provision_test_agent.py \
        --name variant-terser-prompt \
        --prompt-file /app/scripts/eval/variants/grounded_terser.txt \
        --source-ids-from-env
```

### B. Restrict / reorder the sources an agent sees

Pass `--source-ids` with the exact subset (and order) you want.
The first id becomes the primary `source_id`, the rest become
`extra_source_ids`. Keep `--prompt-id` pointing at the live prod
prompt row so the prompt is held constant.

Find the candidate source UUIDs first:

```bash
docker compose -f deployment/docker-compose.yaml exec postgres \
    psql -U docsgpt -d docsgpt -c \
    "SELECT id, name FROM sources ORDER BY name;"
```

(`docker compose exec postgres` is fine here — `psql` ships in the
postgres image; only the backend image is missing the repo `scripts/`
directory.)

Then provision the variant with a custom subset — e.g. drop the L1
contracts and TS API corpora to see whether their presence is
helping or hurting answer quality:

```bash
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/provision_test_agent.py \
        --name variant-no-l1-no-ts \
        --prompt-id 0780959b-3c18-4ad9-8284-691665233a6f \
        --source-ids "<dev-docs-uuid>,<network-docs-uuid>,<aztec-nr-uuid>,<noir-stdlib-uuid>,<noir-docs-uuid>,<examples-uuid>,<circuits-uuid>,<aztec.js-uuid>,<cli-uuid>,<e2e-uuid>"
```

This is also how you A/B-test reordering — same UUIDs, different
order. The primary `source_id` is what the global-rerank fallback
favors on ties, so promoting a corpus to position 0 is observable
in citation diversity.

### C. Evaluate a brand-new knowledge source before pointing prod at it

Two steps: ingest the new corpus into the dev compose so it gets a
fresh `sources.id`, then provision a variant agent that includes
that id alongside the live source list.

1. **Ingest the new source.** See `scripts/ingest/README.md` for the
   full workflow. For a one-off new corpus the short form is:

   ```bash
   docker compose -f deployment/docker-compose.yaml run --rm \
       -v "$(pwd)/scripts:/app/scripts:ro" -e PYTHONPATH=/app \
       backend python -m scripts.ingest.upload \
           --build-dir /tmp/new-corpus \
           --base-url http://backend:7091 \
           --token "$INTERNAL_KEY"
   # Captures the new sources.id in upload_manifest.json.
   ```

2. **Provision a variant agent that includes it.** Take the live
   `AZTEC_SOURCE_IDS` list and append (or insert) the new id. Easiest
   path: copy the env list and add the new UUID at the end.

   ```bash
   LIVE_SOURCES=$(grep '^AZTEC_SOURCE_IDS=' .env | cut -d= -f2-)
   NEW_SOURCE_ID="<uuid-from-step-1>"
   docker compose -f deployment/docker-compose.yaml run --rm \
       -v "$(pwd)/scripts:/app/scripts:ro" \
       -e PYTHONPATH=/app \
       backend python /app/scripts/eval/provision_test_agent.py \
           --name variant-add-new-corpus \
           --prompt-id 0780959b-3c18-4ad9-8284-691665233a6f \
           --source-ids "${LIVE_SOURCES},${NEW_SOURCE_ID}"
   ```

   Run baseline + candidate as before. The diff will surface how
   often the new corpus actually wins a citation slot, whether it
   regresses identifier queries (by displacing a stronger apiref
   hit), and where it changes answer text.

3. **If the candidate looks good**, ingest the same corpus against
   prod and run the canonical swap via `scripts/ingest/swap_sources.py`
   — that's the path that actually edits the live agent's source list.
   `provision_test_agent.py` only touches `eval-variant` rows.

### D. Re-chunked / re-embedded version of an existing source

Same shape as recipe C, but the ingest step uploads the same content
with different chunker settings or a different embedding model.
That writes a *new* `sources.id` distinct from the prod row. A/B
the two UUIDs (old vs. new) by passing each to a different
`--source-ids` invocation; the prod source stays untouched until
you decide to swap.

### E. Different LLM model

Provision with `--model <id>`; everything else (sources, prompt)
stays at prod defaults. Live models are listed in the `application/
llm/open_router.py` reasoning-disable allowlist and in the model
notes in `CLAUDE.md`.

```bash
docker compose -f deployment/docker-compose.yaml run --rm \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e PYTHONPATH=/app \
    backend python /app/scripts/eval/provision_test_agent.py \
        --name variant-grok \
        --prompt-id 0780959b-3c18-4ad9-8284-691665233a6f \
        --source-ids-from-env \
        --model x-ai/grok-4.1-fast
```

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
| `expected_source_prefixes` | retriever | Path prefixes that must appear in retrieved docs. Buckets are normalized to one of `noir-docs/`, `noir-stdlib/`, `typescript-api/`, `aztec-nr/`, `aztec.js/`, `cli/`, `cli-wallet/`, `end-to-end/`, `l1-contracts/`, `noir-contracts/`, `noir-protocol-circuits/`, `version-v4.3.0/`. |
| `min_distinct_sources` | both | Floor on bucket count. Cross-source queries set this ≥ 2. |
| `banned_identifiers` | stream | Substrings that must NOT appear in the answer. Use to lock down hallucinated APIs (e.g. an old method name that was renamed in v4.3.0). |
| `max_response_time_s` | stream | Wall-time SLA. Defaults to 15. |
| `expected_first_prefixes` | stream | Identifier-bucket only. Path prefixes — the first cited source's rewritten URL must contain one of them. Defaults to `("aztec-nr/", "noir-stdlib/")` (`APIREF_PREFIXES`). Override for queries whose canonical apiref isn't a `.nr` file, e.g. TypeScript-API queries set `["typescript-api/", "aztec.js/"]`. Ignored if `expected_apiref_paths` is also set. |

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
  Either tighten the system prompt (`aztec_4_3_0_grounded.txt`) or, if the
  banned name is genuinely valid in some context but wrong here, narrow the
  query's banned list.
- **`slow:<n>s`** (stream mode) — answer exceeded the time budget. Check
  `RAG_MAX_DOC_TOKENS` (10k in prod) and the model latency curve — Qwen
  3.6 Flash (current Discord and /ask default, also `LLM_NAME` fallback)
  and Grok 4.1 Fast (current widget default) are both sub-15s on this
  budget; models that reason by default and aren't in
  `_REASONING_DISABLED_MODEL_PREFIXES` (e.g. step-3.5-flash unflagged)
  are not.
- **`low-diversity`** (stream mode) — fewer distinct sources cited than
  required. Often a sign of the FIFO starvation regression returning;
  re-run retriever mode to localize.

## Citation marker note

The Aztec grounded prompts now require the model to end every answer
with a machine-only `[[cited: i, j, k]]` (or `[[cited: none]]`) marker
referencing the 1-indexed chunk numbers in `{summaries}`. The backend
strips that marker before SSE delivery, and the eval harness sees
already-stripped answer text — so existing assertions (banned
identifiers, apiref-in-top-3, etc.) keep working unchanged. What changes
is the **source frame**: the backend filters it to only the chunks the
LLM actually cited.

- For real doc queries (the existing buckets), the source frame should
  remain non-empty. A regression that produces `[[cited: none]]` on a
  question the prompt should have answered surfaces as a `low-diversity`
  / `missing` style failure depending on the assertion.
- For chitchat / out-of-scope queries (no goldens for this in the
  current corpus), the source frame is expected to be **absent** —
  Honk AI and the widget both render nothing for absent sources, which
  is the user-visible win this filter exists for.

Adding a small set of negative chitchat goldens (e.g. "are you there?",
"thanks!") tagged `chitchat` is a future-work item — useful once we want
to gate prompt changes on "no sources emitted for chitchat".
