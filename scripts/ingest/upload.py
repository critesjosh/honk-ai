"""Upload built corpus zips to the DocsGPT backend and capture source UUIDs.

Workflow:
  1. POST <zip> to /api/upload  →  task_id
  2. Poll  /api/task_status?task_id=...  until SUCCESS or FAILURE
  3. Read the new sources.id row by name (the upload endpoint creates a
     ``sources`` row keyed by the ``name`` form field) and record it
     in the upload manifest.

Usage::

    python -m scripts.ingest.upload \
        --build-dir /tmp/aztec-corpora-build \
        --base-url  http://localhost:7091 \
        --user      local \
        --token     "${INTERNAL_KEY}" \
        --out       /tmp/aztec-corpora-build/upload_manifest.json \
        [--corpus aztec_nr_apiref]   # optional: limit to one corpus

The upload manifest is the input to ``swap_sources.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _post_upload(
    base_url: str, token: Optional[str], user: str, name: str, zip_path: Path,
) -> str:
    """POST a zip and return the Celery task_id."""
    import requests

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with open(zip_path, "rb") as f:
        files = {"file": (zip_path.name, f, "application/zip")}
        data = {"user": user, "name": name}
        r = requests.post(
            f"{base_url.rstrip('/')}/api/upload",
            headers=headers,
            files=files,
            data=data,
            timeout=300,
        )
    r.raise_for_status()
    body = r.json()
    task_id = body.get("task_id") or body.get("id") or body.get("taskId")
    if not task_id:
        raise RuntimeError(f"no task_id in upload response: {body}")
    return task_id


def _poll_task(
    base_url: str, token: Optional[str], task_id: str, poll_s: float = 5.0,
    timeout_s: float = 7200.0,
) -> dict:
    """Poll the task-status endpoint until it terminates. Returns the
    final status dict."""
    import requests

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    deadline = time.time() + timeout_s
    last_status = None
    while time.time() < deadline:
        r = requests.get(
            f"{base_url.rstrip('/')}/api/task_status",
            headers=headers,
            params={"task_id": task_id},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        status = body.get("status") or body.get("state") or "?"
        if status != last_status:
            logger.info("task %s: %s", task_id, status)
            last_status = status
        if status.upper() in ("SUCCESS", "FAILURE", "REVOKED"):
            return body
        time.sleep(poll_s)
    raise TimeoutError(f"task {task_id} did not finish within {timeout_s:.0f}s")


def _resolve_source_id(
    base_url: str, token: Optional[str], name: str, user: str,
) -> Optional[str]:
    """Look up the source UUID for a freshly-uploaded corpus.

    Tries the public ``GET /api/sources`` endpoint and matches the row
    by ``name``. Returns ``None`` if not found — the caller should fall
    back to a manual SQL lookup.
    """
    import requests

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(
            f"{base_url.rstrip('/')}/api/sources",
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, dict) and "sources" in rows:
            rows = rows["sources"]
        for row in rows:
            if row.get("name") == name and row.get("user") in (user, None):
                return row.get("id") or row.get("source_id")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not list sources via /api/sources: %s", exc)
        return None


def upload_corpus(
    base_url: str,
    token: Optional[str],
    user: str,
    corpus_slug: str,
    name: str,
    zip_path: Path,
    rel_prefix: str,
) -> dict:
    logger.info("Uploading %s → %s (%d bytes)", corpus_slug, name, zip_path.stat().st_size)
    task_id = _post_upload(base_url, token, user, name, zip_path)
    final = _poll_task(base_url, token, task_id)
    status = (final.get("status") or final.get("state") or "?").upper()
    if status != "SUCCESS":
        return {
            "slug": corpus_slug,
            "name": name,
            "task_id": task_id,
            "status": status,
            "error": final,
            "source_id": None,
        }
    source_id = _resolve_source_id(base_url, token, name, user)
    return {
        "slug": corpus_slug,
        "name": name,
        "task_id": task_id,
        "status": status,
        "source_id": source_id,
        "rel_prefix": rel_prefix,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload built corpus zips and capture source UUIDs."
    )
    parser.add_argument("--build-dir", required=True,
                        help="Output dir from scripts.ingest.build")
    parser.add_argument("--base-url", required=True,
                        help="Backend base URL (e.g. http://localhost:7091)")
    parser.add_argument("--user", default="local",
                        help="User identifier passed to /api/upload (default: local)")
    parser.add_argument("--token", default=None,
                        help="Bearer token; defaults to env $DOCSGPT_INTERNAL_KEY")
    parser.add_argument("--out", required=True,
                        help="Path to write the upload manifest (JSON).")
    parser.add_argument("--corpus", action="append",
                        help="Upload only the named corpus slug(s) (repeatable).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    build_dir = Path(args.build_dir).resolve()
    build_manifest = json.loads((build_dir / "build_manifest.json").read_text())

    if args.corpus:
        wanted = set(args.corpus)
        entries = [c for c in build_manifest["corpora"] if c["corpus"]["slug"] in wanted]
        missing = wanted - {c["corpus"]["slug"] for c in entries}
        if missing:
            print(f"ERROR: requested corpora not in build manifest: {sorted(missing)}",
                  file=sys.stderr)
            return 2
    else:
        entries = build_manifest["corpora"]

    import os
    token = args.token or os.environ.get("DOCSGPT_INTERNAL_KEY")

    results: List[dict] = []
    for e in entries:
        c = e["corpus"]
        zip_path = Path(e["zip_path"])
        if not zip_path.is_file():
            logger.error("zip missing for %s: %s", c["slug"], zip_path)
            results.append({"slug": c["slug"], "error": "zip missing"})
            continue
        try:
            result = upload_corpus(
                base_url=args.base_url,
                token=token,
                user=args.user,
                corpus_slug=c["slug"],
                name=c["name"],
                zip_path=zip_path,
                rel_prefix=c["rel_prefix"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("upload failed for %s", c["slug"])
            result = {"slug": c["slug"], "error": str(exc)}
        results.append(result)

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    success = sum(1 for r in results if r.get("source_id"))
    print(f"\n{success}/{len(results)} uploads succeeded with a source UUID")
    print(f"upload manifest: {out}")
    return 0 if success == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
