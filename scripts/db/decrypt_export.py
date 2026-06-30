"""Run a read-only SQL query and decrypt ``honkenc:`` content in the results.

Operator helper for surfaces that read encrypted content columns via RAW SQL —
e.g. the ``honk-report`` skill — which otherwise get ``honkenc:`` envelopes back
because they bypass the repository decrypt boundary (see
``PLAN-content-encryption.md`` / ``application/security/content_registry.py``).

Least-privilege by construction:

* **Connects via the MCP DSN resolver** (``MCP_DB_URL`` first → the
  ``docsgpt_mcp_ro`` read-only role), so when run in the ``mcp`` container the
  SQL itself can't touch credential columns (``agents.key``,
  ``conversations.api_key``, ``user_logs.data`` …) the role withholds — the same
  boundary as the ``honk_sql`` tool. Run it via the ``mcp`` service for that.
* **Read-only transaction** (psycopg ``conn.read_only``), so a side-effecting
  statement can't mutate even if it slips past the guard.
* **SELECT/WITH guard** (the same ``guard_select`` ``honk_sql`` uses).
* **Decrypt scope = MCP-readable columns only**
  (``operator_decrypt_candidates``), so it never decodes a secret-bearing blob
  even under a privileged DB role.

IMPORTANT: do NOT ``LEFT(col, n)`` an encrypted column in the query — truncating
the envelope makes it undecryptable. Select the full column and use ``--truncate``
to bound the *decrypted* text instead.

Usage (run in the ``mcp`` container — it has the ``docsgpt_mcp_ro`` DSN + the key;
``scripts/`` is not baked into the image, so bind-mount it)::

    docker compose -f deployment/docker-compose-hub.yaml --env-file .env run --rm \\
        -e PYTHONPATH=/app -v $(pwd)/scripts:/app/scripts:ro mcp \\
        python scripts/db/decrypt_export.py --sql "SELECT prompt, response FROM ..." \\
        --truncate 1200
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg

from application.mcp_server.auth import _resolve_connection_string_from_env
from application.mcp_server.sql_guard import guard_select
from application.security.content_registry import decrypt_deep, operator_decrypt_candidates


def _truncate(value, limit: int):
    """Truncate a decrypted string to ``limit`` chars (post-decrypt only)."""
    if limit and isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…[+{len(value) - limit} chars]"
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", help="A single SELECT/WITH query. If omitted, read from stdin.")
    parser.add_argument(
        "--truncate",
        type=int,
        default=0,
        help="Truncate decrypted string fields to N chars for display (0 = no limit). "
        "Applied AFTER decryption — never truncate encrypted columns in SQL.",
    )
    parser.add_argument("--format", choices=("json", "table"), default="json", help="Output format (default: json lines).")
    args = parser.parse_args()

    query = (args.sql or sys.stdin.read() or "").strip()
    if not query:
        print("ERROR: no query (pass --sql or pipe SQL on stdin)", file=sys.stderr)
        return 2

    guard = guard_select(query)
    if not guard.ok:
        print(f"ERROR: rejected by the SELECT/WITH guard: {guard.reason}", file=sys.stderr)
        return 2

    dsn = _resolve_connection_string_from_env()
    if not dsn:
        print("ERROR: no DB DSN (set MCP_DB_URL / PGVECTOR_CONNECTION_STRING / POSTGRES_URI)", file=sys.stderr)
        return 2
    if not os.environ.get("MCP_DB_URL"):
        # Least-privilege relies on the docsgpt_mcp_ro role (MCP_DB_URL). Without
        # it we fall back to a privileged role that can read credential columns —
        # warn loudly. Run via the `mcp` service (see honk-report SKILL).
        print(
            "WARN: MCP_DB_URL not set — connecting with a non-read-only-role DSN; "
            "credential columns may be readable. Run via the `mcp` service for least-privilege.",
            file=sys.stderr,
        )

    candidates = operator_decrypt_candidates()
    with psycopg.connect(dsn) as conn:
        conn.read_only = True  # READ ONLY transaction — no statement can mutate
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            cur.execute(guard.statement or query)
            columns = [d.name for d in cur.description] if cur.description else []
            raw_rows = cur.fetchall()

    records = []
    for row in raw_rows:
        record = {}
        for col, val in zip(columns, row):
            decrypted = decrypt_deep(val, candidates=candidates)
            record[col] = _truncate(decrypted, args.truncate) if isinstance(decrypted, str) else decrypted
        records.append(record)

    if args.format == "json":
        for record in records:
            print(json.dumps(record, default=str, ensure_ascii=False))
    else:
        if records:
            widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in records)) for c in columns}
            print(" | ".join(c.ljust(widths[c]) for c in columns))
            print("-+-".join("-" * widths[c] for c in columns))
            for record in records:
                print(" | ".join(str(record.get(c, "")).ljust(widths[c]) for c in columns))
    print(f"-- {len(records)} row(s) --", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
