"""0010 agents_surface_slack — add ``slack`` to the surface taxonomy.

Migration ``0009_agents_surface`` introduced a CHECK-constrained
``agents.surface`` enum (``discord`` / ``widget`` / ``web_ask`` / ``mcp``
/ ``eval``). The Slack bot adds a new chat surface, so this migration
extends the enum with ``slack``.

Scope is intentionally tiny: only the CHECK constraint changes. The
column already exists and is NOT NULL, and ``0009`` already granted
``docsgpt_mcp_ro`` column-level ``SELECT (surface)`` — so no grant
change is needed for ``honk_sql.* ... GROUP BY surface`` to keep
working. Per-user MCP keys provisioned for Slack users stay
``surface='mcp'`` (the structural ``mcp_provider`` column distinguishes
them); only the singleton Slack chat agent carries ``surface='slack'``.

The constraint swap is non-atomic (DROP then ADD). Stop the writers
(``slack-bot`` / ``discord-bot`` — the only services that INSERT agents)
during the brief window, same advice as ``0009``.

Downgrade fails loudly if any ``surface='slack'`` rows exist rather than
silently violating the restored constraint.

Revision ID: 0010_agents_surface_slack
Revises: 0009_agents_surface
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0010_agents_surface_slack"
down_revision: Union[str, None] = "0009_agents_surface"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Superset of 0009's set plus ``slack``. Kept as the single source of
# truth for the upgraded constraint.
_ALLOWED_SURFACES = ("discord", "widget", "web_ask", "mcp", "eval", "slack")
# 0009's original set, restored on downgrade.
_PREVIOUS_SURFACES = ("discord", "widget", "web_ask", "mcp", "eval")


def _readd_check(surfaces: Sequence[str]) -> None:
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_surface_check")
    op.execute(
        "ALTER TABLE agents ADD CONSTRAINT agents_surface_check "
        "CHECK (surface IN ("
        + ", ".join(f"'{s}'" for s in surfaces)
        + "))"
    )


def upgrade() -> None:
    _readd_check(_ALLOWED_SURFACES)


def downgrade() -> None:
    # Guard: refuse to restore a constraint that existing rows would
    # violate. An operator who hits this must re-point or delete the
    # Slack agent row(s) first. Mirrors 0009's fail-loud backfill guard.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agents WHERE surface = 'slack') THEN
                RAISE EXCEPTION
                    'Cannot downgrade 0010: agents.surface=''slack'' rows exist; '
                    're-point or delete them before reverting the CHECK constraint';
            END IF;
        END
        $$;
        """
    )
    _readd_check(_PREVIOUS_SURFACES)
