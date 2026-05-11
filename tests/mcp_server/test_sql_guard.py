"""Tests for ``application.mcp_server.sql_guard``.

The guard is the in-process safety layer for ``honk_sql.execute``.
It's not the only safety layer (the read-only Postgres role is the
real backstop), but it's the user-visible one — every rejection
short-circuits before psycopg sees the input. So the test surface
exercises both shapes that should pass and shapes that should fail,
plus a few documented edge cases (leading comments, semicolons in
quoted strings).
"""

from __future__ import annotations

import pytest

from application.mcp_server.sql_guard import (
    MAX_STATEMENT_BYTES,
    guard_select,
)


@pytest.mark.unit
class TestGuardSelectAccepts:
    """Inputs that should pass through cleanly."""

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1",
            "select 1",
            "  SELECT id FROM agents WHERE id = 'x'  ",
            "SELECT id FROM agents;",  # trailing semicolon allowed
            # Leading line comment.
            "-- inspect agents\nSELECT id FROM agents",
            # Leading block comment.
            "/* metric: agent count */ SELECT count(*) FROM agents",
            # Multiple leading comments.
            "-- one\n-- two\nSELECT 1",
            # WITH (CTE) is allowed in addition to SELECT.
            "WITH q AS (SELECT 1) SELECT * FROM q",
            # Semicolon inside a string literal — must NOT be treated as
            # a statement separator. ``stripped`` strips trailing ``;``,
            # the multistatement check ignores quoted text.
            "SELECT 1 WHERE 'a;b' = 'a;b'",
        ],
    )
    def test_accepts(self, query: str) -> None:
        result = guard_select(query)
        assert result.ok, f"expected accept, got reason={result.reason!r}"
        assert result.statement is not None
        # Trailing semicolon is stripped from the cleaned form so the
        # caller can hand the statement straight to psycopg without
        # worrying about whether psycopg treats trailing ``;`` as a
        # second empty statement.
        assert not result.statement.rstrip().endswith(";")


@pytest.mark.unit
class TestGuardSelectRejects:
    """Inputs that must be rejected (with operator-readable reasons)."""

    @pytest.mark.parametrize(
        "query, must_mention",
        [
            (None, "required"),
            ("", "empty"),
            ("   \n  ", "empty"),
            ("INSERT INTO t VALUES (1)", "SELECT/WITH"),
            ("UPDATE t SET x = 1", "SELECT/WITH"),
            ("DELETE FROM t", "SELECT/WITH"),
            ("DROP TABLE t", "SELECT/WITH"),
            ("CREATE TABLE t (id int)", "SELECT/WITH"),
            ("TRUNCATE t", "SELECT/WITH"),
            ("VACUUM", "SELECT/WITH"),
            ("ANALYZE", "SELECT/WITH"),
            # Two statements — semicolon separator outside quotes.
            ("SELECT 1; SELECT 2", "multi"),
            ("SELECT 1; DROP TABLE t", "multi"),
            # Leading comment that doesn't change the conclusion: the
            # actual first keyword is still a write.
            ("-- innocent\nDELETE FROM t", "SELECT/WITH"),
            # Even with a SELECT inside, the first keyword wins.
            ("INSERT INTO t SELECT 1", "SELECT/WITH"),
        ],
    )
    def test_rejects(self, query, must_mention: str) -> None:
        result = guard_select(query)
        assert not result.ok
        assert result.reason is not None
        assert must_mention.lower() in result.reason.lower(), (
            f"expected reason to mention {must_mention!r}, got {result.reason!r}"
        )

    def test_rejects_oversize(self) -> None:
        oversized = "SELECT 1 -- " + ("x" * MAX_STATEMENT_BYTES)
        result = guard_select(oversized)
        assert not result.ok
        assert "maximum length" in result.reason.lower()

    def test_rejects_non_string(self) -> None:
        result = guard_select(123)  # type: ignore[arg-type]
        assert not result.ok
        assert "string" in result.reason.lower()


@pytest.mark.unit
class TestStringSemicolonHeuristic:
    """The multistatement check tolerates ``;`` inside quoted strings.

    These cases are easy to get wrong with a naive ``";" in s`` check,
    so they get their own bucket — operators legitimately put
    semicolons in WHERE-clause string comparisons.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1 WHERE col = 'has;semi'",
            "SELECT 1 WHERE col = 'one'';two'",  # SQL-escaped quote
            "SELECT $$ a;b $$ AS c",  # dollar-quoted text
            # Tagged dollar quote — Postgres allows $foo$...$foo$ in
            # addition to $$...$$. The scanner must treat ``;`` inside
            # as text, not a statement boundary.
            "SELECT $tag$ a;b $tag$ AS c",
            # Semicolon inside a SQL comment is not a separator.
            "SELECT 1 /* ; */",
            "SELECT 1 -- has ; in comment\n",
            # Semicolon inside a quoted identifier is just text.
            'SELECT "a;b" FROM information_schema.tables WHERE table_name = $$x$$',
        ],
    )
    def test_quoted_semicolon_is_not_multistatement(self, query: str) -> None:
        result = guard_select(query)
        assert result.ok, f"expected accept, got reason={result.reason!r}"


@pytest.mark.unit
class TestDataModifyingCTEs:
    """Reject CTEs whose body is a write, even though the first token is WITH.

    Postgres allows ``WITH x AS (DELETE FROM foo RETURNING *) SELECT * FROM x``
    — the leading ``WITH`` passes the SELECT/WITH check but the body still
    performs the DELETE. The role-level read-only setting catches this
    at runtime, but rejecting in-guard gives operators a clean error
    instead of a Postgres permission failure 30s into the call.
    """

    @pytest.mark.parametrize(
        "query, keyword",
        [
            (
                "WITH x AS (DELETE FROM agents RETURNING *) SELECT * FROM x",
                "DELETE",
            ),
            # Block-comment bypass — codex spotted that a naive scanner
            # which strips comments without inserting whitespace would
            # see ``DELETEFROM`` and miss the word-boundary keyword.
            # ``_strip_quoted_and_comments`` emits a single space for
            # every stripped region to preserve token boundaries.
            (
                "WITH x AS (DELETE/**/FROM agents RETURNING *) SELECT * FROM x",
                "DELETE",
            ),
            (
                "WITH x AS (DELETE-- comment\nFROM agents RETURNING *) SELECT * FROM x",
                "DELETE",
            ),
            # Tagged dollar-quote literal that LOOKS like it ends inside
            # but doesn't — the bare ``$$`` inside ``$tag$...$tag$`` is
            # not the closing delimiter. The scanner must use the actual
            # tag to find the end.
            (
                "WITH x AS (DELETE FROM agents WHERE name = $tag$ harmless;DROP$tag$ RETURNING *) SELECT * FROM x",
                "DELETE",
            ),
            (
                "WITH q AS (INSERT INTO mcp_audit DEFAULT VALUES RETURNING id) SELECT * FROM q",
                "INSERT",
            ),
            (
                "WITH up AS (UPDATE agents SET name = 'x' RETURNING id) SELECT * FROM up",
                "UPDATE",
            ),
            (
                "WITH t AS (MERGE INTO agents USING agents AS s ON 1=1 RETURNING *) SELECT * FROM t",
                "MERGE",
            ),
            # Even bare SELECT containing a forbidden keyword as a real
            # token outside string literals (e.g. via FROM clause subquery
            # that someone forgot to wrap in a string) should reject.
            (
                "SELECT * FROM (TRUNCATE agents)",
                "TRUNCATE",
            ),
            # COPY can write to server filesystem on the receiving end
            # for superusers; reject regardless of current grants.
            (
                "SELECT 1; COPY agents TO STDOUT",
                None,  # multi-statement check fires first
            ),
            # DROP / CREATE / ALTER / GRANT / REVOKE — DDL via CTE is
            # exotic but Postgres has been adding more cases over time.
            (
                "WITH d AS (SELECT 1) DROP TABLE agents",
                "DROP",
            ),
        ],
    )
    def test_rejects_data_modifying_cte(self, query: str, keyword: str | None) -> None:
        result = guard_select(query)
        assert not result.ok, f"expected reject, got accept for {query!r}"
        if keyword is not None:
            assert keyword in result.reason, f"expected reason to mention {keyword!r}, got {result.reason!r}"

    @pytest.mark.parametrize(
        "query",
        [
            # Forbidden words inside string literals must NOT trigger
            # the reject. Operators legitimately query for log lines
            # containing words like "DELETE" or "CREATE TABLE".
            "SELECT id FROM user_logs WHERE data::text LIKE '%DELETE FROM%'",
            "SELECT 'I created this' AS msg",
            "SELECT $$ DROP TABLE x $$ AS literal",
            # Forbidden words inside SQL comments must also pass.
            "-- DELETE FROM agents would be bad\nSELECT 1",
            "SELECT /* CREATE TABLE foo */ 1",
            # Identifiers that *contain* a forbidden keyword as a
            # substring (with non-word chars around the match) must
            # pass — \b prevents matching DELETE inside delete_at.
            "SELECT delete_at FROM tombstones",
            "SELECT update_count FROM stats",
            # Quoted identifier shouldn't match keyword.
            'SELECT "create_at_idx" FROM stats',
        ],
    )
    def test_accepts_forbidden_word_in_safe_context(self, query: str) -> None:
        result = guard_select(query)
        assert result.ok, f"expected accept, got reason={result.reason!r}"
