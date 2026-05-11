"""``honk_rag.*`` MCP tools.

Wraps the same pgvector helper the production retriever uses
(``application.vectorstore.pgvector.PGVectorStore.search_by_vector_with_score``)
so the MCP surface and the live ``/stream`` retrieval share one
search path. Two callable shapes are exposed:

* ``honk_rag.search`` — embeds a query string OR accepts a
  pre-computed query vector, returns top-K matches with optional
  full-text bodies (gated behind a separate scope).
* ``honk_rag.list_sources`` — lightweight inventory of corpora.

The MCP server does NOT bypass the embedding service; if a client
passes ``query_text``, the server pays the embedding round-trip cost
the same way the live retriever does. Operators who care about
embedding budget can pre-embed client-side and send ``query_vector``.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg


logger = logging.getLogger(__name__)


# Caps on returned results. ``k`` lets the caller request a larger or
# smaller window; the per-call ceiling protects the server from a
# pathological ``k=10000``.
DEFAULT_K = 8
MAX_K = 50

# Cap on per-row text preview when ``include_full_text=False``. Keeps
# the response small and avoids leaking long text bodies for callers
# that only need a snippet to disambiguate.
TEXT_PREVIEW_CAP_BYTES = 1024


class RAGSearcher:
    """Wraps :class:`PGVectorStore` for the MCP read-only surface.

    We construct a fresh store per call rather than sharing one
    instance because :class:`PGVectorStore` keeps a long-lived
    connection internally; for the low-QPS MCP surface a per-call
    connection is simpler and avoids surprising state across async
    handlers.
    """

    def __init__(self, connection_string: str | None = None):
        self._connection_string = connection_string

    def _store(self):
        # Imported lazily so the MCP server can boot in environments
        # where the pgvector extras aren't yet installed (test envs)
        # — only the SQL/logs tools then.
        from application.core.settings import settings
        from application.vectorstore.pgvector import PGVectorStore

        # ``embeddings_key`` must be the actual API key for the embeddings
        # provider, NOT PGVectorStore's nonsense default literal
        # ``"embeddings"``. ``classic_rag.py`` passes
        # ``settings.EMBEDDINGS_KEY`` for the same reason — without it
        # honk_rag.search 401s against OpenAI on the first call (the
        # singleton cache in vectorstore/base.py would mask this in the
        # backend/worker processes that ingested with a real key, but
        # the MCP server's process starts with an empty cache).
        #
        # ``ensure_schema=False`` is mandatory: the MCP server connects as
        # ``docsgpt_mcp_ro`` which lacks DDL privileges. The documents
        # table is already in place — worker/backend creates it on first
        # ingest. PGVectorStore would otherwise fail on CREATE EXTENSION /
        # CREATE TABLE every call.
        return PGVectorStore(
            connection_string=self._connection_string,
            embeddings_key=settings.EMBEDDINGS_KEY,
            ensure_schema=False,
        )

    def search(
        self,
        *,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        source_ids: list[str] | None = None,
        k: int = DEFAULT_K,
        include_full_text: bool = False,
    ) -> dict[str, Any]:
        """Run a similarity search against the production ``documents`` table.

        Exactly one of ``query_text`` and ``query_vector`` must be
        supplied. ``query_text`` is embedded server-side using whatever
        ``EMBEDDINGS_BASE_URL`` / ``EMBEDDINGS_NAME`` settings the
        backend has configured — same path as the live retriever.

        ``include_full_text`` requires the caller to have been granted
        ``rag:read_full_text`` (enforced by the calling tool wrapper);
        when False, ``text`` is truncated to
        :data:`TEXT_PREVIEW_CAP_BYTES`.
        """
        if (query_text is None) == (query_vector is None):
            raise ValueError("exactly one of query_text or query_vector must be supplied")
        k = max(1, min(int(k), MAX_K))
        # source_ids is REQUIRED. Without it the underlying PGVectorStore
        # falls back to its own ``_source_id`` (unset here) and silently
        # returns an empty result, which is a footgun. Operators discover
        # the available corpora via honk_rag.list_sources.
        if not source_ids:
            raise ValueError("source_ids is required; call honk_rag.list_sources to see available corpora")
        cleaned_sources = [str(s).strip() for s in source_ids if s and str(s).strip()]
        if not cleaned_sources:
            raise ValueError("source_ids contained only blank entries; pass at least one valid UUID")

        store = self._store()
        if query_vector is None:
            vector = store._embedding.embed_query(query_text)  # noqa: SLF001
        else:
            vector = list(query_vector)

        pairs = store.search_by_vector_with_score(
            vector=vector,
            k=k,
            source_ids=cleaned_sources,
        )

        results: list[dict[str, Any]] = []
        for doc, distance in pairs:
            text = doc.page_content or ""
            if not include_full_text:
                preview = text.encode("utf-8")[:TEXT_PREVIEW_CAP_BYTES]
                # Decode safely — preview cap can land mid-multibyte
                # codepoint; ``errors="ignore"`` drops the partial
                # trailing byte rather than blowing up the response.
                text = preview.decode("utf-8", errors="ignore")
            results.append(
                {
                    "text": text,
                    "metadata": dict(doc.metadata or {}),
                    "score": float(distance),
                    "source_id": (doc.metadata or {}).get("_source_id"),
                }
            )
        return {
            "results": results,
            "k": k,
            "include_full_text": include_full_text,
        }

    def list_sources(self) -> dict[str, Any]:
        """Return one row per corpus configured in ``sources``.

        Reads directly from Postgres because we only want a small
        identity / metadata view. Excludes the document bodies and
        embeddings.
        """
        # Resolve the same connection string the SQL tool uses. The
        # PGVectorStore env-var ladder is:
        # PGVECTOR_CONNECTION_STRING > POSTGRES_URI normalized.
        from application.mcp_server.auth import _resolve_connection_string_from_env

        connection_string = self._connection_string or _resolve_connection_string_from_env()
        if not connection_string:
            raise RuntimeError("no Postgres connection string configured for honk_rag.list_sources")

        with psycopg.connect(connection_string) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text, name, user_id, is_public,
                           tokens, date, updated_at
                    FROM sources
                    ORDER BY name
                    LIMIT 200
                    """
                )
                rows = cur.fetchall()

        return {
            "sources": [
                {
                    "id": r[0],
                    "name": r[1],
                    "user_id": r[2],
                    "is_public": bool(r[3]) if r[3] is not None else None,
                    "tokens": r[4],
                    "ingested_at": r[5].isoformat() if r[5] else None,
                    "updated_at": r[6].isoformat() if r[6] else None,
                }
                for r in rows
            ]
        }
