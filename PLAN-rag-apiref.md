# PLAN: Improve API-ref citation behavior in the Aztec RAG system

**Status:** Proposed plan, not yet implemented.
**Owner:** Josh.
**Trigger:** User feedback on aztec.adjacentpossible.dev:

> "It seems to mostly cite the md docs (which can be vague) even in cases
> where the apiref (in nr files) contain the exact answer. Perhaps we
> should nudge towards apiref a bit more? Also the nr sources confusingly
> show as txt files. The apiref is built exclusively from the nr files,
> so you can just feed those in instead. Maybe just remove the
> non-docstring comments (// instead of ///) and function bodies to make
> it docs only?"

This is two threads:

- **T1 — Citation bias.** Concept `.md` chunks out-rank `.nr` chunks even
  when the `.nr` carries the exact signature/identifier the user asked
  about.
- **T2 — Corpus shape.** `.nr` files are ingested by zipping them with a
  `.txt` suffix and sending the whole implementation through. Bodies
  drown out docstrings; users see "Foo.nr.txt" in cite UI.

## Working hypothesis

The fix is **corpus-shape first, retrieval second, parser-extension
cleanup last.** Today's API-reference content is ingested as noisy
implementation text and ranked by pure cosine over a sequential scan
(3072-dim embeddings exceed pgvector's ivfflat/hnsw cap, so there's no
ANN index — see `application/vectorstore/pgvector.py:71-110`). Prompt
nudges and global re-ranking can't rescue chunks whose embedding signal
is dominated by `let mut hash_bytes = [0 as u8; 224];`.

If we densify the apiref chunks (docstrings + signatures only) and make
them item-sized, the existing global-rerank retriever will surface them
naturally for identifier-shaped queries — no source-type bias needed in
the common case.

## Constraints worth re-stating

- `application/parser/chunking.py` discards every chunk with
  `token_count < 50`. **Short signature-only items will silently
  evaporate** under this rule. Phase 1 must address this.
- 3072-dim → sequential scan. Adding more sources / more chunks has
  linear cost; the apiref change should be a **replacement** of the
  current `aztec-nr` / `noir-stdlib` corpora in the agent's
  `extra_source_ids`, not a new corpus on top.
- 12 corpora is small and fixed. We do **not** need a `source_type`
  column in `sources`; a config map keyed by source UUID is enough.
- Production agent source-list edits via the UI are blocked
  (`VITE_DISABLE_AGENT_EDIT=true`). Source list changes go via SQL;
  document them in `.env`.
- Per-release ingest cadence (one-shot per `v4.2.0` tag) is acceptable.
  No CI sync to HEAD is required, but the transform must be
  deterministic and produce a coverage manifest each run.

---

## Phase 0 — Tighten the eval before touching anything

The current `scripts/eval/golden_queries.json` (15 queries) has no test
that asserts "the apiref was cited first when the user asked about a
specific identifier." We can't measure the win without that.

**Deliverables:**

1. Expand `golden_queries.json` to **20–30 queries** in three buckets,
   tagged in the JSON:
   - `bucket: "identifier"` — "What's the signature of `compute_secret_hash`?",
     "How do I call `poseidon2_hash_with_separator`?",
     "Show me the `BoundedVec::push` method.", etc. Each carries an
     `expected_apiref_path` (e.g. `aztec-nr/aztec/src/hash.nr`).
   - `bucket: "concept"` — "What's the difference between private and
     public state?", "How do notes work?". Each carries
     `expected_concept_prefix` (e.g. `version-v4.2.0/docs/`). These are
     **regression guards** — concept queries must keep citing concept
     docs after the change.
   - `bucket: "example"` — "Show me a token contract.". These keep
     citing `noir-contracts/` and `end-to-end/`.
2. Extend `eval_retrieval.py` with two new assertions:
   - **Retriever mode:** for identifier queries, the expected apiref
     file appears in the **top 3** retrieved chunks.
   - **Stream mode:** for identifier queries, the **first emitted
     citation** is from an apiref-tagged source. (Requires Phase 3's
     source-type config; until then, assert via the file path prefix.)
3. Run baseline. Record pass-rate per bucket. This is the number every
   later phase has to beat.

**Acceptance:** baseline numbers committed to a results JSON in `scripts/eval/`.

---

## Phase 1 — Build a Noir API-reference corpus (the highest-leverage change)

Scope: `aztec-nr/` and `noir-stdlib/` only. **Not** `noir-contracts/`,
**not** `noir-protocol-circuits/`, **not** TypeScript or Solidity.
Examples and circuits exist precisely to show implementation; stripping
them defeats the point.

### 1.1 The transform

Add `scripts/ingest/noir_apiref.py`. Input: a directory tree of `.nr`.
Output: a parallel tree of `.md` files, one per source `.nr`, with the
original relative path preserved. Per file, emit:

- `# <relative source path>` as the file header (so retrieval citations
  still ground in the original `.nr` location).
- For each public item, in source order:
  - An `## <kind> <name>` heading (`fn`, `struct`, `enum`, `trait`,
    `impl`, `pub use`, `pub const`).
  - The `///` / `//!` doc-comment block immediately above it, rendered
    as prose (strip the `///` markers).
  - A fenced ` ```noir ` block containing **just the signature**: the
    item up to and including the return type and `where` clause, with
    the function body replaced by `;`. For structs/enums, the field
    list. For `impl` blocks, the impl header followed by the public
    method signatures inside as nested headings.

Keep:

- `///` and `//!` doc comments
- attribute lines (`#[oracle(...)]`, `#[contract]`, `#[storage]`,
  `#[test]` tagged separately and dropped — see below)
- `pub use` re-exports (these are the canonical import paths users want
  to know)
- module-level `pub const` and type aliases
- public struct fields, public enum variants, public trait method
  signatures, public `impl` method signatures

Drop:

- `//` line comments
- function/method bodies
- private (`fn` without `pub`, struct fields without `pub`, etc.) —
  with one safety net: if a public function calls a private helper that
  is itself documented with `///`, we lose nothing; the public surface
  is what's queryable.
- `#[test]` functions
- blank lines beyond a single separator

### 1.2 Parser strategy

**Recommendation: hand-rolled token/brace-aware extractor, not tree-sitter.**

Codex pushed back on tree-sitter integration unless the Noir grammar is
already in our toolchain. Real-world `.nr` from `aztec-nr` is
attribute-heavy and uses `unconstrained`, traits, generic where-clauses,
and macros. Two appetite-sized choices:

- **Hand-rolled** (preferred): tokenize comments / strings / attributes
  / braces; walk top-level items by brace-depth-zero anchoring; extract
  doc-comment block, attribute block, header line, and either field
  list (for type defs) or signature-up-to-`{` (for fns/impls). Body =
  matched-brace skip. Output to markdown.
- **Tree-sitter**: only if the noir-lang/tree-sitter-noir grammar
  parses real aztec-nr cleanly out of the box. Spike this for ~30
  minutes; if it can't load `noir-projects/aztec-nr/aztec/src/hash.nr`
  without errors, fall back to hand-rolled.

### 1.3 Coverage manifest (mandatory)

The transform must emit `apiref_manifest.json` per run with:

- files seen, files with parse errors (full stack), files with zero
  public items extracted (likely a parser miss)
- per file: count of items extracted by kind (`fn`, `struct`, …)
- total chunk-token estimate

CI / release ritual reviews this manifest. **Silent parser drift is the
biggest risk in this phase**, and a coverage report is the only way to
catch it.

### 1.4 Chunking exemption

Today, `application/parser/chunking.py:76` drops anything `< 50` tokens.
A signature like `pub fn compute_secret_hash(secret: Field) -> Field;`
plus its docstring may land at ~30–60 tokens. **Short apiref chunks
will be silently dropped by the existing filter.**

Add a metadata flag and respect it:

- The transform writes `<!-- apiref -->` as the first line of each
  output `.md`, OR — cleaner — the ingest pipeline tags chunks coming
  from this corpus with `metadata.chunk_type = "apiref"` (see Phase 3).
- `Chunker.classic_chunk` reads `doc.extra_info.get("chunk_type")` and
  skips the `<50` discard for `apiref`.

We must not just lower the global threshold — short noise from other
corpora (CSV cells, half-empty files) is a real reason the discard
exists.

### 1.5 Item-sized chunk shape

`MarkdownParser` splits on headings, which is good — each `## fn name`
section becomes its own retrievable unit. But the existing chunker
still mechanically splits on 2000 tokens. With the apiref transform,
heading-sized sections will almost always be small enough to be
single-chunk; verify in the manifest's chunk-token estimate.

If a single trait has dozens of methods that exceed 2000 tokens,
consider emitting one `.md` per top-level item in those edge cases.
Decide based on the manifest, not in advance.

### 1.6 Output naming

Save outputs as `.md` so the existing `MarkdownParser` ingests them
natively. The internal file path inside the zip can be the **original**
`.nr` path (so `metadata.source` ends with `aztec-nr/aztec/src/hash.nr`)
— we just give the zipped artefact a `.md` extension to satisfy the
parser allowlist. **This drops the `.txt` rename hack for these two
corpora**, which directly addresses the user's "shows as txt"
complaint without broadening the parser allowlist.

The downstream `_aztec_source_url` mapping
(`api/answer/routes/base.py:81-120`) already strips `.txt` before
linking; needs a small tweak so that paths from apiref corpora map back
to the original `.nr` on GitHub.

---

## Phase 2 — Switch the public agent to apiref (replacement, not augment)

Codex was emphatic on this: keeping both raw `aztec-nr` and apiref
`aztec-nr` active by default doubles near-duplicates, increases the
sequential-scan cost, and gives the global rerank an even messier set
to discriminate over. **Replace.**

**Rollout:**

1. Ingest the two new corpora (`Aztec.nr Framework v4.2.0 (apiref)`,
   `Noir stdlib v4.2.0 (apiref)`). Capture their UUIDs.
2. Update the production agent's `source_id` + `extra_source_ids` via
   SQL (the canonical-order block in `.env` is the source of truth —
   update it too):
   - Replace the old `aztec-nr` and `noir-stdlib` UUIDs with the new
     apiref UUIDs, in the same canonical positions.
   - Leave the body-bearing originals in `sources` for internal/debug
     experiments, but **omit them from `AZTEC_SOURCE_IDS`** and from
     the agent's source list.
3. Document in `CLAUDE.md` how to swap them back if the apiref corpus
   regresses against the eval.

**No DB migration.** Keep `source_type` as a config map in
`application/core/settings.py` (UUID → `apiref|concept|example|infra`)
loaded at startup. With 12 sources, the config map is sufficient until
we have a reason for a column.

---

## Phase 3 — Source-type metadata + chunk-header tagging (light touch)

Once the apiref corpora are live, add:

- **Config map.** `SOURCE_TYPES: dict[uuid, Literal["apiref","concept","example","infra"]]`
  in settings, populated from `.env` with a comment block.
- **Chunk header tag.** In `ClassicRAG._pack_into_budget`, prepend a
  short tag to the filename header sent to the LLM:
    `[apiref] aztec-nr/aztec/src/hash.nr`
    `[concept] developers/docs/concepts/state.md`
    `[example] noir-contracts/contracts/token_contract/...`
- **System-prompt amendment.** Add one rule to
  `aztec_4_2_0_grounded.txt`: *"When the user asks about a specific
  identifier, signature, or how to call a function, prefer chunks
  tagged `[apiref]` over `[concept]` or `[example]` for code blocks.
  Concept docs remain primary for high-level questions."*

Codex's caution: **prompt changes are not a retrieval fix.** This phase
exists to help the LLM *attribute* correctly when both apiref and
concept chunks land in context — it doesn't change what's retrieved.

---

## Phase 4 — Retrieval nudge (only if eval still shows gaps)

If after Phases 1–3 the identifier-bucket eval is still under target,
add a **query-class-gated lexical fallback**, not a global source-type
distance bias.

### 4.1 Query classification

Cheap heuristic in `ClassicRAG.__init__`, run on
`self.original_question` (and the rephrased one):

- "identifier-shaped" if the question contains `::`, backticks,
  snake_case (≥ 2 underscores), CamelCase tokens, or matches phrases
  like `"signature of"`, `"how do I call"`, `"trait"`, `"struct"`,
  `"enum"`.
- Otherwise "concept" (default).

### 4.2 Lexical fallback, scoped to apiref

For identifier-shaped queries:

1. Run the existing pgvector ANN scan (unchanged).
2. In parallel, run a `pg_trgm` / `tsvector` query against the apiref
   sources only:
       SELECT id, ts_rank(...) FROM documents
       WHERE source_id = ANY($apiref_ids)
         AND text @@ websearch_to_tsquery('simple', $query)
       ORDER BY ts_rank(...) DESC
       LIMIT 30;
3. Fuse with **reciprocal rank fusion** (k=60). RRF is the boring,
   proven choice; no parameter to tune per-source.
4. Pack the fused list into the token budget as today.

For concept-shaped queries: skip the lexical step entirely. (Codex's
specific call-out: a global concept penalty will hurt these, so
don't apply one.)

### 4.3 Index work

Add a `documents_text_tsv_idx` GIN index on
`to_tsvector('simple', text)` for the documents table. Cheap one-time
cost; pays for itself the first time a user pastes an exact identifier.

### 4.4 What we explicitly are NOT doing here

- **Global per-source-type distance bias** — too easy to mis-rank
  legitimate concept queries. Codex flagged this as a foot-gun.
- **Cross-encoder re-rank** — adds latency, and apiref densification
  + lexical fusion should cover the gap.
- **Switching to a 1024-dim embedding to enable ANN** — separate work,
  separate risk surface. Worth scoping later if scan latency degrades.

---

## Phase 5 — Cleanup, deferred

Defer until we've measured the win from Phases 1–4:

- **Add `.nr`, `.ts`, `.sol` to `SUPPORTED_SOURCE_DOCUMENT_EXTENSIONS`.**
  Tempting because it removes the `.txt` rename hack everywhere, but
  Codex flagged the foot-gun: future ingest runs would silently slurp
  raw code bodies into the corpus. Phase 1 already eliminates the
  `.txt` rename for the corpora that mattered (apiref). The remaining
  `.txt` corpora are example-shaped and the bodies are the point.
- **Idempotent `?replace=true` re-ingest.** Already in `TODO.md`. Becomes
  more relevant once we're re-running the apiref transform per release.

---

## Acceptance criteria (gate for production)

Before swapping `AZTEC_SOURCE_IDS` to point at the apiref corpora on the
production agent:

- **Eval — identifier bucket:** ≥ 90% top-3 retrieval-mode hit rate on
  expected apiref file. ≥ 80% first-citation rate from apiref in
  stream mode.
- **Eval — concept bucket:** zero regressions vs baseline; concept docs
  remain primary citation for concept questions.
- **Eval — example bucket:** zero regressions; `noir-contracts/` /
  `end-to-end/` still cited for "show me a contract" queries.
- **Latency:** retriever-mode p95 within +10% of baseline. Stream-mode
  p95 unchanged.
- **Footprint:** apiref corpora ≥ 30% smaller than the body-bearing
  originals in chunk-token count (manifest reports this).
- **Coverage:** apiref manifest reports zero parser errors and ≥ 95%
  of intended public items extracted on a hand-audited sample of 20
  `.nr` files spanning aztec-nr and noir-stdlib.
- **UX:** widget citations show original `.nr` paths and link to the
  correct GitHub blob (no `.md` or `.txt` artefacts visible).

## Telemetry to add at rollout

Add structured fields to the existing `stream_answer` user-log entry
(`api/answer/routes/base.py:588`):

- `query_class` — `identifier | concept | example | mixed` (from the
  Phase 4.1 classifier; safe to add even if Phase 4 fallback is gated
  off).
- `retrieval.candidate_k`, `retrieval.packed_chunks`,
  `retrieval.packed_tokens`, `retrieval.distinct_sources`.
- `retrieval.first_packed_source_type` (Phase 3 config).
- `retrieval.top10_source_types` (small array).
- `answer.first_cited_source_type` (parsed out of emitted citations).
- `latency_ms.{embed, search, pack, llm}`.
- `lexical_fusion.changed_top3` (bool, Phase 4 only).

The single most important production metric: **% of identifier-shaped
queries whose first cited source is `apiref`.** Watch this on a daily
rollup for 1–2 weeks post-rollout.

---

## Risks (reordered by likelihood × impact)

1. **Parser drift** — the hand-rolled extractor silently misses public
   items in some `.nr` files, holes appear randomly. *Mitigation:*
   coverage manifest is a release blocker; hand-audit 20 files.
2. **`<50` token discard** silently dropping signature-only chunks.
   *Mitigation:* Phase 1.4 — exempt `chunk_type == "apiref"` chunks.
3. **Over-biasing toward apiref** for concept questions. *Mitigation:*
   keep distance scoring untouched in Phase 1–3; in Phase 4, gate
   lexical fallback on query class. Concept-bucket eval guards
   against regression.
4. **Doubled corpora** if we forget to remove the body-bearing
   `aztec-nr` / `noir-stdlib` UUIDs from the agent. *Mitigation:* SQL
   change is part of the same rollout PR; document the canonical order
   in `.env` and PR description.
5. **Embedding cost spike** if ingest accidentally re-embeds both
   versions. *Mitigation:* the apiref corpora are smaller; ensure
   `ingest` tasks reference the new sources by UUID, and confirm
   manifest token estimates match the budget before kicking the worker.
6. **Latency regression** from sequential-scan growth (offset by apiref
   being smaller). *Mitigation:* p95 is part of acceptance.

---

## Recommended execution order (TL;DR)

1. **Phase 0** — expand eval to 20–30 queries with bucket tags,
   identifier assertions, baseline numbers. (~½ day)
2. **Phase 1** — Noir → apiref transform + coverage manifest +
   chunking-filter exemption. (~1–2 days)
3. **Phase 2** — ingest apiref, swap agent source list via SQL,
   document in `.env` + `CLAUDE.md`. (~½ day, mostly waiting)
4. **Re-run Phase 0 eval.** If identifier-bucket targets are met and
   concept/example buckets show no regression, this is the rollout —
   stop here.
5. **Phase 3** — only if we want chunk-header tags + prompt
   attribution help. Cheap; do it. (~½ day)
6. **Phase 4** — only if Phase 0 eval still shows identifier gaps after
   Phases 1–3. Lexical fusion gated on query class.
7. **Phase 5** — defer. Revisit only if it removes a real ongoing
   maintenance cost.

The most important single insight: **fix the corpus shape, not the
ranker.** Body-stripped, item-sized apiref chunks are denser and more
semantically aligned with identifier-shaped questions. The existing
global rerank will surface them naturally, no source-bias hack
required.
