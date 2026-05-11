"""Bearer token verification against the ``mcp_tokens`` table.

The MCP server lives behind Cloudflare Access in production, but we
also enforce a per-token bearer at the application layer. Either alone
is insufficient: CF Access protects the network boundary but doesn't
distinguish between operators sharing a service token; a bare bearer
without CF Access leaves the server exposed if the tunnel is ever
misrouted.

Tokens are issued by ``scripts/mcp/issue_token.py``. The plaintext is
shown once at issuance time and only the SHA-256 hash is stored in
``mcp_tokens.token_hash``. We compare in constant time on every
request.

Hash algorithm choice: SHA-256 (single round, no salt) is sufficient
because the underlying secret is a 256-bit random token, not a
human-chosen password — there is no rainbow-table risk and no
brute-force economy. A salted KDF would be theatre. Rotation is the
real defense and is operator-driven: revoke the row, issue a new one.

This module deliberately doesn't depend on Flask or the Celery worker
process — only on ``psycopg`` and the connection-string normalization
already in :mod:`application.core.db_uri`. Keeps the MCP server image
slim and means the auth path can be exercised in tests without
spinning up the full backend.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from typing import Iterable

import psycopg


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenInfo:
    """Resolved metadata for a verified bearer."""

    token_id: str
    """Stable opaque ID used by ``mcp_audit`` to attribute calls.

    Never includes the plaintext token.
    """

    label: str
    """Human-readable label set at issuance time (e.g. ``cb-claudebox-rw``)."""

    scopes: frozenset[str]
    """Granted scopes (e.g. ``{"db:read", "rag:read", "logs:read"}``)."""


# Canonical scopes recognised by the server. Tools require a subset of
# these via the ``required`` argument to :meth:`require_scopes`.
SCOPE_DB_READ = "db:read"
SCOPE_RAG_READ = "rag:read"
SCOPE_RAG_FULL_TEXT = "rag:read_full_text"
SCOPE_LOGS_READ = "logs:read"

KNOWN_SCOPES: frozenset[str] = frozenset({SCOPE_DB_READ, SCOPE_RAG_READ, SCOPE_RAG_FULL_TEXT, SCOPE_LOGS_READ})


def hash_token(plaintext: str) -> str:
    """Return the hex SHA-256 hash of a plaintext token.

    Mirrored by ``scripts/mcp/issue_token.py`` at issuance time, by the
    Alembic migration that bootstraps the table, and by the verify
    path here.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class TokenStore:
    """Resolves bearer tokens against the ``mcp_tokens`` Postgres table.

    Connection management is intentionally simple: each ``verify`` call
    opens, queries, closes. The MCP server is low-QPS (operator-driven)
    so the round-trip cost is fine and we avoid sharing a long-lived
    connection across async request handlers.
    """

    def __init__(self, connection_string: str | None = None):
        if connection_string is None:
            connection_string = _resolve_connection_string_from_env()
        if not connection_string:
            raise RuntimeError(
                "MCP_DB_URL or PGVECTOR_CONNECTION_STRING / POSTGRES_URI "
                "must be set so the MCP server can verify tokens against "
                "the mcp_tokens table"
            )
        self._connection_string = connection_string

    def verify(self, plaintext_token: str) -> TokenInfo | None:
        """Return :class:`TokenInfo` if the token is valid, else ``None``.

        Validation:

        * Looks up the matching ``token_hash`` row.
        * Constant-time comparison against the stored hash (psycopg's
          parameterised lookup already prevents the SELECT from leaking
          a different row, but we double-check to make timing attacks
          on hash equality boring).
        * Rejects revoked rows (``revoked_at IS NOT NULL``).
        * Rejects expired rows when ``expires_at`` is set and in the
          past (NULL ``expires_at`` means non-expiring).
        """
        if not plaintext_token:
            return None
        token_hash = hash_token(plaintext_token)
        try:
            with psycopg.connect(self._connection_string) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id::text, label, scopes, token_hash,
                               revoked_at, expires_at
                        FROM mcp_tokens
                        WHERE token_hash = %s
                        LIMIT 1
                        """,
                        (token_hash,),
                    )
                    row = cur.fetchone()
        except psycopg.Error:
            logger.exception("mcp_tokens lookup failed")
            return None

        if row is None:
            return None
        token_id, label, scopes, stored_hash, revoked_at, expires_at = row

        if not hmac.compare_digest(stored_hash, token_hash):
            # Defensive: SQL already filtered to this hash, but a constant-time
            # comparison removes the timing variance from the LIKE/= path.
            return None
        if revoked_at is not None:
            return None
        if expires_at is not None:
            from datetime import datetime, timezone

            if expires_at <= datetime.now(timezone.utc):
                return None

        cleaned_scopes = _coerce_scopes(scopes)
        return TokenInfo(
            token_id=token_id,
            label=label or "",
            scopes=cleaned_scopes,
        )


def _coerce_scopes(raw: object) -> frozenset[str]:
    """Normalize a Postgres TEXT[]/JSONB into ``frozenset[str]``."""
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        # Defensive: shouldn't happen if the column is TEXT[], but
        # tolerate a JSON-ish single-string fallback.
        return frozenset({raw}) if raw in KNOWN_SCOPES else frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(s for s in raw if isinstance(s, str))
    return frozenset()


class MissingScope(Exception):
    """Raised when a tool call lacks one of its required scopes."""

    def __init__(self, missing: Iterable[str]):
        self.missing = sorted(missing)
        super().__init__(f"missing scope(s): {', '.join(self.missing)}")


def require_scopes(token: TokenInfo, required: Iterable[str]) -> None:
    """Raise :class:`MissingScope` if ``token`` doesn't grant ``required``.

    Tools call this at entry; the server's audit path records the
    failure separately so revoked or under-scoped tokens leave a trail
    in ``mcp_audit`` even when the call returns an error to the
    client.
    """
    needed = set(required)
    missing = needed - token.scopes
    if missing:
        raise MissingScope(missing)


def _resolve_connection_string_from_env() -> str | None:
    """Resolve the libpq connection string from the standard env var ladder.

    Order of precedence (highest first):

    1. ``MCP_DB_URL`` — explicit override for the MCP server. Lets
       operators point the server at a different database (e.g. the
       ``docsgpt_mcp_ro`` role with read-only grants) than the rest of
       the backend.
    2. ``PGVECTOR_CONNECTION_STRING`` — already libpq-shaped.
    3. ``POSTGRES_URI`` — SQLAlchemy-shaped, normalized for libpq.
    """
    explicit = os.environ.get("MCP_DB_URL")
    if explicit:
        return explicit
    pgvector = os.environ.get("PGVECTOR_CONNECTION_STRING")
    if pgvector:
        from application.core.db_uri import normalize_pgvector_connection_string

        return normalize_pgvector_connection_string(pgvector) or pgvector
    postgres = os.environ.get("POSTGRES_URI")
    if postgres:
        from application.core.db_uri import normalize_pgvector_connection_string

        return normalize_pgvector_connection_string(postgres) or postgres
    return None
