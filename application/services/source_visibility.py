"""Source-visibility resolution service.

Single gateway for "given these source UUIDs and a user identity, which
ones are visible?" Replaces three near-duplicate inline implementations
that grew during the source-visibility track:

- ``stream_processor._configure_source`` direct ``active_docs`` path
- ``agents/routes.py:_resolve_visible_sources`` helper
- ``stream_processor._get_data_from_api_key`` agent source resolver

Each used a slightly different style to do the same thing (call
``SourcesRepository.list_visible_by_ids``, partition results, decide
how to react to missing IDs). The diverging behaviour was a real
maintenance risk: e.g. ``create_agent`` silently dropped non-UUID
entries while ``update_agent`` returned 400 for them. Centralising the
policy here keeps the shape check, dedup, and order-preservation in
one place.

The service deliberately doesn't translate failures into HTTP
responses or exceptions — that's the caller's policy:

- **Untrusted request input** (JWT user passing ``active_docs`` in the
  body, agent CRUD with explicit source UUIDs): caller should 400 on
  ``invalid`` and 403 on ``missing``.
- **Curated agent list** (``agents.source_id`` + ``extra_source_ids``
  set by the trusted MCP provisioning endpoint): caller should
  silently skip both ``invalid`` and ``missing`` because the agent
  itself is the source of truth and a missing UUID just means the
  source got deleted out from under it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import Connection

from application.storage.db.base_repository import looks_like_uuid
from application.storage.db.repositories.sources import SourcesRepository


@dataclass(frozen=True)
class ResolvedSources:
    """Result of resolving a list of requested source IDs to visibility.

    Three disjoint partitions of the input (after deduplication):

    - ``visible``: input-order list of UUIDs the user is allowed to read.
      ``rows[uuid]`` gives the source row for each.
    - ``missing``: input-order list of well-formed UUIDs that exist or
      don't, but in either case aren't visible to the user. Untrusted
      callers should 403 these; curated callers should drop them.
    - ``invalid``: input-order list of values that aren't UUID-shaped.
      Untrusted callers should 400 these; curated callers should drop
      them.

    The "default" placeholder string and ``None``/empty values are
    filtered out entirely before partitioning — they're neither valid
    nor invalid, just absent.
    """

    visible: list[str]
    rows: dict[str, dict]
    missing: list[str]
    invalid: list[str] = field(default_factory=list)

    def has_problems(self) -> bool:
        """True if any input ID was either invalid or invisible."""
        return bool(self.missing or self.invalid)


def _normalize_inputs(
    requested_ids: Iterable,
) -> tuple[list[str], list[str]]:
    """Split inputs into (well-formed UUIDs, malformed) preserving order.

    Drops ``None``, empty strings, the ``"default"`` placeholder, and
    duplicates. Returns lists of stringified IDs.
    """
    well_formed: list[str] = []
    malformed: list[str] = []
    seen: set[str] = set()
    for raw in requested_ids:
        if raw is None:
            continue
        s = str(raw).strip()
        if not s or s == "default":
            continue
        if s in seen:
            continue
        seen.add(s)
        if looks_like_uuid(s):
            well_formed.append(s)
        else:
            malformed.append(s)
    return well_formed, malformed


class SourceVisibilityService:
    """Authorize a batch of requested source UUIDs against a user.

    Construct with a Connection, call ``resolve``. Statelessly safe to
    construct per-request inside the existing ``db_session()`` /
    ``db_readonly()`` context.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def resolve(
        self,
        user_id: Optional[str],
        requested_ids: Iterable,
    ) -> ResolvedSources:
        """Partition ``requested_ids`` into visible / missing / invalid.

        ``user_id=None`` is normalised to the empty string for the SQL
        comparison — combined with the ``is_public=TRUE`` clause this
        means anonymous callers can still see public sources, which is
        the right semantic for the widget bypass paths.
        """
        well_formed, invalid = _normalize_inputs(requested_ids)
        if not well_formed:
            return ResolvedSources(
                visible=[], rows={}, missing=[], invalid=invalid,
            )
        repo = SourcesRepository(self._conn)
        rows = repo.list_visible_by_ids(user_id or "", well_formed)
        visible = [s for s in well_formed if s in rows]
        missing = [s for s in well_formed if s not in rows]
        return ResolvedSources(
            visible=visible, rows=rows, missing=missing, invalid=invalid,
        )
