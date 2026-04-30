# Plan: anonymize user identifiers in stored usage data

## Goal

Remove the direct, plaintext link between **Discord users and their
stored conversations / questions / responses** while keeping the
existing user-facing features intact (`/mcp-key` re-provisioning,
`/forget-me`, data export, retention purge, per-user analytics
counters).

> "Anonymize when possible" — perfect anonymization is impossible
> while still serving each user their own conversation history. The
> bar is: **a database snapshot alone should not let an attacker name
> the people behind individual prompts.** Reconstruction must require
> a server-side secret.

## Audit — what is currently identifiable

| Where | Value today | Privacy issue |
|---|---|---|
| `agents.mcp_provider_user_id` | raw Discord numeric ID (e.g. `123456789012345678`) | Discord IDs are stable per-account; trivially looked up via the Discord API to recover the username |
| `agents.name` | `"Aztec MCP - <display_name>"` (truncated 50ch) | Plaintext Discord display name embedded in the row |
| `agents.user_id` | `"discord:<id>"` | Same Discord ID, longer prefix |
| `conversations.user_id`, `conversation_messages.user_id` | `"discord:<id>"` | Direct link from every prompt/response row to a Discord identity |
| `user_logs.user_id`, `stack_logs.user_id`, `token_usage.user_id` | `"discord:<id>"` | Same, for operational logs |
| `users.user_id` | `"discord:<id>"` | Parent row carrying the same identifier |

Web/dashboard users (`AUTH_TYPE=simple_jwt`) all share `user_id="local"`
already — effectively one shared bucket, no per-user PII. **Out of scope.**

The docs-widget on docs.aztec.network calls the backend through a
single shared API key whose owner is one of the indexed agents. All
widget conversations land under that one agent's `user_id`. No
per-end-user identifier ever reaches the DB. **Already anonymous, no
work needed.**

## Approach: HMAC pseudonymization with a server-side pepper

Instead of writing `discord:<id>` directly, compute

```
pseudo_user_id = "discord_p_v1:" + hex(hmac_sha256(USER_ID_PEPPER, raw_discord_user_id))[:32]
```

and use that everywhere the schema currently stores `discord:<id>`.
The `_v1` infix is a **rotation seam** — if we ever change pepper or
hash params, new IDs become `_v2:` and old + new can coexist while a
backfill drains. Cheap to add now, expensive to retrofit later.

`agents.mcp_provider_user_id` stores the bare HMAC hex (no prefix —
the prefix lives in `user_id`).

### Why HMAC + pepper instead of plain hash

Discord user IDs are 18-digit numbers — total ID space ≈ 2^60. A plain
SHA-256 lets an attacker with the database brute-force every Discord
ID and rebuild the mapping in minutes. HMAC with a server-side secret
adds a key the attacker doesn't have; without that key the hashes are
opaque even given the full ID space.

### Why HMAC instead of random-alias mapping table

A mapping table (each Discord user → random UUID, stored in one place)
is stronger — deleting the mapping orphans the data with no path back.
HMAC is weaker but:

- **No schema change**: every existing column keeps its current type.
- **Idempotency for free**: re-provisioning `/mcp-key` recomputes the
  same pseudonym; we don't need a "find or create alias" path.
- **`/forget-me` is unchanged** — compute the pseudo, delete by it.
- **Migration is in-place** — UPDATE every row's `user_id` once.

The mapping-table approach can layer on later if the threat model
tightens. HMAC gets us 80% of the gain at 20% the surgery.

### Why throw away the username

`agents.name = "Aztec MCP - alice"` is the only place a human-readable
identifier reaches the DB. Drop it: `agents.name = "Aztec MCP"`. The
Discord client already shows the user their own ephemeral key followup
with their username in the chat header — re-printing it in the agent
name has no operational value, just leaks PII.

## Concrete code changes

### 1. New env var + helper

`.env-template`: add

```
# Server-side secret used to HMAC user identifiers before storage.
# 32+ bytes of entropy. NEVER rotate carelessly — rotation orphans
# every existing pseudonym (existing users would re-provision under a
# new alias). Generate once: openssl rand -hex 32
USER_ID_PEPPER=
```

`application/core/settings.py`: add `USER_ID_PEPPER: Optional[str] = None`.

New module `application/utils/pseudonyms.py`:

```python
def pseudonymize_provider_user_id(provider: str, raw_id: str) -> str:
    """Return the HMAC-truncated form used for storage.

    Raises if USER_ID_PEPPER is unset — fail closed so a misconfigured
    deploy never accidentally stores raw IDs.
    """
```

and a `canonical_user_id(provider, raw_id) -> str` that returns
`"<provider>_p:<hash>"` for use in the `user_id` column.

### 2. `/api/internal/create_mcp_key`

Compute the pseudonym at the boundary; pass that into
`AgentsRepository.upsert_mcp_key`. `agents.mcp_provider_user_id` stores
the HMAC, not the raw Discord ID. `agents.user_id` becomes
`"discord_p:<hash>"`. `agents.name` becomes a constant `"Aztec MCP"`
(no username embedded).

### 3. `/api/internal/forget_discord_user`

Compute the same pseudonym from the request payload, delete by it.
Endpoint contract is unchanged for callers (still takes `discord_user_id`).

### 4. Discord bot (`extensions/discord/bot.py`)

No code change in the bot itself — it keeps sending the raw Discord
ID to the trusted backend over the internal compose network. The
trust boundary moves: only the backend writes to the DB, and only
the backend computes the HMAC. The bot doesn't need the pepper.

### 5. Stream / answer routes

When a Discord-keyed agent answers a question, the `user_id` recorded
on `conversations` / `conversation_messages` derives from the agent's
own `user_id` column, which is now already the pseudonym. No change
to `application/api/answer/routes/*` should be needed — this falls
out for free if the agent's `user_id` is stored as the pseudo.

## One-off migration

A new Alembic migration (`0023_pseudonymize_discord_user_ids.py`) that
runs in **one transaction per old user_id** with this exact ordering
(parent-first-and-last sandwich, so we never violate the FK and never
lose `users.agent_preferences`):

1. **Pre-create** the pseudonymous `users` row with
   `INSERT ... ON CONFLICT (user_id) DO NOTHING` carrying the same
   `agent_preferences`/`created_at`/`updated_at` as the old row.
2. **Update every user-keyed child table's `user_id` to the new
   pseudo.** All 19 tables below — there is no UPDATE cascade in
   Postgres, so we must enumerate every one (codex review:
   "conversation_messages, shared_conversations, pending_tool_state
   cannot be skipped just because delete-time cascades exist"):

   `prompts, user_tools, token_usage, user_logs, stack_logs,
   agent_folders, sources, agents, attachments, memories, todos,
   notes, connector_sessions, conversations, conversation_messages,
   shared_conversations, pending_tool_state, workflows,
   workflow_runs`.

   Plus `agents.mcp_provider_user_id` (bare HMAC; not user-keyed but
   needs the same scrub).

3. **Delete** the old `users` row last. The trigger on every child
   table will have already created the new `users` row when we
   updated children in step 2 (defense-in-depth — pre-creating in
   step 1 keeps `agent_preferences` intact rather than letting the
   trigger create a fresh empty row).

4. **Strip `agents.name`** to a constant `"Aztec MCP"` for every row
   where `mcp_provider = 'discord'`. (No suffix, no last-4-of-pseudo
   — operators can pivot on `id` / `last_used_at` / a prefix of
   `mcp_provider_user_id` in psql.)

The migration **asserts `USER_ID_PEPPER` is non-empty** and aborts
otherwise. It is run by hand on the prod box, **not** as part of
routine `alembic upgrade head`, since it depends on the env var being
set first and is destructive.

> **Note on FK enforcement.** The Core ``Table`` declarations in
> `models.py` omit the `users` FKs, but the database has them per
> migration `0015_user_id_fk` (with `ON DELETE RESTRICT` and a
> `BEFORE INSERT OR UPDATE OF user_id` trigger that creates missing
> `users` rows). The DB is the authority — relying on the trigger
> alone (skipping step 1) would still work but would replace the
> existing `agent_preferences` JSON with the column default, losing
> any UI-set preferences. Step 1 prevents that.

## What stays plaintext (intentionally)

- **Conversation prompts / responses themselves.** Encrypting message
  bodies at rest is a separate, much larger project (key management,
  search index implications). Out of scope.
- **Source URLs / chunk metadata** in `conversation_messages.sources`.
  These describe Aztec docs, not users.
- **API keys** in `agents.key`. They're already secrets, just
  high-entropy random tokens.

## Decisions resolved during codex review

These were open questions in v1 of this plan; codex review picked the
side. Recorded here so they don't get re-litigated in implementation:

1. **Pepper rotation** → versioned format day 1 (`discord_p_v1:`).
   Rotation seam is cheap now and expensive to retrofit.
2. **Truncation length** → 128 bits / 32 hex. Plenty.
3. **Operational logs** → pseudonymize, **don't NULL**.
   `application/usage.py` and the analytics routes still need `user_id`
   for per-user attribution; NULL would break debugging and weaken
   `/forget-me` precision.
4. **mcp_provider_user_id unique index** → in-place update is safe.
   The partial unique index `(mcp_provider, mcp_provider_user_id,
   mcp_purpose) WHERE mcp_provider IS NOT NULL` from migration 0003
   tolerates a deterministic 1:1 HMAC swap.
5. **agents.name** → constant `"Aztec MCP"`. No pseudonymous suffix.
6. **Backfill scope** → in-place migration covering all 19 user-keyed
   tables plus `agents.mcp_provider_user_id`. Tens of Discord agents
   in prod; the cost is trivial.
7. **Bot pseudonymization** → no. Bot keeps sending the raw Discord
   ID over the internal compose network; pepper stays scoped to the
   backend container.
8. **`USER_ID_PEPPER` validation** → enforced at **settings load**
   (via `application/core/settings.py`), with a defensive assert in
   the helper as belt-and-suspenders.

## Delivery as a single PR

This goes out as **one bundled PR** (no helper-only / write-path-only
splits). Reasoning: a half-deployed state — where the helper exists
but isn't wired in, or is wired in but the migration hasn't run — is
strictly worse than either fully-old or fully-new. Splitting into
sequential PRs creates a window where the prod DB has mixed
plaintext-and-pseudo rows, which makes `/forget-me` skip whichever
half doesn't match the bot's request shape. Atomic is safer.

Order of work inside the single PR:

1. Helper module + settings validation + unit tests
2. Endpoint changes + endpoint integration tests
3. Alembic migration + migration test
4. Update `forget_discord_user` test in `tests/api/test_internal_routes.py`
   to match the new pseudo path
5. Update `EXPECTED_TABLES` in the existing test if the table list
   needs to grow (it shouldn't — the pseudonymization migration's
   table list is broader, but the deletion list in
   `forget_discord_user` is unchanged)

## Test plan

The four tiers below all live in one PR. Tests get run together by
the existing `pytest` CI job — no new infra.

### Tier 1 — Helper unit tests (no DB)

`tests/utils/test_pseudonyms.py` (new file):

- **Golden vector** — fixed pepper (`"a" * 64`) and fixed raw ID
  (`"123456789012345678"`) produce a hard-coded expected output
  string pinned in the test. This is the strongest determinism
  guard: it catches algorithm drift, encoding drift (`utf-8` vs
  `ascii`), truncation drift, prefix typos, and Python `hmac`
  library version skew in one assertion. Recompute the expected
  value once during initial implementation, paste it into the test.
- **Pepper sensitivity** — same raw ID + two different peppers → two
  different outputs. Core security property; pin it explicitly.
- **Input sensitivity** — same pepper + two different raw IDs → two
  different outputs.
- **Format** — output matches regex `^discord_p_v1:[a-f0-9]{32}$`
  exactly. Catches accidental case changes, length drifts, prefix
  typos.
- **Empty/None pepper raises** — `pseudonymize_provider_user_id(...,
  pepper="")` and `..., pepper=None` both raise. Belt-and-suspenders
  against the settings guard.
- **Empty raw ID raises** — `..., raw_id=""` raises. We never want a
  wildcard pseudo for the empty user.
- **`canonical_user_id` shape** — wraps the bare HMAC in the
  `discord_p_v1:` prefix.

> **Dropped from earlier draft (per codex review):** the "raw ID is
> not a substring of the output" test. Once output is constrained
> to lowercase hex with fixed length by the format regex, the
> substring check adds no signal. Saved test for the trim
> behaviour at the endpoint layer instead — `routes.py` already
> does `.strip()` on inputs; the helper itself should not silently
> trim, so we test trim at the boundary not in the helper.

### Tier 2 — Settings boot guard (no DB)

`tests/core/test_settings_pepper.py` (new file or appended to an
existing settings test):

- **Boot fails fast when `USER_ID_PEPPER` is unset.** Importing the
  settings module with the env var unset raises a clear startup
  error mentioning `USER_ID_PEPPER`.
- **Boot fails fast on insufficient entropy.** Validate by **decoded
  byte length ≥ 16**, not string length. Reject inputs that aren't
  valid hex, and reject inputs that decode to fewer than 16 bytes.
  String-length checks accept garbage like `"x" * 32`.
- **Successful boot when pepper is set and ≥ 16 decoded bytes** —
  sanity check the happy path.

**Side-effect — test bootstrap.** Add to `tests/conftest.py` (the
session-scope setup, before any `application.*` import):

```python
os.environ.setdefault("USER_ID_PEPPER", "0" * 64)  # 32 bytes test pepper
```

Without this, every unrelated test that transitively imports
`application.core.settings` would crash because the boot guard
raises. Using `setdefault` means a real pepper from the surrounding
shell still wins; this only fills in the default for ad-hoc dev
runs and CI. **This is not a security footgun** — the value is
public, used only in test, and never written to a real DB.

> **Codex re-review caveat.** Because conftest sets the pepper
> before `application.core.settings` is first imported, the
> "unset pepper raises at boot" test in this tier **cannot** rely
> on `monkeypatch.delenv("USER_ID_PEPPER")` and re-imports — by
> then the singleton has already cached the test pepper. Drive
> the negative test by either:
>
> 1. Constructing a fresh `Settings()` instance (or whatever the
>    settings class is named) directly with no env, asserting the
>    constructor raises; or
> 2. Running the negative path in a `subprocess.run([sys.executable,
>    "-c", ...])` with `USER_ID_PEPPER` removed from the env, and
>    asserting non-zero exit + the expected stderr substring.
>
> Pick (1) — simpler, faster, no subprocess flakiness.

### Tier 3 — Endpoint integration tests (with `pg_conn` / `pg_engine`)

In `tests/api/test_internal_routes.py`, extend `TestCreateMcpKey` and
`TestForgetDiscordUser`:

**`create_mcp_key`:**

- **No raw Discord ID in agents table** — call the endpoint with
  `discord_user_id="123456789012345678"`, then `SELECT * FROM agents
  WHERE mcp_provider_user_id = '123456789012345678'` returns 0 rows.
  This is the single most important regression guard — an accidental
  revert of the pseudonymization is one logic error away from
  silently writing plaintext again.
- **Pseudo present in agents table** — same call, then
  `SELECT count(*) FROM agents WHERE mcp_provider_user_id LIKE 'discord_p_v1:%'`
  is 0 (we said the bare HMAC, no prefix, lives in this column);
  `mcp_provider_user_id` matches `^[a-f0-9]{32}$`.
- **`agents.user_id` carries the prefix** — `user_id` matches
  `^discord_p_v1:[a-f0-9]{32}$`.
- **`agents.name` is the constant** — exactly `"Aztec MCP"`. No
  username, no suffix, no Discord display name.
- **Idempotency under same Discord ID** — calling twice with the
  same `discord_user_id` produces one row (the existing
  `test_re_provision_preserves_key_and_refreshes_sources` covers
  half of this; extend to also assert no plaintext leaked on the
  second call).
- **Idempotency under different `discord_username`** — calling twice
  with the same numeric ID and different usernames still produces
  one row, and `agents.name` is the constant `"Aztec MCP"` after
  both calls.
- **Distinct Discord IDs → distinct pseudos** — two calls with
  different `discord_user_id` values produce two distinct
  `mcp_provider_user_id` values (collision sanity check on the
  helper at the SQL level).

**`forget_discord_user`:**

- **End-to-end via the pseudo bridge** — provision a Discord agent
  via `/mcp-key`, insert a conversation + message under that agent,
  call `/forget-me` with the *raw* Discord ID, assert all rows are
  gone. The endpoint's request shape is unchanged (callers still
  pass the raw ID); only the storage representation changed.
- **No plaintext lookup path** — reuse the same `EXPECTED_TABLES`
  set the existing `TestForgetDiscordUser` already pins; for each
  table assert that a `WHERE user_id = 'discord:<raw>'` query
  returns 0 rows post-forget. Reusing the existing set keeps the
  positive (response shape) and negative (no-plaintext) assertions
  in sync — codex flagged that maintaining two parallel 17-table
  lists invites drift.

**Strengthened idempotency** (codex review): when `/mcp-key` is
called twice with the same Discord ID and *different* usernames,
assert in one test that **all four** of these are unchanged across
the two calls — `agents` row count is exactly 1, `agents.api_key`,
`agents.mcp_provider_user_id`, `agents.user_id`. The original "name
is constant" check catches less than half the bug surface here.

**Three-property contract test** (codex review — most important
single test in this tier):

```python
def test_pseudonymize_create_forget_parity(self, pg_conn):
    """If create writes a pseudo and forget computes a different
    pseudo, /forget-me silently fails. Lock the contract end-to-end.
    """
    # 1. /mcp-key with raw ID → row exists, raw not findable, pseudo findable
    # 2. /forget-me with same raw ID → row gone
    # 3. SELECT for raw ID returns 0 in EVERY user-keyed table
```

This is the single most important regression guard for this PR. If
the helper drifts between the create and forget paths, this fails.

**Answer-path propagation test** (codex review — agent-row tests
alone don't prove conversation rows inherit the pseudo):

The `extensions/discord/bot.py` flow only POSTs to
`/api/internal/create_mcp_key`. From there `/stream` identity
comes from the agent row via
`StreamProcessor._get_data_from_api_key()` — which means the
pseudo has to *propagate* through the answer pipeline into
`conversations.user_id` and `conversation_messages.user_id`.

> **Codex re-review note on the right test seam.** The existing
> `tests/api/answer/routes/test_stream.py` still depends on a
> removed `mock_mongo_db` fixture, so "extend the existing stream
> tests" is a trap. The useful seam is patching
> `StreamProcessor.build_agent` and `LLMCreator.create_llm` while
> letting `application/api/answer/services/conversation_service.py`
> write to the ephemeral Postgres. "Stub retriever" alone isn't
> the right surface.

```python
def test_stream_writes_pseudonymous_conversation_user_id(self, pg_conn, monkeypatch):
    """Provision an MCP key, hit /stream with that key,
    confirm the resulting conversations + conversation_messages rows
    carry the pseudo, NOT discord:<raw>.
    """
    # 1. /mcp-key with discord_user_id="42" → returns api_key
    # 2. monkeypatch StreamProcessor.build_agent  → returns a stub
    #    monkeypatch LLMCreator.create_llm        → returns a stub
    #    (let conversation_service still write rows to pg_conn)
    # 3. POST /stream with that api_key
    # 4. SELECT user_id FROM conversations WHERE api_key = ...
    #    → matches '^discord_p_v1:[a-f0-9]{32}$'
    # 5. Same for conversation_messages.user_id
```

Without this, a regression where the stream path falls back to
plaintext (e.g. someone reintroduces a `f"discord:{raw}"` pattern
elsewhere) ships green.

### Tier 4 — Migration test (with `pg_engine`)

Two layers (codex review caught: callable-only is good for coverage
but can drift from the actual migration's SQL / ordering / imports —
need a thin integration test against the real Alembic file too).

`tests/storage/db/test_migration_0023_pseudonymize.py` (new file).

**Layer A — callable unit tests.** The migration ships its logic
behind an importable function `do_pseudonymize_discord_users(conn)`
that the migration's `upgrade()` calls. Tests target the function
directly — no Alembic fight, no fixture hacking.

Add a **seed factory** in the same test module
(`_seed_discord_user(conn, raw_id, **overrides)`) that creates one
valid row per touched table for a given Discord identity, satisfying
all the NOT NULL constraints (`agents.name`, `agents.status`,
`prompts.content`, `notes.content`, `memories.path/content`,
`workflow_runs.workflow_id`, etc.). Without a factory, hand-seeding
19 tables in every test devolves into a pile of brittle fixtures —
codex flagged this explicitly. Make the factory a one-liner per
test.

Tests using the factory:

- **Plaintext-free post-state** — `_seed_discord_user(conn, "99")`
  in every table, run the helper, assert
  `SELECT count(*) FROM <table> WHERE user_id LIKE 'discord:%'` is
  `0` in every user-keyed table. Loop over the table list so
  forgetting to add a new table to the migration fails loudly.
- **Move-not-copy contract** (replaces "counts unchanged" — codex
  flagged the original was too weak; a buggy migration that hits
  `NOT NULL` and rolls back also satisfies "counts unchanged"). For
  each table:
  1. Seed rows under the target Discord ID **and** at least one
     **control row** under an unrelated `user_id` (e.g. `"local"`).
     Without a control row, "total count unchanged" is implied by
     parts 2 + 3 below and adds no signal — codex flagged this.
  2. After: `count(*) WHERE user_id = old_raw` is `0`.
  3. After: `count(*) WHERE user_id = new_pseudo` equals the old
     raw count from *before*.
  4. After: control row's `user_id` is unchanged. Catches bugs
     where the migration's `WHERE` clause is too broad and
     rewrites unrelated rows.
- **`agent_preferences` survives** — seed `users.agent_preferences =
  '{"pinned": ["x"]}'`, run, assert the new pseudo row's
  `agent_preferences` is the seed value. Whole point of the
  parent-first-and-last sandwich.
- **`agents.name` stripped only for Discord agents** — seed two
  rows: one with `mcp_provider='discord'` and name
  `"Aztec MCP - alice"`, one with `mcp_provider IS NULL` and name
  `"Aztec MCP - bob"`. After: first is `"Aztec MCP"`, second is
  unchanged. Don't touch non-Discord rows.
- **mcp_provider_user_id swap** — seed
  `agents.mcp_provider_user_id = '99'`; post-migration column
  matches `^[a-f0-9]{32}$` and is not `"99"`.
- **Old `users` row gone** — seed `users.user_id = 'discord:99'`;
  post-migration that row is gone, the new pseudo row is present.
- **Idempotency** — call the helper twice on the same DB. Second
  call must be a no-op (no double-pseudonymization, no errors).
- **Empty-pepper aborts before any UPDATE** — run the helper with
  the env unset; assert raise; assert no row was touched (run
  `SELECT count(*) WHERE user_id LIKE 'discord:%'` before and after
  and confirm equal).

**Layer B — one Alembic integration test.** Confirms the actual
`0023_pseudonymize_discord_user_ids.py` file's `upgrade()` works
when invoked through Alembic's machinery. Without this, the
callable-only tests can pass while the migration file itself has a
broken import, missing `op.execute`, or wrong revision header.

> **Codex re-review caught the wrong sequence in v1 of this section.**
> Upgrading to head, *then* seeding, *then* upgrading again never
> exercises `0023`'s logic — the second run is trivially a no-op
> because head already includes it. The right sequence is:

```python
def test_alembic_upgrade_through_0023_runs(postgresql, _alembic_ini_path):
    """Run alembic upgrade to revision **just before 0023**, seed
    plaintext rows in the resulting schema, then upgrade through
    head. Verify post-conditions match the Layer A callable test.

    Confirms the migration file's upgrade() actually executes
    through Alembic — not just its extracted callable.
    """
    # 1. alembic upgrade <prev_rev>      ← schema before 0023
    # 2. seed_discord_user(conn, "99")   ← all 19 tables, plaintext
    # 3. alembic upgrade head            ← 0023 fires here
    # 4. assert plaintext gone, pseudo present in users + agents
```

One test is enough; we're not retesting all the Layer A
assertions, just confirming the file is well-formed and the
ordering through Alembic actually rewrites rows.

### Tier 5 — Smoke checks (manual, post-deploy on josh-box)

Ship a single runnable script alongside the migration:
`scripts/db/verify_pseudonymization.sql` — operator runs
`docker compose exec postgres psql -f /docker-entrypoint-initdb.d/...`
once after deploy and reads one summary row.

```sql
-- verify_pseudonymization.sql — run post-deploy.
-- Every count must be 0 except the final row.

\echo '=== negative sentinels (must be 0) ==='

-- "user_id still contains discord:<raw>" — across every user-keyed table:
SELECT 'users'                AS tbl, count(*) FROM users               WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'conversations',          count(*) FROM conversations       WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'conversation_messages',  count(*) FROM conversation_messages WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'user_logs',              count(*) FROM user_logs           WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'stack_logs',             count(*) FROM stack_logs          WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'token_usage',            count(*) FROM token_usage         WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'attachments',            count(*) FROM attachments         WHERE user_id LIKE 'discord:%'
-- (extend with the remaining 12 user-keyed tables — full list in PLAN)
;

-- "agents.mcp_provider_user_id is still all-digits" → still a Discord snowflake.
-- This is a NEGATIVE sentinel; the new format is 32-char lowercase hex.
SELECT count(*) AS still_raw_snowflake
FROM agents
WHERE mcp_provider = 'discord' AND mcp_provider_user_id ~ '^[0-9]+$';

-- "agents.name still embeds a username" — should be exactly 'Aztec MCP'.
SELECT count(*) AS still_named_with_username
FROM agents
WHERE mcp_provider = 'discord' AND name <> 'Aztec MCP';

\echo '=== positive sentinels (must be > 0 if any Discord users exist) ==='

-- Two distinct shapes to verify, since the column types differ:
-- 1. user_id columns carry the prefixed form `discord_p_v1:<32hex>`.
-- 2. agents.mcp_provider_user_id carries the BARE 32-char hex (no prefix).
-- Codex re-review flagged that checking only one shape would misread
-- a correctly migrated deployment as broken in the other column.

SELECT count(*) AS pseudo_users
  FROM users WHERE user_id LIKE 'discord_p_v1:%';

SELECT count(*) AS pseudo_agents
  FROM agents
  WHERE mcp_provider = 'discord' AND mcp_provider_user_id ~ '^[a-f0-9]{32}$';
```

Run end-to-end smoke in Discord:

1. `/mcp-key` in the Noir guild → get a fresh key.
2. Configure an MCP client (Claude Desktop) with the key + the prod
   `API_URL`; ask one question, confirm a successful round-trip.
3. `/forget-me` → confirm the response says agents=1 / conversations≥1.
4. Re-run the SQL above — counts unchanged from baseline (the
   user's rows are gone but the structural numbers stay zero).

### What's intentionally NOT tested

- **Pepper rotation flow.** No `_v2:` suffix exists yet; that's a
  future PR if/when rotation becomes necessary.
- **Performance of the migration on prod-scale data.** Tens of
  agents in prod today, low millions of rows total — far below
  anything Alembic can't handle in a single transaction. If the row
  count grew 100×, this test would need to be added.
