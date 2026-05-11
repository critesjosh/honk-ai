"""Issue or revoke bearer tokens for the honk-ai-host MCP server.

Usage::

    # Issue a fresh token for a claudebox group, with all three read scopes:
    python scripts/mcp/issue_token.py issue \\
        --label cb-claudebox-rw \\
        --scopes db:read,rag:read,logs:read

    # Issue with full-text RAG access too:
    python scripts/mcp/issue_token.py issue \\
        --label cb-research-full \\
        --scopes db:read,rag:read,rag:read_full_text,logs:read

    # Revoke (sets revoked_at = now()):
    python scripts/mcp/issue_token.py revoke --label cb-claudebox-rw

    # List active tokens (no plaintext shown — just metadata):
    python scripts/mcp/issue_token.py list

The plaintext token is shown ONCE at issuance and only the SHA-256
hash is stored. Operators are expected to copy the printed value into
the claudebox credentials store immediately.

Run inside the backend container (so it picks up the configured
``POSTGRES_URI``)::

    docker compose -f deployment/docker-compose-hub.yaml run --rm \\
        -v "$(pwd)/scripts:/app/scripts:ro" \\
        -e PYTHONPATH=/app \\
        backend python scripts/mcp/issue_token.py issue --label X --scopes ...
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

import psycopg

# Importable from /app inside the backend image when scripts/ is bind-mounted
# and PYTHONPATH=/app, matching the pattern used by
# scripts/db/create_ask_aztec_public_agent.py.
from application.mcp_server.auth import KNOWN_SCOPES, hash_token


def _connection_string() -> str:
    """Resolve the Postgres URI for the privileged ``docsgpt`` role.

    Token issuance writes to ``mcp_tokens`` so it needs the privileged
    role, not ``docsgpt_mcp_ro`` (which has SELECT-only on this table).
    The script reads the same env vars as the rest of the backend; we
    don't reach for ``MCP_DB_URL`` here because that is intentionally
    bound to the read-only role.
    """
    pgvector = os.environ.get("PGVECTOR_CONNECTION_STRING")
    if pgvector:
        from application.core.db_uri import normalize_pgvector_connection_string

        return normalize_pgvector_connection_string(pgvector) or pgvector
    postgres = os.environ.get("POSTGRES_URI")
    if postgres:
        from application.core.db_uri import normalize_pgvector_connection_string

        return normalize_pgvector_connection_string(postgres) or postgres
    raise SystemExit(
        "POSTGRES_URI or PGVECTOR_CONNECTION_STRING must be set so the token script can write to mcp_tokens."
    )


def _parse_scopes(raw: str) -> list[str]:
    if not raw:
        raise SystemExit("--scopes is required (e.g. db:read,rag:read,logs:read)")
    parsed = [s.strip() for s in raw.split(",") if s.strip()]
    bad = [s for s in parsed if s not in KNOWN_SCOPES]
    if bad:
        raise SystemExit(f"unknown scope(s): {bad}. allowed: {sorted(KNOWN_SCOPES)}")
    return parsed


def _issue(args: argparse.Namespace) -> int:
    scopes = _parse_scopes(args.scopes)
    plaintext = secrets.token_urlsafe(48)
    token_hash = hash_token(plaintext)
    expires_clause = "%s::timestamptz" if args.expires_at else "NULL"
    params: list[object] = [args.label, token_hash, scopes]
    if args.expires_at:
        params.append(args.expires_at)
    if args.note:
        params.append(args.note)
    note_clause = "%s" if args.note else "NULL"
    sql = f"""
        INSERT INTO mcp_tokens (label, token_hash, scopes, expires_at, note)
        VALUES (%s, %s, %s, {expires_clause}, {note_clause})
        RETURNING id::text
    """
    with psycopg.connect(_connection_string()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            assert row is not None
            token_id = row[0]
        conn.commit()
    print("Token issued. Copy this exact value into the claudebox credentials")
    print("store; it will not be shown again.")
    print()
    print(f"  id:     {token_id}")
    print(f"  label:  {args.label}")
    print(f"  scopes: {','.join(scopes)}")
    print(f"  token:  {plaintext}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    sql = "UPDATE mcp_tokens SET revoked_at = now() WHERE label = %s AND revoked_at IS NULL RETURNING id::text"
    with psycopg.connect(_connection_string()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (args.label,))
            rows = cur.fetchall()
        conn.commit()
    if not rows:
        print(f"No active token with label={args.label!r} to revoke.", file=sys.stderr)
        return 1
    for row in rows:
        print(f"Revoked token id={row[0]} (label={args.label})")
    return 0


def _list(_args: argparse.Namespace) -> int:
    sql = """
        SELECT id::text, label, scopes, created_at, expires_at, revoked_at, note
        FROM mcp_tokens
        ORDER BY created_at DESC
        LIMIT 200
    """
    with psycopg.connect(_connection_string()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    if not rows:
        print("(no mcp_tokens rows)")
        return 0
    print(f"{'id':<38} {'label':<32} {'scopes':<48} status")
    for tid, label, scopes, _created, _expires, revoked_at, _note in rows:
        status = "revoked" if revoked_at else "active"
        scope_str = ",".join(scopes) if scopes else ""
        print(f"{tid:<38} {label:<32} {scope_str:<48} {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Manage MCP-server bearer tokens")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_issue = sub.add_parser("issue", help="Issue a new token")
    p_issue.add_argument("--label", required=True, help="Operator-readable label")
    p_issue.add_argument(
        "--scopes",
        required=True,
        help="Comma-separated scopes (e.g. db:read,rag:read,logs:read)",
    )
    p_issue.add_argument(
        "--expires-at",
        default=None,
        help="Optional ISO-8601 timestamp (timezone-aware) for token expiry",
    )
    p_issue.add_argument("--note", default=None, help="Free-form note")
    p_issue.set_defaults(func=_issue)

    p_revoke = sub.add_parser("revoke", help="Revoke a token by label")
    p_revoke.add_argument("--label", required=True)
    p_revoke.set_defaults(func=_revoke)

    p_list = sub.add_parser("list", help="List active and revoked tokens")
    p_list.set_defaults(func=_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
