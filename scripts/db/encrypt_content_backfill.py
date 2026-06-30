"""One-time backfill: encrypt existing plaintext content rows in place.

Part of the content-encryption-at-rest rollout (``PLAN-content-encryption.md``
§11/§13). After the wiring deploys with ``CONTENT_ENCRYPTION_ENABLED=true`` (new
writes encrypted), this walks every Class-A table and encrypts the rows written
before the flip. Idempotent — already-encrypted values are skipped — so it is
safe to re-run until ``scan_plaintext.py`` reports clean.

For ``user_logs`` the bearer ``api_key`` is read from the still-plaintext
``data`` and written to ``api_key_fp`` BEFORE ``data`` is encrypted, so the
blind-index lookup keeps working (PLAN §8).

Requires ``CONTENT_ENCRYPTION_ENABLED=true`` (else ``encrypt_value`` is a no-op
and nothing would change). Run via::

    docker compose -f deployment/docker-compose-hub.yaml run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro -e PYTHONPATH=/app backend \\
        python scripts/db/encrypt_content_backfill.py            # or --dry-run

Snapshot / pg_dump first — this rewrites content rows in place with no undo.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from application.core.settings import settings
from application.security.content_registry import CONTENT_FIELDS, api_key_fingerprint, encrypt_value
from application.storage.db.session import db_session

# Every Class-A table is keyed by ``id``.
_PK = "id"


def _is_jsonb(mode: str) -> bool:
    return mode in ("json_blob", "json_leaf", "json_keep")


def _backfill_table(conn, table: str, cols: dict, *, batch: int, dry_run: bool) -> tuple[int, int]:
    """Encrypt classified columns for every row of ``table``. Returns (seen, updated)."""
    colnames = list(cols.keys())
    select_cols = ", ".join([_PK, *colnames])
    seen = updated = 0
    offset = 0
    while True:
        rows = (
            conn.execute(
                text(f"SELECT {select_cols} FROM {table} ORDER BY {_PK} LIMIT :lim OFFSET :off"),
                {"lim": batch, "off": offset},
            )
            .mappings()
            .fetchall()
        )
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            seen += 1
            set_parts: list[str] = []
            params: dict = {"pk": row[_PK]}

            # user_logs: fingerprint the bearer key from the still-plaintext data
            # first. An already-encrypted data blob ({"__enc__": ...}) has no
            # top-level 'api_key', so fp is None and nothing is rewritten.
            if table == "user_logs":
                data = row.get("data")
                if isinstance(data, dict):
                    fp = api_key_fingerprint(data.get("api_key"))
                    if fp is not None:
                        set_parts.append("api_key_fp = :api_key_fp")
                        params["api_key_fp"] = fp

            for col in colnames:
                val = row[col]
                if val is None:
                    continue
                # encrypt_value is idempotent via is_valid_envelope: a truly-encrypted
                # value is a no-op (new == val), but a value that only *looks*
                # encrypted is re-encrypted rather than skipped.
                new = encrypt_value(table, col, val)
                if new is val or new == val:
                    continue  # nothing changed (e.g. empty []/{} / all-structural metadata)
                if _is_jsonb(cols[col]["mode"]):
                    set_parts.append(f"{col} = CAST(:{col} AS jsonb)")
                    params[col] = json.dumps(new, default=str)
                else:
                    set_parts.append(f"{col} = :{col}")
                    params[col] = new

            if not set_parts:
                continue
            updated += 1
            if not dry_run:
                conn.execute(
                    text(f"UPDATE {table} SET {', '.join(set_parts)} WHERE {_PK} = :pk"),
                    params,
                )
    return seen, updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Count what would change without writing.")
    parser.add_argument("--batch", type=int, default=500, help="Rows per page (default: 500).")
    args = parser.parse_args()

    if not settings.CONTENT_ENCRYPTION_ENABLED:
        print("ERROR: CONTENT_ENCRYPTION_ENABLED is not set; encrypt_value would be a no-op. Aborting.")
        return 1

    total_seen = total_updated = 0
    with db_session() as conn:
        for table, cols in CONTENT_FIELDS.items():
            seen, updated = _backfill_table(conn, table, cols, batch=args.batch, dry_run=args.dry_run)
            total_seen += seen
            total_updated += updated
            verb = "would encrypt" if args.dry_run else "encrypted"
            print(f"  {table}: {seen} row(s) scanned, {verb} {updated}")
    print("-" * 60)
    print(
        f"{'DRY RUN — ' if args.dry_run else ''}{total_updated}/{total_seen} row(s) {'would be ' if args.dry_run else ''}encrypted."
    )
    if not args.dry_run:
        print("Now run scripts/db/scan_plaintext.py to verify zero plaintext remains.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
