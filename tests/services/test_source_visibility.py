"""Tests for application.services.source_visibility.

Exercises the partition into visible / missing / invalid for the canonical
inputs callers actually send: a mix of UUIDs, malformed strings, "default"
placeholders, and duplicates. Visibility is owned-OR-public.
"""

from __future__ import annotations

import uuid as _uuid

from sqlalchemy import text as _sql_text

from application.services.source_visibility import (
    ResolvedSources,
    SourceVisibilityService,
    _normalize_inputs,
)


def _set_public(conn, source_id: str, value: bool) -> None:
    conn.execute(
        _sql_text("UPDATE sources SET is_public = :v WHERE id = CAST(:i AS uuid)"),
        {"v": value, "i": source_id},
    )


def _make_source(conn, name: str, *, user_id: str, public: bool = False) -> str:
    from application.storage.db.repositories.sources import SourcesRepository

    row = SourcesRepository(conn).create(name, user_id=user_id)
    if public:
        _set_public(conn, row["id"], True)
    return str(row["id"])


# ---------------------------------------------------------------------------
# Pure normalization (no DB needed) — partitions, dedups, drops placeholders
# ---------------------------------------------------------------------------


class TestNormalizeInputs:
    def test_partitions_uuid_vs_malformed(self):
        valid_a = str(_uuid.uuid4())
        valid_b = str(_uuid.uuid4())
        well_formed, malformed = _normalize_inputs(
            [valid_a, "abc", valid_b, "not-a-uuid"],
        )
        assert well_formed == [valid_a, valid_b]
        assert malformed == ["abc", "not-a-uuid"]

    def test_drops_default_placeholder(self):
        valid = str(_uuid.uuid4())
        well_formed, malformed = _normalize_inputs(["default", valid, "default"])
        assert well_formed == [valid]
        assert malformed == []

    def test_drops_none_and_empty(self):
        valid = str(_uuid.uuid4())
        well_formed, malformed = _normalize_inputs([None, "", "  ", valid])
        assert well_formed == [valid]
        assert malformed == []

    def test_dedups_preserving_first_occurrence(self):
        a = str(_uuid.uuid4())
        b = str(_uuid.uuid4())
        well_formed, _ = _normalize_inputs([a, b, a, b])
        assert well_formed == [a, b]

    def test_uuid_object_accepted(self):
        u = _uuid.uuid4()
        well_formed, malformed = _normalize_inputs([u])
        assert well_formed == [str(u)]
        assert malformed == []


# ---------------------------------------------------------------------------
# End-to-end against pg_conn — visibility check
# ---------------------------------------------------------------------------


class TestResolve:
    def test_returns_owned_source(self, pg_conn):
        src = _make_source(pg_conn, "mine", user_id="alice")
        result = SourceVisibilityService(pg_conn).resolve("alice", [src])
        assert result.visible == [src]
        assert result.rows[src]["user_id"] == "alice"
        assert result.missing == []
        assert result.invalid == []

    def test_returns_public_source_for_other_user(self, pg_conn):
        src = _make_source(pg_conn, "aztec-docs", user_id="local", public=True)
        result = SourceVisibilityService(pg_conn).resolve("discord:42", [src])
        assert result.visible == [src]
        assert result.missing == []

    def test_invisible_private_source_lands_in_missing(self, pg_conn):
        src = _make_source(pg_conn, "alice-private", user_id="alice")
        result = SourceVisibilityService(pg_conn).resolve("bob", [src])
        assert result.visible == []
        assert result.missing == [src]
        assert result.invalid == []

    def test_malformed_uuid_lands_in_invalid(self, pg_conn):
        result = SourceVisibilityService(pg_conn).resolve("alice", ["not-a-uuid"])
        assert result.visible == []
        assert result.missing == []
        assert result.invalid == ["not-a-uuid"]

    def test_mixed_input_partitions_correctly(self, pg_conn):
        """Caller passes one of each: visible, invisible, malformed."""
        mine = _make_source(pg_conn, "mine", user_id="alice")
        not_mine = _make_source(pg_conn, "not-mine", user_id="bob")
        result = SourceVisibilityService(pg_conn).resolve(
            "alice", [mine, not_mine, "garbage"],
        )
        assert result.visible == [mine]
        assert result.missing == [not_mine]
        assert result.invalid == ["garbage"]

    def test_preserves_input_order(self, pg_conn):
        public_a = _make_source(pg_conn, "a", user_id="local", public=True)
        public_b = _make_source(pg_conn, "b", user_id="local", public=True)
        public_c = _make_source(pg_conn, "c", user_id="local", public=True)
        # Reverse so SQL heap order can't accidentally satisfy the test.
        in_order = [public_c, public_a, public_b]
        result = SourceVisibilityService(pg_conn).resolve("anyone", in_order)
        assert result.visible == in_order

    def test_empty_input(self, pg_conn):
        result = SourceVisibilityService(pg_conn).resolve("alice", [])
        assert result == ResolvedSources(
            visible=[], rows={}, missing=[], invalid=[],
        )

    def test_none_user_id_can_see_public_sources(self, pg_conn):
        """Widget bypass paths run without a verified user identity but
        should still be able to read public corpora."""
        src = _make_source(pg_conn, "public", user_id="local", public=True)
        result = SourceVisibilityService(pg_conn).resolve(None, [src])
        assert result.visible == [src]

    def test_dedup_collapses_repeated_uuid(self, pg_conn):
        src = _make_source(pg_conn, "s", user_id="alice")
        result = SourceVisibilityService(pg_conn).resolve("alice", [src, src, src])
        assert result.visible == [src]


# ---------------------------------------------------------------------------
# Repo-layer defensive shape filter
# ---------------------------------------------------------------------------


class TestRepoDefensiveFilter:
    """``SourcesRepository.list_visible_by_ids`` now drops non-UUID
    inputs before the SQL cast. Future direct callers (bypassing the
    service) can't crash the query with malformed input.
    """

    def test_malformed_string_does_not_raise(self, pg_conn):
        from application.storage.db.repositories.sources import SourcesRepository

        result = SourcesRepository(pg_conn).list_visible_by_ids(
            "alice", ["not-a-uuid"],
        )
        assert result == {}

    def test_mixed_input_returns_only_well_formed(self, pg_conn):
        from application.storage.db.repositories.sources import SourcesRepository

        src = _make_source(pg_conn, "mine", user_id="alice")
        result = SourcesRepository(pg_conn).list_visible_by_ids(
            "alice", [src, "not-a-uuid", ""],
        )
        assert src in result
        assert len(result) == 1
