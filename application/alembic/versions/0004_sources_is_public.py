"""0004 sources_is_public — public/system-source visibility flag.

Adds ``sources.is_public`` so corpora can be marked as readable by any
authenticated user regardless of ``sources.user_id``. The Aztec
v4.2.0 corpora ingested as ``user_id='local'`` are backfilled to
``is_public=TRUE`` so the new ``list_visible_by_ids`` resolver lets
Discord MCP agents (``user_id='discord:<id>'``) see them without the
narrow agent-row exception introduced in step 1 of the auth/source-
visibility plan.

The data step is folded into this migration so there's never a window
where the resolver enforces ``is_public`` against unbackfilled rows
(which would silently break the widget agent path during a deploy).

Revision ID: 0004_sources_is_public
Revises: 0003_mcp_provisioning
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0004_sources_is_public"
down_revision: Union[str, None] = "0003_mcp_provisioning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Names backfilled to ``is_public=TRUE``. Source-of-truth is
# ``AZTEC_SOURCE_IDS`` in ``.env``; we match by name here so the migration
# isn't environment-dependent and is replayable on any database that has
# the standard Aztec corpus ingested.
_PUBLIC_SOURCE_NAMES: tuple[str, ...] = (
    "Aztec Developer Docs v4.2.0",
    "Aztec.nr Framework v4.2.0",
    "Noir Language Docs v4.2.0",
    "Aztec Example Contracts v4.2.0",
    "aztec.js SDK v4.2.0",
    "Aztec TypeScript API v4.2.0",
    "Noir stdlib v4.2.0",
    "Aztec CLI v4.2.0",
    "Aztec Network Docs v4.2.0",
    "Aztec E2E Tests v4.2.0",
    "Aztec Protocol Circuits v4.2.0",
    "Aztec L1 Contracts v4.2.0",
)


def upgrade() -> None:
    # Step a: nullable column so the backfill can run without violating
    # NOT NULL on existing rows.
    op.execute("ALTER TABLE sources ADD COLUMN is_public BOOLEAN")

    # Step b: backfill known-public Aztec corpora by name. Anything not
    # in the list stays NULL through this step and gets defaulted to
    # FALSE in step c.
    name_list = ", ".join(f"'{n}'" for n in _PUBLIC_SOURCE_NAMES)
    op.execute(
        f"UPDATE sources SET is_public = TRUE WHERE name IN ({name_list})"
    )
    op.execute("UPDATE sources SET is_public = FALSE WHERE is_public IS NULL")

    # Step c: lock down the column. After this, every new source row
    # defaults to is_public=FALSE; opting in is an explicit DDL or a
    # column override at insert time.
    op.execute(
        """
        ALTER TABLE sources
            ALTER COLUMN is_public SET NOT NULL,
            ALTER COLUMN is_public SET DEFAULT FALSE;
        CREATE INDEX idx_sources_is_public_true
            ON sources(id) WHERE is_public = TRUE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_sources_is_public_true;
        ALTER TABLE sources DROP COLUMN IF EXISTS is_public;
        """
    )
