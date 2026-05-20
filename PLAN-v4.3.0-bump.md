# PLAN: Bump knowledge corpora v4.2.0 → v4.3.0

**Trigger**: aztec-packages PR
[#23375](https://github.com/AztecProtocol/aztec-packages/pull/23375)
(`chore(docs): cut v4.3.0 docs version (mainnet)`) merged into the
`next` branch at 2026-05-20 16:29 UTC, merge commit
`3f7cbc05e9a522ca81f5416278e99633dc47ee91`. The `v4.3.0` git tag exists
(commit `e44e98d`, 2026-05-20 08:14 UTC, not draft, not prerelease).

**Goal**: rotate every agent's corpus and every code-path that hard-codes
`v4.2.0` / `version-v4.2.0` so the bot answers from v4.3.0 sources and
links to v4.3.0 URLs.

**Approach — Option B (two-root build)**: the `version-v4.3.0/` folder
does **not** exist at the `v4.3.0` git tag. The tag was cut from the
release branch before #23375 merged into `next`; at the tag the latest
dev docs folder is `version-v4.2.0-aztecnr-rc.2` and the latest network
docs folder is `version-v4.1.2`. The docs version snapshot is taken
from a moving branch — same pattern that applied to v4.2.0 (the tag
had it under `version-v4.1.0-rc.2`). We split the build:

| Source root | Pin | Used by |
|---|---|---|
| `aztec-packages` (tag) | `v4.3.0` | Code corpora + auto-generated TS API reference. Stable URLs to `aztec-packages/blob/v4.3.0/...`. |
| `aztec-packages-docs` (`next` snapshot) | `3f7cbc05e9a522ca81f5416278e99633dc47ee91` | The three rendered-docs corpora (developer / network / site-networks) — URLs go to docs.aztec.network, no git ref needed. |
| `noir` | `1d9727a6e0a9df75a71bb9c87daacbe30659ba09` (noir/noir-repo submodule pin at the `v4.3.0` tag) | Noir docs corpus + Noir stdlib apiref. |

The codebase changes that support Option B + bump versions are
implemented in branch `chore/bump-corpora-v4.3.0` (this PR).

---

## A. Code-and-config edits (PR `chore/bump-corpora-v4.3.0`)

### A1. Corpus definitions (`scripts/ingest/corpora.py`)
- All 13 corpus `name=` strings → `v4.3.0`.
- Two docs SourceTree `path`s `version-v4.2.0/…` → `version-v4.3.0/…`.
- Two docs SourceTree `zip_prefix`s `version-v4.2.0/…` → `version-v4.3.0/…`.
- **Three docs corpora re-rooted** to the new `aztec-packages-docs`
  source root: `aztec_developer_docs`, `aztec_network_docs`,
  `aztec_site_networks`. Other corpora stay on `aztec-packages` (tag).
- **TypeScript API stays on `testnet/`**: the `mainnet/` rename only
  landed on `next`; the `v4.3.0` tag still ships `testnet/`. Switching
  the corpus to `mainnet/` would mean every TS API GitHub link 404s.

### A2. URL rewriter (`application/api/answer/routes/base.py`)
- Three `source_path.startswith("version-v4.2.0/…")` branches → `v4.3.0`.
- `_AZTEC_GITHUB_BASE` → `…/blob/v4.3.0`.
- `_NOIR_GITHUB_BASE` → `…/blob/1d9727a6e0a9df75a71bb9c87daacbe30659ba09`.
- TS API mapping in `_SOURCE_TO_REPO_PREFIX` stays on `testnet/`
  (matches the tag's actual folder).
- Comments swept including the "what is NOT here" block, which now
  explains the v4.3.0 tag-vs-next nuance.

### A3. Slug-override map (`application/api/answer/routes/aztec_doc_slugs.py`)
- `scripts/build_aztec_doc_slug_map.py` `_DOC_ROOTS` constants bumped
  to `version-v4.3.0` (codex flagged: these are runtime, not docstring).
- Regenerated against the `next@3f7cbc05` worktree. **Same 14 overrides
  as v4.2.0**, key prefix bumped to `version-v4.3.0`.
- Generator `--aztec-pkg` help text now warns that the input must be a
  `next`-branch worktree containing `version-v4.3.0/`, not the tag.

### A4. Grounded prompts
- Files renamed via `git mv`:
  - `aztec_4_2_0_grounded.txt` → `aztec_4_3_0_grounded.txt`
  - `aztec_4_2_0_grounded_discord.txt` → `aztec_4_3_0_grounded_discord.txt`
- "Retrieved Aztec v4.2.0 chunks" → "v4.3.0" in both files.
- **Postgres is the source of truth**. After PR merges, the operator
  must:
  ```sql
  UPDATE prompts SET
      content = pg_read_file('/tmp/aztec_4_3_0_grounded.txt'),
      name = 'Aztec Docs (widget) — Aztec 4.3.0 grounded'
  WHERE id = '0780959b-3c18-4ad9-8284-691665233a6f';
  UPDATE prompts SET
      content = pg_read_file('/tmp/aztec_4_3_0_grounded_discord.txt'),
      name = 'Honk AI (Discord bot) — Aztec 4.3.0 grounded'
  WHERE id = '4bfa9ddf-5d8e-4d5d-a94e-c9e52e1a9ba2';
  ```
  (Per CLAUDE.md "docker cp into postgres" workflow — codex called out
  that the prompt-row UPDATE was missing from the original plan.)

### A5. Settings + env-template
- `application/core/settings.py` docstring example: `"v4.2.0"` → `"v4.3.0"`.
- `.env-template:41` `AZTEC_CORPUS_VERSION` default → `v4.3.0`.
- `.env-template:207` "`Aztec 4.2.0` is pinned to qwen3.6-flash" → `Aztec 4.3.0`.
- `scripts/ingest/build.py` + `scripts/ingest/noir_apiref.py` docstring
  examples bumped (codex flagged).

### A6. Eval harness
- `scripts/eval/eval_retrieval.py:46-47` bucket prefixes →
  `version-v4.3.0/…`.
- `scripts/eval/golden_queries.json` all `version-v4.2.0/` →
  `version-v4.3.0/` (13 hits).
- `scripts/eval/README.md` references to renamed prompt files.

### A7. Tests
- `tests/api/answer/routes/test_source_url_rewrite.py` — version
  strings + `aztec-packages/blob/v4.2.0` URLs swept.
- `tests/api/answer/routes/test_search.py` — corpus paths swept.
- `tests/api/answer/routes/test_version.py` — fixtures updated.
- `tests/api/answer/test_citation_marker.py` — sample source paths.
- `tests/test_ingest_corpora.py` — comment.
- **Migration scripts NOT touched** (`0004_sources_is_public.py`,
  `0009_agents_surface.py`) — codex flagged they document historical
  state.

### A8. Documentation sweep
- `CLAUDE.md` — all ~28 version references swept; Noir submodule hash
  updated; Data sources section rewritten to explain the two-root
  Option B approach.
- `README.md` — corpus-pinning sentence.
- `scripts/ingest/README.md` — end-to-end workflow now shows three
  checkouts and `--aztec-pkg-docs`. `--base-url` updated to
  `127.0.0.1:5080` (the prod Caddy loopback; backend port 7091 is
  dev-only — codex flagged).
- `scripts/eval/README.md` — prompt-file references.
- `AZTEC_SETUP.md` — clean (no v4.2.0 refs).

### A9. Discord agent display-name update
Renamed `"Aztec 4.2.0"` → `"Aztec 4.3.0"` in:
- `extensions/discord/bot.py:535, 1694`
- `scripts/db/backfill.py:1021`
- `scripts/loadtest/run_stress.py:190`
- `application/core/model_configs.py:213`
- `CLAUDE.md` (agent-map table at line 21, and 5 narrative mentions)

After PR merge, the operator runs:
```sql
UPDATE agents SET name = 'Aztec 4.3.0'
WHERE name = 'Aztec 4.2.0' AND surface = 'discord';
```

---

## B. Ingest run (after PR merges)

Standard `scripts/ingest/README.md` workflow with `vNEW=v4.3.0` and two
aztec-packages checkouts:

```bash
git -C ../aztec-packages worktree add --detach /tmp/aztec-v4.3.0      v4.3.0
git -C ../aztec-packages worktree add --detach /tmp/aztec-v4.3.0-docs 3f7cbc05e9a522ca81f5416278e99633dc47ee91
# Noir submodule pin at the v4.3.0 tag (NOT at next):
git -C /tmp/aztec-v4.3.0 submodule status noir/noir-repo
# → 1d9727a6e0a9df75a71bb9c87daacbe30659ba09
git clone https://github.com/noir-lang/noir /tmp/noir-v4.3.0
git -C /tmp/noir-v4.3.0 checkout 1d9727a6e0a9df75a71bb9c87daacbe30659ba09

python -m scripts.ingest.build \
    --aztec-pkg      /tmp/aztec-v4.3.0 \
    --aztec-pkg-docs /tmp/aztec-v4.3.0-docs \
    --noir           /tmp/noir-v4.3.0 \
    --out            /tmp/aztec-corpora-v4.3.0-build

# Apiref hand-audit (~20 files across aztec/src/, state_vars/, note/,
# oracle/, messages/). Check files_with_zero_items / parse_errors vs
# the v4.2.0 baseline (~29 zero-item, 0 parse errors).

# Upload — use 127.0.0.1:5080 (Caddy loopback) for prod compose.
python -m scripts.ingest.upload \
    --build-dir /tmp/aztec-corpora-v4.3.0-build \
    --base-url  http://127.0.0.1:5080 \
    --user      local \
    --token     "$INTERNAL_KEY" \
    --out       /tmp/aztec-corpora-v4.3.0-build/upload_manifest.json

# Flip is_public=true on the 13 new sources (upload.py does NOT set this):
psql "$POSTGRES_URI" -c "UPDATE sources SET is_public = true WHERE id = ANY(ARRAY[<13 new uuids>]::uuid[]);"
```

## C. Swap agents (manual SQL)

Four agent classes need repointing. Capture the pre-swap snapshot first
for rollback:

```sql
\copy (
  SELECT id, name, surface, source_id, extra_source_ids
  FROM agents
  WHERE surface IN ('discord','widget','web_ask','mcp')
  ORDER BY surface
) TO '/tmp/agents-pre-v4.3.0.tsv';
```

Then for each structural agent (discord / widget / web_ask):

```bash
python -m scripts.ingest.swap_sources \
    --upload-manifest /tmp/aztec-corpora-v4.3.0-build/upload_manifest.json \
    --agent-id <agent_uuid> \
    --out     /tmp/swap-<surface>.sql
psql "$POSTGRES_URI" -f /tmp/swap-<surface>.sql  # after review
```

For **per-Discord-user `Aztec MCP` agents** (many rows, all surface='mcp'),
generate the SQL once with any MCP agent's UUID, then hand-edit the
WHERE clause:
```sql
-- Original (from swap_sources):
-- WHERE id = '<uuid>'
-- Replace with:
WHERE surface = 'mcp';
```
Review the array literal carefully before executing — this is the only
bulk-UPDATE step.

Then update prompts (A4 SQL), update `.env`
(`AZTEC_CORPUS_VERSION=v4.3.0` + new `AZTEC_SOURCE_IDS` block from
swap_sources output), and rename the Discord agent (A9 SQL).

Recreate the stack:
```bash
docker compose -f deployment/docker-compose-hub.yaml --env-file .env build backend worker discord-bot
docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d --force-recreate backend worker discord-bot
```

## D. Verify

1. **`/api/version`**:
   ```bash
   curl -s https://aztec.adjacentpossible.dev/api/version | jq
   # → {"aztec_corpus_version": "v4.3.0", "source_count": 13}
   ```
2. **MCP smoke** (codex flagged: `/api/version` alone doesn't prove the
   bulk MCP-agent UPDATE landed). From a claudebox session:
   ```
   # Use a Discord-issued mcp-key bearer:
   honk_rag.search "what is the L1 testnet Rollup address"
   # → expect citations pointing at v4.3.0 (or docs.aztec.network/networks)
   ```
3. **Eval gates** (per CLAUDE.md):
   ```bash
   docker compose ... exec backend python scripts/eval/eval_retrieval.py --mode retriever
   docker compose ... exec backend python scripts/eval/eval_retrieval.py --mode stream --api-key "$PROD_AGENT_KEY"
   ```
4. **Hand smoke**: widget, /ask, Discord @-mention, MCP.

## E. Cleanup (deferred ≥72h with verified traffic)

Codex flagged 24h as too short for sparse Discord/MCP traffic. After
**72h of clean traffic across all four surfaces**:

```sql
DELETE FROM sources WHERE name LIKE '% v4.2.0%';
-- CASCADEs to documents (the embedded chunks).
```

Keep `/tmp/agents-pre-v4.3.0.tsv` and the upload manifest until cleanup
runs — they're the rollback inputs.

---

## Codex-flagged items folded in

- ✅ Prod upload URL is `127.0.0.1:5080`, not `localhost:7091` (B step).
- ✅ Slug-map generator `_DOC_ROOTS` constants bumped before regen (A3).
- ✅ TS API stays on `testnet/` (codex confirmed the `mainnet/` rename
  doesn't reach the `v4.3.0` tag).
- ✅ Prompt-row `UPDATE prompts SET content = …` explicit (A4).
- ✅ MCP smoke step added (D2).
- ✅ `.env-template:17` non-edit dropped (the "13 v4.2.0 corpora"
  header was in `.env`, not `.env-template`).
- ✅ Submodule path is `noir/noir-repo`, not `noir`.
- ✅ Alembic `0004` / `0009` left alone — historical.
- ✅ `build.py` + `noir_apiref.py` docstring examples bumped.
- ✅ 24h → 72h cleanup window (E).
- ✅ A4 file rename to `aztec_4_3_0_grounded*.txt` adopted.
- ✅ Discord-bot rebuild added to the recreate command (A9 changes
  bot.py).
- ✅ Site-networks corpus moved to `aztec-packages-docs` root —
  `networks.md` was updated in PR #23375 (sizes diverged 10774 →
  11046 between tag and merge commit).

The Option B build (which the original plan did not specify) is now
the build script's first-class mode via `--aztec-pkg-docs`.
