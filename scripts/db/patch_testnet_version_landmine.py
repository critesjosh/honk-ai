"""Annotate the v4.3.0 testnet getting-started chunks so retrieval stops
asserting "testnet runs 4.3.0".

Background
----------
The docs knowledge base is the Aztec **v4.3.0** snapshot, but the public
**testnet has moved to the v5 line (5.0.0-rc.1)** — see the "Version
coverage" section now in all three grounded prompts and the
``[[v5-testnet-kb-gap-prompt]]`` project memory. The single most harmful
stale chunk is ``version-v4.3.0/getting_started_on_testnet.md``'s install
step, which both tells the user to ``VERSION=4.3.0`` *and* states
":::warning Testnet is version-dependent. It is currently running version
4.3.0". The weekly usage report traced real wrong guidance (and a ~20-turn
genesis-mismatch thread) to this chunk: the prompt says "testnet is on the
v5 line" while this retrieved chunk says "install 4.3.0 for testnet", and
retrieval was winning the conflict.

This is the *interim* fix. The durable fix is the v5 re-ingest (which
replaces this corpus). An ingest-time transform would not help until then
because the live corpus is only mutated by re-ingest — so this patches the
live ``documents`` rows directly.

What it does
------------
Prepends a clearly-marked snapshot caveat (guarded by a sentinel so re-runs
are no-ops) to every chunk whose ``metadata.source`` is the testnet
getting-started page AND whose body carries the ``VERSION=4.3.0`` install
pin. It does NOT rewrite the original instruction — fidelity to the
published doc is preserved; the caveat is added above it so the model sees
the correction at retrieval time. The page is ingested twice in prod (two
rendered-docs corpora), so the source+content match deliberately catches
both copies regardless of row id.

Scope is deliberately narrow: the bare ``VERSION=4.3.0`` install command
also appears in ~6 other v4.3.0 docs (local-network, operators, a
tutorial), but those are legitimate v4.3.0 install steps in non-testnet
contexts, not "testnet runs 4.3.0" claims — the v5 re-ingest handles them.

Idempotent and reversible:
- default: apply (skips rows already carrying the sentinel).
- ``--dry-run``: report matching rows, write nothing.
- ``--undo``: remove the caveat block (for re-wording).

Usage (scripts/ is NOT in the backend image — bind-mount it; see CLAUDE.md)::

    docker compose -f deployment/docker-compose-hub.yaml --env-file .env run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro -e PYTHONPATH=/app \\
        backend python /app/scripts/db/patch_testnet_version_landmine.py --dry-run

    # apply for real (drop --dry-run):
    docker compose -f deployment/docker-compose-hub.yaml --env-file .env run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro -e PYTHONPATH=/app \\
        backend python /app/scripts/db/patch_testnet_version_landmine.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from sqlalchemy import text

# Make ``application`` importable when run directly (repo root is parents[2]).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from application.storage.db.session import db_readonly, db_session  # noqa: E402

SOURCE_PATH = "version-v4.3.0/getting_started_on_testnet.md"

# Sentinel makes the prepend idempotent and the undo exact-matchable. Keep it
# stable — changing it would orphan already-patched rows.
SENTINEL = "[snapshot-note:testnet-version]"

NOTE_BLOCK = (
    f"> {SENTINEL} This page is from the Aztec **v4.3.0** docs snapshot. Since then the "
    "public **testnet has moved to the v5 line (5.0.0-rc.1)**. The `VERSION=4.3.0` install "
    "pin and the \"testnet is currently running 4.3.0\" line below were correct for the "
    "v4.3.0 testnet but are now stale: treat the exact version and install command as "
    "version-pinned, and verify the current testnet version + install command against the "
    "v5 release notes before using them."
)
# Exact string prepended to (and stripped from) chunk bodies.
PREFIX = NOTE_BLOCK + "\n\n"


def _matches(conn) -> List[int]:
    """Rows for the testnet getting-started page that still carry the install pin."""
    rows = conn.execute(
        text(
            "SELECT id FROM documents"
            " WHERE metadata->>'source' = :src"
            " AND text ILIKE '%VERSION=4.3.0%'"
            " ORDER BY id"
        ),
        {"src": SOURCE_PATH},
    ).fetchall()
    return [r[0] for r in rows]


def _run_apply(dry_run: bool) -> int:
    if dry_run:
        with db_readonly() as conn:
            matches = _matches(conn)
            todo = conn.execute(
                text(
                    "SELECT count(*) FROM documents"
                    " WHERE metadata->>'source' = :src AND text ILIKE '%VERSION=4.3.0%'"
                    " AND position(:sent in text) = 0"
                ),
                {"src": SOURCE_PATH, "sent": SENTINEL},
            ).scalar()
        print(f"source={SOURCE_PATH!r} VERSION=4.3.0 chunks matched: {matches or 'none'}")
        print(f"[dry-run] would prepend caveat to {todo} row(s); the rest already carry it")
        return 0

    with db_session() as conn:
        matches = _matches(conn)
        # Atomic + idempotent: the position()=0 guard prevents a double prepend.
        result = conn.execute(
            text(
                "UPDATE documents SET text = :prefix || text"
                " WHERE metadata->>'source' = :src"
                " AND text ILIKE '%VERSION=4.3.0%'"
                " AND position(:sent in text) = 0"
            ),
            {"prefix": PREFIX, "src": SOURCE_PATH, "sent": SENTINEL},
        )
    print(f"source={SOURCE_PATH!r} VERSION=4.3.0 chunks matched: {matches or 'none'}")
    print(f"[done] prepended caveat to {result.rowcount} row(s) "
          f"({len(matches) - result.rowcount} already had it)")
    return 0


def _run_undo(dry_run: bool) -> int:
    # Undo gates on source + sentinel only (NOT the VERSION pin), so it can still
    # recover a row whose body was later reworded or lost the install pin.
    where = "metadata->>'source' = :src AND position(:sent in text) > 0"
    params = {"prefix": PREFIX, "src": SOURCE_PATH, "sent": SENTINEL}
    if dry_run:
        with db_readonly() as conn:
            n = conn.execute(
                text(f"SELECT count(*) FROM documents WHERE {where}"), params
            ).scalar()
        print(f"[dry-run] would strip caveat from {n} row(s)")
        return 0

    with db_session() as conn:
        result = conn.execute(
            text(f"UPDATE documents SET text = replace(text, :prefix, '') WHERE {where}"),
            params,
        )
    print(f"[undo] stripped caveat from {result.rowcount} row(s)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Annotate v4.3.0 testnet getting-started chunks with a v5 snapshot caveat."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report matches; write nothing.")
    parser.add_argument("--undo", action="store_true", help="Remove the caveat block (for re-wording).")
    args = parser.parse_args(argv)

    return _run_undo(args.dry_run) if args.undo else _run_apply(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
