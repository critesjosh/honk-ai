# Citation marker source filter

Filter implementation in [`routes/base.py`](routes/base.py). Both Aztec grounded prompts (`application/prompts/aztec_4_3_0_grounded.txt` and `..._discord.txt`) instruct the LLM to end every answer with a machine-only marker:

```
[[cited: i, j, k]]   # 1-indexed chunk numbers from the {summaries} block
[[cited: none]]      # small talk / out-of-scope / refusal
```

The backend tail-buffers the last 128 chars to strip the marker from both the streamed bytes and `response_full` so it never reaches the client. Chunk headers are built in `stream_processor.pre_fetch_docs` and `workflow_engine._get_source_template_data`.

Structured (JSON-schema) agents bypass the filter entirely.

## End-of-stream source-filter chain

1. **`strategy=marker`** — marker matched with indices → emit only cited chunks.
2. **`strategy=marker_none`** — `[[cited: none]]` / `[[cited: ]]` → emit no source frame.
3. **`strategy=filename_fallback`** — marker absent or malformed → `_filename_fallback` extracts filename tokens from the full response (`` `foo.md` ``, `[link](foo.md)`, `Source: foo.md`, italic/bold, etc.) and matches them against each retrieved doc's `filename`/`title`/basename(`source`) aliases. Path-form tokens (`aztec-nr/index.md`) match by path-suffix; bare basenames (`index.md`) by basename — the two passes run independently on the literal tokens the model emitted, so a path mention doesn't shadow a separate bare-basename mention. Basename collisions ≥ 3 docs flag `ambiguous_basename_count`. Logs `llm.cited_via_filenames`.
4. **`strategy=fail_open`** — otherwise emit the **top `_FAIL_OPEN_MAX_SOURCES` (3)** retrieved docs. fail_open has no grounding signal from the model (no marker AND no filename match), so the source list is "what was retrieved", not "what the answer used" — capping to the top few avoids overstating confidence. The pre-cap retrieval count is preserved in the audit as `available_count`. Both fail_open branches share this cap via `_fail_open_sources`, but log differently: the no-marker branch logs `llm.cited_missing`, the malformed-marker branch logs `llm.cited_malformed`.

## Inline-marker scrubber

`_scrub_inline_citation_markers` is defense in depth against models that emit `[[cited: N]]` per paragraph instead of (or in addition to) the single trailing marker.

The trailing-marker regex `_CITATION_MARKER_RE` is `\Z`-anchored and catches at most one marker. The unanchored `_INLINE_CITATION_RE` strips every span from:

- streamed bytes (`_emit_answer_delta`, `_flush_pending_tail`)
- `response_full` before persistence
- the GeneratorExit save path (client disconnect mid-stream)

Surrounding whitespace is deliberately preserved (extra space is harmless, fused words are not). A delta-boundary check in `_emit_answer_delta` holds back partial `[[…` prefixes so a marker straddling two deltas doesn't leak its prefix; the holdback is bounded to `_PARTIAL_MARKER_HOLDBACK_LEN` (64 chars) so an unmatched `[[` in legitimate prose / code-block discussion earlier in the answer doesn't pin the buffer split forever.

Why this exists: 2026-05-22 — one qwen3.6-flash answer emitted 283 inline marker spans, every one of which leaked because the `\Z`-anchored parser only saw the last one.

## Audit metadata

Lands at `conversation_messages.message_metadata.citation_filter`:

| Field | When | Notes |
|---|---|---|
| `strategy` | always | `marker` / `marker_none` / `filename_fallback` / `fail_open` |
| `marker_present` | always | `true` iff the trailing-marker regex matched |
| `marker_malformed` | conditional | `true` when payload didn't parse (e.g. `[[cited: banana]]`) |
| `cited_indices` | always | 1-indexed source positions the LLM cited |
| `invalid_indices` | always | Indices outside `[1, source_count]` (dropped, logged) |
| `filtered_count` | always | Count of docs in the filtered list this strategy produced, BEFORE the `_MAX_SOURCES_EMITTED` (10) render cap. For `marker` this is the cited subset; for `fail_open` it's already capped to `_FAIL_OPEN_MAX_SOURCES` (3). NOT necessarily what the user sees — the SSE frame applies the 10-cap on top. |
| `available_count` | `fail_open` only | Pre-cap retrieval count (how many docs were available before the fail_open top-3 cap). Lets audit distinguish "model grounded nothing out of 3" from "...out of 78". |
| `matched_aliases` | `filename_fallback` only | Filename tokens that matched a retrieved doc |
| `ambiguous_basename_count` | `filename_fallback` only | Basename collisions ≥ 3 docs |
| `inline_markers_scrubbed` | scrubber removed any | Count of inline `[[cited: …]]` spans removed |

## Observability + regression guard

- `llm.marker_leak_scrubbed agent_id=… count=N strategy=… response_len=…` WARN log fires whenever `inline_markers_scrubbed > 0`. On the GeneratorExit (aborted) path, `strategy=aborted` is used. Grep production logs to surface prompt/model drift.
- `scripts/eval/eval_retrieval.py --mode stream` hard-fails any answer matching `_MARKER_LEAK_RE` (`[[\s*cited\s*:[^\]\n]*\]\]`, case-insensitive — mirrors the backend's `_INLINE_CITATION_RE`). Flag: `marker-leak!`.
- Surface a leak rate per surface in SQL:
  ```sql
  SELECT a.surface,
         COUNT(*) AS total,
         COUNT(*) FILTER (
           WHERE jsonb_array_length(
             jsonb_path_query_array(
               COALESCE(cm.message_metadata->'citation_filter', '{}'::jsonb),
               '$.inline_markers_scrubbed'
             )
           ) > 0
         ) AS leaked
  FROM conversation_messages cm
  JOIN conversations c ON c.id = cm.conversation_id
  JOIN agents a ON a.id = c.agent_id
  WHERE cm.timestamp >= NOW() - INTERVAL '7 days'
  GROUP BY a.surface;
  ```

## Tests

`tests/api/answer/test_citation_marker.py` — 82 cases covering the parser, the inline scrubber, all four end-of-stream strategies, the `fail_open` top-`_FAIL_OPEN_MAX_SOURCES` cap (`_fail_open_sources`, both branches) + `available_count` audit, audit-metadata shape, delta-boundary straddle, unmatched-`[[` no-stall, and the GeneratorExit observability.
