"""Tests for ``application.api.answer.routes.search:SearchResource``.

The Aztec fork rewrote ``_search_vectorstores`` (per-source FIFO) into
``_search_global`` (global rerank via ``pgvector.search_by_vector_with_score``)
and added URL rewriting via ``_aztec_source_url`` so MCP-side consumers
don't see the parser-friendliness ``.txt``/``.md`` extension hack.
These tests guard those properties.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


# Helper: build a mock Document-like object that the global-rerank code can
# consume. The real pgvector path returns ``langchain.Document`` instances,
# but ``_search_global`` only reads ``page_content`` and ``metadata``.
def _make_doc(text: str, metadata: dict):
    doc = MagicMock()
    doc.page_content = text
    doc.metadata = metadata
    return doc


@pytest.mark.unit
class TestSearchResourceValidation:
    def test_returns_error_when_question_missing(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context():
            with flask_app.test_request_context(json={"api_key": "test_key"}):
                resource = SearchResource()
                result = resource.post()

                assert result.status_code == 400
                assert "question" in result.json["error"]

    def test_returns_error_when_api_key_missing(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context():
            with flask_app.test_request_context(json={"question": "test query"}):
                resource = SearchResource()
                result = resource.post()

                assert result.status_code == 400
                assert "api_key" in result.json["error"]


@pytest.mark.unit
class TestCoerceChunks:
    """``chunks`` is clamped to [1, 20] and silently coerced from
    strings/floats/ints. Defense-in-depth against direct callers (the
    MCP client clamps too, but the endpoint is also called by the
    standalone aztec-docs MCP server)."""

    def test_clamps_below_minimum(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context():
            assert SearchResource()._coerce_chunks(0) == 1
            assert SearchResource()._coerce_chunks(-5) == 1

    def test_clamps_above_maximum(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context():
            assert SearchResource()._coerce_chunks(100) == 20

    def test_truncates_floats(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context():
            assert SearchResource()._coerce_chunks(3.7) == 3

    def test_falls_back_to_default_on_garbage(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context():
            assert SearchResource()._coerce_chunks("not a number") == 5
            assert SearchResource()._coerce_chunks(None) == 5
            assert SearchResource()._coerce_chunks(True) == 5  # bool rejected

    def test_passes_through_valid_int(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context():
            assert SearchResource()._coerce_chunks(7) == 7
            assert SearchResource()._coerce_chunks("12") == 12


@pytest.mark.unit
class TestSearchGlobal:
    """Properties of the new global-rerank ``_search_global`` method."""

    def _patched_vs(self, pairs):
        """Build a mock vectorstore that supports the new contract."""
        vs = MagicMock()
        vs._embedding.embed_query.return_value = [0.0] * 8
        vs.search_by_vector_with_score.return_value = pairs
        return vs

    def test_returns_empty_when_no_source_ids(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context():
            assert SearchResource()._search_global("q", [], 5) == []

    def test_returns_empty_on_vectorstore_creation_failure(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            side_effect=Exception("boom"),
        ):
            assert SearchResource()._search_global("q", ["src"], 5) == []

    def test_raises_when_backend_lacks_search_by_vector_with_score(self, flask_app):
        """A misconfigured backend should fail loudly, not silently
        return empty — same discipline as ``ClassicRAG._get_data``."""
        from application.api.answer.routes.search import SearchResource

        bad_vs = MagicMock(spec=["search"])  # no search_by_vector_with_score

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=bad_vs,
        ):
            with pytest.raises(RuntimeError, match="search_by_vector_with_score"):
                SearchResource()._search_global("q", ["src"], 5)

    def test_returns_global_ranked_results(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        # The first pair has the smallest distance — it must come first
        # in the result, regardless of which source_id it came from.
        # Without global rerank, the old per-source FIFO would have
        # returned source-1's result first.
        pairs = [
            (_make_doc("best match",
                      {"source": "version-v4.2.0/docs/foo.md", "title": "Best"}), 0.10),
            (_make_doc("worse match",
                      {"source": "aztec-nr/aztec/src/lib.nr.md", "title": "Worse"}), 0.50),
        ]
        vs = self._patched_vs(pairs)

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            results = SearchResource()._search_global(
                "q", ["src1", "src2"], 5
            )

        assert len(results) == 2
        assert results[0]["text"] == "best match"
        assert results[1]["text"] == "worse match"

    def test_passes_all_source_ids_to_pgvector(self, flask_app):
        """The single SQL query must receive ALL source_ids — that's
        how global rerank avoids per-source starvation."""
        from application.api.answer.routes.search import SearchResource

        vs = self._patched_vs([])
        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            SearchResource()._search_global(
                "q", ["src-a", "src-b", "src-c"], 5
            )

        call = vs.search_by_vector_with_score.call_args
        assert call.kwargs["source_ids"] == ["src-a", "src-b", "src-c"]
        # over-fetches so dedup has headroom
        assert call.kwargs["k"] >= 5

    def test_chunk_level_dedup(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        # Two chunks with identical leading-200 chars from the same
        # source: the second must be dropped.
        dup_text = "Duplicate content " * 20
        pairs = [
            (_make_doc(dup_text,
                      {"source": "version-v4.2.0/docs/x.md", "filename": "x.md"}), 0.1),
            (_make_doc(dup_text,
                      {"source": "version-v4.2.0/docs/x.md", "filename": "x.md"}), 0.2),
            (_make_doc("Unique content here",
                      {"source": "version-v4.2.0/docs/y.md", "filename": "y.md"}), 0.3),
        ]
        vs = self._patched_vs(pairs)

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            results = SearchResource()._search_global("q", ["src"], 10)

        assert len(results) == 2
        assert results[0]["text"] == dup_text
        assert results[1]["text"] == "Unique content here"

    def test_url_rewrite_developer_docs(self, flask_app):
        """Developer docs paths get rewritten to docs.aztec.network."""
        from application.api.answer.routes.search import SearchResource

        pairs = [
            (_make_doc("hello",
                      {"source": "version-v4.2.0/docs/getting_started.md",
                       "filename": "getting_started.md"}), 0.1),
        ]
        vs = self._patched_vs(pairs)

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            results = SearchResource()._search_global("q", ["src"], 5)

        assert results[0]["source"].startswith("https://docs.aztec.network/")
        # the .md extension is stripped from the URL
        assert results[0]["source"].endswith("getting_started")

    def test_url_rewrite_aztec_nr_apiref(self, flask_app):
        """aztec-nr apiref paths get rewritten to GitHub at v4.2.0,
        with the .nr.md extension hack stripped."""
        from application.api.answer.routes.search import SearchResource

        pairs = [
            (_make_doc("api",
                      {"source": "aztec-nr/aztec/src/hash.nr.md",
                       "filename": "hash.nr.md"}), 0.1),
        ]
        vs = self._patched_vs(pairs)

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            results = SearchResource()._search_global("q", ["src"], 5)

        assert "github.com/AztecProtocol/aztec-packages" in results[0]["source"]
        assert results[0]["source"].endswith("/hash.nr")
        # Title hack strip: hash.nr.md → hash.nr
        assert results[0]["title"] == "hash.nr"

    def test_url_rewrite_noir_contracts_code(self, flask_app):
        """Noir contracts get rewritten to GitHub, with the .nr.txt
        extension hack stripped."""
        from application.api.answer.routes.search import SearchResource

        pairs = [
            (_make_doc("contract code",
                      {"source": "noir-contracts/contracts/Token.nr.txt",
                       "filename": "Token.nr.txt"}), 0.1),
        ]
        vs = self._patched_vs(pairs)

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            results = SearchResource()._search_global("q", ["src"], 5)

        assert results[0]["source"].endswith("/Token.nr")
        assert results[0]["title"] == "Token.nr"

    def test_respects_chunks_limit(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        pairs = [
            (_make_doc(f"content {i}",
                      {"source": f"version-v4.2.0/docs/f{i}.md",
                       "filename": f"f{i}.md"}), 0.1 + i * 0.01)
            for i in range(20)
        ]
        vs = self._patched_vs(pairs)

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            results = SearchResource()._search_global("q", ["src"], 5)

        assert len(results) == 5

    def test_uses_filename_when_no_title_metadata(self, flask_app):
        from application.api.answer.routes.search import SearchResource

        pairs = [
            (_make_doc("content",
                      {"source": "unknown/path/no_rewrite.txt",
                       "filename": "document.pdf"}), 0.1),
        ]
        vs = self._patched_vs(pairs)

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            results = SearchResource()._search_global("q", ["src"], 5)

        assert results[0]["title"] == "document.pdf"


# ---------------------------------------------------------------------------
# Real-PG tests for SearchResource end-to-end through ``post()``.
# ---------------------------------------------------------------------------


@contextmanager
def _patch_search_db(conn):
    @contextmanager
    def _yield():
        yield conn

    with patch(
        "application.api.answer.routes.search.db_readonly", _yield
    ):
        yield


class TestSearchResourcePgConn:
    def test_invalid_api_key_returns_401(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource

        with _patch_search_db(pg_conn), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "does-not-exist"},
            ):
                result = SearchResource().post()
        assert result.status_code == 401

    def test_no_sources_returns_empty(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository

        AgentsRepository(pg_conn).create(
            "u", "a", "published", key="no-src-key",
        )
        with _patch_search_db(pg_conn), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "no-src-key"},
            ):
                result = SearchResource().post()
        assert result.status_code == 200
        assert result.json == []

    def test_search_returns_results(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src = SourcesRepository(pg_conn).create("src", user_id="u")
        AgentsRepository(pg_conn).create(
            "u", "a", "published",
            key="search-key",
            source_id=str(src["id"]),
        )

        fake_vs = MagicMock()
        fake_vs._embedding.embed_query.return_value = [0.0] * 8
        fake_vs.search_by_vector_with_score.return_value = [
            (_make_doc("answer text",
                      {"source": "version-v4.2.0/docs/x.md", "title": "Doc"}), 0.1),
        ]

        with _patch_search_db(pg_conn), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=fake_vs,
        ), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "search-key"},
            ):
                result = SearchResource().post()
        assert result.status_code == 200
        assert len(result.json) == 1
        # URL was rewritten to public docs domain
        assert result.json[0]["source"].startswith("https://docs.aztec.network/")

    def test_search_uses_extra_source_ids(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src1 = SourcesRepository(pg_conn).create("s1", user_id="u")
        src2 = SourcesRepository(pg_conn).create("s2", user_id="u")
        AgentsRepository(pg_conn).create(
            "u", "a", "published",
            key="extra-key",
            extra_source_ids=[str(src1["id"]), str(src2["id"])],
        )

        fake_vs = MagicMock()
        fake_vs._embedding.embed_query.return_value = [0.0] * 8
        fake_vs.search_by_vector_with_score.return_value = [
            (_make_doc("one", {"source": "version-v4.2.0/docs/x.md", "title": "A"}), 0.1),
        ]
        with _patch_search_db(pg_conn), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=fake_vs,
        ), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "extra-key", "chunks": 4},
            ):
                result = SearchResource().post()
        assert result.status_code == 200
        # Both source ids must be present in the global-rerank query so
        # neither corpus is starved.
        call = fake_vs.search_by_vector_with_score.call_args
        assert set(call.kwargs["source_ids"]) == {str(src1["id"]), str(src2["id"])}

    def test_search_exception_returns_500(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src = SourcesRepository(pg_conn).create("src", user_id="u")
        AgentsRepository(pg_conn).create(
            "u", "a", "published",
            key="err-key",
            source_id=str(src["id"]),
        )

        with _patch_search_db(pg_conn), patch(
            "application.api.answer.routes.search.SearchResource._get_sources_from_api_key",
            side_effect=RuntimeError("boom"),
        ), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "err-key"},
            ):
                result = SearchResource().post()
        assert result.status_code == 500


class TestGetSourcesFromApiKeyPg:
    def test_empty_for_unknown_key(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource

        with _patch_search_db(pg_conn), flask_app.app_context():
            got = SearchResource()._get_sources_from_api_key("nope")
        assert got == []

    def test_returns_extra_source_ids(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src = SourcesRepository(pg_conn).create("s", user_id="u")
        AgentsRepository(pg_conn).create(
            "u", "a", "published",
            key="sources-key",
            extra_source_ids=[str(src["id"])],
        )
        with _patch_search_db(pg_conn), flask_app.app_context():
            got = SearchResource()._get_sources_from_api_key("sources-key")
        assert got == [str(src["id"])]

    def test_falls_back_to_single_source(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src = SourcesRepository(pg_conn).create("s", user_id="u")
        AgentsRepository(pg_conn).create(
            "u", "a", "published",
            key="single-key",
            source_id=str(src["id"]),
        )
        with _patch_search_db(pg_conn), flask_app.app_context():
            got = SearchResource()._get_sources_from_api_key("single-key")
        assert got == [str(src["id"])]

    def test_primary_and_extras_combine_with_primary_first(self, pg_conn, flask_app):
        """Regression guard: the upstream version of this method only
        looked at ``extra_source_ids`` and fell back to ``source_id``
        only when extras was empty, silently dropping the primary
        corpus for every multi-source MCP agent. ``create_mcp_key``
        stores ``valid_source_ids[0]`` in ``source_id`` and the rest
        in ``extra_source_ids``; the canonical search order must
        preserve that.
        """
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        primary = SourcesRepository(pg_conn).create("primary", user_id="u")
        extra1 = SourcesRepository(pg_conn).create("e1", user_id="u")
        extra2 = SourcesRepository(pg_conn).create("e2", user_id="u")
        AgentsRepository(pg_conn).create(
            "u", "a", "published",
            key="combined-key",
            source_id=str(primary["id"]),
            extra_source_ids=[str(extra1["id"]), str(extra2["id"])],
        )
        with _patch_search_db(pg_conn), flask_app.app_context():
            got = SearchResource()._get_sources_from_api_key("combined-key")
        assert got == [str(primary["id"]), str(extra1["id"]), str(extra2["id"])]

    def test_dedups_when_primary_appears_in_extras(self, pg_conn, flask_app):
        """Defensive: if a malformed write put the primary into both
        ``source_id`` AND ``extra_source_ids``, dedupe — duplicates
        in the source list would cause pgvector to scan the same
        corpus twice."""
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        primary = SourcesRepository(pg_conn).create("primary", user_id="u")
        AgentsRepository(pg_conn).create(
            "u", "a", "published",
            key="dup-key",
            source_id=str(primary["id"]),
            extra_source_ids=[str(primary["id"])],
        )
        with _patch_search_db(pg_conn), flask_app.app_context():
            got = SearchResource()._get_sources_from_api_key("dup-key")
        assert got == [str(primary["id"])]
