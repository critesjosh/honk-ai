"""Corpus-ingest blueprint.

Owns the two endpoints that ``scripts/ingest/upload.py`` (and the
broader Aztec re-ingest workflow documented in
``scripts/ingest/README.md``) depends on:

- ``POST /api/upload`` — receive a corpus zip, save it to local storage,
  enqueue an ``ingest`` Celery job that runs the parser + chunker +
  embedder, and return the task id.
- ``GET /api/task_status`` — poll a Celery task by id (used by the
  upload script to wait for the ingest worker to finish).

These previously lived under the upstream admin-SPA blueprint at
``application/api/user/sources/upload.py``. They were carved out into
their own blueprint when the admin SPA was removed so the corpus
re-ingest pipeline still works.

Auth is identical to the upstream registration: requires ``decoded_token``
to be set (so anonymous → 401 unless ``AUTH_TYPE`` is unset, in which
case ``handle_auth`` returns ``{"sub": "local"}`` and the upload runs
as ``user_id="local"``). The Aztec ingest workflow runs from inside
the compose network with ``AUTH_TYPE`` unset, matching upstream behaviour.
"""

from flask import Blueprint

from application.api.ingest.routes import ingest_ns

__all__ = ["ingest_bp", "ingest_ns"]

ingest_bp = Blueprint("ingest", __name__)
