"""Verify that no user content sits in Postgres as plaintext at rest.

This is the *defensibility* check behind the Discord "encrypted at rest"
attestation (see ``PLAN-content-encryption.md`` §12): "we encrypt at the
repository boundary" is a claim; "every classified column is NULL/tombstone or a
recognized ``honkenc`` envelope" is a repeatable proof. Registry-driven — it
enumerates :data:`application.security.content_registry.CONTENT_FIELDS` so a new
content field is covered automatically.

Exit code: ``0`` only if there are NO Class-A (content) violations, NO
``message_metadata`` non-structural plaintext key, and the bot-unused (dormant)
tables are empty. Class-C credential columns and the scoped Tier-2 tables are
reported informationally and do NOT affect the exit code (they are
defense-in-depth / out of scope for the content attestation).

Usage::

    docker compose -f deployment/docker-compose-hub.yaml run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro -e PYTHONPATH=/app backend \\
        python scripts/db/scan_plaintext.py

Run it AFTER the backfill (legacy plaintext rows are expected to fail before).
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from application.security.content_encryption import _JSON_SENTINEL, _PREFIX, is_valid_envelope
from application.security.content_registry import CONTENT_FIELDS, STRUCTURAL_METADATA_ALLOWLIST
from application.storage.db.session import db_session

# Bot-unused upstream features: these tables must be EMPTY in this deployment
# (D0 measured them so 2026-06-29). A non-empty one means a dormant surface got
# enabled without encryption — a content gap. documents/sources/user_tools are
# legitimately non-empty (public corpus / operator tools) and need scoped
# predicates, not a bare empty-guard — reported informationally below.
_DORMANT_EMPTY_TABLES = (
    "attachments",
    "memories",
    "todos",
    "notes",
    "workflows",
    "workflow_nodes",
    "workflow_edges",
    "workflow_runs",
    "connector_sessions",
)

# Class-C credential columns (plaintext bearers) — informational inventory.
_CREDENTIAL_LIKE = ("token", "api_key", "secret", "webhook")
_CREDENTIAL_ALLOW = ("token_limit", "limited_token_mode", "prompt_tokens", "generated_tokens")


def _iter_leaf_strings(obj, parts):
    """Yield string values at the dotted ``parts`` path (``*`` maps over a list)."""
    if obj is None or not parts:
        if isinstance(obj, str):
            yield obj
        return
    head, rest = parts[0], parts[1:]
    if head == "*":
        if isinstance(obj, list):
            for item in obj:
                yield from _iter_leaf_strings(item, rest)
        return
    if isinstance(obj, dict) and head in obj:
        if rest:
            yield from _iter_leaf_strings(obj[head], rest)
        elif isinstance(obj[head], str):
            yield obj[head]


def _scan_field(conn, table, column, spec) -> int:
    """Return the count of rows where ``column`` holds plaintext content.

    "Encrypted" means the value actually *decrypts* (:func:`is_valid_envelope`),
    not merely that it looks shaped like an envelope — a plaintext value that
    starts with ``honkenc:`` or carries a literal ``__enc__`` key is counted as
    a violation, so nothing can masquerade as ciphertext past the verifier.
    """
    mode = spec["mode"]
    rows = conn.execute(text(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")).fetchall()
    bad = 0
    for (val,) in rows:
        if mode == "text":
            if not is_valid_envelope(val, table=table, column=column):
                bad += 1
        elif mode == "json_blob":
            if val in ([], {}):  # empty default / forget tombstone — no content
                continue
            if not is_valid_envelope(val, table=table, column=column):
                bad += 1
        elif mode == "json_keep":
            # Bad if any top-level key is neither structural nor a valid sentinel
            # (the sentinel's *string* value must decrypt). Extra plaintext keys
            # alongside a valid sentinel are still flagged.
            if isinstance(val, dict):
                sentinel = val.get(_JSON_SENTINEL)
                sentinel_ok = isinstance(sentinel, str) and is_valid_envelope(sentinel, table=table, column=column)
                if any(
                    k not in STRUCTURAL_METADATA_ALLOWLIST and not (k == _JSON_SENTINEL and sentinel_ok) for k in val
                ):
                    bad += 1
        elif mode == "json_leaf":
            if any(
                not is_valid_envelope(s, table=table, column=column)
                for path in spec["leaf_paths"]
                for s in _iter_leaf_strings(val, path.split("."))
            ):
                bad += 1
        else:
            raise ValueError(f"unknown mode {mode!r}")
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="Only print violations + the verdict.")
    args = parser.parse_args()

    violations = 0
    with db_session() as conn:
        print("== Class A — content fields (must be ciphertext) ==")
        for table, cols in CONTENT_FIELDS.items():
            for column, spec in cols.items():
                n = _scan_field(conn, table, column, spec)
                if n:
                    violations += n
                    print(f"  FAIL  {table}.{column} ({spec['mode']}): {n} plaintext row(s)")
                elif not args.quiet:
                    print(f"  ok    {table}.{column} ({spec['mode']})")

        print("== Dormant tables (must be empty in this deployment) ==")
        for table in _DORMANT_EMPTY_TABLES:
            try:
                n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            except Exception:  # noqa: BLE001 - table may not exist on a minimal install
                continue
            if n:
                violations += n
                print(f"  FAIL  {table}: {n} row(s) — dormant feature enabled without encryption")
            elif not args.quiet:
                print(f"  ok    {table}: empty")

        print("== Class C — credential columns (informational; hardening, not attestation) ==")
        cred = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND data_type IN ('text','character varying') "
                "ORDER BY table_name, column_name"
            )
        ).fetchall()
        for tbl, col in cred:
            lc = col.lower()
            if (
                any(p in lc for p in _CREDENTIAL_LIKE)
                and not lc.endswith(("_fp", "_hash"))
                and lc not in _CREDENTIAL_ALLOW
            ):
                try:
                    n = conn.execute(
                        text(f"SELECT count(*) FROM {tbl} WHERE {col} IS NOT NULL AND {col} NOT LIKE :p"),
                        {"p": _PREFIX + "%"},
                    ).scalar()
                except Exception:  # noqa: BLE001
                    continue
                if n and not args.quiet:
                    print(f"  warn  {tbl}.{col}: {n} plaintext bearer value(s) — Class-C hardening TODO")

    print("-" * 60)
    if violations:
        print(f"PLAINTEXT FOUND: {violations} content violation(s). Attestation NOT clean.")
        return 1
    print("CLEAN: no content plaintext at rest. ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
