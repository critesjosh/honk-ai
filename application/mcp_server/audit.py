"""Append-only audit log for the MCP server.

Every tool call writes one row to ``mcp_audit`` regardless of outcome
(success, scope rejection, statement-guard rejection, Postgres error,
docker error, embedding error). The row records:

* the token's stable ID (never the plaintext)
* the tool name
* a SHA-256 hash of the JSON-serialised arguments (for change-detection
  and de-duplication; the raw arguments are NOT stored because SQL
  queries can contain operator-pasted secrets, document fragments, etc.)
* a coarse status code (``ok`` / ``error`` / ``denied``)
* the elapsed wall-clock time
* row count for SQL/RAG, log-byte count for ``honk_logs.tail``

Retention is bounded by the cron task in
``scripts/mcp/prune_audit.py`` (off by default; operators opt in).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg


logger = logging.getLogger(__name__)


def _hash_args(args: dict[str, Any]) -> str:
    """Return the SHA-256 hex of the canonical JSON form of ``args``.

    ``json.dumps(..., sort_keys=True)`` so the hash is stable across
    Python dict orderings.
    """
    payload = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@contextmanager
def audit_call(
    connection_string: str,
    *,
    token_id: str | None,
    tool: str,
    args: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Context manager that records one ``mcp_audit`` row.

    Usage::

        with audit_call(uri, token_id=tok.token_id, tool="honk_sql.execute",
                         args={"query": q}) as record:
            rows = run_query(q)
            record["row_count"] = len(rows)
            record["status"] = "ok"

    The record dict accepts ``status`` (default ``"error"`` so an
    exception leaves a useful trace), ``row_count``, ``byte_count``,
    and ``error``. The ``elapsed_ms`` field is filled in by the
    context manager itself.
    """
    record: dict[str, Any] = {
        "status": "error",
        "row_count": None,
        "byte_count": None,
        "error": None,
    }
    started = time.monotonic()
    try:
        yield record
    except Exception as exc:
        # MissingScope (auth) and StatementRejected (SQL guard) are
        # operator errors, not server errors — record them as ``denied``
        # so the audit log distinguishes "we rejected this call" from
        # "something inside the handler blew up". Caller can override by
        # setting record["status"] / record["error"] before the
        # exception propagates (e.g. server.py converts
        # StatementRejected to ValueError but sets error="StatementRejected"
        # so the audit taxonomy survives the conversion).
        if record.get("status") in (None, "error"):
            exc_name = type(exc).__name__
            if exc_name in ("MissingScope", "StatementRejected"):
                record["status"] = "denied"
            else:
                record["status"] = "error"
        if not record.get("error"):
            record["error"] = type(exc).__name__
        raise
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        record["elapsed_ms"] = elapsed_ms
        try:
            with psycopg.connect(connection_string) as conn:
                with conn.cursor() as cur:
                    # docsgpt_mcp_ro is pinned to ``default_transaction_read_only = on``
                    # at the role level (see alembic 0006). The INSERT below is
                    # the ONE write path this role is granted, and it has to opt
                    # out of read-only locally for that single transaction. Must
                    # run before the INSERT and before any other statement that
                    # would take the read-only state into a non-overridable form.
                    cur.execute("SET LOCAL transaction_read_only = off")
                    cur.execute(
                        """
                        INSERT INTO mcp_audit (
                            token_id, tool, args_hash, status,
                            row_count, byte_count, elapsed_ms, error
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            token_id,
                            tool,
                            _hash_args(args),
                            record["status"],
                            record["row_count"],
                            record["byte_count"],
                            elapsed_ms,
                            record["error"],
                        ),
                    )
        except psycopg.Error:
            # Auditing must never crash the request; log and move on.
            logger.exception("mcp_audit insert failed for tool=%s", tool)
