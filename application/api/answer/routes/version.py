"""Public endpoint exposing the Aztec corpus version this backend serves.

The MCP server (``@aztec/mcp-server``) calls ``GET /api/version`` to
compare its local ``aztec-packages`` clone tag against the corpus the
DocsGPT backend has indexed. On mismatch the MCP server refuses to
search unless the caller passes an override flag.

The endpoint is intentionally unauthenticated — knowing which version
of the public Aztec docs are indexed leaks nothing sensitive, and
gating it behind an API key would force MCP clients to acquire a key
just to learn whether they should ask for one.
"""

import logging
from typing import Any, Dict

from flask import make_response
from flask_restx import Resource

from application.api.answer.routes.base import answer_ns
from application.core.settings import settings
from application.storage.db.session import db_readonly
from sqlalchemy import text

logger = logging.getLogger(__name__)


@answer_ns.route("/api/version")
class VersionResource(Resource):
    """Returns the corpus version the backend is currently serving.

    Accepts both GET and POST. POST exists because some auth proxies
    (Cloudflare Access in particular) gate GET routes for the apex
    hostname while letting POST /api/* through unauthenticated. The
    MCP client uses POST so version sync works without operator-
    configured bypass rules. GET stays for ergonomic curl probing.
    """

    @answer_ns.doc(description="Aztec corpus version served by this backend")
    def get(self):
        return self._respond()

    @answer_ns.doc(description="Aztec corpus version served by this backend (POST variant for auth-proxy compatibility)")
    def post(self):
        return self._respond()

    def _respond(self):
        version = (settings.AZTEC_CORPUS_VERSION or "").strip() or "unknown"

        # Source count is best-effort — informational only. A failure
        # here must not break the version response.
        source_count: int = 0
        if settings.AZTEC_SOURCE_IDS:
            try:
                ids = [
                    s.strip() for s in settings.AZTEC_SOURCE_IDS.split(",")
                    if s.strip()
                ]
                if ids:
                    with db_readonly() as conn:
                        row = conn.execute(
                            text(
                                "SELECT COUNT(*) FROM sources "
                                "WHERE id = ANY(CAST(:ids AS uuid[])) "
                                "  AND is_public = TRUE"
                            ),
                            {"ids": ids},
                        ).fetchone()
                        if row is not None:
                            source_count = int(row[0])
            except Exception:
                logger.warning(
                    "Failed to count sources for /api/version", exc_info=True
                )

        body: Dict[str, Any] = {
            "aztec_corpus_version": version,
            "source_count": source_count,
        }
        return make_response(body, 200)
