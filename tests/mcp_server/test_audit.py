"""Pure-function tests for ``application.mcp_server.audit``.

We don't try to exercise the actual ``mcp_audit`` insert here — that
needs Postgres. The :func:`audit_call` context manager is exercised
end-to-end in the integration tests. What we DO test is the args
hashing, since the hash format is the durable part of the audit log
schema and operators rely on it for change-detection.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from application.mcp_server.audit import _hash_args


@pytest.mark.unit
class TestHashArgs:
    def test_hash_is_sorted_canonical(self) -> None:
        # Different dict insertion order, same canonical JSON, same hash.
        a = _hash_args({"a": 1, "b": 2})
        b = _hash_args({"b": 2, "a": 1})
        assert a == b

    def test_hash_changes_with_value(self) -> None:
        a = _hash_args({"a": 1})
        b = _hash_args({"a": 2})
        assert a != b

    def test_matches_canonical_recipe(self) -> None:
        # Lock the recipe so an audit consumer can reproduce the hash
        # offline if needed.
        args = {"query": "SELECT 1", "row_cap": 100}
        expected = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        assert _hash_args(args) == expected

    def test_handles_non_jsonable_values(self) -> None:
        from datetime import datetime, timezone

        # ``default=str`` lets us include datetimes / UUIDs without
        # blowing up. We don't promise the exact serialization here,
        # just that hashing succeeds and is deterministic.
        ts = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        assert _hash_args({"ts": ts}) == _hash_args({"ts": ts})


@pytest.mark.unit
class TestAuditCallStatusMapping:
    """``audit_call`` must mark MissingScope / StatementRejected as denied.

    Other exceptions should leave ``status=error`` so the audit log
    distinguishes "we rejected this call" from "something inside the
    handler blew up". This is the on-fail-path counterpart to the
    handler explicitly setting ``status=ok`` on the happy path.
    """

    def _captured_record(self, exc: Exception) -> dict:
        """Run audit_call against a non-existent DB, catch the raise, return
        the record. We pass an obviously-bad connection string so the
        ``finally`` INSERT fails internally (swallowed) but the in-memory
        record still reflects the right status.
        """
        from application.mcp_server.audit import audit_call

        bad_conn = "postgresql://nonexistent:0/__never__"
        captured: dict | None = None
        try:
            with audit_call(bad_conn, token_id=None, tool="t", args={}) as rec:
                captured = rec
                raise exc
        except Exception:
            pass
        assert captured is not None
        return captured

    def test_missing_scope_marked_denied(self) -> None:
        class MissingScope(Exception):
            pass

        rec = self._captured_record(MissingScope("scope x"))
        assert rec["status"] == "denied"
        assert rec["error"] == "MissingScope"

    def test_statement_rejected_marked_denied(self) -> None:
        class StatementRejected(Exception):
            pass

        rec = self._captured_record(StatementRejected("bad sql"))
        assert rec["status"] == "denied"
        assert rec["error"] == "StatementRejected"

    def test_other_exception_marked_error(self) -> None:
        rec = self._captured_record(RuntimeError("boom"))
        assert rec["status"] == "error"
        assert rec["error"] == "RuntimeError"

    def test_explicit_status_preserved(self) -> None:
        """Handler-set status should not be overwritten by the exception path."""
        from application.mcp_server.audit import audit_call

        bad_conn = "postgresql://nonexistent:0/__never__"
        captured: dict | None = None
        try:
            with audit_call(bad_conn, token_id=None, tool="t", args={}) as rec:
                captured = rec
                rec["status"] = "denied"
                raise RuntimeError("with manual status")
        except Exception:
            pass
        assert captured is not None
        assert captured["status"] == "denied"
