"""0009 agents_surface — add ``agents.surface`` taxonomy column.

``agents.name`` was doing two jobs at once: a user-visible display label
AND an implicit surface taxonomy (Discord bot vs widget vs public /ask
vs MCP server). The labels do not line up with the surfaces in a
discoverable way — e.g. the agent named ``Aztec 4.2.0`` is the Discord
bot, and ``docs.aztec.network`` is the widget only by coincidence. A
weekly usage report joined on ``agents.name`` and mis-attributed all
Discord traffic as ``Ask Aztec public web``, which is what motivated
this column.

This migration adds a strict ``surface`` column with a CHECK-constrained
enum so analytics, dashboards, and per-surface limits have a stable
axis. Active insert paths must pass ``surface`` explicitly; there is
no DEFAULT — that is deliberate, so a forgotten provisioner trips the
NOT NULL at write time instead of silently mis-labelling rows.

Backfill mapping for the 7 existing rows (verified against prod DB):

* ``mcp_provider IS NOT NULL`` → ``mcp`` (provider-agnostic key — covers
  the 4 Discord-bound MCP rows and any future non-Discord MCP variants)
* ``name='Aztec 4.2.0' AND user_id='local'`` → ``discord``
* ``name='docs.aztec.network' AND user_id='local'`` → ``widget``
* ``name='Ask Aztec — public web' AND user_id='public-web'`` → ``web_ask``
* ``user_id='eval-variant'`` → ``eval`` (throwaway comparison agents
  provisioned by ``scripts/eval/provision_test_agent.py``)

The migration is wrapped in a single Alembic transaction:
ADD COLUMN nullable → UPDATE → SET NOT NULL → ADD CHECK. Postgres is
fine with this ordering on a ~10-row table; locking is not a concern.

This migration also extends the ``docsgpt_mcp_ro`` column allowlist
from migration ``0006_mcp_admin_tables`` so the host-side MCP server
can ``GROUP BY surface`` for reporting. Without this, the weekly-report
use case that motivated the column is blocked.

Operator note for rollout: the old backend container will fail to
insert agents after ``SET NOT NULL`` lands but before the new code
ships, so stop ``discord-bot`` (the only writer in the short window)
during the migration-and-recreate sequence. See CLAUDE.md for the
deploy ordering.

Revision ID: 0009_agents_surface
Revises: 0008_eval_variant_unique
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0009_agents_surface"
down_revision: Union[str, None] = "0008_eval_variant_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ALLOWED_SURFACES = ("discord", "widget", "web_ask", "mcp", "eval")


def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN surface TEXT")

    # mcp_provider goes first so the per-user MCP rows are categorised
    # by the structural column (set by /api/internal/create_mcp_key)
    # rather than the display ``name``. Subsequent branches key off
    # (name, user_id) for the three singleton prod agents that have no
    # provisioner. ``eval-variant`` rows from
    # ``scripts/eval/provision_test_agent.py`` close out the set.
    op.execute(
        """
        UPDATE agents
        SET surface = CASE
            WHEN mcp_provider IS NOT NULL THEN 'mcp'
            WHEN name = 'Aztec 4.2.0' AND user_id = 'local' THEN 'discord'
            WHEN name = 'docs.aztec.network' AND user_id = 'local' THEN 'widget'
            WHEN name = 'Ask Aztec — public web' AND user_id = 'public-web' THEN 'web_ask'
            WHEN user_id = 'eval-variant' THEN 'eval'
        END
        """
    )

    # Fail loud if anything was missed — better than silently letting
    # rows through with NULL and tripping NOT NULL below with a
    # cryptic constraint-violation message. If this fires, a manual
    # UPDATE is required before re-running the migration.
    op.execute(
        """
        DO $$
        DECLARE missing INTEGER;
        BEGIN
            SELECT COUNT(*) INTO missing FROM agents WHERE surface IS NULL;
            IF missing > 0 THEN
                RAISE EXCEPTION
                    'agents.surface backfill missed % row(s); '
                    'set surface manually before re-running migration 0009',
                    missing;
            END IF;
        END
        $$;
        """
    )

    op.execute("ALTER TABLE agents ALTER COLUMN surface SET NOT NULL")
    op.execute(
        "ALTER TABLE agents ADD CONSTRAINT agents_surface_check "
        "CHECK (surface IN ("
        + ", ".join(f"'{s}'" for s in _ALLOWED_SURFACES)
        + "))"
    )

    # Extend the column allowlist from migration 0006 so the host-side
    # MCP server (role ``docsgpt_mcp_ro``) can read the new column.
    # Without this grant, ``honk_sql.*`` queries that ``GROUP BY surface``
    # would error with "permission denied for column surface".
    op.execute("GRANT SELECT (surface) ON agents TO docsgpt_mcp_ro")


def downgrade() -> None:
    op.execute("REVOKE SELECT (surface) ON agents FROM docsgpt_mcp_ro")
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_surface_check")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS surface")
