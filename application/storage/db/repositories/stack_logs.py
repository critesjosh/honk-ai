"""Repository for the ``stack_logs`` table.

Covers the single operation the legacy Mongo code performs:

1. ``insert_one`` in logging.py ``_log_to_mongodb`` — append-only debug/error
   activity log. The Mongo collection is ``stack_logs``; the Mongo variable
   inside ``_log_to_mongodb`` is misleadingly named ``user_logs_collection``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import Connection, text

from application.security.content_registry import encrypt_value


class StackLogsRepository:
    """Postgres-backed replacement for Mongo ``stack_logs`` collection."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def insert(
        self,
        *,
        activity_id: str,
        endpoint: Optional[str] = None,
        level: Optional[str] = None,
        user_id: Optional[str] = None,
        api_key: Optional[str] = None,
        query: Optional[str] = None,
        stacks: Optional[list] = None,
        timestamp: Optional[datetime] = None,
        requester_user_id: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            text(
                """
                INSERT INTO stack_logs (activity_id, endpoint, level, user_id, api_key, query, stacks, timestamp, requester_user_id)
                VALUES (
                    :activity_id, :endpoint, :level, :user_id, :api_key, :query,
                    CAST(:stacks AS jsonb),
                    COALESCE(:timestamp, now()),
                    :requester_user_id
                )
                """
            ),
            {
                "activity_id": activity_id,
                "endpoint": endpoint,
                "level": level,
                "user_id": user_id,
                "api_key": api_key,
                # Content: prompt text + tool/exception trace. No-op when disabled.
                "query": encrypt_value("stack_logs", "query", query),
                "stacks": json.dumps(encrypt_value("stack_logs", "stacks", stacks or [])),
                "timestamp": timestamp,
                "requester_user_id": requester_user_id,
            },
        )
