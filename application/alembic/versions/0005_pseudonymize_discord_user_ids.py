"""0005 pseudonymize_discord_user_ids — HMAC-pseudonymize stored Discord identifiers.

Replaces every plaintext ``discord:<raw_id>`` value across user-keyed
tables with a deterministic HMAC-SHA256 over a server-side pepper
(``USER_ID_PEPPER``). Same applies to ``agents.mcp_provider_user_id``
which previously held the raw Discord numeric ID. ``agents.name``
loses its embedded Discord display name and becomes the constant
``"Aztec MCP"``.

Why HMAC + pepper rather than plain hash: Discord user IDs are 18-digit
numbers (~2^60 space). A plain SHA-256 over a leaked DB lets an
attacker brute-force every Discord ID and rebuild the mapping in
minutes. HMAC with a server-side secret defeats that.

Why this migration is gated on ``USER_ID_PEPPER`` being set:
without the pepper, pseudonymization silently becomes the identity
function — we'd ship a "migration ran successfully" log line while
storing the same plaintext we started with. Fail-closed.

Migration order (sandwich, per codex review):

1. **Pre-create** the pseudonymous ``users`` row with
   ``INSERT ... ON CONFLICT DO NOTHING``, copying the existing
   ``agent_preferences`` from the old row. Without this, the
   per-table BEFORE INSERT/UPDATE trigger from migration
   ``0015_user_id_fk`` would auto-create a new ``users`` row with
   the column default and silently lose any UI-set preferences.
2. **Update every user-keyed child table's ``user_id``** to the new
   pseudo. Postgres does not provide UPDATE cascades on FKs, so
   ``conversation_messages``, ``shared_conversations``, and
   ``pending_tool_state`` cannot be skipped just because they
   cascade on DELETE.
3. Update ``agents.mcp_provider_user_id`` (bare HMAC; not user-keyed
   but pseudonymized for the same reason).
4. Strip ``agents.name`` to the constant ``"Aztec MCP"`` for every
   row where ``mcp_provider = 'discord'``.
5. **Delete** the old ``users`` row last.

Revision ID: 0005_pseudonymize_discord_user_ids
Revises: 0004_sources_is_public
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import os
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0005_pseudonymize_discord_user_ids"
down_revision: Union[str, None] = "0004_sources_is_public"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables with a ``user_id`` column that takes the prefixed pseudo.
# Order is intentional: parent ``users`` is rewritten LAST (sandwich)
# even though the FK is ON DELETE RESTRICT — UPDATE-time integrity
# is enforced by the trigger from migration 0015_user_id_fk.
_USER_KEYED_CHILD_TABLES: tuple[str, ...] = (
    "prompts",
    "user_tools",
    "token_usage",
    "user_logs",
    "stack_logs",
    "agent_folders",
    "sources",
    "agents",
    "attachments",
    "memories",
    "todos",
    "notes",
    "connector_sessions",
    "conversations",
    "conversation_messages",
    "shared_conversations",
    "pending_tool_state",
    "workflows",
    "workflow_runs",
)


_DISCORD_PSEUDO_PREFIX = "discord_p_v1:"
_HMAC_HEX_LEN = 32


def _pseudo_bare(raw_id: str, pepper: str) -> str:
    return _hmac.new(
        pepper.encode("utf-8"), raw_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:_HMAC_HEX_LEN]


def _pseudo_canonical(raw_id: str, pepper: str) -> str:
    return _DISCORD_PSEUDO_PREFIX + _pseudo_bare(raw_id, pepper)


def _validate_pepper(pepper: str) -> None:
    """Same contract as ``Settings.USER_ID_PEPPER``'s validator —
    duplicated here so the migration cannot run with an invalid-but-
    nonempty pepper that would later disagree with the running app's
    pseudonyms.

    If this drifts, an operator could run Alembic with e.g.
    ``USER_ID_PEPPER=xxx...`` (32 hex-looking but invalid chars),
    commit pseudonyms, then boot the app with a corrected pepper.
    The corrected pepper produces different HMACs, so
    ``/api/internal/forget_discord_user`` would silently fail to
    find anything.
    """
    if not pepper:
        raise RuntimeError(
            "USER_ID_PEPPER must be set in the environment before this "
            "migration runs. Without it, pseudonymization is a silent "
            "no-op. Aborting."
        )
    try:
        decoded = bytes.fromhex(pepper)
    except ValueError as exc:
        raise RuntimeError(
            "USER_ID_PEPPER must be hex-encoded (run `openssl rand -hex 32`)."
        ) from exc
    if len(decoded) < 16:
        raise RuntimeError(
            f"USER_ID_PEPPER must decode to >=16 bytes; got {len(decoded)}. "
            "Use `openssl rand -hex 32` for 32 bytes (recommended)."
        )


def do_pseudonymize_discord_users(conn) -> dict[str, int]:
    """The migration's actual logic, exposed as an importable callable
    so the test suite can drive it directly without fighting Alembic.

    Returns a per-table dict of ``rows_rewritten`` for diagnostic
    logging. Raises ``RuntimeError`` if ``USER_ID_PEPPER`` is missing
    or invalid (same contract as the app's settings validator) so
    the caller can abort before any UPDATE runs.
    """
    pepper = os.environ.get("USER_ID_PEPPER", "")
    _validate_pepper(pepper)

    # 1. Discover every distinct raw Discord ID still in plaintext form.
    raw_ids = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT DISTINCT user_id FROM users "
                "WHERE user_id LIKE 'discord:%'"
            )
        ).fetchall()
    ]
    if not raw_ids:
        return {"users_rewritten": 0, "tables_touched": 0}

    rewritten: dict[str, int] = {}
    for raw_user_id in raw_ids:
        # ``raw_user_id`` is the full ``discord:<id>`` form.
        bare_id = raw_user_id.removeprefix("discord:")
        new_user_id = _pseudo_canonical(bare_id, pepper)

        # Step 1: pre-create the pseudonymous ``users`` row. Carries
        # over agent_preferences and timestamps so the trigger doesn't
        # later overwrite them with column defaults.
        conn.execute(
            text(
                """
                INSERT INTO users (user_id, agent_preferences, created_at, updated_at)
                SELECT :new_uid, agent_preferences, created_at, updated_at
                  FROM users WHERE user_id = :old_uid
                ON CONFLICT (user_id) DO NOTHING
                """
            ),
            {"new_uid": new_user_id, "old_uid": raw_user_id},
        )

        # Step 2: rewrite every user-keyed child table.
        for table in _USER_KEYED_CHILD_TABLES:
            result = conn.execute(
                text(
                    f"UPDATE {table} SET user_id = :new_uid "
                    f"WHERE user_id = :old_uid"
                ),
                {"new_uid": new_user_id, "old_uid": raw_user_id},
            )
            rewritten[table] = rewritten.get(table, 0) + (result.rowcount or 0)

        # Step 3: pseudonymize the bare HMAC slot in ``agents``.
        new_provider_user_id = _pseudo_bare(bare_id, pepper)
        conn.execute(
            text(
                "UPDATE agents SET mcp_provider_user_id = :new_pid "
                "WHERE mcp_provider = 'discord' "
                "AND mcp_provider_user_id = :old_pid"
            ),
            {"new_pid": new_provider_user_id, "old_pid": bare_id},
        )

        # Step 5 (4 is a sweep over all Discord agents at the end):
        # delete the now-orphaned old ``users`` row.
        conn.execute(
            text("DELETE FROM users WHERE user_id = :old_uid"),
            {"old_uid": raw_user_id},
        )

    # Step 4 (run once at the end, not per-user): scrub any embedded
    # Discord display name from agents.name. Catches both rows we
    # just rewrote AND any pre-existing Discord rows that never went
    # through pseudonymization (defense-in-depth).
    name_result = conn.execute(
        text(
            "UPDATE agents SET name = 'Aztec MCP' "
            "WHERE mcp_provider = 'discord' AND name <> 'Aztec MCP'"
        )
    )
    rewritten["agents.name"] = name_result.rowcount or 0
    rewritten["users_rewritten"] = len(raw_ids)
    rewritten["tables_touched"] = len(_USER_KEYED_CHILD_TABLES)
    return rewritten


def upgrade() -> None:
    bind = op.get_bind()
    do_pseudonymize_discord_users(bind)


def downgrade() -> None:
    # Pseudonymization is intentionally one-way. The HMAC pepper is
    # the only path back to a raw Discord ID; without it we cannot
    # reverse this migration. Operators who need to roll back must
    # restore from a pre-migration backup.
    raise RuntimeError(
        "0005_pseudonymize_discord_user_ids cannot be downgraded — "
        "the original Discord IDs are intentionally not recoverable "
        "from the stored pseudonyms. Restore from backup if needed."
    )
