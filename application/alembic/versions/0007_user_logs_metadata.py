"""0007 user_logs_metadata — non-secret per-call analytics column + MCP grant.

``user_logs.data`` is a free-form JSONB request log into which the
``/stream`` path writes the request's plaintext ``api_key`` (see
``application/api/answer/routes/base.py:675``). Migration 0006
deliberately excludes the ``data`` column from the
``docsgpt_mcp_ro`` allowlist for exactly that reason — exposing it
would hand any ``db:read`` MCP token holder the live bearer keys of
every agent that has ever streamed an answer.

That exclusion blocks a legitimate operability need: the
``/api/search`` endpoint (used by ``@aztec/mcp-server``) writes one
``user_logs`` row per call, and operators want to surface per-key
search analytics (which agent, which question, how many results, which
sources came back) from the host MCP server. Putting those fields in
``data`` would either (a) keep them invisible to the MCP role, or (b)
require carving out a per-row column allowlist Postgres doesn't
natively support.

This migration adds ``user_logs.metadata`` — a *non-secret-bearing*
JSONB column — and grants ``docsgpt_mcp_ro`` SELECT on it. The
contract for application code:

* ``data`` MAY contain bearer keys / question text / response bodies.
  Treat as secret. Grant policy: SELECT EXCLUDED for ``docsgpt_mcp_ro``.
* ``metadata`` MUST NOT contain bearer keys or other secrets. Anything
  that goes in must be safe to surface to a remote claudebox operator
  via ``honk_sql.*``. Grant policy: SELECT GRANTED for ``docsgpt_mcp_ro``.

Today only ``/api/search`` writes to ``metadata``; ``/stream`` continues
to write its full payload into ``data``. A future change MAY split
``/stream``'s non-secret fields into ``metadata`` too, but that needs a
deliberate redaction pass on the existing row body and is out of scope
here.

Revision ID: 0007_user_logs_metadata
Revises: 0006_mcp_admin_tables
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0007_user_logs_metadata"
down_revision: Union[str, None] = "0006_mcp_admin_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_logs ADD COLUMN metadata jsonb")

    # Grant SELECT only if the MCP role exists. Fresh dev installs that
    # haven't run the MCP server bootstrap will not have the role, and
    # we don't want this migration to require it.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'docsgpt_mcp_ro'
            ) THEN
                GRANT SELECT (metadata) ON user_logs TO docsgpt_mcp_ro;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'docsgpt_mcp_ro'
            ) THEN
                REVOKE SELECT (metadata) ON user_logs FROM docsgpt_mcp_ro;
            END IF;
        END
        $$;
        """
    )
    op.execute("ALTER TABLE user_logs DROP COLUMN metadata")
