"""Periodic Celery tasks for the Aztec fork.

Two beat schedules survive the upstream-admin-API removal:

- ``cleanup_pending_tool_state`` — Postgres has no native row TTL, so we
  sweep ``pending_tool_state`` every 60 s. Replaces the Mongo
  ``expireAfterSeconds=0`` index that the upstream code relied on.
- ``purge_old_user_data`` — daily retention sweep aligned with the Aztec
  Foundation privacy policy (https://aztec.network/privacy-policy):
  "Technical usage information: 12 months". Agents (including
  MCP-provisioned Discord agents) are intentionally preserved so issued
  API keys keep working; only conversational content ages out.

This module replaces the trio of beat-schedule registrations that used
to live in ``application/api/user/tasks.py`` (deleted alongside the
admin SPA).
"""

from datetime import timedelta

from application.celery_init import celery


# Use ``on_after_finalize`` instead of ``on_after_configure`` —
# ``configure`` fires when the app is configured (i.e. before the
# ``imports`` modules in celeryconfig are even loaded), so the previous
# upstream registration only worked because the tasks module was
# imported in-process by the routes blueprint at app boot. With the
# tasks rehomed under ``application/workers/periodic.py`` and loaded
# via Celery's own ``imports`` setting, ``finalize`` is the only signal
# guaranteed to fire AFTER this module is imported. Without this
# change the beat schedule registers no periodic tasks and retention
# sweeps + pending-tool-state TTL cleanup silently stop running.
@celery.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(
        timedelta(seconds=60),
        cleanup_pending_tool_state.s(),
        name="cleanup-pending-tool-state",
    )
    # Daily so the purge window slides forward continuously.
    sender.add_periodic_task(
        timedelta(days=1),
        purge_old_user_data.s(),
        name="purge-old-user-data",
    )


@celery.task(bind=True)
def purge_old_user_data(self, retention_days: int = 365):
    """Delete user-generated content older than ``retention_days``.

    Tables purged:

    - ``conversation_messages`` — purged by **the message's own
      ``timestamp``**, not the parent conversation's ``updated_at``.
      A long-running thread that gets one new reply must not preserve
      its old messages just because the parent row was touched.
    - ``conversations`` — only rows whose remaining ``updated_at`` is
      past the retention cutoff AND whose ``conversation_messages``
      table is now empty are dropped. This sweeps up "skeleton" rows
      orphaned by the message purge above without yanking active
      threads. ``shared_conversations`` and ``pending_tool_state``
      cascade-delete via FK.
    - ``user_logs``, ``stack_logs``, ``token_usage`` — append-only
      operational tables keyed on ``timestamp``.

    Each table runs in its own transaction so a failure on one table
    does not roll back the others (a 24h gap is preferable to a stuck
    purge).
    """
    from sqlalchemy import text

    from application.core.settings import settings

    if not settings.POSTGRES_URI:
        return {"skipped": "POSTGRES_URI not set"}

    from application.storage.db.engine import get_engine

    engine = get_engine()
    cutoff_sql = f"now() - interval '{int(retention_days)} days'"
    purged: dict[str, int] = {}

    statements = (
        ("conversation_messages",
            f"DELETE FROM conversation_messages WHERE timestamp < {cutoff_sql}"),
        ("conversations",
            f"DELETE FROM conversations c "
            f"WHERE c.updated_at < {cutoff_sql} "
            f"AND NOT EXISTS ("
            f"  SELECT 1 FROM conversation_messages m "
            f"  WHERE m.conversation_id = c.id"
            f")"),
        ("user_logs",
            f"DELETE FROM user_logs WHERE timestamp < {cutoff_sql}"),
        ("stack_logs",
            f"DELETE FROM stack_logs WHERE timestamp < {cutoff_sql}"),
        ("token_usage",
            f"DELETE FROM token_usage WHERE timestamp < {cutoff_sql}"),
    )
    for table, sql in statements:
        try:
            with engine.begin() as conn:
                result = conn.execute(text(sql))
                purged[table] = result.rowcount or 0
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "purge_old_user_data: failed on %s", table
            )
            purged[table] = -1
    return {"retention_days": retention_days, "purged": purged}


@celery.task(bind=True)
def cleanup_pending_tool_state(self):
    """Delete pending_tool_state rows past their TTL."""
    from application.core.settings import settings
    if not settings.POSTGRES_URI:
        return {"deleted": 0, "skipped": "POSTGRES_URI not set"}

    from application.storage.db.engine import get_engine
    from application.storage.db.repositories.pending_tool_state import (
        PendingToolStateRepository,
    )

    engine = get_engine()
    with engine.begin() as conn:
        deleted = PendingToolStateRepository(conn).cleanup_expired()
    return {"deleted": deleted}
