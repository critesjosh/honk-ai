"""0006 mcp_admin_tables — bearer tokens + audit log + read-only role for the host MCP server.

The honk-ai-host MCP server (``application/mcp_server/``) needs two
admin tables and a least-privilege Postgres role:

* ``mcp_tokens`` — stores SHA-256 hashes of issued bearer tokens, the
  granted scopes, an operator-supplied label, and revocation /
  expiration metadata.
* ``mcp_audit`` — append-only call log: every tool invocation writes
  one row regardless of outcome (ok / denied / error). Records the
  token's stable id, the tool name, a SHA-256 hash of the
  JSON-encoded arguments (so we never store raw operator queries),
  status, and elapsed time.
* ``docsgpt_mcp_ro`` — Postgres role the MCP server connects as. NOT
  granted ``pg_read_all_data`` (deliberately) — that role-attribute
  is a blanket SELECT on every column of every table, which would
  hand a ``db:read`` token holder plaintext bearers from
  ``agents.key`` / ``agents.shared_token`` / ``conversations.api_key``
  / ``connector_sessions.session_token`` and friends. Instead we hand-
  list which tables and columns the MCP role can read, excluding all
  known bearer / OAuth / per-user-secret columns. If a future migration
  adds a new secret column, audit this file at the same time.

The role is also pinned to ``default_transaction_read_only = on`` —
the SQL tool path further opts into read-only at the connection level,
and the audit-insert path explicitly opts out via
``SET LOCAL transaction_read_only = off`` for the one INSERT it
performs. See ``application/mcp_server/audit.py``.

Revision ID: 0006_mcp_admin_tables
Revises: 0005_pseudonymize_user_ids
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0006_mcp_admin_tables"
down_revision: Union[str, None] = "0005_pseudonymize_user_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that contain no bearer / secret columns and are safe to expose
# in full to the MCP role. Adding new tables to the schema: decide
# whether they contain user-supplied secrets and add to this list or
# the column-allowlisted block below accordingly.
_FULL_SELECT_TABLES = (
    "users",
    "prompts",
    "app_metadata",
    "agent_folders",
    "sources",
    "attachments",
    "memories",
    "todos",
    "notes",
    "conversation_messages",
    "workflows",
    "workflow_nodes",
    "workflow_edges",
    "workflow_runs",
    "mcp_audit",
    # NOTE: ``documents`` (the pgvector chunks table) is NOT in this
    # list because it is created at runtime by PGVectorStore on first
    # ingest, not by alembic. The conditional DO block at the end of
    # ``upgrade()`` grants SELECT once the table exists; post-first-
    # ingest deployments may need to grant manually — see the operator
    # runbook in AZTEC_SETUP.md.
)


# Columns the MCP role is allowed to SELECT from secret-bearing tables.
# Anything not listed is unreadable through this role — operators with
# a legitimate need for those columns must go through the privileged
# ``docsgpt`` role (psql / migration script), not the MCP surface.
#
# Audit policy: when a migration ADDS a column to one of these tables,
# explicitly decide whether to add it to the allowlist. Default to
# leaving it off — new columns are unreadable until reviewed.
_COLUMN_ALLOWLIST = {
    # user_tools: ``config`` is a JSONB that carries per-tool credentials
    # (Brave / Telegram / Postgres / MCP-tool API keys); ``actions`` can
    # also carry callback URLs with embedded tokens. Both excluded.
    "user_tools": (
        "id",
        "user_id",
        "name",
        "custom_name",
        "display_name",
        "description",
        "config_requirements",
        "status",
        "created_at",
        "updated_at",
        "legacy_mongo_id",
    ),
    # user_logs: ``data`` is a free-form JSONB request log. Repositories
    # write ``data->>'api_key'`` into it for some endpoints, so the
    # column itself carries plaintext bearers. Excluded.
    "user_logs": (
        "id",
        "user_id",
        "endpoint",
        "timestamp",
    ),
    # pending_tool_state: every JSONB column on this table carries
    # in-flight tool-call state including ``agent_config.api_key`` /
    # ``user_api_key``. Excluded.
    "pending_tool_state": (
        "id",
        "conversation_id",
        "user_id",
        "created_at",
        "expires_at",
    ),
    # agents: ``key`` / ``shared_token`` / ``incoming_webhook_token`` are
    # plaintext bearers handed to Discord users via /mcp-key. Excluded.
    "agents": (
        "id",
        "user_id",
        "name",
        "description",
        "agent_type",
        "status",
        "image",
        "source_id",
        "extra_source_ids",
        "chunks",
        "retriever",
        "prompt_id",
        "tools",
        "json_schema",
        "models",
        "default_model_id",
        "folder_id",
        "workflow_id",
        "limited_token_mode",
        "token_limit",
        "limited_request_mode",
        "request_limit",
        "allow_system_prompt_override",
        "shared",
        "shared_metadata",
        "created_at",
        "updated_at",
        "last_used_at",
        "legacy_mongo_id",
        "mcp_provider",
        "mcp_provider_user_id",
        "mcp_purpose",
    ),
    # conversations: ``api_key`` / ``shared_token`` are bearers the live
    # chat path uses to attribute requests. Excluded.
    "conversations": (
        "id",
        "user_id",
        "agent_id",
        "name",
        "is_shared_usage",
        "shared_with",
        "compression_metadata",
        "date",
        "created_at",
        "updated_at",
        "legacy_mongo_id",
    ),
    # shared_conversations: ``api_key`` excluded for the same reason.
    "shared_conversations": (
        "id",
        "uuid",
        "conversation_id",
        "user_id",
        "prompt_id",
        "chunks",
        "is_promptable",
        "first_n_queries",
        "created_at",
    ),
    # token_usage: ``api_key`` is the live request-time bearer.
    "token_usage": (
        "id",
        "user_id",
        "agent_id",
        "prompt_tokens",
        "generated_tokens",
        "timestamp",
    ),
    # stack_logs: same as token_usage.
    "stack_logs": (
        "id",
        "activity_id",
        "endpoint",
        "level",
        "user_id",
        "query",
        "stacks",
        "timestamp",
    ),
    # connector_sessions: ``session_token`` is an OAuth bearer, ``token_info``
    # JSONB carries OAuth state (access + refresh tokens), and ``user_email``
    # is direct PII tying a Discord pseudonym back to an email address.
    # All three excluded.
    "connector_sessions": (
        "id",
        "user_id",
        "provider",
        "server_url",
        "status",
        "session_data",
        "expires_at",
        "created_at",
        "legacy_mongo_id",
    ),
}


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mcp_tokens (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            label        TEXT NOT NULL,
            -- 64 hex chars = SHA-256 hex digest. CHAR(64) keeps the
            -- column compact on disk and lets a misconfigured
            -- non-hex insert error out at write time.
            token_hash   CHAR(64) NOT NULL UNIQUE,
            -- Granted scopes — see application/mcp_server/auth.py for
            -- the canonical list. TEXT[] (not JSONB) so we get
            -- straightforward array containment queries.
            scopes       TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ,
            revoked_at   TIMESTAMPTZ,
            note         TEXT
        );

        CREATE INDEX mcp_tokens_active_idx
            ON mcp_tokens (token_hash)
            WHERE revoked_at IS NULL;

        CREATE TABLE mcp_audit (
            id           BIGSERIAL PRIMARY KEY,
            -- Nullable: anonymous / unauthenticated calls (e.g. a
            -- request that fails token verification before reaching
            -- a tool handler) do not produce audit rows; rows for
            -- known callers always have a non-null token_id.
            token_id     UUID REFERENCES mcp_tokens(id) ON DELETE SET NULL,
            tool         TEXT NOT NULL,
            args_hash    CHAR(64) NOT NULL,
            -- ok / denied / error. Free-form on purpose so the server
            -- can introduce new statuses without a migration.
            status       TEXT NOT NULL,
            row_count    INTEGER,
            byte_count   INTEGER,
            elapsed_ms   INTEGER NOT NULL,
            error        TEXT,
            ts           TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX mcp_audit_ts_idx ON mcp_audit (ts DESC);
        CREATE INDEX mcp_audit_tool_ts_idx ON mcp_audit (tool, ts DESC);
        CREATE INDEX mcp_audit_token_ts_idx ON mcp_audit (token_id, ts DESC);
        """
    )

    # Least-privilege role for the MCP server. We use DO/EXCEPTION so
    # the migration is replayable on environments where the role
    # already exists (operator may have created it manually before
    # running migrations).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'docsgpt_mcp_ro'
            ) THEN
                CREATE ROLE docsgpt_mcp_ro
                    NOLOGIN
                    NOINHERIT;
            END IF;
        END
        $$;

        GRANT USAGE ON SCHEMA public TO docsgpt_mcp_ro;

        -- Audit table needs INSERT (the MCP server appends a row per
        -- tool call) and SELECT (operators query the log via the SQL
        -- tool). The audit-insert path in application/mcp_server/audit.py
        -- explicitly issues SET LOCAL transaction_read_only = off so the
        -- INSERT runs even with the role's default read-only mode below.
        GRANT INSERT ON mcp_audit TO docsgpt_mcp_ro;
        GRANT USAGE, SELECT ON SEQUENCE mcp_audit_id_seq TO docsgpt_mcp_ro;

        -- Tokens table: SELECT only (auth path reads token_hash + scopes).
        -- Issuance writes via the privileged ``docsgpt`` role from
        -- scripts/mcp/issue_token.py.
        GRANT SELECT ON mcp_tokens TO docsgpt_mcp_ro;

        -- Read-only at the session level: default_transaction_read_only
        -- blocks DML/DDL even if a future grant accidentally leaks write
        -- privileges. The audit-insert path opts out per-transaction via
        -- SET LOCAL; everything else stays read-only.
        ALTER ROLE docsgpt_mcp_ro SET default_transaction_read_only = on;
        """
    )

    # Full-table SELECTs on the non-secret-bearing tables. Built as one
    # statement so a future audit run can grep for ``GRANT SELECT ON``
    # in this file and see the complete allowlist at a glance.
    full_select_sql = "GRANT SELECT ON " + ", ".join(_FULL_SELECT_TABLES) + " TO docsgpt_mcp_ro;"
    op.execute(full_select_sql)

    # Column-level SELECTs on the secret-bearing tables. Anything NOT in
    # the allowlist is unreadable through the MCP surface.
    for table, columns in _COLUMN_ALLOWLIST.items():
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON {table} TO docsgpt_mcp_ro;")

    # ``documents`` (pgvector chunks) is created by PGVectorStore at
    # runtime, not by alembic, so it may not exist when this migration
    # first runs on a fresh database. Wrap the grant in a DO block so
    # the migration is idempotent: if the table is already in place
    # (re-run on a populated DB, or after first ingest), the grant
    # applies; otherwise the block is a no-op and the operator runs
    # ``GRANT SELECT ON documents TO docsgpt_mcp_ro;`` manually after
    # first ingest. See AZTEC_SETUP.md → "Honk-ai MCP server".
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_class
                WHERE relname = 'documents' AND relkind = 'r'
            ) THEN
                GRANT SELECT ON documents TO docsgpt_mcp_ro;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    # REVOKE the column-level grants explicitly — Postgres needs the
    # matching column list, otherwise the REVOKE is a no-op for partial
    # grants.
    for table, columns in _COLUMN_ALLOWLIST.items():
        op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON {table} FROM docsgpt_mcp_ro;")

    # ``documents`` may or may not exist — same DO-block guard as upgrade.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_class
                WHERE relname = 'documents' AND relkind = 'r'
            ) THEN
                REVOKE SELECT ON documents FROM docsgpt_mcp_ro;
            END IF;
        END
        $$;
        """
    )

    op.execute("REVOKE SELECT ON " + ", ".join(_FULL_SELECT_TABLES) + " FROM docsgpt_mcp_ro;")
    op.execute(
        """
        REVOKE INSERT ON mcp_audit FROM docsgpt_mcp_ro;
        REVOKE USAGE, SELECT ON SEQUENCE mcp_audit_id_seq FROM docsgpt_mcp_ro;
        REVOKE SELECT ON mcp_tokens FROM docsgpt_mcp_ro;
        REVOKE USAGE ON SCHEMA public FROM docsgpt_mcp_ro;
        DROP TABLE IF EXISTS mcp_audit;
        DROP TABLE IF EXISTS mcp_tokens;
        DROP ROLE IF EXISTS docsgpt_mcp_ro;
        """
    )
