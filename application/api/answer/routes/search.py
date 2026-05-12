import logging
from typing import Any, Dict, List

from flask import make_response, request
from flask_restx import fields, Resource

from application.api.answer.routes.base import _aztec_source_url, answer_ns
from application.core.settings import settings
from application.retriever.apiref_resolver import resolve_canonical_apiref
from application.storage.db.repositories.agents import AgentsRepository
from application.storage.db.session import db_readonly
from application.vectorstore.vector_creator import VectorCreator

logger = logging.getLogger(__name__)

# Mirrors the MCP server's clamp on `chunks` (1..20). The endpoint is
# also called directly by the standalone aztec MCP client, which clamps
# itself, so this is defense-in-depth: bad inputs from a future client
# can't push the SQL ``LIMIT`` to absurd values or starve the candidate
# pool with ``chunks=0``.
_MIN_CHUNKS = 1
_MAX_CHUNKS = 20


@answer_ns.route("/api/search")
class SearchResource(Resource):
    """Fast search endpoint for retrieving relevant documents.

    Aztec fork: uses global rerank across all sources connected to the
    API key (single embed + single SQL query via
    ``pgvector.search_by_vector_with_score``), then rewrites each result's
    ``source`` field to a public URL via ``_aztec_source_url`` so MCP and
    other API consumers don't see the parser-friendliness ``.txt``/``.md``
    extension hack used during ingest.
    """

    search_model = answer_ns.model(
        "SearchModel",
        {
            "question": fields.String(
                required=True, description="Search query"
            ),
            "api_key": fields.String(
                required=True, description="API key for authentication"
            ),
            "chunks": fields.Integer(
                required=False, default=5,
                description=f"Number of results to return ({_MIN_CHUNKS}..{_MAX_CHUNKS})",
            ),
        },
    )

    def _get_sources_from_api_key(self, api_key: str) -> List[str]:
        """Get source IDs connected to the API key/agent.

        Combines the agent's primary ``source_id`` with ``extra_source_ids``
        in the canonical order (primary first, extras after). The
        upstream version of this method appended only ``extra_source_ids``
        and fell back to ``source_id`` only if extras was empty — which
        silently dropped the primary corpus for any multi-source agent.
        Provisioning (``/api/internal/create_mcp_key``) writes
        ``valid_source_ids[0]`` to ``source_id`` and the remainder to
        ``extra_source_ids``, so the bug ate the most-relevant corpus
        for every Discord-issued MCP key.
        """
        with db_readonly() as conn:
            agent_data = AgentsRepository(conn).find_by_key(api_key)
        if not agent_data:
            return []

        ordered: List[str] = []
        seen: set = set()

        primary = agent_data.get("source_id")
        if primary:
            sid = str(primary)
            ordered.append(sid)
            seen.add(sid)

        # extra_source_ids is a PG ARRAY(UUID) of source UUIDs.
        for src in agent_data.get("extra_source_ids") or []:
            if not src:
                continue
            sid = str(src)
            if sid in seen:
                continue
            ordered.append(sid)
            seen.add(sid)

        return ordered

    @staticmethod
    def _line_is_path_shaped(line: str) -> bool:
        """A line is "path-shaped" if it looks like a filesystem path
        rather than a code/docs line.

        Strips a leading markdown heading marker (``#``, ``##`` …) so
        ``# aztec-nr/.../foo.nr`` is recognized as path-shaped just
        like the bare ``aztec-nr/.../foo.nr``. Path-shaped means:
        contains ``/`` and has no whitespace. Real signature lines
        (``pub fn foo(...)``, ``struct Bar { ... }``, ``pub use a::b;``)
        always have whitespace, so they never trip this predicate.
        """
        cleaned = line.lstrip("#").strip()
        return bool(cleaned) and "/" in cleaned and " " not in cleaned

    def _is_empty_apiref_chunk(
        self, page_content: str, metadata: Dict[str, Any]
    ) -> bool:
        """Detect apiref chunks whose extracted body is empty / path-only.

        ``noir_apiref`` (``scripts/ingest/noir_apiref.py``) writes one
        Markdown file per ``.nr`` source containing the public surface
        — doc comments + signatures, no bodies. For files with no doc
        comments and only ``pub use foo;`` declarations, the apiref
        output can render as a file-path heading and nothing else.
        The chunker honors the ``chunk_type=apiref`` exemption from
        the ``<50 token`` floor (legitimate signature-only chunks are
        very short), so these path-only chunks slip through and end up
        in the vector store as semantic noise.

        For error-lookup queries especially, surfacing a chunk that
        consists of just ``aztec-nr/aztec/src/.../foo.nr`` and nothing
        else looks like an empty result to the user. This filter drops
        chunks whose every non-empty line is path-shaped — i.e. there
        is no extracted API surface at all. Legitimate signature
        chunks (``pub fn poseidon(input: [Field; N]) -> Field``)
        survive: a real signature line has whitespace, which the
        path-shaped predicate excludes.

        Defense-in-depth: the right long-term fix is on the ingest
        side (``noir_apiref`` should skip files with no extractable
        public surface). This guard protects the existing index +
        any future ingest regressions for the same class of bug.
        """
        if metadata.get("chunk_type") != "apiref":
            return False

        lines = [
            line.strip()
            for line in (page_content or "").splitlines()
            if line.strip()
        ]
        if not lines:
            return True

        # All non-empty lines are path-shaped → no real API content.
        # Earlier versions of this helper tried to strip a "heading"
        # line by string-comparing against metadata.source / filename
        # / title; that was brittle because the rendered heading
        # often lacks the parser-friendliness extension (``.md``)
        # the metadata path carries, and on the MCP side the source
        # is a rewritten public URL that never matches the raw
        # heading string. The shape predicate is more robust and
        # doesn't need any metadata coupling.
        return all(self._line_is_path_shaped(line) for line in lines)

    def _coerce_chunks(self, raw: Any) -> int:
        """Coerce/clamp the ``chunks`` parameter to ``[_MIN_CHUNKS, _MAX_CHUNKS]``.

        Accepts ints and integer-valued floats/strings. Anything else
        falls back to the default (5).
        """
        try:
            if isinstance(raw, bool):  # bool is a subclass of int — reject
                raise TypeError
            value = int(raw)
        except (TypeError, ValueError):
            value = 5
        return max(_MIN_CHUNKS, min(_MAX_CHUNKS, value))

    def _search_global(
        self, query: str, source_ids: List[str], chunks: int
    ) -> List[Dict[str, Any]]:
        """Global rerank across all ``source_ids`` in a single SQL query.

        Replaces the per-source FIFO loop that caused sources past the
        first 2-3 in ``AZTEC_SOURCE_IDS`` to contribute zero results.
        Mirrors ``ClassicRAG._get_data`` minus the token-budget packer
        (the API contract is "return ``chunks`` results" — no token
        accounting). Dedup at the chunk level on
        ``(source_path, page_content[:200])`` so adjacent near-identical
        chunks from a single document don't crowd out other sources.
        Source URLs are rewritten via ``_aztec_source_url`` AFTER
        retrieval so multiple chunks from the same page can survive
        dedup but still emit clean public URLs.
        """
        if not source_ids:
            return []

        try:
            docsearch = VectorCreator.create_vectorstore(
                settings.VECTOR_STORE,
                source_ids[0],
                settings.EMBEDDINGS_KEY,
            )
        except Exception:
            logger.error("Error creating vectorstore", exc_info=True)
            return []

        if not hasattr(docsearch, "search_by_vector_with_score"):
            # Fail loud rather than silently degrade. Same discipline as
            # ClassicRAG._get_data — a misconfigured backend should
            # surface as a 5xx, not as a soft empty result.
            raise RuntimeError(
                f"Vector backend {type(docsearch).__name__} does not "
                "implement search_by_vector_with_score. Implement the "
                "method or switch to pgvector."
            )

        try:
            query_vector = docsearch._embedding.embed_query(query)
        except Exception:
            logger.error("Error embedding query", exc_info=True)
            return []

        # Over-fetch so dedup AND the empty-apiref filter have headroom.
        # With chunks=20 and 12 sources we want enough candidates that a
        # few duplicates plus a few path-only apiref hits don't starve
        # the result set. The previous formula assumed only dedup would
        # consume candidates; bump the floor so the apiref filter can
        # also drop a non-trivial fraction without leaving us short.
        candidate_k = max(80, chunks * 8, len(source_ids) * 6)

        pairs = docsearch.search_by_vector_with_score(
            query_vector,
            k=candidate_k,
            source_ids=list(source_ids),
        )

        # Scoped apiref resolver — mirror of the ClassicRAG path so that
        # /api/search (called by @aztec/mcp-server) and /stream both get
        # the canonical reference pinned first for identifier queries.
        # Returns None for concept queries and TS queries without a
        # matching .nr apiref, in which case the result list is unchanged.
        try:
            pin = resolve_canonical_apiref(docsearch, query, query_vector, source_ids)
        except Exception:
            logger.warning(
                "apiref_resolver raised on /api/search; falling back",
                exc_info=True,
            )
            pin = None
        if pin is not None:
            pinned_doc, pinned_distance = pin
            existing_top = pairs[0][0] if pairs else None
            same = False
            if existing_top is not None and hasattr(existing_top, "metadata"):
                same = (
                    existing_top.metadata.get("source")
                    == pinned_doc.metadata.get("source")
                    and (getattr(existing_top, "page_content", "") or "")[:200]
                    == (pinned_doc.page_content or "")[:200]
                )
            if not same:
                pairs = [(pinned_doc, pinned_distance)] + list(pairs)

        seen_keys: set = set()
        results: List[Dict[str, Any]] = []

        for doc, _distance in pairs:
            if len(results) >= chunks:
                break

            page_content = getattr(doc, "page_content", "") or ""
            metadata = getattr(doc, "metadata", {}) or {}

            raw_source = metadata.get("source") or metadata.get("_source_id") or ""
            filename = metadata.get("filename") or ""

            # Drop apiref chunks whose extracted body is empty or just
            # the file path. See ``_is_empty_apiref_chunk`` for why.
            # Done BEFORE dedup so an empty-apiref chunk doesn't pre-
            # empt a non-empty chunk at the same dedup key.
            if self._is_empty_apiref_chunk(page_content, metadata):
                continue

            # Chunk-level dedup BEFORE URL rewrite, so two distinct
            # chunks from the same page (which would collapse to one
            # public URL) both survive when relevant.
            dedup_key = (raw_source, filename, page_content[:200])
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            public_source = _aztec_source_url(raw_source) if raw_source else raw_source

            # Title preference: ingest-stamped title → filename (with
            # parser extensions stripped) → first ~50 chars of content.
            title = metadata.get("title") or metadata.get("post_title") or ""
            if not isinstance(title, str):
                title = str(title) if title else ""
            if title:
                title = title.split("/")[-1]
            else:
                title = filename or page_content[:50] + "..."
            for ext in (".nr.md", ".nr.txt", ".ts.txt", ".sol.txt", ".md", ".txt"):
                if title.endswith(ext):
                    if ext in (".nr.md", ".nr.txt"):
                        title = title[: -len(ext)] + ".nr"
                    elif ext == ".ts.txt":
                        title = title[: -len(ext)] + ".ts"
                    elif ext == ".sol.txt":
                        title = title[: -len(ext)] + ".sol"
                    else:
                        title = title[: -len(ext)]
                    break

            results.append({
                "text": page_content,
                "title": title,
                "source": public_source,
            })

        return results

    @answer_ns.expect(search_model)
    @answer_ns.doc(description="Search for relevant documents based on query")
    def post(self):
        data = request.get_json()

        question = data.get("question")
        api_key = data.get("api_key")
        chunks = self._coerce_chunks(data.get("chunks", 5))

        if not question:
            return make_response({"error": "question is required"}, 400)

        if not api_key:
            return make_response({"error": "api_key is required"}, 400)

        # Validate API key
        with db_readonly() as conn:
            agent = AgentsRepository(conn).find_by_key(api_key)
        if not agent:
            return make_response({"error": "Invalid API key"}, 401)

        try:
            source_ids = self._get_sources_from_api_key(api_key)

            if not source_ids:
                return make_response([], 200)

            results = self._search_global(question, source_ids, chunks)

            return make_response(results, 200)

        except Exception as e:
            logger.error(
                f"/api/search - error: {str(e)}",
                extra={"error": str(e)},
                exc_info=True,
            )
            return make_response({"error": "Search failed"}, 500)
