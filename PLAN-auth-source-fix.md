# Plan: fix MCP grounding + harden auth boundary + introduce public sources

Addresses items 1–3 from the security review (2026-04-27).

## Problem statements

### 1. MCP grounding is silently broken in production

**Confirmed live** by hitting `/stream` with the `joshc` MCP key — 0 sources
returned, ungrounded answer. Same query against the `Aztec 4.2.0` widget agent
returns 10 sources with correct citations. Root cause:

- All 12 source rows have `user_id='local'` (set at ingest time by
  `POST /api/upload` with `user=local`).
- `/api/internal/create_mcp_key` validates source UUIDs with an **unscoped**
  `SELECT id FROM sources WHERE id = ANY(...)`, then creates an agent with
  `user_id=f"discord:{discord_user_id}"`.
- At retrieval time `_get_data_from_api_key` calls
  `sources_repo.get(sid, agent.user_id)` whose SQL is
  `WHERE id=:id AND user_id=:user_id`. For Discord agents this returns None
  for every source, so `sources_list` ends up empty and
  `_has_active_docs()` returns False. The retriever is never invoked.

Every Discord user who has run `/mcp-key` and configured the mcp-server in
Claude Desktop / Cursor / etc. has been getting ungrounded answers since
launch. The web widget keeps working because that agent is owned by `local`.

### 2. Auth boundary is implicit

CLAUDE.md acknowledges: real auth = Cloudflare Access. The backend itself
allows anonymous identity minting (`/api/generate_token`), disables JWT
expiry (`verify_exp=False`), and falls through to `{"sub": "local"}` when
`AUTH_TYPE` isn't set. If anything ever fronts the origin without Caddy +
correct trusted-proxy config, the `Cf-Access-Authenticated-User-Email` header
is forgeable and there's nothing else stopping a caller.

### 3. Source IDs leak through unscoped paths

Even after #1 is fixed, two other paths bypass ownership:
- `_configure_source` (line 469-472) trusts `data["active_docs"]` from the
  request body unchecked → `ClassicRAG` queries them via pgvector.
- `agents/routes.py:543-548` accepts UUIDs in agent create/update without
  verifying the caller owns the source — only `looks_like_uuid()`.

---

## Approach

Pick the smallest set of changes that closes all three findings without
breaking the running production system. Order matters; auth hardening can
proceed in parallel with the source-model work since it touches a disjoint
surface.

1. **Hot-fix MCP grounding** with the narrowest possible change — restore
   service today.
2. **Fix `upsert_mcp_key` so it refreshes source attachments on conflict**
   (independent bug surfaced during plan review — without this, existing
   Discord users keep stale source IDs even after the resolver is fixed).
3. **Schema migration + backfill folded in** — add `sources.is_public`,
   backfill known public Aztec source IDs, then NOT NULL DEFAULT FALSE.
   Switch the resolver to honor it via a **batched** lookup
   (`list_visible_by_ids(user_id, ids)`, one SQL call per request).
4. **Audit every source dereference path** — not just
   `_get_data_from_api_key`. Agent CRUD, template/share adoption, and any
   shared/system flow needs to use `list_visible_by_ids` consistently or
   step 3 will re-break legitimate cross-owner cases.
5. **Auth hardening** — multi-step JWT migration (no flag-day on
   `verify_exp`), disable `/api/generate_token` in prod, validate
   Cloudflare Access JWTs server-side. Can ship in parallel with step 3.

Each step ships independently and is reversible.

---

## Step 1 — Hot-fix MCP grounding (30 min change, no migration)

**Goal:** restore grounding for existing Discord MCP keys today, with the
narrowest possible change. Step 3 will replace this with the proper model.

### Decision: narrow `get_for_agent`, gated on `mcp_purpose`

I considered two options and rejected the simpler one:

- **Option A — stamp `user_id='local'` on MCP agents.** One-line change in
  `create_mcp_key`. Works immediately. Rejected because it conflates
  ownership semantics — every Discord agent would now appear "owned" by the
  admin user in any future UI listing, quota, or analytics flow. We'd have
  to remember to special-case `mcp_provider IS NOT NULL` everywhere
  ownership matters. Easier to fix today, more confusing forever.
- **Option B — `get_for_agent(source_id, agent_id)` resolver.** Selected,
  but **narrowed** to `mcp_purpose='aztec_mcp'` so it doesn't open a
  privilege escalation if any other agent CRUD path ever lets a user
  attach someone else's source UUID to a regular agent. Today no such
  bug is known, but generalizing to all agents would make any future bug
  in agent create/update an exfiltration vector.

### Code change

`application/storage/db/repositories/sources.py` — add:

```python
def get_for_aztec_mcp_agent(self, source_id: str, agent_id: str) -> Optional[dict]:
    """Resolve a source visible to a verified Aztec MCP agent.

    Trust boundary: the agent row is created by /api/internal/create_mcp_key
    which is gated on MCP_PROVISIONING_KEY, so the source UUIDs persisted
    on (source_id, extra_source_ids) are authoritative. We cross-check the
    request against that exact list and require mcp_purpose='aztec_mcp'.
    """
```

The SQL inlines that scope:

```sql
SELECT s.* FROM sources s
JOIN agents a
  ON a.id = :agent_id
 AND a.mcp_purpose = 'aztec_mcp'
 AND a.mcp_provider IS NOT NULL
WHERE s.id = :id
  AND s.id::uuid = ANY(
    ARRAY[a.source_id] || COALESCE(a.extra_source_ids, '{}')
  )
```

This is the only path that reads a source without a `user_id` match, and it
only triggers for the specific agent shape created by the MCP provisioning
endpoint.

### Caller change

`stream_processor._get_data_from_api_key` (three call sites): branch on
`agent.get('mcp_purpose') == 'aztec_mcp'`. If yes, call the new method
passing `agent['id']`. Otherwise keep the existing `get(sid, user_id)`.

### Tests

- Repo test: owner match (allow), non-owner with `mcp_purpose='aztec_mcp'`
  agent referencing the source (allow), non-owner with non-MCP agent (deny),
  non-owner with `mcp_purpose='aztec_mcp'` agent NOT referencing the source
  (deny).
- Integration test: create an MCP agent + non-owned public-shaped sources,
  hit `/stream`, assert non-empty `sources_list` and at least one citation.

### Smoke test

Re-run the live curl with the `joshc` key — expect ≥ 6 sources (matching
the widget agent baseline). The `poseidon2.nr.txt` query should return the
correct file as a citation.

### Rollback

Pure code change in one new method + three branch sites. Revert the commit.

---

## Step 2 — Fix `upsert_mcp_key` to refresh source attachments on conflict

**Goal:** independent bug surfaced during plan review. Without this, even
after step 1 lands, existing Discord users keep stale source IDs whenever
the canonical corpus changes (reorder, swap, add/remove).

### Bug

`AgentsRepository.upsert_mcp_key` at `application/storage/db/repositories/agents.py:171`:

```python
stmt.on_conflict_do_update(
    index_elements=["mcp_provider", "mcp_provider_user_id", "mcp_purpose"],
    index_where=agents_table.c.mcp_provider.is_not(None),
    set_={"name": stmt.excluded.name, "updated_at": func.now()},
)
```

Only `name` and `updated_at` refresh on conflict. So an existing user
running `/mcp-key` after we've reordered `AZTEC_SOURCE_IDS` (or after
adding Noir Docs / stdlib / TypeScript API in PR #42) gets the same agent
row with the **original** source order. The doc CLAUDE.md flags this for
the UI-edit path but not for the upsert.

### Fix

Extend the `set_=` clause to refresh `source_id`, `extra_source_ids`,
`description`, `mcp_purpose`, `chunks`, `retriever`. Treat the upsert as
"the source of truth is the latest provisioning call".

```python
set_={
    "name": stmt.excluded.name,
    "description": stmt.excluded.description,
    "source_id": stmt.excluded.source_id,
    "extra_source_ids": stmt.excluded.extra_source_ids,
    "chunks": stmt.excluded.chunks,
    "retriever": stmt.excluded.retriever,
    "updated_at": func.now(),
}
```

Do NOT refresh `key` on conflict — existing users would lose their
configured API key in their MCP client. Document this carefully.

### Tests

- Repo test: insert an MCP agent with sources `[A, B]`, upsert again with
  sources `[B, A, C]` — assert the row now has the new order.
- Repo test: `key` does NOT change on conflict.

### Rollback

Single-method change. Revert.

---

## Step 3 — Public/system source model (the real fix)

**Goal:** model "shared corpora" as a first-class concept so agent ownership
and source ownership are decoupled cleanly. Closes findings 1 and 3.

### Schema + backfill in one migration

`application/alembic/versions/0004_sources_is_public.py` — three steps in
one transaction so there's no window where the resolver enforces `is_public`
but the data hasn't been backfilled yet:

```sql
-- Step a: nullable column (no constraint yet)
ALTER TABLE sources ADD COLUMN is_public BOOLEAN;

-- Step b: backfill known-public Aztec sources by name
UPDATE sources SET is_public = TRUE WHERE name IN (
  'Aztec Developer Docs v4.2.0',
  'Aztec.nr Framework v4.2.0',
  'Noir Language Docs v4.2.0',
  'Aztec Example Contracts v4.2.0',
  'aztec.js SDK v4.2.0',
  'Aztec TypeScript API v4.2.0',
  'Noir stdlib v4.2.0',
  'Aztec CLI v4.2.0',
  'Aztec Network Docs v4.2.0',
  'Aztec E2E Tests v4.2.0',
  'Aztec Protocol Circuits v4.2.0',
  'Aztec L1 Contracts v4.2.0'
);
UPDATE sources SET is_public = FALSE WHERE is_public IS NULL;

-- Step c: lock down the column
ALTER TABLE sources ALTER COLUMN is_public SET NOT NULL;
ALTER TABLE sources ALTER COLUMN is_public SET DEFAULT FALSE;
CREATE INDEX idx_sources_is_public_true ON sources(id) WHERE is_public = TRUE;
```

This deploys as one atomic migration — no separate "run-this-script-then-deploy"
step. The widget agent path keeps working throughout.

### Repository — batched, not per-UUID

Replace the hot-fix `get_for_aztec_mcp_agent` with **one batched method**:

```python
def list_visible_by_ids(
    self, user_id: str, ids: list[str]
) -> dict[str, dict]:
    """Resolve a batch of source UUIDs visible to ``user_id``.

    Returns a {uuid: row} map for the subset of ``ids`` that are either
    owned by ``user_id`` or marked ``is_public=TRUE``. Missing IDs in the
    return map indicate denial — callers decide whether that's 403 or a
    silent skip based on their context.

    One SQL query regardless of input size — never call this in a loop.
    """
```

SQL:

```sql
SELECT * FROM sources
WHERE id = ANY(CAST(:ids AS uuid[]))
  AND (user_id = :user_id OR is_public = TRUE)
```

Call sites (every place that takes a source UUID from a request and turns
it into anything):

- `_get_data_from_api_key` — one call to resolve the full
  `[source_id, *extra_source_ids]` set; build `sources_list` from the
  returned map. This replaces both the step-1 hot-fix AND the original
  three `sources_repo.get(...)` calls.
- `_configure_source` direct-`active_docs` path — one call, the resolver
  filters out any UUIDs the requester can't see.
- `agents/routes.py` create + update — one call per request, returns 403
  if any supplied UUID is missing from the result map.

Silent-drop policy: in `_get_data_from_api_key` (the widget agent path)
silent skip is fine because the agent owner curated the list. In
`_configure_source` direct-payload path the request is untrusted, so a
missing UUID returns 403. In agent CRUD a missing UUID returns 403 to
prevent the user from saving an agent referencing data they can't see.

### `create_mcp_key` validation

Tighten: `SELECT id FROM sources WHERE id = ANY(...) AND is_public = TRUE`.
`AZTEC_SOURCE_IDS` is **explicitly** the set of public corpora; if an
operator listed a private source there by mistake, it should fail loudly
at provisioning rather than leak access.

### `ClassicRAG` change

None at the retriever layer. By contract, by the time `active_docs` reaches
`ClassicRAG` it has already been authorized by the resolver above. Keep the
retriever's responsibility narrow: take UUIDs, search them.

### Tests

- Migration test that round-trips `0004_sources_is_public`.
- Repo test for `get_visible`: owner-only / public / neither.
- Endpoint tests:
  - Agent create with a non-owned, non-public source → 403.
  - Agent create with `is_public=TRUE` source by another user → 200 (this
    is the Aztec MCP shape).
  - `/stream` with `active_docs=[private-uuid-of-other-user]` → 403.
  - `/stream` with `active_docs=[public-uuid]` → 200.
- Eval harness re-run via `scripts/eval/eval_retrieval.py --mode stream` to
  confirm no regression in the widget agent path.

### Rollback

Migration is additive (new column, default FALSE). Code can be reverted
without dropping the column. The backfill script is idempotent.

---

## Step 4 — Audit every source dereference path

**Goal:** make sure step 3's `list_visible_by_ids` is actually called from
every entry point that reads a source UUID from a request. Easy to miss
one and re-introduce the same class of bug.

### Audit list

Grep for `SourcesRepository(` and `sources_table` and triage each callsite:

- `application/api/answer/services/stream_processor.py` — agent + direct
  payload paths (covered above).
- `application/api/user/agents/routes.py` — create, update,
  template/share adoption, "shared" agent retrieval.
- `application/api/user/sources/*.py` — list/delete (already
  user-scoped; verify `is_public` doesn't accidentally leak public sources
  into someone else's "my sources" list — they should NOT appear there).
- `application/api/internal/routes.py` — `create_mcp_key` (covered).
- `application/api/v1/*.py` — any v1 endpoints that take source IDs
  (e.g. the OpenAI-compatible chat completions path).
- Workflow / connector creators that resolve source bindings.

For each, the rule is: any path that takes a source UUID from request
input → must round-trip through `list_visible_by_ids` for the
**requester's** identity (not the agent's identity, when the request is
authenticated by JWT/Cloudflare Access). Resolution must happen before the
UUID reaches the retriever, the storage layer, or any persisted field.

### Test strategy

Add a parametrized integration test that, for each entry point, calls it
with `[private-uuid-of-other-user]`:
- Create endpoints → 403
- Read/use endpoints → either filtered out (with a warning log) or 403,
  depending on the policy chosen above.

If a callsite is missed, the test will fail and the CI will catch it.
Better than discovering it via a security review eight months later.

---

## Step 5 — Auth boundary hardening

**Goal:** make the backend stop trusting forged identities even if Caddy /
Cloudflare Access is misconfigured.

### Multi-step JWT migration (NOT a flag-day)

Existing session JWTs were minted without an `exp` claim. Flipping
`verify_exp=True` in one shot would 401 every active session in flight.
Stage it:

**5a. Mint new tokens with `exp`.** Update the `/api/generate_token` issuer
to embed `"exp": now + 24h`. Don't change the verifier yet — old and new
tokens both work, old tokens still skip expiry. Ship and let the install
base roll forward for 24-48h.

**5b. Disable `/api/generate_token` in prod.** Gate on a new
`ALLOW_ANONYMOUS_TOKEN` setting (default `False` in prod, `True` in dev
compose). Returns 404 when off. After this lands, no new
unauthenticated tokens can be minted; only tokens issued via authenticated
flows (or already in users' browsers) work.

**5c. Enforce `verify_exp` strictly.** Remove `options={"verify_exp": False}`
from `application/auth.py:19`. By the time this ships, every token in
circulation has an `exp` claim from step 5a, so nothing 401s unexpectedly.

**5d. Validate Cloudflare Access JWTs.** Add a small middleware
(`application/auth/cf_access.py`) that, when `CF_ACCESS_TEAM_DOMAIN` and
`CF_ACCESS_AUD` are set in `.env`, verifies the `CF-Access-Jwt-Assertion`
header against Cloudflare's JWKs (cached in-memory with 1h TTL) and
stamps the verified `email` claim onto the request context. Caddy already
forwards this header.

When CF Access JWT validation is enabled, the backend stops trusting the
`X-Auth-Email` / `Cf-Access-Authenticated-User-Email` headers entirely —
the verified JWT claim is the only identity source.

### Caddyfile change

Caddy already forwards Access JWTs by default; no change required for the
hostname path. Keep widget endpoint bypasses (`/stream`, `/api/search`,
`/api/feedback`) as-is — those paths skip Access at the CF edge so no JWT
will arrive, and they continue to use agent-key auth on the backend.

### Settings

New env vars in `.env-template`:
- `ALLOW_ANONYMOUS_TOKEN` (default `false`)
- `CF_ACCESS_TEAM_DOMAIN` (e.g. `aztec.cloudflareaccess.com`, optional)
- `CF_ACCESS_AUD` (the application's AUD tag, optional)

Setting both CF_ACCESS_* variables enables strict JWT validation; either
unset means the backend continues trusting the `X-Auth-Email` header (dev
mode).

### Tests

- Unit test for the JWT validator: signature mismatch → 401, wrong AUD →
  401, expired → 401, valid → identity extracted.
- Integration test: `/api/generate_token` → 404 when
  `ALLOW_ANONYMOUS_TOKEN=false`.
- Integration test: a request with a forged `X-Auth-Email` header but no
  valid CF JWT → 401 (when CF_ACCESS_* set).

### Rollback

Each of the three is independently reversible. The CF Access validation
defaults to off, so simply not setting the env vars keeps current behavior.

---

## Sequencing & deployment

The source-model track (steps 1-4) and the auth-hardening track (step 5)
touch disjoint surfaces and can ship in parallel. Track A is the urgent
one — track B can move at a steadier cadence.

### Track A — source visibility

| Day | Step | Surface |
|---|---|---|
| Day 0 | Step 1 hot-fix shipped | Restores MCP grounding for existing keys |
| Day 0 | Step 2 shipped (same PR or fast-follow) | Existing Discord users pick up canonical source order on next `/mcp-key` |
| Day 1–2 | Step 3 PR opened with migration + batched resolver | All entry points switched to `list_visible_by_ids` |
| Day 3 | Step 3 deployed (single migration, no separate backfill) | Closes findings 1 and 3 architecturally |
| Day 3-4 | Step 4 audit PR | Catches any missed dereference paths |

### Track B — auth hardening

| Day | Step | Surface |
|---|---|---|
| Day 1 | Step 5a — issue tokens with `exp` | New tokens have expiry; old still work |
| Day 3 | Step 5b — disable `/api/generate_token` in prod | Gated on ALLOW_ANONYMOUS_TOKEN |
| Day 5 | Step 5c — enforce `verify_exp` | All circulating tokens have `exp` by now |
| Day 6 | Step 5d shipped, CF_ACCESS_* unset | Validation code present but inert |
| Day 7 | CF_ACCESS_* enabled in prod | Closes finding 3 |

Each numbered step is its own PR. Step 1 + step 2 can be a single small PR
since they're both touching the MCP path; step 3 deserves careful review
since it touches the retrieval hot path; step 5d should land with the
env vars unset so the code path can be exercised in staging before flipping
in prod.

## Out of scope (intentionally)

- Findings 4 (ZIP bomb validation) and 5 (SSRF redirect/rebinding) — keep
  for a follow-up. Both have meaningful but not urgent risk given the
  Cloudflare Access gate on `/api/upload` and the rare use of URL ingest.
- Edge abuse controls (rate limits, queue limits, spend limits) — separate
  workstream; doesn't depend on this one.
- Replacing the JWT-based session auth entirely with CF Access JWT only.
  Considered, but the JWT path is needed for the agent-key flow and for
  dev compose where there's no Cloudflare in front. Step 3 narrows the
  attack surface without ripping out the existing path.

## Validation criteria

The plan is done when all of the following are true on prod:

- `/stream` with the `joshc` MCP key returns ≥ 6 sources (matching the
  widget agent baseline) and a correctly-cited answer.
- Re-running `/mcp-key` for an existing Discord user updates their agent's
  `extra_source_ids` order to match the current `AZTEC_SOURCE_IDS`.
- Every UUID supplied in an `active_docs` payload or agent CRUD body is
  resolved through `SourcesRepository.list_visible_by_ids` before reaching
  the retriever or being persisted.
- A request with `active_docs=[private-uuid-of-other-user]` returns 403,
  not a silent drop.
- `/api/generate_token` returns 404 in prod.
- `CF_ACCESS_TEAM_DOMAIN` is set and a request with a forged `X-Auth-Email`
  but no valid `CF-Access-Jwt-Assertion` returns 401.
- The eval harness (`scripts/eval/eval_retrieval.py --mode stream`) passes
  with no regression in the widget agent path.
