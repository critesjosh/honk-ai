"""``honk_sql.*`` MCP tools.

The SQL surface is intentionally narrow: a single statement at a time,
SELECT/WITH only, executed under a Postgres role with read-only
grants. Connection settings enforce per-statement timeouts so a
long-running query can't hold a backend slot indefinitely.

Authoritative description of the guard logic is in
:mod:`application.mcp_server.sql_guard`. This module wires the guard
into the runtime path and shapes the result into what FastMCP returns
to the client.
"""

from __future__ import annotations

import decimal
import logging
import uuid
from datetime import date, datetime, time as _time
from typing import Any

import psycopg

from application.mcp_server.sql_guard import GuardResult, guard_select


logger = logging.getLogger(__name__)


# Per-call result cap. The MCP transport doesn't enforce a payload size
# at the framing layer, so we trim explicitly on the server side. 500
# rows fits comfortably under the default 256 KiB cap when each row is
# a handful of small columns; pagination via ``LIMIT/OFFSET`` is the
# user's responsibility once they hit the cap.
DEFAULT_ROW_CAP = 500
MAX_ROW_CAP = 5000

# Default per-statement timeout. Aligns with the cap on a /stream
# request — operators are unlikely to want longer for ad-hoc inspection.
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120


def _to_jsonable(value: Any) -> Any:
    """Coerce a Postgres value into a JSON-serialisable form.

    The MCP wire format is JSON. psycopg returns a few Python types
    that ``json.dumps`` can't handle directly (``datetime``, ``UUID``,
    ``Decimal``, ``bytes``); we convert them to canonical string forms
    so the client always sees a string-or-primitive on the other side.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, _time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        # Stringify rather than cast to float so big integers / fixed-
        # precision values survive the round trip.
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return str(value)


class SQLExecutor:
    """Single-purpose helper that opens a per-call read-only connection.

    Keeping the open/close inside one class makes it easy to test the
    guard + result-shaping logic against a stub connection without
    pulling in the full FastMCP server.
    """

    def __init__(self, connection_string: str):
        self._connection_string = connection_string

    def execute(
        self,
        query: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> dict[str, Any]:
        """Run a guarded SELECT/WITH and return a JSON-shaped result.

        Returns ``{"columns": [...], "rows": [[...], ...],
        "row_count": N, "truncated": bool, "elapsed_ms": int}`` on
        success, or raises :class:`StatementRejected` /
        :class:`psycopg.Error`. The caller (the MCP tool wrapper) is
        responsible for translating those into MCP-shaped errors and
        for writing the audit row.
        """
        guard = guard_select(query)
        if not guard.ok:
            raise StatementRejected(guard)

        # Clamp arguments to safe bounds. The MCP schema declares the
        # maxima but we re-clamp here because the schema is an
        # advisory contract, not an enforced one.
        timeout_seconds = max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
        row_cap = max(1, min(int(row_cap), MAX_ROW_CAP))

        statement = guard.statement
        assert statement is not None  # narrowed by guard.ok above

        # Pin the session to read-only at the psycopg layer rather than
        # via ``SET TRANSACTION READ ONLY`` mid-cursor. The latter is
        # order-sensitive (must come before any snapshot-taking query
        # and is fragile across psycopg versions). ``conn.read_only``
        # issues the equivalent ``SET default_transaction_read_only = on``
        # on the session before any query runs, so every transaction the
        # cursor opens is read-only by default. Belt-and-braces with the
        # role-level pin set by alembic 0006.
        with psycopg.connect(self._connection_string) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                # ``SET`` / ``SET LOCAL`` are GUC commands and Postgres rejects
                # bound parameters here ("syntax error at or near \"$1\""), so
                # the value has to be inlined. Safe because timeout_seconds is
                # already clamped to an int in [1, MAX_TIMEOUT_SECONDS].
                cur.execute(f"SET LOCAL statement_timeout = '{int(timeout_seconds)}s'")
                cur.execute("SET LOCAL idle_in_transaction_session_timeout = '60s'")
                cur.execute(statement)
                rows = cur.fetchmany(row_cap + 1)
                columns = [d.name for d in cur.description] if cur.description else []

        truncated = len(rows) > row_cap
        if truncated:
            rows = rows[:row_cap]
        coerced_rows = [[_to_jsonable(v) for v in row] for row in rows]
        return {
            "columns": columns,
            "rows": coerced_rows,
            "row_count": len(coerced_rows),
            "truncated": truncated,
        }

    def list_tables(self, schema: str = "public") -> dict[str, Any]:
        """Return a bounded list of base tables in ``schema``.

        Uses ``information_schema`` rather than ``pg_catalog`` so
        operators familiar with cross-database SQL can read it without
        a Postgres reference open. Capped at 200 rows; large schemas
        should be inspected by direct SELECTs against
        ``information_schema.tables``.
        """
        if not isinstance(schema, str) or not schema:
            schema = "public"
        sql = """
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type IN ('BASE TABLE', 'VIEW', 'MATERIALIZED VIEW')
            ORDER BY table_name
            LIMIT 200
        """
        with psycopg.connect(self._connection_string) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(sql, (schema,))
                rows = cur.fetchall()
        return {
            "schema": schema,
            "tables": [{"name": r[0], "kind": r[1].lower().replace(" ", "_")} for r in rows],
        }

    def describe(self, table: str, schema: str = "public") -> dict[str, Any]:
        """Return column metadata for ``schema.table``."""
        if not isinstance(table, str) or not table:
            raise ValueError("table is required")
        if not isinstance(schema, str) or not schema:
            schema = "public"
        sql = """
            SELECT column_name, data_type, is_nullable, column_default,
                   character_maximum_length, numeric_precision,
                   numeric_scale
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            LIMIT 200
        """
        with psycopg.connect(self._connection_string) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(sql, (schema, table))
                rows = cur.fetchall()
        return {
            "schema": schema,
            "table": table,
            "columns": [
                {
                    "name": r[0],
                    "type": r[1],
                    "nullable": r[2] == "YES",
                    "default": r[3],
                    "char_max_length": r[4],
                    "numeric_precision": r[5],
                    "numeric_scale": r[6],
                }
                for r in rows
            ],
        }


class StatementRejected(Exception):
    """The SQL guard rejected the input. ``reason`` is operator-readable."""

    def __init__(self, result: GuardResult):
        self.reason = result.reason or "rejected"
        super().__init__(self.reason)
