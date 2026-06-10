"""Attach an existing source to production chat agents.

Appends a source UUID to ``agents.extra_source_ids`` for every agent on
the given surfaces, skipping agents that already search it (as primary
or extra). Used when a corpus is ingested AFTER the agents were
provisioned — e.g. the ``Awesome Aztec (community resources)`` corpus
was only attached to the widget agent, so the eval's ``awesome-*``
resource queries could never pass on the Discord/Slack agents.

Why not ``scripts/ingest/swap_sources.py``: that tool rebuilds the FULL
source list from an upload manifest (overwriting ``extra_source_ids``),
which is the right shape after a corpus re-ingest but the wrong one for
"these existing agents should also search this one source".

NOTE: ``AZTEC_SOURCE_IDS`` in ``.env`` is consumed at agent CREATION
only (CLAUDE.md) — appending the UUID there fixes future provisions,
this script fixes the agents that already exist. Do both.

Usage (scripts/ is NOT in the backend image — bind-mount it)::

    docker compose -f deployment/docker-compose-hub.yaml --env-file .env run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro -e PYTHONPATH=/app \\
        backend python scripts/db/attach_source_to_agents.py \\
        --source-id 5afa85e2-41b7-4957-a642-a82ac65affde \\
        --surfaces slack,discord,web_ask [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from sqlalchemy import text

# Same bootstrap as create_slack_chat_agent.py: make ``application``
# importable when run directly (sys.path[0] is the script's dir).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from application.storage.db.session import db_session  # noqa: E402

# agents.surface CHECK constraint allowlist (migrations 0009/0010) minus
# the non-chat surfaces — attaching corpora to ``mcp`` agents is done at
# provisioning time from AZTEC_SOURCE_IDS, and ``eval`` agents are
# managed by scripts/eval/provision_test_agent.py.
_CHAT_SURFACES = {"discord", "widget", "web_ask", "slack"}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Append a source to chat agents' extra_source_ids.")
    parser.add_argument("--source-id", required=True, help="UUID of the source to attach (must exist in sources)")
    parser.add_argument(
        "--surfaces", required=True,
        help=f"Comma-separated agents.surface values to target ({', '.join(sorted(_CHAT_SURFACES))})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing")
    args = parser.parse_args(argv)

    try:
        source_id = str(uuid.UUID(args.source_id))
    except ValueError:
        print(f"--source-id is not a UUID: {args.source_id!r}", file=sys.stderr)
        return 2

    surfaces = [s.strip() for s in args.surfaces.split(",") if s.strip()]
    bad = [s for s in surfaces if s not in _CHAT_SURFACES]
    if bad or not surfaces:
        print(f"--surfaces must be a non-empty subset of {sorted(_CHAT_SURFACES)}; got {surfaces}", file=sys.stderr)
        return 2

    with db_session() as conn:
        src = conn.execute(
            text("SELECT name FROM sources WHERE id = CAST(:sid AS uuid)"), {"sid": source_id}
        ).fetchone()
        if src is None:
            print(f"Source {source_id} not found in sources — refusing to attach a dangling UUID.", file=sys.stderr)
            return 2
        print(f"Source: {src[0]} ({source_id})")

        agents = conn.execute(
            text(
                "SELECT id, name, surface, source_id::text, extra_source_ids::text[] "
                "FROM agents WHERE surface = ANY(:surfaces) ORDER BY surface, name"
            ),
            {"surfaces": surfaces},
        ).fetchall()
        if not agents:
            print(f"No agents found for surfaces {surfaces} — nothing to do.", file=sys.stderr)
            return 2

        changed = 0
        for agent_id, name, surface, primary, extras in agents:
            if source_id == primary or source_id in (extras or []):
                print(f"  [skip]   {surface:8s} {name!r} — already attached")
                continue
            if args.dry_run:
                print(f"  [would]  {surface:8s} {name!r} — append to extra_source_ids")
            else:
                # The WHERE re-checks attachment so the append is atomic —
                # a concurrent run can't slip a duplicate in between our
                # SELECT and this UPDATE.
                result = conn.execute(
                    text(
                        "UPDATE agents SET"
                        " extra_source_ids = extra_source_ids || CAST(:sid AS uuid),"
                        " updated_at = now()"
                        " WHERE id = :id"
                        " AND NOT (CAST(:sid AS uuid) = ANY(extra_source_ids))"
                        " AND source_id IS DISTINCT FROM CAST(:sid AS uuid)"
                    ),
                    {"sid": source_id, "id": agent_id},
                )
                outcome = "appended to extra_source_ids" if result.rowcount else "already attached (raced)"
                print(f"  [done]   {surface:8s} {name!r} — {outcome}")
            changed += 1

        verb = "would change" if args.dry_run else "changed"
        print(f"{verb}: {changed} agent(s); skipped: {len(agents) - changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
