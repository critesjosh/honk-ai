from unittest.mock import MagicMock, Mock, patch

import pytest

from application.retriever.base import BaseRetriever
from application.retriever.retriever_creator import RetrieverCreator


# ── BaseRetriever ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestBaseRetriever:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseRetriever()

    def test_subclass_must_implement_search(self):
        class Incomplete(BaseRetriever):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_concrete_subclass_works(self):
        class Concrete(BaseRetriever):
            def search(self, *args, **kwargs):
                return "ok"

        instance = Concrete()
        assert instance.search() == "ok"


# ── RetrieverCreator ───────────────────────────────────────────────────────────


@pytest.mark.unit
class TestRetrieverCreator:
    def test_create_classic(self):
        mock_cls = Mock(return_value="rag_instance")
        original = RetrieverCreator.retrievers.copy()
        RetrieverCreator.retrievers["classic"] = mock_cls
        try:
            result = RetrieverCreator.create_retriever("classic", "arg1", key="val")
            mock_cls.assert_called_once_with("arg1", key="val")
            assert result == "rag_instance"
        finally:
            RetrieverCreator.retrievers.update(original)

    def test_create_default(self):
        mock_cls = Mock(return_value="rag_instance")
        original = RetrieverCreator.retrievers.copy()
        RetrieverCreator.retrievers["default"] = mock_cls
        try:
            result = RetrieverCreator.create_retriever("default")
            mock_cls.assert_called_once_with()
            assert result == "rag_instance"
        finally:
            RetrieverCreator.retrievers.update(original)

    def test_create_none_type_uses_default(self):
        mock_cls = Mock(return_value="rag_instance")
        original = RetrieverCreator.retrievers.copy()
        RetrieverCreator.retrievers["default"] = mock_cls
        try:
            result = RetrieverCreator.create_retriever(None)
            mock_cls.assert_called_once()
            assert result == "rag_instance"
        finally:
            RetrieverCreator.retrievers.update(original)

    def test_case_insensitive(self):
        mock_cls = Mock(return_value="rag_instance")
        original = RetrieverCreator.retrievers.copy()
        RetrieverCreator.retrievers["classic"] = mock_cls
        try:
            RetrieverCreator.create_retriever("CLASSIC")
            mock_cls.assert_called_once()
        finally:
            RetrieverCreator.retrievers.update(original)

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="No retievers class found"):
            RetrieverCreator.create_retriever("nonexistent")


# ── ClassicRAG ─────────────────────────────────────────────────────────────────


@pytest.fixture
def _patch_llm_creator(mock_llm, monkeypatch):
    """Patch LLMCreator.create_llm to return the shared mock_llm fixture."""
    monkeypatch.setattr(
        "application.retriever.classic_rag.LLMCreator.create_llm",
        Mock(return_value=mock_llm),
    )
    return mock_llm


def _make_rag(source=None, _patch_llm_creator=None, **overrides):
    """Helper – builds a ClassicRAG with sensible defaults."""
    from application.retriever.classic_rag import ClassicRAG

    defaults = dict(
        source=source or {"question": "hello"},
        chat_history=None,
        prompt="",
        chunks=2,
        doc_token_limit=50000,
        model_id="test-model",
        user_api_key=None,
        agent_id=None,
        llm_name="openai",
        api_key="fake",
        decoded_token={"sub": "user1"},
    )
    defaults.update(overrides)
    return ClassicRAG(**defaults)


@pytest.mark.unit
class TestClassicRAGInit:
    def test_basic_init(self, _patch_llm_creator):
        rag = _make_rag()
        assert rag.original_question == "hello"
        assert rag.chunks == 2
        assert rag.vectorstores == []

    def test_active_docs_as_list(self, _patch_llm_creator):
        rag = _make_rag(source={"question": "q", "active_docs": ["a", "b"]})
        assert rag.vectorstores == ["a", "b"]

    def test_active_docs_as_string(self, _patch_llm_creator):
        rag = _make_rag(source={"question": "q", "active_docs": "single"})
        assert rag.vectorstores == ["single"]

    def test_active_docs_none(self, _patch_llm_creator):
        rag = _make_rag(source={"question": "q", "active_docs": None})
        assert rag.vectorstores == []

    def test_chunks_string_converted(self, _patch_llm_creator):
        rag = _make_rag(chunks="5")
        assert rag.chunks == 5

    def test_chunks_invalid_string_defaults(self, _patch_llm_creator):
        rag = _make_rag(chunks="abc")
        assert rag.chunks == 2

    def test_decoded_token_none(self, _patch_llm_creator):
        rag = _make_rag(decoded_token=None)
        assert rag.decoded_token is None


@pytest.mark.unit
class TestClassicRAGValidateVectorstore:
    def test_removes_empty_ids(self, _patch_llm_creator):
        rag = _make_rag(source={"question": "q", "active_docs": ["ok", "", "  ", "good"]})
        assert rag.vectorstores == ["ok", "good"]

    def test_empty_vectorstores_no_error(self, _patch_llm_creator):
        rag = _make_rag(source={"question": "q"})
        assert rag.vectorstores == []


@pytest.mark.unit
class TestClassicRAGRephraseQuery:
    """Rephrasing happens via ``search(query)``; ``__init__`` only seeds
    ``self.question`` from ``source["question"]`` without an LLM call
    (the eval script relies on the seed and calls ``_get_data`` directly)."""

    @staticmethod
    def _search(rag, query):
        """Run ``search`` with retrieval stubbed out; return rag.question."""
        rag._get_data = Mock(return_value=[])
        rag.search(query)
        return rag.question

    def test_init_seeds_question_without_llm_call(
        self, _patch_llm_creator, mock_llm
    ):
        mock_llm.gen = Mock(return_value="rephrased question")
        rag = _make_rag(
            source={"question": "original", "active_docs": ["vs1"]},
            chat_history=[{"prompt": "hi", "response": "hello"}],
        )
        assert rag.question == "original"
        mock_llm.gen.assert_not_called()

    def test_no_history_returns_original(self, _patch_llm_creator, mock_llm):
        mock_llm.gen = Mock(return_value="rephrased question")
        rag = _make_rag(
            source={"active_docs": ["vs1"]},
            chat_history=[],
        )
        assert self._search(rag, "original") == "original"
        mock_llm.gen.assert_not_called()

    def test_no_vectorstores_returns_original(self, _patch_llm_creator, mock_llm):
        mock_llm.gen = Mock(return_value="rephrased question")
        rag = _make_rag(
            source={},
            chat_history=[{"prompt": "hi", "response": "hello"}],
        )
        assert self._search(rag, "original") == "original"
        mock_llm.gen.assert_not_called()

    def test_chunks_zero_returns_original(self, _patch_llm_creator, mock_llm):
        mock_llm.gen = Mock(return_value="rephrased question")
        rag = _make_rag(
            source={"active_docs": ["vs1"]},
            chat_history=[{"prompt": "hi", "response": "hello"}],
            chunks=0,
        )
        assert self._search(rag, "original") == "original"
        mock_llm.gen.assert_not_called()

    def test_rephrase_called_with_history(self, _patch_llm_creator, mock_llm):
        mock_llm.gen = Mock(return_value="rephrased question")
        rag = _make_rag(
            source={"active_docs": ["vs1"]},
            chat_history=[{"prompt": "hi", "response": "hello"}],
        )
        assert self._search(rag, "original") == "rephrased question"
        mock_llm.gen.assert_called_once()

    def test_rephrase_llm_returns_empty_falls_back(self, _patch_llm_creator, mock_llm):
        mock_llm.gen = Mock(return_value="")
        rag = _make_rag(
            source={"active_docs": ["vs1"]},
            chat_history=[{"prompt": "hi", "response": "hello"}],
        )
        assert self._search(rag, "original") == "original"

    def test_rephrase_llm_exception_falls_back(self, _patch_llm_creator, mock_llm):
        mock_llm.gen = Mock(side_effect=RuntimeError("boom"))
        rag = _make_rag(
            source={"active_docs": ["vs1"]},
            chat_history=[{"prompt": "hi", "response": "hello"}],
        )
        assert self._search(rag, "original") == "original"


def _mock_global_docsearch(pairs):
    """Build a mock vectorstore whose search_by_vector_with_score returns
    ``pairs`` (list of (doc, distance) tuples). Also wires up the embedding
    client so _get_data can call embed_query without blowing up.
    """
    mock = MagicMock()
    mock._embedding.embed_query = Mock(return_value=[0.1, 0.2, 0.3])
    mock.search_by_vector_with_score = Mock(return_value=pairs)
    return mock


def _mock_doc(content, **metadata):
    m = MagicMock()
    m.page_content = content
    m.metadata = metadata
    return m


@pytest.mark.unit
class TestClassicRAGGetData:
    def test_chunks_zero_returns_empty(self, _patch_llm_creator):
        rag = _make_rag(chunks=0)
        assert rag._get_data() == []

    def test_no_vectorstores_returns_empty(self, _patch_llm_creator):
        rag = _make_rag(source={"question": "q"})
        assert rag._get_data() == []

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_returns_docs_with_metadata(self, mock_tokens, mock_vc, _patch_llm_creator):
        doc = _mock_doc(
            "content here",
            title="path/to/Title",
            filename="/docs/file.txt",
            source="http://example.com",
        )
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch([(doc, 0.1)])

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        docs = rag._get_data()

        assert len(docs) == 1
        assert docs[0]["text"] == "content here"
        assert docs[0]["title"] == "Title"
        assert docs[0]["filename"] == "file.txt"
        assert docs[0]["source"] == "http://example.com"

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_propagates_chunk_type_apiref(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        # ``metadata.chunk_type`` is stamped by the ingest chunker on
        # ``*.nr.md`` apiref output (see application/parser/file/bulk.py).
        # The pack step must surface it so the chunk-header builder in
        # ``stream_processor.pre_fetch_docs`` can tag the chunk header
        # ``[apiref]`` for the LLM.
        apiref_doc = _mock_doc(
            "pub fn foo(...)",
            title="foo.nr",
            filename="aztec-nr/foo.nr.md",
            source="aztec-nr/foo.nr.md",
            chunk_type="apiref",
        )
        markdown_doc = _mock_doc(
            "Markdown explainer prose",
            title="how_to.md",
            filename="how_to.md",
            source="version-v4.3.0/docs/how_to.md",
        )
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch(
            [(apiref_doc, 0.1), (markdown_doc, 0.2)]
        )

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        docs = rag._get_data()

        assert len(docs) == 2
        assert docs[0]["chunk_type"] == "apiref"
        # Non-apiref docs must NOT carry a chunk_type — downstream code
        # tests truthiness, so a stray empty string would falsely
        # suppress the tag at the chunk-header step.
        assert "chunk_type" not in docs[1]

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_dict_style_docs(self, mock_tokens, mock_vc, _patch_llm_creator):
        pair = ({"text": "dict content", "metadata": {"title": "Dict Title"}}, 0.1)
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch([pair])

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        docs = rag._get_data()

        assert len(docs) == 1
        assert docs[0]["text"] == "dict content"

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=100000)
    def test_token_budget_respected(self, mock_tokens, mock_vc, _patch_llm_creator):
        # 3 distinct docs so dedup doesn't eat them; each claims 100000 tokens
        # which blows the 90-token budget so none should land.
        pairs = [
            (_mock_doc(f"big content {i}", title="t", source=f"s{i}"), 0.1)
            for i in range(3)
        ]
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch(pairs)

        rag = _make_rag(
            source={"question": "q", "active_docs": ["vs1"]},
            doc_token_limit=100,
        )
        assert len(rag._get_data()) == 0

    @patch("application.retriever.classic_rag.VectorCreator")
    def test_vectorstore_create_error_returns_empty(self, mock_vc, _patch_llm_creator):
        mock_vc.create_vectorstore.side_effect = RuntimeError("connection failed")

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        assert rag._get_data() == []

    @patch("application.retriever.classic_rag.VectorCreator")
    def test_backend_without_global_search_raises(
        self, mock_vc, _patch_llm_creator
    ):
        # A misconfigured backend (e.g. a legacy one that never grew the
        # global-search primitive) must fail loudly, not silently answer
        # without retrieved context. spec=[] forbids attribute access so
        # hasattr() returns False for search_by_vector_with_score.
        mock_docsearch = MagicMock(spec=[])
        mock_vc.create_vectorstore.return_value = mock_docsearch

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        with pytest.raises(RuntimeError, match="search_by_vector_with_score"):
            rag._get_data()

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_global_rerank_packs_in_score_order(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        # Lower distance = better match. Verify retriever returns docs in
        # the SAME order the search returns them (which is score order).
        pairs = [
            (_mock_doc("best match",   source="src-a", title="a"), 0.05),
            (_mock_doc("middle match", source="src-b", title="b"), 0.20),
            (_mock_doc("weak match",   source="src-c", title="c"), 0.80),
        ]
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch(pairs)

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1", "vs2", "vs3"]})
        docs = rag._get_data()
        assert [d["text"] for d in docs] == ["best match", "middle match", "weak match"]

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_embeds_question_once_across_all_sources(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        # Previously embedded the query once per source. Now must be ONE
        # embed call per user question regardless of source count.
        mock_ds = _mock_global_docsearch([
            (_mock_doc("c", source="s", title="t"), 0.1),
        ])
        mock_vc.create_vectorstore.return_value = mock_ds

        rag = _make_rag(
            source={"question": "q", "active_docs": [f"vs{i}" for i in range(12)]}
        )
        rag._get_data()

        assert mock_ds._embedding.embed_query.call_count == 1
        # Confirm we only created one vectorstore, not one per source.
        assert mock_vc.create_vectorstore.call_count == 1

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_invariant_to_source_order(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        # Same candidates, different active_docs orderings: results should
        # match AND the retriever should pass the user-provided source_ids
        # through verbatim to the backend (the SQL `source_id = ANY(...)`
        # clause is set-invariant — correctness is the backend's
        # responsibility, but the retriever must not reorder here).
        pairs = [
            (_mock_doc("alpha", source="src-a", title="A"), 0.05),
            (_mock_doc("beta",  source="src-b", title="B"), 0.15),
            (_mock_doc("gamma", source="src-c", title="C"), 0.25),
        ]
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch(pairs)

        order_1 = ["a", "b", "c"]
        order_2 = ["c", "a", "b"]
        rag1 = _make_rag(source={"question": "q", "active_docs": order_1})
        rag2 = _make_rag(source={"question": "q", "active_docs": order_2})
        assert rag1._get_data() == rag2._get_data()

        # Verify the retriever passed each caller's source_ids through
        # verbatim. We recreate the mock for each call so we can inspect.
        mock_ds_1 = _mock_global_docsearch(pairs)
        mock_ds_2 = _mock_global_docsearch(pairs)
        mock_vc.create_vectorstore.side_effect = [mock_ds_1, mock_ds_2]
        rag_a = _make_rag(source={"question": "q", "active_docs": order_1})
        rag_b = _make_rag(source={"question": "q", "active_docs": order_2})
        rag_a._get_data()
        rag_b._get_data()
        assert mock_ds_1.search_by_vector_with_score.call_args.kwargs["source_ids"] == order_1
        assert mock_ds_2.search_by_vector_with_score.call_args.kwargs["source_ids"] == order_2

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_candidate_k_scales_with_budget_not_fixed_cap(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        # Regression: if candidate_k is a fixed floor (previously 100) the
        # packer can run out of candidates long before filling the budget
        # on a large deployment with lots of short chunks. With a large
        # doc_token_limit the retriever should ask for many more than 100
        # candidates so the packer has room to fill.
        mock_ds = _mock_global_docsearch([])
        mock_vc.create_vectorstore.return_value = mock_ds

        rag = _make_rag(
            source={"question": "q", "active_docs": ["vs1", "vs2"]},
            doc_token_limit=50000,  # budget=45000, floor-bound k = 45000/50*2 = 1800
        )
        rag._get_data()
        asked_k = mock_ds.search_by_vector_with_score.call_args.kwargs["k"]
        assert asked_k >= 1000

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_dedup_near_identical_chunks(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        # Two chunks from the same source with the same leading text should
        # collapse to one, so we don't waste budget on adjacent dupes.
        dup_text = (
            "This is a duplicated chunk that starts identically for the "
            "first 200 characters but may diverge much later on in the "
            "body. The retriever should collapse duplicates keyed on "
            "(source_path, leading 200 chars) to avoid wasting the token "
            "budget on near-identical neighbours within the same document."
        )
        pairs = [
            (_mock_doc(dup_text,             source="same.md", title="x"), 0.05),
            (_mock_doc(dup_text + " trail",  source="same.md", title="x"), 0.06),
            (_mock_doc("distinct chunk",     source="other.md", title="y"), 0.10),
        ]
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch(pairs)

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        docs = rag._get_data()
        assert len(docs) == 2
        assert {d["source"] for d in docs} == {"same.md", "other.md"}

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_doc_missing_filename_uses_title(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        doc = _mock_doc("content", title="MyTitle")
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch([(doc, 0.1)])

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        docs = rag._get_data()
        assert docs[0]["filename"] == "MyTitle"

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_non_string_title_converted(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        doc = _mock_doc("content", title=42)
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch([(doc, 0.1)])

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        docs = rag._get_data()
        assert docs[0]["title"] == "42"

    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_uses_source_id_fallback_when_source_missing(
        self, mock_tokens, mock_vc, _patch_llm_creator
    ):
        # When metadata.source is absent, the retriever should fall back to
        # the _source_id the vectorstore stamps into metadata so URL
        # rewriting downstream still has something to key on.
        doc = _mock_doc("content", title="t", _source_id="src-uuid-xyz")
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch([(doc, 0.1)])

        rag = _make_rag(source={"question": "q", "active_docs": ["vs1"]})
        docs = rag._get_data()
        assert docs[0]["source"] == "src-uuid-xyz"


@pytest.mark.unit
class TestClassicRAGSearch:
    @patch("application.retriever.classic_rag.VectorCreator")
    @patch("application.retriever.classic_rag.num_tokens_from_string", return_value=10)
    def test_search_with_query_override(
        self, mock_tokens, mock_vc, _patch_llm_creator, mock_llm
    ):
        doc = _mock_doc("result", title="t")
        mock_vc.create_vectorstore.return_value = _mock_global_docsearch([(doc, 0.1)])
        mock_llm.gen = Mock(return_value="")

        rag = _make_rag(source={"question": "original", "active_docs": ["vs1"]})
        docs = rag.search(query="override query")
        assert rag.original_question == "override query"
        assert len(docs) == 1

    def test_search_without_query_uses_default(self, _patch_llm_creator):
        rag = _make_rag(source={"question": "q"})
        docs = rag.search()
        assert docs == []


# ── Aztec extension-hack stripper ────────────────────────────────────────────


@pytest.mark.unit
class TestStripExtensionHack:
    """``ClassicRAG._strip_extension_hack`` strips the
    parser-friendliness suffix appended at ingest time so the LLM
    sees canonical filenames in chunk headers (``hash.nr`` not
    ``hash.nr.md``). Plain ``.md`` / ``.txt`` filenames must NOT be
    stripped — only the doubled ``.<code>.<parser>`` shape."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            # Apiref Markdown view of code → strip the .md
            ("hash.nr.md", "hash.nr"),
            ("lifecycle.nr.md", "lifecycle.nr"),
            ("foo.ts.md", "foo.ts"),
            ("Bar.sol.md", "Bar.sol"),
            # Body-bearing code with .txt parser-friendliness suffix
            ("Token.nr.txt", "Token.nr"),
            ("foo.ts.txt", "foo.ts"),
            ("Bar.sol.txt", "Bar.sol"),
            # Plain markdown / text concept docs — leave alone
            ("overview.md", "overview.md"),
            ("release_notes.md", "release_notes.md"),
            ("api.txt", "api.txt"),
            # Other extensions untouched
            ("foo.json", "foo.json"),
            ("bar.nr", "bar.nr"),
            ("Token.sol", "Token.sol"),
            # Edge cases
            ("", ""),
            (None, None),
        ],
    )
    def test_strip_extension_hack(self, name, expected):
        from application.retriever.classic_rag import ClassicRAG
        assert ClassicRAG._strip_extension_hack(name) == expected

    def test_strips_within_extract_doc_fields(self, _patch_llm_creator):
        """Confirms the integration: the stripped filename is what
        gets baked into the chunk header the LLM grounds against;
        the raw `metadata.source` (used by the URL rewriter) keeps
        the suffix so source-URL mapping continues to work."""
        rag = _make_rag(source={"question": "q"})
        doc = Mock()
        doc.page_content = "content"
        doc.metadata = {
            "title": "hash.nr.md",
            "source": "aztec-nr/aztec/src/hash.nr.md",
            "filename": "hash.nr.md",
        }
        _content, _md, title, filename, source = rag._extract_doc_fields(doc)
        assert filename == "hash.nr"
        assert title == "hash.nr"
        assert source == "aztec-nr/aztec/src/hash.nr.md"
