"""Upload built corpus zips to the DocsGPT backend and capture source UUIDs.

Workflow:
  1. POST <zip> to /api/upload  →  task_id
  2. Poll  /api/task_status?task_id=...  until SUCCESS or FAILURE
  3. Read the created source UUID from the finished task's ``result``
     (the ingest worker returns ``source_id``, surfaced under ``result``
     by /api/task_status) and record it in the upload manifest. (The old
     ``GET /api/sources`` listing endpoint was removed with the admin SPA.)

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
    host_header: Optional[str] = None,
) -> str:
    """POST a zip and return the Celery task_id."""
    import requests

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host_header:
        headers["Host"] = host_header

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
    timeout_s: float = 7200.0, host_header: Optional[str] = None,
) -> dict:
    """Poll the task-status endpoint until it terminates. Returns the
    final status dict."""
    import requests

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host_header:
        headers["Host"] = host_header

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


def _source_id_from_task(final: dict) -> Optional[str]:
    """Extract the created source UUID from a finished ingest task result.

    The ingest worker returns ``source_id`` in its result dict (see
    ``application/workers/ingest.py``), which ``GET /api/task_status``
    surfaces under ``result``. This replaces the old ``GET /api/sources``
    lookup — that listing endpoint was removed with the admin SPA and now
    404s, so the manifest captured no UUIDs. Falls back to ``None`` (the
    caller can reconstruct from the DB by name) for an older backend whose
    task result predates this field.
    """
    if not isinstance(final, dict):
        return None
    result = final.get("result")
    if isinstance(result, dict):
        sid = result.get("source_id") or result.get("id")
        if sid:
            return str(sid)
    return None


def upload_corpus(
    base_url: str,
    token: Optional[str],
    user: str,
    corpus_slug: str,
    name: str,
    zip_path: Path,
    rel_prefix: str,
    host_header: Optional[str] = None,
) -> dict:
    logger.info("Uploading %s → %s (%d bytes)", corpus_slug, name, zip_path.stat().st_size)
    task_id = _post_upload(base_url, token, user, name, zip_path, host_header=host_header)
    final = _poll_task(base_url, token, task_id, host_header=host_header)
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
    source_id = _source_id_from_task(final)
    if not source_id:
        logger.warning(
            "no source_id in task result for %s — manifest will need a DB "
            "fallback (older backend, or ingest worker not yet rebuilt)",
            corpus_slug,
        )
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
    parser.add_argument("--host-header", default=None,
                        help="Override the Host header sent on every request. "
                             "Required when --base-url is the loopback Caddy "
                             "port (127.0.0.1:5080) because the prod Caddyfile "
                             "matches on $PUBLIC_HOSTNAME — without this, "
                             "Caddy falls through to the default vhost and "
                             "returns 200 with an empty body.")
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
                host_header=args.host_header,
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
