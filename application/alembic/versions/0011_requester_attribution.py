"""0011 requester_attribution — per-message requester pseudonym + erasure marker.

Bot chat turns (Discord/Slack) are persisted under the shared chat agent's
owner ``user_id='local'`` (StreamProcessor overwrites the request identity with
the agent owner), so ``/forget-me`` — which deletes rows keyed to the user's
pseudonym — could never reach a bot user's own questions/answers. This migration
adds the attribution needed to erase them.

Columns added:

* ``conversation_messages.requester_user_id`` (text, nullable) — the canonical
  pseudonym (``discord_p_v1:<hex>`` / ``slack_p_v1:<hex>``, identical to what the
  forget endpoint computes) of the end-user who triggered THAT turn. Per-message
  (not per-conversation) because thread conversations are SHARED: multiple users
  append to one ``conversation_id``, so only message-level attribution erases the
  right turns. NULL = anonymous/unattributable (widget, ``/ask``, pre-migration
  rows) — not per-user erasable; ages out via the retention purge.
* ``conversation_messages.erased_at`` (timestamptz, nullable) — tombstone
  marker. ``/forget-me`` REDACTS in place (NULLs content, sets ``erased_at``)
  rather than DELETEing the row, so the ``MAX(position)+1`` allocation and the
  bots' in-memory ``answer_count``/feedback-position contract stay intact.
* ``user_logs.requester_user_id`` (text, nullable) — same pseudonym; successful
  streams copy the full question/response/sources into ``user_logs.data``, so the
  log row is a second content-bearing copy that forget must erase (by DELETE —
  logs have no position contract).
* ``stack_logs.requester_user_id`` (text, nullable) — same pseudonym; the
  ``@log_activity`` decorator writes the bot's ``query`` (prompt) to
  ``stack_logs.query``. No ``conversation_id`` column there, so the attribution
  column is the only erasure key (DELETE on forget).
* ``pending_tool_state.requester_user_id`` (text, nullable) — same pseudonym;
  paused tool-continuation state stores the full ``messages``/``pending_tool_calls``
  under the shared ``user_id='local'`` owner and no longer cascades on forget
  (we tombstone, not delete, the parent conversation), so it carries the
  attribution column to be erasable by ``requester_user_id`` (DELETE on forget).
  Classic prod agents never pause, so this is currently dead weight, but keeps
  erasure correct for any future tool-using chat agent.

**No FK** on any ``requester_user_id`` column: the ``ensure_user_exists`` /
``conversation_messages_fill_user_id`` triggers act only on ``user_id`` (and
``UPDATE OF user_id``), never on these columns, so no ``users`` row is
auto-created for a requester pseudonym. These are denormalized deletion tags, not
ownership links — a bot chat user never gets a ``users`` row.

**No new GRANTs.** Each new column inherits whatever grant its table already has
for ``docsgpt_mcp_ro`` (set in migration 0006 — full-table on
``conversation_messages``, column-allowlisted on the log tables, none on
``pending_tool_state``). ``requester_user_id`` is a one-way pseudonym (same
sensitivity class as the already-readable ``user_id``) and ``erased_at`` is a
timestamp — neither is a secret like the bearer keys 0007 walls off in ``data`` —
so no grant change is warranted either way. (A column allowlist can be added
later if policy changes.)

Revision ID: 0011_requester_attribution
Revises: 0010_agents_surface_slack
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0011_requester_attribution"
down_revision: Union[str, None] = "0010_agents_surface_slack"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE conversation_messages ADD COLUMN requester_user_id text"
    )
    op.execute(
        "ALTER TABLE conversation_messages ADD COLUMN erased_at timestamptz"
    )
    op.execute("ALTER TABLE user_logs ADD COLUMN requester_user_id text")
    op.execute("ALTER TABLE stack_logs ADD COLUMN requester_user_id text")
    op.execute("ALTER TABLE pending_tool_state ADD COLUMN requester_user_id text")

    # Partial indexes: forget looks rows up by requester_user_id, and the vast
    # majority of rows (widget/anonymous) carry NULL, so a partial index keeps
    # it small.
    op.execute(
        "CREATE INDEX conversation_messages_requester_idx "
        "ON conversation_messages (requester_user_id) "
        "WHERE requester_user_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX user_logs_requester_idx "
        "ON user_logs (requester_user_id) "
        "WHERE requester_user_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX stack_logs_requester_idx "
        "ON stack_logs (requester_user_id) "
        "WHERE requester_user_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX pending_tool_state_requester_idx "
        "ON pending_tool_state (requester_user_id) "
        "WHERE requester_user_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pending_tool_state_requester_idx")
    op.execute("DROP INDEX IF EXISTS stack_logs_requester_idx")
    op.execute("DROP INDEX IF EXISTS user_logs_requester_idx")
    op.execute("DROP INDEX IF EXISTS conversation_messages_requester_idx")
    op.execute("ALTER TABLE pending_tool_state DROP COLUMN IF EXISTS requester_user_id")
    op.execute("ALTER TABLE stack_logs DROP COLUMN IF EXISTS requester_user_id")
    op.execute("ALTER TABLE user_logs DROP COLUMN IF EXISTS requester_user_id")
    op.execute(
        "ALTER TABLE conversation_messages DROP COLUMN IF EXISTS erased_at"
    )
    op.execute(
        "ALTER TABLE conversation_messages DROP COLUMN IF EXISTS requester_user_id"
    )
