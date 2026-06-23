# Aztec corpus ingest toolkit

This directory holds the tooling for (re-)ingesting the corpora that
make up the Aztec DocsGPT knowledge base. It exists so that bumping to
a new aztec-packages release is a small number of commands instead of a
folkloric afternoon of `zip` calls and SQL guesses.

**Two-version KB (in flight — see `PLAN-two-version-kb.md`).** `corpora.py`
now emits **27 corpora**: 12 for **v4.3.1 (mainnet)** + 12 for **v5.0.0-rc.1
(testnet)** + 3 version-agnostic shared corpora (`awesome_aztec`,
`aztec_site_networks`, `aztec_participate_docs`). Per-version corpora carry
`version`/`network` fields and a version-suffixed slug (e.g.
`aztec_developer_docs_v5_0_0_rc_1`). Retrieval is scoped to ONE version per
request (`application/retriever/version_scope.py`); the swap wires BOTH bundles
into the agent so the source-id narrowing can pick per request. (Until the
cutover stamps `sources.metadata.version`, prod still runs the single v4.3.0
corpus — this tooling builds the replacement.)

## Files

| File | Purpose |
|---|---|
| `corpora.py` | Canonical definition of all corpora (paths, extensions, transform). Single source of truth — edit here when paths change. |
| `noir_apiref.py` | Transforms `.nr` source into a Markdown API-reference view (signatures + doc comments only). Used by the `noir_apiref` transform. |
| `build.py` | CLI: builds upload-ready zips from local checkouts of `aztec-packages` and `noir`. Writes per-corpus + overall manifests. |
| `upload.py` | CLI: POSTs the zips to `/api/upload`, polls the Celery task, captures the resulting `sources.id` UUIDs. |
| `swap_sources.py` | Generates the SQL needed to point an agent at the new corpora, plus the `.env` `AZTEC_SOURCE_IDS` block. **Does not execute SQL** — you do that yourself with `psql`, after reviewing. |
| `../db/attach_source_to_agents.py` | Appends ONE already-uploaded source to existing chat agents' `extra_source_ids` (idempotent, `--dry-run`). Use when a corpus lands after agents were provisioned — `swap_sources.py` rebuilds the whole list and is the wrong shape for this. Example: the Awesome Aztec corpus was initially attached only to the widget agent, which made the eval's `awesome-*` resource queries unpassable on the Discord/Slack agents. |

## What the corpora are

Per version (×2: v4.3.1 mainnet, v5.0.0-rc.1 testnet) the corpora are built
from upstream git repos pinned at that release's revisions:

  * `aztec-packages` at the release tag (`v4.3.1` / `v5.0.0-rc.1`) — code corpora.
  * `aztec-packages` at a `next`-branch snapshot commit that contains
    `version-<version>/` — rendered-docs corpora. The docs version snapshot is
    taken from a moving branch; the literal release tag does NOT contain
    `version-<version>/`. **Both versions live on the SAME `next` snapshot**
    (`d69ab88adc…` carries both `version-v4.3.1/` and `version-v5.0.0-rc.1/`).
    The rendered **TypeScript-API** artifact also comes from this snapshot, split
    by network folder: `mainnet/` = v4.3.1, `testnet/` = v5 (it is NOT on the
    release tag — the tag's copy is stale).
  * `noir-lang/noir` at the commit pinned by that tag's `noir/noir-repo`
    submodule (`git -C aztec-packages submodule status noir/noir-repo`). v4.3.1
    & v4.3.0 share `1d9727a6…`; v5.0.0-rc.1 is `c57152f9…`.

Plus **3 shared (version-agnostic) corpora** built once: `awesome_aztec`,
`aztec_site_networks`, `aztec_participate_docs`.

Selecting a bundle at build time: `--version v4.3.1` / `--version v5.0.0-rc.1`
builds that version's 12 corpora; `--version shared` builds the 3 shared ones;
no selector builds all 27. (`--corpus <slug>` still selects individual corpora
by their version-suffixed slug.)
  * `AztecProtocol/awesome-aztec` — the community resource list (`awesome_aztec`
    corpus, built via `--awesome-aztec <checkout>`). This is a **moving**
    community repo, NOT release-pinned; source URLs link to the GitHub blob
    on `main`, and each re-ingest re-pins to whatever `main` is at build
    time. Live in prod since 2026-06-02 (pin `280f24f0`). Because it's a
    production corpus now, the standard build **requires** `--awesome-aztec`
    (a missing root hard-errors rather than skipping).

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
    (was 285 of ~1000 chunks at v4.3.0)
  * **Aztec Network Docs** — `operators/reference/changelog/*` and
    `reference/changelog/*` (~60 chunks of release notes)

`SourceTree.include_paths` is the inverse — an allowlist used when
the wanted slice is far smaller than the source tree. Currently:

  * **Aztec Site Networks Page** — `include_paths=("networks.md",)`
    over `docs/docs/`, so the corpus is exactly one file. The
    unversioned `docs/docs/` folder is otherwise intentionally
    excluded from indexing (`index.mdx`, `aztec_connect_sunset.mdx`
    aren't load-bearing); the allowlist pulls in just the L1
    contract address table that the versioned operator docs defer
    to.

## End-to-end version bump (≈ 1 hour, mostly waiting on embeds)

Outline; details below.

For the two-version KB you do steps 1–2 once PER version (v4.3.1 + v5.0.0-rc.1),
plus the shared bundle once; the SAME `next`-snapshot docs worktree serves both
versions (it carries both `version-v*` folders), so only the code/noir checkouts
differ per version. Steps 4–7 run once over the combined build dir.

```bash
# 1. Get clean checkouts of the source trees ("Option B" — the four-root
#    layout shipped by PR #150). $V is the release, e.g. v4.3.1 or v5.0.0-rc.1.
#    The docs snapshot is taken from a moving branch (release tag lacks
#    version-<V>/); ONE next snapshot carries BOTH versions' docs folders.
#    awesome-aztec is a moving community repo — just clone current main.
git -C ../aztec-packages worktree add --detach /tmp/aztec-$V       $V
git -C ../aztec-packages worktree add --detach /tmp/aztec-next-docs <next-snapshot-sha>   # shared by both versions
NOIR_PIN=$(git -C /tmp/aztec-$V submodule status noir/noir-repo | awk '{print $1}' | tr -d -)
git clone https://github.com/noir-lang/noir /tmp/noir-$V
git -C /tmp/noir-$V checkout "$NOIR_PIN"
git clone --depth 1 https://github.com/AztecProtocol/awesome-aztec /tmp/awesome-aztec

# 2. Build this version's 12 zips (--version selects the bundle). Idempotent.
#    Run once per version, then once with --version shared for the 3 shared
#    corpora (--awesome-aztec required there — it's a production corpus).
python -m scripts.ingest.build \
    --version        $V \
    --aztec-pkg      /tmp/aztec-$V \
    --aztec-pkg-docs /tmp/aztec-next-docs \
    --noir           /tmp/noir-$V \
    --awesome-aztec  /tmp/awesome-aztec \
    --out            /tmp/aztec-corpora-build
# ... repeat for the other version, then: --version shared (same --out dir).

# 3. Review the build manifests — especially the apiref ones.
cat /tmp/aztec-corpora-build/manifests/aztec_nr_apiref.json | jq
# Hand-audit ~20 of the .md files (`vimdiff` against the .nr originals
# to make sure no public symbol was silently dropped).

# 4. Upload each zip and capture the source UUIDs. This kicks off
#    Celery embeddings — costs scale with new chunk count.
#    NB: prod compose only publishes Caddy on 127.0.0.1:5080 — use
#    that base URL when running against the hub compose. The dev
#    compose still exposes backend directly on 127.0.0.1:7091.
python -m scripts.ingest.upload \
    --build-dir /tmp/aztec-corpora-build \
    --base-url  http://127.0.0.1:5080 \
    --user      local \
    --token     "$INTERNAL_KEY" \
    --out       /tmp/aztec-corpora-build/upload_manifest.json

# 5. Generate the SQL to swap the production agent's source list. This SQL
#    ALSO stamps sources.metadata.{version,network} on each uploaded source —
#    the producer half the retrieval version-scoping resolver reads, WITHOUT
#    which the per-request narrowing stays a no-op. REVIEW BEFORE EXECUTING.
python -m scripts.ingest.swap_sources \
    --upload-manifest /tmp/aztec-corpora-build/upload_manifest.json \
    --agent-id $PROD_AGENT_ID \
    --out     /tmp/swap.sql
psql "$POSTGRES_URI" -f /tmp/swap.sql

# 6. Update AZTEC_SOURCE_IDS in .env (the swap_sources output prints the
#    canonical-order block — now spans BOTH versions' source ids + shared)
#    and set AZTEC_CORPUS_VERSION (GET /api/version; two-version → use a
#    combined label like "v4.3.1+v5.0.0-rc.1"). Also roll the two-version
#    system prompt to the three Postgres prompt ids (see PLAN §5 / CLAUDE.md
#    "Postgres is source of truth for prompts"). Then rebuild + force-recreate.
#    ``discord-bot`` is included because the agent display-name and citation
#    footer may have changed across a version bump.
docker compose -f deployment/docker-compose-hub.yaml --env-file .env \
    build backend worker discord-bot
docker compose -f deployment/docker-compose-hub.yaml --env-file .env \
    up -d --force-recreate backend worker discord-bot

# 7. Run the eval to confirm no regressions.
docker compose -f deployment/docker-compose-hub.yaml exec backend \
    python scripts/eval/eval_retrieval.py --mode retriever
docker compose -f deployment/docker-compose-hub.yaml exec backend \
    python scripts/eval/eval_retrieval.py --mode stream \
        --api-key "$PROD_AGENT_KEY"

# 8. Cleanup — deferred ≥72h. Once you've seen healthy traffic on all
#    four surfaces (widget, /ask, Discord, MCP) since the swap, drop the
#    old version. The old corpora stay addressable by UUID until the
#    DELETE; rolling back during the 72h window is restoring the
#    agents-pre-vNEW.tsv snapshot and reverting .env.
psql "$POSTGRES_URI" -c "DELETE FROM sources WHERE name LIKE '% vOLD%';"
```

## Apiref-only swap (smaller blast radius)

If you only want to swap the apiref corpora (e.g. iterating on the
`noir_apiref.py` transform without redoing every other corpus):

Corpus slugs are version-suffixed now, so target the version you're iterating
on (e.g. `aztec_nr_apiref_v5_0_0_rc_1` / `noir_stdlib_apiref_v5_0_0_rc_1`):

```bash
python -m scripts.ingest.build \
    --aztec-pkg /tmp/aztec-v5.0.0-rc.1 \
    --noir      /tmp/noir-v5.0.0-rc.1 \
    --out       /tmp/aztec-corpora-build \
    --corpus    aztec_nr_apiref_v5_0_0_rc_1 \
    --corpus    noir_stdlib_apiref_v5_0_0_rc_1

python -m scripts.ingest.upload \
    --build-dir /tmp/aztec-corpora-build \
    --base-url  http://127.0.0.1:5080 \
    --user      local \
    --token     "$INTERNAL_KEY" \
    --out       /tmp/aztec-corpora-build/upload_manifest.json \
    --corpus    aztec_nr_apiref_v5_0_0_rc_1 \
    --corpus    noir_stdlib_apiref_v5_0_0_rc_1

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
      "source_dirs": ["/tmp/aztec-v4.3.0/noir-projects/aztec-nr"],
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
    "name": "Aztec.nr Framework v4.3.0 (apiref)",
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
    `application/parser/file/bulk.py`. With a handful of corpora a config
    map is enough — see PLAN-rag-apiref.md.
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
manifest is missing any production corpus in `_CANONICAL_ORDER` (now
derived from `corpora.py`, so it spans BOTH versions + shared = 27).
The default mode rewrites `extra_source_ids` wholesale — a partial
manifest would silently truncate the agent's source list. Use one of:

  * `--apiref-only` to rotate just the apiref sources: emits a commented
    OLD→NEW UUID guide + an inspect `SELECT` for you to hand-edit
    `extra_source_ids`, plus the `metadata` stamp UPDATEs for the new
    apiref sources (preserves all other slot positions)
  * `--allow-partial` to acknowledge that you intentionally only
    uploaded a subset
  * Upload every production corpus before generating SQL

For an in-place rotation of a small subset (e.g. just `apiref`, or
just `(clean)` rebuilds), prefer the `--apiref-only`-style approach
or hand-write the `array_replace` SQL — it's more surgical and
preserves slot order without the risk of dropping unrelated entries.
