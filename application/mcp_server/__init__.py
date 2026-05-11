"""MCP server colocated with the honk-ai backend.

Runs as a sibling Compose service (``aztec/docsgpt-mcp``) on the same host
as the honk-ai Flask backend. Exposes three read-only surfaces over HTTP
to remote claudebox clients via Cloudflare Access + bearer token:

* ``honk_sql.*``  — SELECT/WITH-only statements against the docsgpt
  Postgres database, executed under a dedicated ``docsgpt_mcp_ro`` role
  with read-only transactions and statement timeouts.
* ``honk_rag.*``  — pgvector similarity search wrapping the same
  ``application.vectorstore.pgvector`` helper the live retriever uses.
* ``honk_logs.*`` — bounded ``docker logs --tail/--since/--grep`` reads
  proxied through ``tecnativa/docker-socket-proxy`` (LOGS=1 only).

See ``AZTEC_SETUP.md`` and ``application/mcp_server/Dockerfile`` for how
this is deployed in dev / prod compose, and
``application/alembic/versions/0006_mcp_admin_tables.py`` for the token
and audit tables.
"""
