import os

broker_url = os.getenv("CELERY_BROKER_URL")
result_backend = os.getenv("CELERY_RESULT_BACKEND")

task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']

# Autodiscover tasks. Importing these modules at boot registers their
# @celery.task decorators with this app instance. The upstream
# 'application.api.user.tasks' module was deleted alongside the admin
# SPA; the corpus-ingest task and the periodic retention/cleanup tasks
# now live under application/workers/ next to their workers.
imports = (
    'application.workers.ingest',
    'application.workers.periodic',
    # Eager-import so mcp_oauth_task / mcp_oauth_status_task names are
    # registered at worker boot. application.agents.tools.mcp_tool also
    # imports them, but mcp_tool is loaded lazily — without this entry
    # any stale task dispatched before the first agent-tool resolution
    # would fail with "task not registered."
    'application.workers.mcp_oauth',
)
