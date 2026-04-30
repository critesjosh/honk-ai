from datetime import timedelta

from application.celery_init import celery
from application.worker import (
    agent_webhook_worker,
    attachment_worker,
    ingest_worker,
    mcp_oauth,
    mcp_oauth_status,
    remote_worker,
    sync,
    sync_worker,
)


@celery.task(bind=True)
def ingest(
    self, directory, formats, job_name, user, file_path, filename, file_name_map=None
):
    resp = ingest_worker(
        self,
        directory,
        formats,
        job_name,
        file_path,
        filename,
        user,
        file_name_map=file_name_map,
    )
    return resp


@celery.task(bind=True)
def ingest_remote(self, source_data, job_name, user, loader):
    resp = remote_worker(self, source_data, job_name, user, loader)
    return resp


@celery.task(bind=True)
def reingest_source_task(self, source_id, user):
    from application.worker import reingest_source_worker

    resp = reingest_source_worker(self, source_id, user)
    return resp


@celery.task(bind=True)
def schedule_syncs(self, frequency):
    resp = sync_worker(self, frequency)
    return resp


@celery.task(bind=True)
def sync_source(
    self,
    source_data,
    job_name,
    user,
    loader,
    sync_frequency,
    retriever,
    doc_id,
):
    resp = sync(
        self,
        source_data,
        job_name,
        user,
        loader,
        sync_frequency,
        retriever,
        doc_id,
    )
    return resp


@celery.task(bind=True)
def store_attachment(self, file_info, user):
    resp = attachment_worker(self, file_info, user)
    return resp


@celery.task(bind=True)
def process_agent_webhook(self, agent_id, payload):
    resp = agent_webhook_worker(self, agent_id, payload)
    return resp


@celery.task(bind=True)
def ingest_connector_task(
    self,
    job_name,
    user,
    source_type,
    session_token=None,
    file_ids=None,
    folder_ids=None,
    recursive=True,
    retriever="classic",
    operation_mode="upload",
    doc_id=None,
    sync_frequency="never",
):
    from application.worker import ingest_connector

    resp = ingest_connector(
        self,
        job_name,
        user,
        source_type,
        session_token=session_token,
        file_ids=file_ids,
        folder_ids=folder_ids,
        recursive=recursive,
        retriever=retriever,
        operation_mode=operation_mode,
        doc_id=doc_id,
        sync_frequency=sync_frequency,
    )
    return resp


@celery.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(
        timedelta(days=1),
        schedule_syncs.s("daily"),
    )
    sender.add_periodic_task(
        timedelta(weeks=1),
        schedule_syncs.s("weekly"),
    )
    sender.add_periodic_task(
        timedelta(days=30),
        schedule_syncs.s("monthly"),
    )
    # Replaces Mongo's TTL index on pending_tool_state.expires_at.
    sender.add_periodic_task(
        timedelta(seconds=60),
        cleanup_pending_tool_state.s(),
        name="cleanup-pending-tool-state",
    )
    # Aligns retention with the Aztec Foundation privacy policy
    # (https://aztec.network/privacy-policy): "Technical usage
    # information: 12 months". Run daily so the purge window slides
    # forward continuously rather than in 24h jumps.
    sender.add_periodic_task(
        timedelta(days=1),
        purge_old_user_data.s(),
        name="purge-old-user-data",
    )


@celery.task(bind=True)
def mcp_oauth_task(self, config, user):
    resp = mcp_oauth(self, config, user)
    return resp


@celery.task(bind=True)
def mcp_oauth_status_task(self, task_id):
    resp = mcp_oauth_status(self, task_id)
    return resp


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

    Agents (including MCP-provisioned Discord agents) are intentionally
    preserved so existing API keys keep working; only the conversational
    content tied to those keys ages out.
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
    """Delete pending_tool_state rows past their TTL.

    Replaces Mongo's ``expireAfterSeconds=0`` TTL index — Postgres has
    no native TTL, so this task runs every 60 seconds to keep
    ``pending_tool_state`` bounded. No-ops if ``POSTGRES_URI`` isn't
    configured (keeps the task runnable in Mongo-only environments).
    """
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
