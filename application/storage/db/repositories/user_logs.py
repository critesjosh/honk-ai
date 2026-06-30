"""Repository for the ``user_logs`` table.

Covers every operation the legacy Mongo code performs on
``user_logs_collection``:

1. ``insert_one`` in logging.py (per-request activity log via
   ``_log_to_mongodb`` — note: the *Mongo* variable is confusingly named
   ``user_logs_collection`` but points at the ``user_logs`` Mongo
   collection, not ``stack_logs``)
2. ``insert_one`` in answer/routes/base.py (per-stream log entry)
3. ``find`` with sort/skip/limit in analytics/routes.py (paginated log list)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import Connection, text

from application.security.content_registry import api_key_fingerprint, decrypt_value, encrypt_value
from application.storage.db.base_repository import row_to_dict


def _row_to_dict(row) -> dict:
    """Decrypt choke point for user_logs reads (``data`` blob + ``metadata``
    question leaf). Read-both: a no-op on legacy plaintext."""
    out = row_to_dict(row)
    if "data" in out:
        out["data"] = decrypt_value("user_logs", "data", out["data"])
    if out.get("metadata") is not None:
        out["metadata"] = decrypt_value("user_logs", "metadata", out["metadata"])
    return out


class UserLogsRepository:
    """Postgres-backed replacement for Mongo ``user_logs_collection``."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def insert(
        self,
        *,
        user_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        data: Optional[dict] = None,
        metadata: Optional[dict] = None,
        timestamp: Optional[datetime] = None,
        requester_user_id: Optional[str] = None,
    ) -> None:
        """Append a row.

        ``data`` is the free-form request payload (may contain bearer
        keys / response bodies). ``metadata`` is the non-secret analytics
        surface added in migration 0007 — the ``docsgpt_mcp_ro`` role
        has SELECT on ``metadata`` but NOT on ``data``, so anything that
        should be visible to claudebox via the host MCP server must go
        in ``metadata``. See migration 0007 for the contract.

        ``requester_user_id`` is the canonical pseudonym of the end-user who
        triggered the request (bot chat). It lets ``/forget-me`` DELETE the
        log rows that copy that user's question/response into ``data``. NULL
        for anonymous/widget traffic. See migration 0011.
        """
        # Blind-index the bearer key BEFORE encrypting ``data`` (the encrypted
        # blob hides ``data->>'api_key'`` from the lookup). Dual-written in all
        # states so reads can match on it. See PLAN §8.
        api_key_fp = api_key_fingerprint(data.get("api_key")) if data else None
        enc_data = encrypt_value("user_logs", "data", data)
        enc_metadata = encrypt_value("user_logs", "metadata", metadata)
        self._conn.execute(
            text(
                """
                INSERT INTO user_logs (user_id, endpoint, data, metadata, timestamp, requester_user_id, api_key_fp)
                VALUES (
                    :user_id,
                    :endpoint,
                    CAST(:data AS jsonb),
                    CAST(:metadata AS jsonb),
                    COALESCE(:timestamp, now()),
                    :requester_user_id,
                    :api_key_fp
                )
                """
            ),
            {
                "user_id": user_id,
                "endpoint": endpoint,
                "data": json.dumps(enc_data, default=str) if enc_data is not None else None,
                "metadata": (json.dumps(enc_metadata, default=str) if enc_metadata is not None else None),
                "timestamp": timestamp,
                "requester_user_id": requester_user_id,
                "api_key_fp": api_key_fp,
            },
        )

    def list_paginated(
        self,
        *,
        user_id: Optional[str] = None,
        api_key: Optional[str] = None,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[list[dict], bool]:
        """Return ``(rows, has_more)`` for the requested page.

        Mirrors the Mongo ``find(query).sort().skip().limit(page_size+1)``
        pattern used in analytics/routes.py.
        """
        clauses: list[str] = []
        params: dict = {"limit": page_size + 1, "offset": (page - 1) * page_size}
        if user_id is not None:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if api_key is not None:
            # Dual-read: blind-index for encrypted rows, legacy data->>'api_key'
            # for pre-backfill plaintext rows. See PLAN §8/§13.
            clauses.append("(api_key_fp = :api_key_fp OR data->>'api_key' = :api_key)")
            params["api_key"] = api_key
            params["api_key_fp"] = api_key_fingerprint(api_key)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        result = self._conn.execute(
            text(f"SELECT * FROM user_logs {where} ORDER BY timestamp DESC LIMIT :limit OFFSET :offset"),
            params,
        )
        rows = [_row_to_dict(r) for r in result.fetchall()]
        has_more = len(rows) > page_size
        return rows[:page_size], has_more

    def find_by_api_key(
        self,
        api_key: str,
        *,
        timestamp_gte: Optional[datetime] = None,
        timestamp_lt: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Return user_logs rows whose ``data->>'api_key'`` matches ``api_key``.

        Replacement for the legacy Mongo filter by top-level ``api_key``;
        on the PG side the per-request payload lives in ``data`` JSONB,
        so the filter reaches in via ``data->>'api_key'``. Rows are
        ordered by ``timestamp DESC`` to match the Mongo sort.
        """
        clauses = ["(api_key_fp = :api_key_fp OR data->>'api_key' = :api_key)"]
        params: dict = {"api_key": api_key, "api_key_fp": api_key_fingerprint(api_key)}
        if timestamp_gte is not None:
            clauses.append("timestamp >= :timestamp_gte")
            params["timestamp_gte"] = timestamp_gte
        if timestamp_lt is not None:
            clauses.append("timestamp < :timestamp_lt")
            params["timestamp_lt"] = timestamp_lt
        where = " AND ".join(clauses)
        sql = f"SELECT * FROM user_logs WHERE {where} ORDER BY timestamp DESC"
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit
        result = self._conn.execute(text(sql), params)
        return [_row_to_dict(r) for r in result.fetchall()]
