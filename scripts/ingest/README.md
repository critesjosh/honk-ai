# Aztec corpus ingest toolkit

This directory holds the tooling for (re-)ingesting the 12 corpora that
make up the Aztec DocsGPT knowledge base. It exists so that bumping to
a new aztec-packages release (e.g. `v4.2.0` → `v4.3.0`) is a small
number of commands instead of a folkloric afternoon of `zip` calls and
SQL guesses.

## Files

| File | Purpose |
|---|---|
| `corpora.py` | Canonical definition of all 12 corpora (paths, extensions, transform). Single source of truth — edit here when paths change. |
| `noir_apiref.py` | Transforms `.nr` source into a Markdown API-reference view (signatures + doc comments only). Used by the `noir_apiref` transform. |
| `build.py` | CLI: builds upload-ready zips from local checkouts of `aztec-packages` and `noir`. Writes per-corpus + overall manifests. |
| `upload.py` | CLI: POSTs the zips to `/api/upload`, polls the Celery task, captures the resulting `sources.id` UUIDs. |
| `swap_sources.py` | Generates the SQL needed to point an agent at the new corpora, plus the `.env` `AZTEC_SOURCE_IDS` block. **Does not execute SQL** — you do that yourself with `psql`, after reviewing. |

## What the corpora are

12 corpora total, all built from two upstream git repos pinned at
specific revisions per Aztec release:

  * `aztec-packages` at the release tag (`v4.2.0` etc.)
  * `noir-lang/noir` at the commit pinned by aztec-packages' `noir/`
    submodule (run `git -C aztec-packages submodule status noir` to
    find this commit). At v4.2.0 it's `842974fcf...`.

Run `python -m scripts.ingest.list` (TODO) or read `corpora.py` for the
full table. The most important distinction is between:

  * **Markdown / docs corpora** — `passthrough` transform, ingested
    as-is. (Developer Docs, Network Docs, TypeScript API, Noir Docs.)
  * **Apiref corpora** — `noir_apiref` transform, body-stripped
    Markdown view of public symbols. (`aztec-nr`, `noir-stdlib`.)
  * **Body-bearing code corpora** — `rename_code_to_txt` transform,
    raw source with `.txt` appended so the parser allowlist accepts
    them. (Examples, circuits, aztec.js, CLI, e2e tests, L1
    contracts.) Bodies stay in for these because *the example/test
    body IS the answer* the user wants.

### Per-corpus path exclusions

`SourceTree.exclude_paths` is a tuple of fnmatch patterns evaluated
against each file's path relative to the tree's source directory.
Patterns may target individual files (`docs/resources/migration_notes.*`)
or whole directories (`operators/reference/changelog/*`). Use it to
surgically drop transitional content like migration notes / release
changelogs — they mention every renamed identifier in both old and
new spellings, so they embed well for identifier queries but are
never the canonical answer.

Currently active exclusions:

  * **Aztec Developer Docs** — `docs/resources/migration_notes.*`
    (was 285 of ~1000 chunks at v4.2.0)
  * **Aztec Network Docs** — `operators/reference/changelog/*` and
    `reference/changelog/*` (~60 chunks of release notes)

## End-to-end version bump (≈ 1 hour, mostly waiting on embeds)

Outline; details below.

```bash
# 1. Get clean checkouts of both source trees at the new pin.
git -C ../aztec-packages worktree add --detach /tmp/aztec-vNEW vNEW
NOIR_PIN=$(git -C ../aztec-packages submodule status noir | awk '{print $1}' | tr -d -)
git clone https://github.com/noir-lang/noir /tmp/noir-vNEW
git -C /tmp/noir-vNEW checkout "$NOIR_PIN"

# 2. Build all 12 zips. Idempotent; rerunnable.
python -m scripts.ingest.build \
    --aztec-pkg /tmp/aztec-vNEW \
    --noir      /tmp/noir-vNEW \
    --out       /tmp/aztec-corpora-build

# 3. Review the build manifests — especially the apiref ones.
cat /tmp/aztec-corpora-build/manifests/aztec_nr_apiref.json | jq
# Hand-audit ~20 of the .md files (`vimdiff` against the .nr originals
# to make sure no public symbol was silently dropped).

# 4. Upload each zip and capture the source UUIDs. This kicks off
#    Celery embeddings — costs scale with new chunk count.
python -m scripts.ingest.upload \
    --build-dir /tmp/aztec-corpora-build \
    --base-url  http://localhost:7091 \
    --user      local \
    --token     "$INTERNAL_KEY" \
    --out       /tmp/aztec-corpora-build/upload_manifest.json

# 5. Generate the SQL to swap the production agent's source list.
#    REVIEW BEFORE EXECUTING. Then run via psql.
python -m scripts.ingest.swap_sources \
    --upload-manifest /tmp/aztec-corpora-build/upload_manifest.json \
    --agent-id $PROD_AGENT_ID \
    --out     /tmp/swap.sql
psql "$POSTGRES_URI" -f /tmp/swap.sql

# 6. Update AZTEC_SOURCE_IDS in .env (the swap_sources output prints
#    the canonical-order block to copy). Then:
docker compose -f deployment/docker-compose-hub.yaml --env-file .env \
    up -d --force-recreate backend worker

# 7. Run the eval to confirm no regressions.
docker compose -f deployment/docker-compose-hub.yaml exec backend \
    python scripts/eval/eval_retrieval.py --mode retriever
docker compose -f deployment/docker-compose-hub.yaml exec backend \
    python scripts/eval/eval_retrieval.py --mode stream \
        --api-key "$PROD_AGENT_KEY"

# 8. (Optional cleanup) DELETE old `sources` rows from the previous
#    version once you're confident the new ones work, so the documents
#    table doesn't grow unboundedly. The old corpora are still
#    addressable by UUID if you need to roll back.
```

## Apiref-only swap (smaller blast radius)

If you only want to swap the apiref corpora (e.g. iterating on the
`noir_apiref.py` transform without redoing every other corpus):

```bash
python -m scripts.ingest.build \
    --aztec-pkg /tmp/aztec-v4.2.0 \
    --noir      /tmp/noir-v4.2.0 \
    --out       /tmp/aztec-corpora-build \
    --corpus    aztec_nr_apiref \
    --corpus    noir_stdlib_apiref

python -m scripts.ingest.upload \
    --build-dir /tmp/aztec-corpora-build \
    --base-url  http://localhost:7091 \
    --user      local \
    --token     "$INTERNAL_KEY" \
    --out       /tmp/aztec-corpora-build/upload_manifest.json \
    --corpus    aztec_nr_apiref \
    --corpus    noir_stdlib_apiref

python -m scripts.ingest.swap_sources \
    --upload-manifest /tmp/aztec-corpora-build/upload_manifest.json \
    --agent-id $PROD_AGENT_ID \
    --apiref-only \
    --out     /tmp/apiref-swap.sql
```

`--apiref-only` produces commented SQL that documents which UUIDs need
to replace which OLD ones — the user has to wire them in by hand
because it can't know your current `extra_source_ids` array contents.
This is intentional: for an in-place rotation you should look at the
existing array first.

## Manifest formats

### Build manifest (`build_manifest.json`)

```json
{
  "out": "/tmp/aztec-corpora-build",
  "corpora": [
    {
      "corpus": { "name": ..., "slug": ..., "rel_prefix": ..., ... },
      "source_dirs": ["/tmp/aztec-v4.2.0/noir-projects/aztec-nr"],
      "zip_path": "/tmp/.../zips/aztec_nr_apiref.zip",
      "zip_files": 223,
      "transform_stats": {
        "files_seen": 223,
        "files_emitted": 223,
        "files_with_parse_errors": 0,
        "files_with_zero_items": 29,
        "totals_by_kind": { "fn": 683, "struct": 84, "trait": 9, ... }
      }
    },
    ...
  ]
}
```

### Upload manifest (`upload_manifest.json`)

```json
[
  {
    "slug": "aztec_nr_apiref",
    "name": "Aztec.nr Framework v4.2.0 (apiref)",
    "task_id": "...",
    "status": "SUCCESS",
    "source_id": "8c9d...",
    "rel_prefix": "aztec-nr/"
  },
  ...
]
```

The upload manifest is the input to `swap_sources.py`.

## When the apiref transform regresses

The apiref output is the result of a hand-rolled Noir parser. If a
real-world `.nr` file uses a syntax the parser doesn't understand,
items can silently disappear from the corpus.

Defenses, in order of cheapness:

1. **Parse-error and zero-item counts** in the build manifest. If the
   zero-item count moves dramatically (excluding `*/test*.nr`, which
   correctly contain only `#[test]` items and SHOULD be empty), that's
   a parser regression.
2. **Hand-audit 20 random files** per release. Pick files spanning
   `aztec/src/`, `state_vars/`, `note/`, `oracle/`, `messages/`. Diff
   the `.md` output against the `.nr` source by eye — every `pub fn`,
   `pub struct`, `pub trait`, `impl Trait for Type`, and `pub use`
   should round-trip.
3. **Eval delta**. The `identifier`-bucket queries in
   `scripts/eval/golden_queries.json` target specific `.nr` files. A
   parser regression typically shows up as the wrong corpus being
   cited first.

If you find a regression, the parser lives in `noir_apiref.py`. The
key shape is `parse_file_items()` → `_all_top_level_item_positions()`
→ `_classify_item()` → `_build_item()` (or `_build_trait_or_impl()`
for trait/impl blocks).

## Why this exists / design notes

  * **No DB column for `chunk_type`.** The chunker
    (`application/parser/chunking.py`) detects apiref by file
    extension (`*.nr.md`) via a tag set in
    `application/parser/file/bulk.py`. With 12 corpora a config map
    is enough — see PLAN-rag-apiref.md.
  * **Apiref output is `.nr.md`, not `.nr.txt`.** This both
    (a) avoids the user-visible "shows as txt" complaint and
    (b) gets free heading-based chunking via `MarkdownParser`, which
    gives us item-sized retrieval units for free.
  * **Bodies kept for examples / tests / TS / Solidity.** Stripping
    bodies in those corpora would destroy the user's intent — those
    corpora exist to show how to *use* the API, not to document it.
  * **Idempotency.** The current `/api/upload` endpoint does NOT
    de-duplicate by `name` — re-uploading creates a second `sources`
    row with the same `name` and the same N chunks. Before re-running
    a corpus you should `DELETE FROM sources WHERE id = '<old_uuid>';`
    (which cascades to `documents`). A `?replace=true` mode is on
    `TODO.md`. The `swap_sources.py` SQL output references the *new*
    UUIDs only; you wire in the cleanup of old UUIDs by hand.

## Gotchas

### `is_public` defaults to `false`

Freshly-ingested `sources` rows default to `is_public=false`. The
backend's `SourceVisibilityService.resolve()` (called from
`stream_processor._configure_source`) silently filters out any source
that the requesting agent doesn't own AND isn't `is_public=true`.
That means a freshly-uploaded source will be invisible to retrieval
on agents that didn't run the upload — including all the Discord MCP
agents (which own nothing) and the production widget agent (user
`local`, owns nothing).

Symptom: SQL shows the agent's `extra_source_ids` includes the new
UUID, but `/stream` answers never cite it; direct
`ClassicRAG._get_data()` probes do retrieve it because they bypass
the visibility check.

Fix:
```sql
UPDATE sources SET is_public = true WHERE id = '<new_uuid>'::uuid;
```

This step is currently manual — `scripts/ingest/upload.py` should
ideally set `is_public=true` on success. Tracked as a follow-up.

### `swap_sources.py --allow-partial`

By default `swap_sources.py` refuses to emit SQL when the upload
manifest is missing any of the 12 canonical corpora. The default
mode rewrites `extra_source_ids` wholesale — a partial manifest
would silently truncate the agent's source list. Use one of:

  * `--apiref-only` to rotate just the apiref UUIDs in place via
    `array_replace` (preserves all other slot positions)
  * `--allow-partial` to acknowledge that you intentionally only
    uploaded a subset
  * Upload all 12 corpora before generating SQL

For an in-place rotation of a small subset (e.g. just `apiref`, or
just `(clean)` rebuilds), prefer the `--apiref-only`-style approach
or hand-write the `array_replace` SQL — it's more surgical and
preserves slot order without the risk of dropping unrelated entries.
