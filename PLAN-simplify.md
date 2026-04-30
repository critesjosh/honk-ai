# Plan: simplify the Aztec docsgpt fork

Working from the maintainability review in 2026-04-27 chat. The review
proposed eight workstreams; I re-ranked them through a strict
simplification lens (less code OR easier to navigate, same behavior)
and dropped the items that were really "make this codebase production
grade" rather than "simplify it".

The five items below are sequenced by leverage. Items 1+2 ship as the
foundation; item 3 has the biggest LOC reduction but the highest blast
radius (touches every route); items 4+5 are cognitive simplification
of large files. Two items from the original review are explicitly
**skipped** — see the bottom of this file.

## Status

| # | Item | PR | State |
|---|---|---|---|
| 1 | Two validation bugs + `SourceVisibilityService` | [#50](https://github.com/critesjosh/docsgpt-aztec/pull/50) | ✅ Merged 2026-04-27 |
| 2 | Split `worker.py` into per-job modules | [#51](https://github.com/critesjosh/docsgpt-aztec/pull/51) | ✅ open, CI green, ready to merge |
| 3 | Error-shape standardisation (BadRequest/Forbidden/etc.) | — | ⏸ Deferred. Weave into other PRs over time rather than as its own sweeping change. |
| 4 | Split `agents/routes.py` into routes / service | — | 🔵 **Next** |
| 5 | Operational clarity / docs consolidation under `docs/aztec-fork/` | — | ⏸ Defer to a docs-only PR when convenient |

---

## 1. Validation bugs + `SourceVisibilityService` — DONE

**Shipped in PR #50.**

Two bugs from the maintainability review:
- `create_agent` silently dropped non-UUID entries in `sources` while
  `update_agent` returned 400 for the same input.
- `/stream` `active_docs` with malformed UUID surfaced as a 500 / generic
  400 via the Postgres `CAST(:ids AS uuid[])` cast.

Plus a refactor: 3 near-duplicate inline visibility checks
(`stream_processor`, `agents/routes.py`, `_get_data_from_api_key`)
consolidated into one
`application/services/source_visibility.py:SourceVisibilityService`
that returns a `ResolvedSources` dataclass with `visible / missing /
invalid` partitions. Caller decides policy (untrusted JWT path → 400
on invalid, 403 on missing; curated agent path → silent skip).

Also: defensive `looks_like_uuid` filter at
`SourcesRepository.list_visible_by_ids` so any future direct caller
(bypassing the service) can't crash the SQL cast.

Codex review caught two follow-ups (commit `367cd88b`):
- A JSON `"sources": "not-a-uuid"` (string, not list) was iterated
  character-by-character. Both create and update now reject non-list
  with a 400 before passing to the service.
- The malformed-`active_docs` flow only had service-layer + live
  smoke coverage. Added direct `_configure_source` unit tests for
  ValueError + PermissionError paths.

## 2. Split `worker.py` into per-job modules — IN REVIEW

**PR #51, open, CI green.**

`application/worker.py` was a 1,578-line monolith mixing nine
concerns. Split into `application/workers/*` with a 75-line
re-export shim left at `worker.py` for back-compat.

| Module | LOC | Concern |
|---|---|---|
| `workers/zip_safety.py` | 140 | ZIP slip + zip-bomb defenses |
| `workers/agent_runtime.py` | 140 | `run_agent_logic` |
| `workers/ingest.py` | 196 | Local-file ingest |
| `workers/reingest.py` | 344 | Incremental re-ingest |
| `workers/connectors.py` | 449 | Remote/OAuth connectors + sync |
| `workers/attachments.py` | 115 | `attachment_worker` |
| `workers/webhooks.py` | 65 | `agent_webhook_worker` |
| `workers/mcp_oauth.py` | 102 | MCP OAuth dance |
| `workers/_helpers.py` | 131 | Shared utilities |

7 test files updated because `monkeypatch.setattr(worker, X, ...)`
patches don't reach call sites that bind X from the new submodules
directly.

Codex flagged that the shim creates two valid import paths and the
patch-compat gap. Addressed in commit `2501fc8a` by marking the shim
as TEMPORARY in the docstring + warning about the patch behaviour.

## 3. Error-shape standardisation — DEFERRED, OPPORTUNISTIC

**Not its own PR.**

The codebase has hundreds of:

```python
return make_response(
    jsonify({"success": False, "message": "..."}),
    400,
)
```

A custom exception hierarchy + Flask error adapter would let routes
just `raise BadRequest("...")` instead. Real LOC reduction.

But this is sweeping (touches every route) and mechanical. The cost
of doing it as one big PR is high (review fatigue, merge conflicts);
the value-per-PR is low until you've done all of it. Better to convert
each route file's error shape **opportunistically** as part of
whatever change you're already making there. After ~3-4 such
piecemeal conversions the pattern becomes obvious to follow.

If we change our mind and want to do this as one PR: probably 1-2
days, mostly find-and-replace with code review for each route's
specific error semantics.

## 4. Split `agents/routes.py` — NEXT

**Not started.**

`application/api/user/agents/routes.py` is the second 1,500+ line
file in the codebase. Now easier to split because PR #50 introduced
`SourceVisibilityService` — the visibility logic that used to be
inline can be wired through the service in one place per concern.

Proposed shape:

- `application/api/user/agents/routes.py` — HTTP routes only
  (parse request → call service → render response). ~300 LOC.
- `application/api/user/agents/service.py` — create / update / delete /
  adopt / pin / share business logic. The bulk of the current file.
- Optional: `application/api/user/agents/serializers.py` —
  `_format_agent_output` and similar response-shaping helpers if
  they're large enough to warrant.

Same compat-shim pattern as PR #51 isn't needed here because no
external callers import from `agents/routes.py`; tests are the
only consumers and they already use Flask test clients (route-level)
rather than calling the handler functions directly. So this PR can
just move code without a re-export shim.

Estimated effort: half-day. Test churn should be minimal — most
tests already target `/api/...` paths via the Flask test client.

## 5. Docs consolidation under `docs/aztec-fork/` — DEFER

**Not started.**

The Aztec fork has accumulated fork-specific behaviour across
`CLAUDE.md`, `AZTEC_SETUP.md`, `PLAN-auth-source-fix.md`, scattered
inline comments. A `docs/aztec-fork/` directory with one page per
concern would help anyone onboarding to this fork. Not urgent;
docs-only PR when convenient.

Suggested pages:
- `docs/aztec-fork/sources.md` — public-source backfill, canonical
  source ordering, visibility model
- `docs/aztec-fork/mcp-provisioning.md` — Discord `/mcp-key` flow,
  `create_mcp_key` semantics, agent ownership
- `docs/aztec-fork/auth-boundary.md` — Cloudflare Access, JWT
  session, agent-key auth for widget endpoints
- `docs/aztec-fork/deployment.md` — hub vs dev compose, "source
  edits don't cross composes" gotcha

---

## Explicitly SKIPPED from the original review

These were in the maintainability review but **rejected as
over-engineering for a simplification PR**:

- **Split `stream_processor.py`** into config / source / retriever /
  history / attachments / tools modules. The `StreamProcessor`
  pipeline shares state (`self.source`, `self.history`,
  `self.agent_config`) across phases — splitting creates artificial
  seams and you'd end up passing a giant config object between the
  new modules. That's not simpler, it's pretend-OO.
- **Pydantic domain types** for `AgentCreateRequest` /
  `ResolvedSources` / etc. The codebase already mixes Flask-RESTX
  models, SQLAlchemy rows, and ad-hoc dicts — adding Pydantic makes
  it three type systems coexisting. The "fragile dict mutation"
  pain is real but local; fix it where it bites, not as a
  cross-cutting initiative.
- **Move all SQL behind repositories**. Agree where there's
  duplication; disagree for one-shots. Inline SQL in
  `create_mcp_key` (the `is_public=TRUE` filter) and `AdoptAgent`
  (the system-template lookup) is justified — adding repo methods
  for one caller is anti-pattern. Move SQL when there's 2+ callers.

## Reference: what the original review proposed

The review at the top of the chat had eight items. Mapping to this plan:

| Original item | Outcome |
|---|---|
| 1. Source visibility service + two validation bugs | ✅ shipped (PR #50) |
| 2. Thin `agents/routes.py` (split into service/serializers/validators) | 🔵 next as item 4 above |
| 3. Break up `worker.py` | ✅ in review (PR #51) |
| 4. Standardize errors (BadRequest/Forbidden/...) | ⏸ deferred to opportunistic |
| 5. Move direct SQL behind repositories | ⏸ situational; do where there's real duplication |
| 6. Introduce Pydantic request/domain types | ❌ skipped |
| 7. Characterization tests before refactors | Partial — gaps already covered by PRs #46/#47 tests; one open gap is the `pytestmark = pytest.mark.skip` on `tests/api/answer/test_stream_processor.py` (Mongo cutover debt) |
| 8. Operational clarity / docs | ⏸ deferred as item 5 above |
