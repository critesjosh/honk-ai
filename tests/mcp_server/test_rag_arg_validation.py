"""Argument-validation tests for ``honk_rag.search``.

We don't exercise the actual pgvector path here — that needs Postgres
plus an embedding service. What we DO test is the argument-validation
that runs BEFORE we touch the store, so a missing ``source_ids`` (or a
``query_text`` AND ``query_vector`` both being passed) yields a clean
``ValueError`` instead of a silent empty response.
"""

from __future__ import annotations

import pytest

from application.mcp_server.tools.rag import RAGSearcher


@pytest.mark.unit
class TestRAGSearchArgValidation:
    """RAG arg validation runs before any DB / embedding call."""

    def _searcher(self) -> RAGSearcher:
        # No connection_string — we expect to fail validation before
        # the store is constructed, so the bad URL is never reached.
        return RAGSearcher(connection_string="postgresql://nonexistent:0/__never__")

    def test_requires_query_text_or_vector(self) -> None:
        searcher = self._searcher()
        with pytest.raises(ValueError, match="exactly one of query_text or query_vector"):
            searcher.search(source_ids=["abc"])

    def test_rejects_both_query_text_and_vector(self) -> None:
        searcher = self._searcher()
        with pytest.raises(ValueError, match="exactly one of query_text or query_vector"):
            searcher.search(
                query_text="hello",
                query_vector=[0.0, 0.1],
                source_ids=["abc"],
            )

    def test_requires_source_ids(self) -> None:
        """``source_ids=None`` previously returned [] silently — now raises."""
        searcher = self._searcher()
        with pytest.raises(ValueError, match="source_ids is required"):
            searcher.search(query_text="hello", source_ids=None)

    def test_rejects_empty_source_ids(self) -> None:
        searcher = self._searcher()
        with pytest.raises(ValueError, match="source_ids is required"):
            searcher.search(query_text="hello", source_ids=[])

    def test_rejects_blank_only_source_ids(self) -> None:
        searcher = self._searcher()
        with pytest.raises(ValueError, match="blank entries"):
            searcher.search(query_text="hello", source_ids=["", "   "])
