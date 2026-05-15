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
class TestIsEmptyApirefChunk:
    """Defense-in-depth: docsgpt's ``noir_apiref`` ingest can produce
    chunks for ``.nr`` files whose extracted public surface is empty —
    the chunk body is then just the file path. These slip through the
    chunker's <50-token filter (apiref is exempt). On the search side
    we drop such chunks so the LLM consumer doesn't see file-path-only
    semantic results that look indistinguishable from empty.
    """

    def _meta(self, **kwargs):
        m = {"chunk_type": "apiref"}
        m.update(kwargs)
        return m

    def test_non_apiref_chunks_pass_through(self, flask_app):
        from application.api.answer.routes.search import SearchResource
        with flask_app.app_context():
            r = SearchResource()
            assert r._is_empty_apiref_chunk("", {}) is False
            assert r._is_empty_apiref_chunk(
                "any short text", {"chunk_type": "regular"}
            ) is False

    def test_drops_apiref_chunk_with_only_path_heading(self, flask_app):
        """The reproducer from the v1.21 dogfood test."""
        from application.api.answer.routes.search import SearchResource
        text = "\n\naztec-nr/aztec/src/context/note_existence_request.nr\n\n\n"
        meta = self._meta(
            source="aztec-nr/aztec/src/context/note_existence_request.nr",
            filename="note_existence_request.nr.md",
        )
        with flask_app.app_context():
            assert SearchResource()._is_empty_apiref_chunk(text, meta) is True

    def test_drops_apiref_chunk_with_md_heading_only(self, flask_app):
        from application.api.answer.routes.search import SearchResource
        text = "# aztec-nr/aztec/src/foo.nr\n"
        meta = self._meta(source="aztec-nr/aztec/src/foo.nr")
        with flask_app.app_context():
            assert SearchResource()._is_empty_apiref_chunk(text, meta) is True

    def test_drops_apiref_chunk_with_md_heading_when_metadata_has_md_extension(self, flask_app):
        """Regression for codex review: the metadata source has the
        parser-friendliness ``.md`` extension while the rendered
        heading does not. The earlier implementation tried to strip
        the heading by string-comparing against metadata.source and
        failed silently, leaving the heading line intact — at which
        point the path-shaped predicate also failed because ``# ...``
        contains whitespace. The new shape-only predicate catches it.
        """
        from application.api.answer.routes.search import SearchResource
        text = "# aztec-nr/aztec/src/foo.nr\n"
        meta = self._meta(
            source="aztec-nr/aztec/src/foo.nr.md",  # ← .md extension
            filename="foo.nr.md",
        )
        with flask_app.app_context():
            assert SearchResource()._is_empty_apiref_chunk(text, meta) is True

    def test_drops_completely_empty_apiref_chunk(self, flask_app):
        from application.api.answer.routes.search import SearchResource
        with flask_app.app_context():
            r = SearchResource()
            assert r._is_empty_apiref_chunk("", self._meta()) is True
            assert r._is_empty_apiref_chunk("\n\n   \n", self._meta()) is True

    def test_keeps_signature_only_apiref_chunk(self, flask_app):
        """Critical: short-but-meaningful signature chunks must NOT
        be dropped. ``pub fn poseidon(...)`` is a legitimate apiref
        result — the filter must look at content shape, not length."""
        from application.api.answer.routes.search import SearchResource
        text = (
            "# aztec-nr/aztec/src/hash.nr\n"
            "pub fn poseidon(input: [Field; N]) -> Field\n"
        )
        meta = self._meta(source="aztec-nr/aztec/src/hash.nr")
        with flask_app.app_context():
            assert SearchResource()._is_empty_apiref_chunk(text, meta) is False

    def test_keeps_apiref_chunk_with_doc_comment(self, flask_app):
        from application.api.answer.routes.search import SearchResource
        text = (
            "aztec-nr/aztec/src/note.nr\n"
            "\n"
            "Note existence and non-nullification.\n"
        )
        meta = self._meta(
            source="aztec-nr/aztec/src/note.nr",
            filename="note.nr.md",
        )
        with flask_app.app_context():
            assert SearchResource()._is_empty_apiref_chunk(text, meta) is False

    def test_keeps_apiref_chunk_with_struct_signature(self, flask_app):
        from application.api.answer.routes.search import SearchResource
        text = (
            "# aztec-nr/aztec/src/state_vars/private_set.nr\n"
            "pub struct PrivateSet<T, Context>\n"
        )
        meta = self._meta(source="aztec-nr/aztec/src/state_vars/private_set.nr")
        with flask_app.app_context():
            assert SearchResource()._is_empty_apiref_chunk(text, meta) is False

    def test_drops_apiref_chunk_where_every_line_is_path_shaped(self, flask_app):
        """Some apiref outputs have multiple ``pub use`` re-exports that
        render as path-shaped lines with no signatures. Treat as empty."""
        from application.api.answer.routes.search import SearchResource
        text = (
            "# aztec-nr/aztec/src/lib.nr\n"
            "aztec/src/foo/bar.nr\n"
            "aztec/src/baz/qux.nr\n"
        )
        meta = self._meta(source="aztec-nr/aztec/src/lib.nr")
        with flask_app.app_context():
            assert SearchResource()._is_empty_apiref_chunk(text, meta) is True


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

    def test_search_global_drops_empty_apiref_chunks(self, flask_app):
        """Integration: empty-apiref chunks must be dropped during
        ``_search_global``, even when they're the highest-ranked
        candidates. Otherwise the user sees file-path-only semantic
        results that look indistinguishable from empty.
        """
        from application.api.answer.routes.search import SearchResource

        # First (best) candidate is an empty apiref chunk; second is a
        # signature-bearing apiref chunk. Without the filter the empty
        # one would dominate the response.
        pairs = [
            (_make_doc(
                "\n\naztec-nr/aztec/src/context/note_existence_request.nr\n\n",
                {
                    "source": "aztec-nr/aztec/src/context/note_existence_request.nr",
                    "filename": "note_existence_request.nr.md",
                    "chunk_type": "apiref",
                },
            ), 0.10),
            (_make_doc(
                "# aztec-nr/aztec/src/hash.nr\n"
                "pub fn poseidon(input: [Field; N]) -> Field\n",
                {
                    "source": "aztec-nr/aztec/src/hash.nr",
                    "filename": "hash.nr.md",
                    "chunk_type": "apiref",
                },
            ), 0.30),
        ]
        vs = self._patched_vs(pairs)

        with flask_app.app_context(), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ):
            results = SearchResource()._search_global("q", ["src"], 5)

        assert len(results) == 1
        # Only the signature-bearing chunk survives
        assert "poseidon" in results[0]["text"]

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
    """Swap both ``db_readonly`` and ``db_session`` for the test conn.

    ``db_session`` is patched alongside ``db_readonly`` so the
    ``_log_request`` user_logs write lands on the same fixture
    connection as agent/sources reads. Without this, the post-success
    log write would try to open a real Postgres session and the test
    would fail in environments without a default DSN.
    """
    @contextmanager
    def _yield():
        yield conn

    with patch(
        "application.api.answer.routes.search.db_readonly", _yield
    ), patch(
        "application.api.answer.routes.search.db_session", _yield,
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
            "u", "a", "published",
            surface="web_ask", key="no-src-key",
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
            surface="web_ask",
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
            surface="web_ask",
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
            surface="web_ask",
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
            surface="web_ask",
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
            surface="web_ask",
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
            surface="web_ask",
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
            surface="web_ask",
            key="dup-key",
            source_id=str(primary["id"]),
            extra_source_ids=[str(primary["id"])],
        )
        with _patch_search_db(pg_conn), flask_app.app_context():
            got = SearchResource()._get_sources_from_api_key("dup-key")
        assert got == [str(primary["id"])]


# ---------------------------------------------------------------------------
# user_logs observability — every authenticated /api/search call must write
# a row so MCP-server traffic is attributable per agent key alongside
# /stream. ``user_logs`` is the only persistence path for /api/search.
# ---------------------------------------------------------------------------


class TestSearchUserLogs:
    """Per-call attribution for ``/api/search`` lands in
    ``user_logs.metadata`` (added by migration 0007). The
    ``docsgpt_mcp_ro`` role has SELECT on ``metadata`` but NOT on
    ``data``, so writing analytics into ``metadata`` is what makes them
    visible to claudebox via the host MCP server's ``honk_sql.*`` tools.

    Bearer keys (api_key) are deliberately NOT persisted — the call is
    attributable via ``user_logs.user_id`` (agent owner pseudonym) and
    ``metadata->>'agent_id'`` (specific issued key).
    """

    def _fake_vs(self, pairs):
        vs = MagicMock()
        vs._embedding.embed_query.return_value = [0.0] * 8
        vs.search_by_vector_with_score.return_value = pairs
        return vs

    def _read_api_search_logs(self, pg_conn, *, user_id):
        """Return all rows for endpoint='api_search' attributed to
        ``user_id``. Queries the actual table the MCP role would hit,
        with the same column projection (``id, user_id, endpoint,
        timestamp, metadata``) so the test mirrors operator access.
        """
        from sqlalchemy import text

        result = pg_conn.execute(
            text(
                """
                SELECT id, user_id, endpoint, timestamp, metadata
                FROM user_logs
                WHERE endpoint = :endpoint AND user_id = :user_id
                ORDER BY id
                """
            ),
            {"endpoint": "api_search", "user_id": user_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]

    def test_writes_user_log_row_on_success(self, pg_conn, flask_app):
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src = SourcesRepository(pg_conn).create("src", user_id="discord_p_v1:abc")
        agent = AgentsRepository(pg_conn).create(
            "discord_p_v1:abc", "Aztec MCP", "published",
            surface="web_ask",
            key="log-success-key",
            source_id=str(src["id"]),
        )

        vs = self._fake_vs([
            (_make_doc(
                "hit body",
                {"source": "version-v4.2.0/docs/foo.md", "title": "Foo"},
            ), 0.1),
        ])

        with _patch_search_db(pg_conn), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ), flask_app.app_context():
            with flask_app.test_request_context(
                json={
                    "question": "what is poseidon",
                    "api_key": "log-success-key",
                    "chunks": 3,
                },
            ):
                result = SearchResource().post()
        assert result.status_code == 200

        rows = self._read_api_search_logs(pg_conn, user_id="discord_p_v1:abc")
        assert len(rows) == 1, "exactly one user_logs row per call"
        row = rows[0]
        assert row["endpoint"] == "api_search"
        assert row["user_id"] == "discord_p_v1:abc"

        meta = row["metadata"]
        assert meta["action"] == "api_search"
        assert meta["question"] == "what is poseidon"
        assert meta["chunks_requested"] == 3
        assert meta["result_count"] == 1
        # Sources are the public-URL-rewritten form, matching the response.
        assert meta["sources"] and meta["sources"][0].startswith(
            "https://docs.aztec.network/"
        )
        # Agent id stored as a string so JSONB consumers don't have to
        # know about UUID types.
        assert isinstance(meta["agent_id"], str)
        assert meta["agent_id"] == str(agent["id"])

    def test_does_not_persist_bearer_key(self, pg_conn, flask_app):
        """Defense-in-depth regression: the api_key MUST NOT appear in
        the persisted row. Migration 0007 makes ``metadata`` MCP-readable;
        if a future refactor accidentally puts ``api_key`` in metadata
        (or anywhere queryable as ``docsgpt_mcp_ro``), this test fails.
        """
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository
        from sqlalchemy import text as sa_text

        src = SourcesRepository(pg_conn).create("src", user_id="u-leak")
        AgentsRepository(pg_conn).create(
            "u-leak", "Aztec MCP", "published",
            surface="web_ask",
            key="should-not-be-in-row",
            source_id=str(src["id"]),
        )

        vs = self._fake_vs([
            (_make_doc("body", {"source": "version-v4.2.0/docs/x.md"}), 0.1),
        ])

        with _patch_search_db(pg_conn), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "should-not-be-in-row"},
            ):
                SearchResource().post()

        # Cast both columns to text and grep — defeats whatever JSONB
        # nesting a future regression might use to hide the key.
        result = pg_conn.execute(
            sa_text(
                """
                SELECT (COALESCE(data::text, '') || COALESCE(metadata::text, ''))
                FROM user_logs
                WHERE endpoint = 'api_search' AND user_id = 'u-leak'
                """
            )
        )
        for (combined,) in result.fetchall():
            assert "should-not-be-in-row" not in combined, (
                "bearer key leaked into user_logs row"
            )

    def test_redacts_bearer_key_echoed_in_question(self, pg_conn, flask_app):
        """A client that echoes its own ``api_key`` into the ``question``
        field would otherwise drop the plaintext bearer into
        ``metadata.question``, which the ``docsgpt_mcp_ro`` role can
        SELECT. The bearer must be redacted before the row is written.
        Codex review finding (review round 1).
        """
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository
        from sqlalchemy import text as sa_text

        src = SourcesRepository(pg_conn).create("src", user_id="u-echo")
        bearer = "echo-bearer-key-xyz123"
        AgentsRepository(pg_conn).create(
            "u-echo", "Aztec MCP", "published",
            surface="web_ask",
            key=bearer,
            source_id=str(src["id"]),
        )

        vs = self._fake_vs([
            (_make_doc("body", {"source": "version-v4.2.0/docs/x.md"}), 0.1),
        ])

        # Question contains the bearer three times in different
        # positions (front, middle, tail). All three occurrences must be
        # redacted. Redaction runs BEFORE the 10k slice, so a bearer
        # echo near the slice boundary can't be half-truncated into the
        # row — that property is implicit, not exercised at the boundary.
        question = (
            f"please search for {bearer} thanks "
            f"also {bearer} and {bearer} at the end"
        )

        with _patch_search_db(pg_conn), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": question, "api_key": bearer},
            ):
                result = SearchResource().post()
        assert result.status_code == 200

        rows = self._read_api_search_logs(pg_conn, user_id="u-echo")
        assert len(rows) == 1
        stored = rows[0]["metadata"]["question"]
        assert bearer not in stored, (
            "bearer leaked into metadata.question — redaction failed"
        )
        assert "[REDACTED_BEARER]" in stored
        # Also re-run the column-cast leak check on the row in case a
        # future refactor parks the bearer in some other JSONB field.
        result = pg_conn.execute(
            sa_text(
                """
                SELECT (COALESCE(data::text, '') || COALESCE(metadata::text, ''))
                FROM user_logs
                WHERE endpoint = 'api_search' AND user_id = 'u-echo'
                """
            )
        )
        for (combined,) in result.fetchall():
            assert bearer not in combined

    def test_request_succeeds_when_question_is_non_string(self, pg_conn, flask_app):
        """Best-effort logging: a malformed ``question`` (non-string, e.g.
        an int from a rogue client) must not turn a successful 200
        search into a 500 via the log path. ``post()`` already accepts
        the request because ``not 42`` is False; ``_log_request`` must
        coerce defensively or swallow. Codex review finding.
        """
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src = SourcesRepository(pg_conn).create("src", user_id="u-typed")
        AgentsRepository(pg_conn).create(
            "u-typed", "Aztec MCP", "published",
            surface="web_ask",
            key="typed-key",
            source_id=str(src["id"]),
        )

        vs = self._fake_vs([
            (_make_doc("body", {"source": "version-v4.2.0/docs/x.md"}), 0.1),
        ])

        with _patch_search_db(pg_conn), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ), flask_app.app_context():
            # JSON number, not a string — Flask passes it through as int.
            with flask_app.test_request_context(
                json={"question": 42, "api_key": "typed-key"},
            ):
                result = SearchResource().post()
        # Request itself succeeds even though the log payload assembly
        # has to coerce. The log row is best-effort here — we don't
        # care if it lands; we care that 200 isn't downgraded.
        assert result.status_code == 200

    def test_writes_user_log_row_when_agent_has_no_sources(self, pg_conn, flask_app):
        """Empty-source agents still authenticate and return 200; the
        call should be attributable in user_logs with ``result_count=0``.
        Otherwise misconfigured agents are silently invisible.
        """
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository

        AgentsRepository(pg_conn).create(
            "discord_p_v1:def", "Aztec MCP", "published",
            surface="web_ask",
            key="log-empty-key",
        )

        with _patch_search_db(pg_conn), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "log-empty-key"},
            ):
                result = SearchResource().post()
        assert result.status_code == 200
        assert result.json == []

        rows = self._read_api_search_logs(pg_conn, user_id="discord_p_v1:def")
        assert len(rows) == 1
        assert rows[0]["metadata"]["result_count"] == 0
        assert rows[0]["metadata"]["sources"] == []

    def test_no_user_log_row_on_missing_question(self, pg_conn, flask_app):
        """400 responses must not produce a log row — there's no
        agent to attribute the call to and the field validator
        rejected it before any work was done."""
        from application.api.answer.routes.search import SearchResource
        from sqlalchemy import text as sa_text

        with _patch_search_db(pg_conn), flask_app.app_context():
            with flask_app.test_request_context(json={"api_key": "x"}):
                result = SearchResource().post()
        assert result.status_code == 400

        n = pg_conn.execute(
            sa_text("SELECT COUNT(*) FROM user_logs WHERE endpoint = 'api_search'")
        ).scalar()
        assert n == 0

    def test_no_user_log_row_on_invalid_api_key(self, pg_conn, flask_app):
        """401 responses must not produce a log row — without a
        resolved agent we don't have a user_id to attribute to."""
        from application.api.answer.routes.search import SearchResource
        from sqlalchemy import text as sa_text

        with _patch_search_db(pg_conn), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "no-such-key"},
            ):
                result = SearchResource().post()
        assert result.status_code == 401

        n = pg_conn.execute(
            sa_text("SELECT COUNT(*) FROM user_logs WHERE endpoint = 'api_search'")
        ).scalar()
        assert n == 0

    def test_no_user_log_row_when_search_500s(self, pg_conn, flask_app):
        """500 responses must not produce a log row — the call did not
        successfully complete and we'd be recording a misleading
        ``result_count=0`` for an actual failure."""
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src = SourcesRepository(pg_conn).create("src", user_id="u-err")
        AgentsRepository(pg_conn).create(
            "u-err", "a", "published",
            surface="web_ask",
            key="log-err-key",
            source_id=str(src["id"]),
        )

        with _patch_search_db(pg_conn), patch(
            "application.api.answer.routes.search.SearchResource._get_sources_from_api_key",
            side_effect=RuntimeError("boom"),
        ), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "log-err-key"},
            ):
                result = SearchResource().post()
        assert result.status_code == 500

        rows = self._read_api_search_logs(pg_conn, user_id="u-err")
        assert rows == []

    def test_request_succeeds_when_log_write_raises(self, pg_conn, flask_app):
        """Best-effort logging: if the user_logs write fails (e.g.
        Postgres transient error), the search response MUST still
        succeed. Mirrors the swallowed-exception discipline in
        /stream's log write (base.py:700-704)."""
        from application.api.answer.routes.search import SearchResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.sources import SourcesRepository

        src = SourcesRepository(pg_conn).create("src", user_id="u-swallow")
        AgentsRepository(pg_conn).create(
            "u-swallow", "a", "published",
            surface="web_ask",
            key="log-swallow-key",
            source_id=str(src["id"]),
        )

        vs = self._fake_vs([
            (_make_doc("body", {"source": "version-v4.2.0/docs/x.md"}), 0.1),
        ])

        # Patch ``db_session`` (the symbol used by ``_log_request``)
        # to raise on entry. ``db_readonly`` stays pointed at the
        # fixture conn so the auth + sources lookups succeed.
        @contextmanager
        def _broken_session():
            raise RuntimeError("postgres on fire")
            yield  # pragma: no cover

        @contextmanager
        def _yield():
            yield pg_conn

        with patch(
            "application.api.answer.routes.search.db_readonly", _yield,
        ), patch(
            "application.api.answer.routes.search.db_session", _broken_session,
        ), patch(
            "application.api.answer.routes.search.VectorCreator.create_vectorstore",
            return_value=vs,
        ), flask_app.app_context():
            with flask_app.test_request_context(
                json={"question": "q", "api_key": "log-swallow-key"},
            ):
                result = SearchResource().post()
        assert result.status_code == 200
        assert len(result.json) == 1
