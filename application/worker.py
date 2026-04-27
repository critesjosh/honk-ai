"""Backward-compatible re-export shim — TEMPORARY.

The worker functions live in ``application/workers/*``. This shim
exists to keep import-time paths working during the transition; it is
not intended as a permanent public API. Treat ``application.workers.*``
as canonical and migrate callers off this module when you next touch
them.

Limitations to know about:

- **Patch compatibility ≠ import compatibility.** ``from application
  import worker`` still resolves, but ``monkeypatch.setattr(worker,
  "X", ...)`` will NOT reach call sites that bind ``X`` from
  ``application.workers.*`` directly (which is most of them). Tests
  must patch the actual import site — see
  ``tests/worker/conftest.py:patch_worker_db`` for the pattern.
- **Don't add new symbols here.** New worker functions live in their
  submodule from day one.

Layout:

- ``workers.zip_safety``    — extract_zip_recursive + ZipExtractionError
- ``workers.agent_runtime`` — run_agent_logic
- ``workers.ingest``        — ingest_worker (local files)
- ``workers.reingest``      — reingest_source_worker (incremental update)
- ``workers.connectors``    — remote_worker, sync, sync_worker, ingest_connector
- ``workers.attachments``   — attachment_worker
- ``workers.webhooks``      — agent_webhook_worker
- ``workers.mcp_oauth``     — mcp_oauth, mcp_oauth_status
- ``workers._helpers``      — small shared utils (metadata_from_filename,
                              upload_index, download_file, chunking
                              constants); private — internal callers
                              should import from here, external callers
                              should use this shim or the specific
                              consumer module
"""

# F401: the imports here are the public re-exports; flake8/ruff would
# otherwise flag them as unused.

from application.workers._helpers import (  # noqa: F401
    MAX_TOKENS,
    MIN_TOKENS,
    RECURSION_DEPTH,
    _apply_display_names_to_structure,
    _get_display_name,
    _normalize_file_name_map,
    download_file,
    generate_random_string,
    metadata_from_filename,
    upload_index,
)
from application.workers.agent_runtime import run_agent_logic  # noqa: F401
from application.workers.attachments import attachment_worker  # noqa: F401
from application.workers.connectors import (  # noqa: F401
    ingest_connector,
    remote_worker,
    sync,
    sync_worker,
)
from application.workers.ingest import ingest_worker  # noqa: F401
from application.workers.mcp_oauth import (  # noqa: F401
    mcp_oauth,
    mcp_oauth_status,
)
from application.workers.reingest import reingest_source_worker  # noqa: F401
from application.workers.webhooks import agent_webhook_worker  # noqa: F401
from application.workers.zip_safety import (  # noqa: F401
    MAX_COMPRESSION_RATIO,
    MAX_FILE_COUNT,
    MAX_UNCOMPRESSED_SIZE,
    ZipExtractionError,
    _is_path_safe,
    _validate_zip_safety,
    extract_zip_recursive,
)
