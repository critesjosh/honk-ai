"""FastMCP server wiring for the honk-ai backend MCP surface.

Boot path:

1. Resolve the Postgres connection string for the read-only role
   (``MCP_DB_URL`` overrides ``PGVECTOR_CONNECTION_STRING`` /
   ``POSTGRES_URI``).
2. Build a :class:`fastmcp.FastMCP` server with a custom
   :class:`fastmcp.server.auth.TokenVerifier` that resolves bearers
   against the ``mcp_tokens`` table.
3. Register ``honk_sql.*``, ``honk_rag.*``, ``honk_logs.*`` tool
   functions with per-tool scope checks and audit-row writes.
4. Run via the FastMCP HTTP transport on
   ``MCP_HOST:MCP_PORT`` (default ``0.0.0.0:7092``).

Each tool function is async because FastMCP is async-first; the
underlying executors are sync (psycopg, requests) so we run them in a
thread executor via ``asyncio.to_thread`` to keep the FastMCP event
loop responsive.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from application.mcp_server.audit import audit_call
from application.mcp_server.auth import (
    KNOWN_SCOPES,
    SCOPE_DB_READ,
    SCOPE_LOGS_READ,
    SCOPE_RAG_FULL_TEXT,
    SCOPE_RAG_READ,
    MissingScope,
    TokenInfo,
    TokenStore,
    require_scopes,
    _resolve_connection_string_from_env,
)
from application.mcp_server.tools.logs import LogQuery, LogReader
from application.mcp_server.tools.rag import RAGSearcher
from application.mcp_server.tools.sql import (
    DEFAULT_ROW_CAP,
    DEFAULT_TIMEOUT_SECONDS,
    SQLExecutor,
    StatementRejected,
)


logger = logging.getLogger(__name__)


class MCPTokensVerifier(TokenVerifier):
    """FastMCP TokenVerifier backed by the ``mcp_tokens`` Postgres table."""

    def __init__(self, store: TokenStore):
        super().__init__()
        self._store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        # Run the blocking psycopg call in a thread to avoid stalling
        # the event loop — psycopg is sync-only here on purpose.
        info = await asyncio.to_thread(self._store.verify, token)
        if info is None:
            return None
        return AccessToken(
            token=token,
            client_id=info.token_id,
            scopes=sorted(info.scopes),
            claims={"label": info.label},
        )


def _token_info_from_access(access: AccessToken) -> TokenInfo:
    """Reverse of :meth:`MCPTokensVerifier.verify_token` for tool handlers."""
    return TokenInfo(
        token_id=access.client_id,
        label=str(access.claims.get("label", "")),
        scopes=frozenset(access.scopes),
    )


def build_server(
    *,
    connection_string: str | None = None,
    server_name: str = "honk-ai",
    auth_required: bool = True,
) -> FastMCP:
    """Construct the FastMCP server with all three tool families wired in.

    ``auth_required`` (default True) enforces the ``mcp_tokens`` bearer at the
    FastMCP middleware layer and resolves ``_current_token`` from the verified
    access token. When False, FastMCP runs with no auth middleware and
    ``_current_token`` returns a synthetic TokenInfo with all canonical scopes
    granted and ``token_id=None``. The ``mcp_audit`` row is still written; the
    ``token_id`` column is nullable for this case. Anonymous mode is intended
    for deployments where the network boundary itself is the auth boundary
    (e.g. the MCP container is bound to loopback only and reached exclusively
    through SSH tunnels with key-based access). The Postgres role
    ``docsgpt_mcp_ro`` remains the second defense layer in either mode.
    """

    db_url = connection_string or _resolve_connection_string_from_env()
    if not db_url:
        raise RuntimeError(
            "No Postgres connection string configured for the MCP server. "
            "Set MCP_DB_URL (preferred) or PGVECTOR_CONNECTION_STRING / "
            "POSTGRES_URI."
        )

    if auth_required:
        store = TokenStore(connection_string=db_url)
        verifier: MCPTokensVerifier | None = MCPTokensVerifier(store)
    else:
        verifier = None
        logger.warning(
            "mcp_server: MCP_AUTH_REQUIRED is disabled; the bearer middleware is OFF. "
            "This is only safe when the listening interface is private (loopback / SSH tunnel)."
        )

    sql = SQLExecutor(db_url)
    rag = RAGSearcher(connection_string=db_url)
    logs = LogReader()

    mcp = FastMCP(name=server_name, auth=verifier)

    _anonymous_token = TokenInfo(
        token_id=None,
        label="anonymous",
        scopes=KNOWN_SCOPES,
    )

    def _current_token() -> TokenInfo:
        if not auth_required:
            return _anonymous_token
        access = get_access_token()
        if access is None:
            # FastMCP rejects unauthenticated requests at the auth
            # middleware, but we re-check here so a misconfigured
            # subclass can't silently allow anonymous calls.
            raise MissingScope(["authenticated"])
        return _token_info_from_access(access)

    @mcp.tool(
        name="honk_sql.execute",
        description=(
            "Execute a single SELECT or WITH statement against the docsgpt "
            "Postgres database. Multi-statement input, DML, and DDL are "
            "rejected by both an in-process guard and the docsgpt_mcp_ro "
            "Postgres role. Returns columns, rows (up to row_cap), and "
            "truncation/elapsed metadata."
        ),
    )
    async def honk_sql_execute(
        query: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> dict[str, Any]:
        token = _current_token()
        # audit_call wraps the entire tool body so scope failures and
        # guard rejections both produce an mcp_audit row with
        # status=denied, not a silent drop.
        with audit_call(
            db_url,
            token_id=token.token_id,
            tool="honk_sql.execute",
            args={"timeout_seconds": timeout_seconds, "row_cap": row_cap},
        ) as record:
            require_scopes(token, [SCOPE_DB_READ])
            try:
                result = await asyncio.to_thread(
                    sql.execute,
                    query,
                    timeout_seconds=timeout_seconds,
                    row_cap=row_cap,
                )
            except StatementRejected as exc:
                # Convert to ValueError so FastMCP returns an
                # operator-readable message, but mark status/error
                # explicitly first — audit.py's exception-class auto-
                # detect can no longer see "StatementRejected" once we
                # re-raise as ValueError.
                record["status"] = "denied"
                record["error"] = "StatementRejected"
                raise ValueError(exc.reason) from exc
            record["row_count"] = result["row_count"]
            record["status"] = "ok"
            return result

    @mcp.tool(
        name="honk_sql.list_tables",
        description=("List base tables / views / matviews in a schema (default 'public'). Bounded at 200 rows."),
    )
    async def honk_sql_list_tables(schema: str = "public") -> dict[str, Any]:
        token = _current_token()
        with audit_call(
            db_url,
            token_id=token.token_id,
            tool="honk_sql.list_tables",
            args={"schema": schema},
        ) as record:
            require_scopes(token, [SCOPE_DB_READ])
            result = await asyncio.to_thread(sql.list_tables, schema)
            record["row_count"] = len(result["tables"])
            record["status"] = "ok"
            return result

    @mcp.tool(
        name="honk_sql.describe",
        description=("Return information_schema.columns metadata for one table. Bounded at 200 columns."),
    )
    async def honk_sql_describe(table: str, schema: str = "public") -> dict[str, Any]:
        token = _current_token()
        with audit_call(
            db_url,
            token_id=token.token_id,
            tool="honk_sql.describe",
            args={"schema": schema, "table": table},
        ) as record:
            require_scopes(token, [SCOPE_DB_READ])
            result = await asyncio.to_thread(sql.describe, table, schema)
            record["row_count"] = len(result["columns"])
            record["status"] = "ok"
            return result

    @mcp.tool(
        name="honk_rag.search",
        description=(
            "pgvector similarity search against the production documents "
            "table. Pass either query_text (server embeds via "
            "EMBEDDINGS_BASE_URL) or query_vector (must match "
            "EMBEDDINGS_DIMENSION). include_full_text=true requires the "
            "rag:read_full_text scope."
        ),
    )
    async def honk_rag_search(
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        source_ids: list[str] | None = None,
        k: int = 8,
        include_full_text: bool = False,
    ) -> dict[str, Any]:
        token = _current_token()
        with audit_call(
            db_url,
            token_id=token.token_id,
            tool="honk_rag.search",
            args={
                "k": k,
                "include_full_text": include_full_text,
                "source_ids": source_ids,
                "has_query_text": query_text is not None,
                "has_query_vector": query_vector is not None,
            },
        ) as record:
            if include_full_text:
                require_scopes(token, [SCOPE_RAG_READ, SCOPE_RAG_FULL_TEXT])
            else:
                require_scopes(token, [SCOPE_RAG_READ])
            result = await asyncio.to_thread(
                rag.search,
                query_text=query_text,
                query_vector=query_vector,
                source_ids=source_ids,
                k=k,
                include_full_text=include_full_text,
            )
            record["row_count"] = len(result["results"])
            record["status"] = "ok"
            return result

    @mcp.tool(
        name="honk_rag.list_sources",
        description=("List ingested corpora (sources table). Returns id, name, is_public, ingested_at."),
    )
    async def honk_rag_list_sources() -> dict[str, Any]:
        token = _current_token()
        with audit_call(
            db_url,
            token_id=token.token_id,
            tool="honk_rag.list_sources",
            args={},
        ) as record:
            require_scopes(token, [SCOPE_RAG_READ])
            result = await asyncio.to_thread(rag.list_sources)
            record["row_count"] = len(result["sources"])
            record["status"] = "ok"
            return result

    @mcp.tool(
        name="honk_logs.tail",
        description=(
            "Read recent stdout/stderr from a whitelisted compose service "
            "(backend, worker, postgres, redis, caddy, discord-bot, "
            "frontend-ask, mcp). Bounded by MAX_BYTES_PER_CALL = 256 KiB. "
            "Pass next_since from a previous call to paginate."
        ),
    )
    async def honk_logs_tail(
        service: str,
        lines: int = 200,
        since: str | None = None,
        until: str | None = None,
        grep: str | None = None,
    ) -> dict[str, Any]:
        token = _current_token()
        with audit_call(
            db_url,
            token_id=token.token_id,
            tool="honk_logs.tail",
            args={
                "service": service,
                "lines": lines,
                "since": since,
                "until": until,
                "grep_set": grep is not None,
            },
        ) as record:
            require_scopes(token, [SCOPE_LOGS_READ])
            result = await asyncio.to_thread(
                logs.tail,
                LogQuery(
                    service=service,
                    lines=lines,
                    since=since,
                    until=until,
                    grep=grep,
                ),
            )
            record["byte_count"] = result.get("byte_count")
            record["row_count"] = len(result.get("lines", []))
            record["status"] = "ok"
            return result

    @mcp.tool(
        name="honk_logs.list_services",
        description=(
            "List the compose services this MCP server can stream logs for and their current container state."
        ),
    )
    async def honk_logs_list_services() -> dict[str, Any]:
        token = _current_token()
        with audit_call(
            db_url,
            token_id=token.token_id,
            tool="honk_logs.list_services",
            args={},
        ) as record:
            require_scopes(token, [SCOPE_LOGS_READ])
            result = await asyncio.to_thread(logs.list_services)
            record["row_count"] = len(result["services"])
            record["status"] = "ok"
            return result

    return mcp


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var with a strict allowlist; unknown values fail loud."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}={raw!r} is not a recognised boolean (use true/false)")


def run() -> None:
    """Entry point for ``python -m application.mcp_server``."""
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "7092"))
    auth_required = _env_bool("MCP_AUTH_REQUIRED", default=True)
    server = build_server(auth_required=auth_required)
    server.run(transport="http", host=host, port=port)
