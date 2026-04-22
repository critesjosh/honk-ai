"""0003 mcp_provisioning — identity columns for MCP-key provisioning.

Adds `mcp_provider`, `mcp_provider_user_id`, `mcp_purpose` to the
`agents` table and a partial unique index so the
`/api/internal/create_mcp_key` endpoint can atomically upsert one agent
per (provider, provider_user_id, purpose) triple. Replaces the Mongo-era
sparse compound index on the `agents` collection.

Revision ID: 0003_mcp_provisioning
Revises: 0002_app_metadata
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0003_mcp_provisioning"
down_revision: Union[str, None] = "0002_app_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agents
            ADD COLUMN mcp_provider         TEXT,
            ADD COLUMN mcp_provider_user_id TEXT,
            ADD COLUMN mcp_purpose          TEXT;

        CREATE UNIQUE INDEX agents_mcp_identity_idx
            ON agents (mcp_provider, mcp_provider_user_id, mcp_purpose)
            WHERE mcp_provider IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS agents_mcp_identity_idx;
        ALTER TABLE agents
            DROP COLUMN IF EXISTS mcp_purpose,
            DROP COLUMN IF EXISTS mcp_provider_user_id,
            DROP COLUMN IF EXISTS mcp_provider;
        """
    )
