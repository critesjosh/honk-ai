"""0008 eval_variant_unique — partial unique indexes for the eval-variant namespace.

``scripts/eval/provision_test_agent.py`` upserts a throwaway agent + prompt
row under ``user_id = 'eval-variant'`` so the variant-comparison workflow
can iterate on a candidate prompt / source list / model without editing
prod agents. The upsert is keyed on ``(user_id, name)`` — sequential
re-runs find the existing row by ``SELECT ... LIMIT 1`` and update it in
place.

Without a uniqueness constraint, two concurrent provisioner invocations
with the same ``--name`` can both miss the SELECT and both INSERT, leaving
duplicate rows that the next ``--list`` / ``--delete`` will silently
collapse onto the oldest. Plain ``UNIQUE (user_id, name)`` on the whole
table is too strong — prod rows under ``public-web`` / ``local`` /
``discord_p_v1:*`` are allowed to share names. A *partial* unique index
scoped to ``user_id = 'eval-variant'`` gives the provisioner the ON
CONFLICT target it needs without touching any other namespace.

Revision ID: 0008_eval_variant_unique
Revises: 0007_user_logs_metadata
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0008_eval_variant_unique"
down_revision: Union[str, None] = "0007_user_logs_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS prompts_eval_variant_name_uniq "
        "ON prompts (name) WHERE user_id = 'eval-variant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS agents_eval_variant_name_uniq "
        "ON agents (name) WHERE user_id = 'eval-variant'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS agents_eval_variant_name_uniq")
    op.execute("DROP INDEX IF EXISTS prompts_eval_variant_name_uniq")
