"""Targeted tests for application/api/answer/services/stream_processor.py.

Tests the ``get_prompt`` helper and simpler StreamProcessor methods against
real ephemeral Postgres.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest


@contextmanager
def _patch_db(conn):
    @contextmanager
    def _yield():
        yield conn

    with patch(
        "application.api.answer.services.stream_processor.db_readonly", _yield
    ), patch(
        "application.api.answer.services.stream_processor.db_session", _yield
    ):
        yield


class TestGetPrompt:
    def test_default_preset(self):
        from application.api.answer.services.stream_processor import get_prompt
        got = get_prompt("default")
        assert isinstance(got, str)
        assert len(got) > 0

    def test_creative_preset(self):
        from application.api.answer.services.stream_processor import get_prompt
        got = get_prompt("creative")
        assert isinstance(got, str) and len(got) > 0

    def test_strict_preset(self):
        from application.api.answer.services.stream_processor import get_prompt
        got = get_prompt("strict")
        assert isinstance(got, str) and len(got) > 0

    def test_agentic_default_preset(self):
        from application.api.answer.services.stream_processor import get_prompt
        got = get_prompt("agentic_default")
        assert isinstance(got, str) and len(got) > 0

    def test_none_defaults_to_default(self):
        from application.api.answer.services.stream_processor import get_prompt
        got = get_prompt(None)
        assert isinstance(got, str) and len(got) > 0

    def test_empty_string_defaults_to_default(self):
        from application.api.answer.services.stream_processor import get_prompt
        assert get_prompt("") == get_prompt("default")

    def test_non_string_id_converted(self):
        from application.api.answer.services.stream_processor import get_prompt
        # A UUID object would be stringified; use an int to test the branch
        with pytest.raises(ValueError):
            # Int converts to str "42" which isn't a preset, and will
            # raise ValueError once lookup fails
            get_prompt(42)

    def test_unknown_prompt_id_raises(self, pg_conn):
        from application.api.answer.services.stream_processor import get_prompt
        with _patch_db(pg_conn), pytest.raises(ValueError):
            get_prompt("00000000-0000-0000-0000-000000000000")

    def test_legacy_id_unknown_raises(self, pg_conn):
        from application.api.answer.services.stream_processor import get_prompt
        with _patch_db(pg_conn), pytest.raises(ValueError):
            get_prompt("507f1f77bcf86cd799439011")

    def test_uuid_lookup_returns_content(self, pg_conn):
        from application.api.answer.services.stream_processor import get_prompt
        from application.storage.db.repositories.prompts import (
            PromptsRepository,
        )

        prompt = PromptsRepository(pg_conn).create(
            "u", "myprompt", "custom prompt content",
        )
        with _patch_db(pg_conn):
            got = get_prompt(str(prompt["id"]))
        assert got == "custom prompt content"


class TestStreamProcessorInit:
    def test_basic_init(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        data = {"question": "hi", "conversation_id": "conv-1"}
        token = {"sub": "user-abc"}
        sp = StreamProcessor(data, token)
        assert sp.data == data
        assert sp.decoded_token == token
        assert sp.initial_user_id == "user-abc"
        assert sp.conversation_id == "conv-1"

    def test_init_no_token(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"question": "hi"}, None)
        assert sp.decoded_token is None
        assert sp.initial_user_id is None
        assert sp.conversation_id is None

    def test_init_sets_agent_id_from_data(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        data = {"question": "hi", "agent_id": "agent-xyz"}
        sp = StreamProcessor(data, {"sub": "u"})
        assert sp.agent_id == "agent-xyz"


class TestLoadConversationHistory:
    def test_no_conversation_id_uses_request_history(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        import json as _json

        data = {
            "question": "hi",
            "history": _json.dumps([{"prompt": "a", "response": "b"}]),
        }
        sp = StreamProcessor(data, {"sub": "u"})
        sp._load_conversation_history()
        assert len(sp.history) == 1

    def test_loads_existing_conversation_history(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.storage.db.repositories.conversations import (
            ConversationsRepository,
        )

        user = "u-load-hist"
        repo = ConversationsRepository(pg_conn)
        conv = repo.create(user, name="c")
        conv_id = str(conv["id"])
        repo.append_message(
            conv_id,
            {"prompt": "q1", "response": "r1"},
        )
        repo.append_message(
            conv_id,
            {"prompt": "q2", "response": "r2"},
        )

        sp = StreamProcessor(
            {"question": "x", "conversation_id": conv_id},
            {"sub": user},
        )
        # Also patch conversation_service.get_conversation's DB accessor
        with _patch_db(pg_conn), patch(
            "application.api.answer.services.conversation_service.db_readonly",
        ) as mock_readonly:
            @contextmanager
            def _yield():
                yield pg_conn
            mock_readonly.side_effect = _yield
            sp._load_conversation_history()
        assert len(sp.history) == 2

    def test_unauthorized_conversation_raises(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.storage.db.repositories.conversations import (
            ConversationsRepository,
        )

        repo = ConversationsRepository(pg_conn)
        conv = repo.create("owner-user", name="c")
        sp = StreamProcessor(
            {"question": "hack", "conversation_id": str(conv["id"])},
            {"sub": "hacker"},
        )
        with _patch_db(pg_conn), patch(
            "application.api.answer.services.conversation_service.db_readonly"
        ) as mock_readonly:
            @contextmanager
            def _yield():
                yield pg_conn
            mock_readonly.side_effect = _yield
            with pytest.raises(ValueError):
                sp._load_conversation_history()


class TestHasActiveDocs:
    def test_false_when_no_active_docs(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"question": "q"}, {"sub": "u"})
        sp.source = {}
        sp.all_sources = []
        assert sp._has_active_docs() is False

    def test_true_when_source_active(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"question": "q"}, {"sub": "u"})
        sp.source = {"active_docs": "abc"}
        assert sp._has_active_docs() is True

    def test_false_when_source_empty(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"question": "q"}, {"sub": "u"})
        sp.source = None
        assert sp._has_active_docs() is False

    def test_default_returns_false(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"question": "q"}, {"sub": "u"})
        sp.source = {"active_docs": "default"}
        sp.all_sources = []
        assert sp._has_active_docs() is False


class TestProcessAttachments:
    def test_no_attachments(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"question": "q"}, {"sub": "u"})
        # attachment_ids not set
        with _patch_db(pg_conn):
            sp._process_attachments()
        assert sp.attachments == []

    def test_retrieves_attachments_by_id(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.storage.db.repositories.attachments import (
            AttachmentsRepository,
        )

        user = "u-atts"
        att = AttachmentsRepository(pg_conn).create(
            user, "doc.txt", "/path",
            content="content here",
            size=100,
        )
        sp = StreamProcessor(
            {"question": "q", "attachments": [str(att["id"])]},
            {"sub": user},
        )
        with _patch_db(pg_conn):
            sp._process_attachments()
        assert len(sp.attachments) == 1
        assert sp.attachments[0]["content"] == "content here"


class TestGetAttachmentsContent:
    def test_empty_list(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"question": "q"}, {"sub": "u"})
        with _patch_db(pg_conn):
            got = sp._get_attachments_content([], "u")
        assert got == []

    def test_skips_missing_attachments(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"question": "q"}, {"sub": "u"})
        with _patch_db(pg_conn):
            got = sp._get_attachments_content(
                ["00000000-0000-0000-0000-000000000000"], "u",
            )
        assert got == []


class TestResolveAgentId:
    def test_returns_agent_id_from_request(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"agent_id": "req-agent"}, {"sub": "u"})
        assert sp._resolve_agent_id() == "req-agent"

    def test_returns_none_if_not_set(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        assert sp._resolve_agent_id() is None


class TestGetAgentKey:
    def test_returns_tuple_for_none_agent_id(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        key, is_shared, tok = sp._get_agent_key(None, "u")
        assert key is None and is_shared is False and tok is None

    def test_raises_for_missing_agent(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        with _patch_db(pg_conn), pytest.raises(ValueError, match="Agent not found"):
            sp._get_agent_key(
                "00000000-0000-0000-0000-000000000000", "u",
            )

    def test_returns_key_for_owned_agent(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.storage.db.repositories.agents import AgentsRepository

        agent = AgentsRepository(pg_conn).create(
            "owner", "a", "published",
            surface="web_ask", key="the-key",
        )
        sp = StreamProcessor({}, {"sub": "owner"})
        with _patch_db(pg_conn):
            key, shared, tok = sp._get_agent_key(str(agent["id"]), "owner")
        assert key == "the-key"
        assert shared is False

    def test_raises_on_unauthorized_access(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.storage.db.repositories.agents import AgentsRepository

        agent = AgentsRepository(pg_conn).create(
            "owner", "a", "published",
            surface="web_ask", key="k", shared=False,
        )
        sp = StreamProcessor({}, {"sub": "not-owner"})
        with _patch_db(pg_conn), pytest.raises(PermissionError, match="Unauthorized"):
            sp._get_agent_key(str(agent["id"]), "not-owner")


class TestConfigureSource:
    def test_agent_data_with_sources_list(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp._agent_data = {
            "sources": [
                {"id": "s1", "retriever": "classic"},
                {"id": "default"},
            ],
        }
        sp._configure_source()
        assert sp.source == {"active_docs": ["s1"]}

    def test_agent_data_with_single_source(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp._agent_data = {"source": "src-1", "retriever": "classic"}
        sp._configure_source()
        assert sp.source == {"active_docs": "src-1"}

    def test_agent_data_default_source(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp._agent_data = {"source": "default"}
        sp._configure_source()
        assert sp.source == {}

    def test_request_active_docs_used(self):
        """Direct active_docs from a JWT user is now authorized via
        SourceVisibilityService before reaching ``self.source``. Mock the
        service so this stays a unit test; integration coverage lives in
        the service + endpoint tests.
        """
        from unittest.mock import MagicMock, patch
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.services.source_visibility import ResolvedSources

        with patch(
            "application.api.answer.services.stream_processor.db_readonly"
        ) as mock_db_ro, patch(
            "application.api.answer.services.stream_processor.SourceVisibilityService"
        ) as MockSvc:
            mock_db_ro.return_value.__enter__ = lambda self: MagicMock()
            mock_db_ro.return_value.__exit__ = lambda *a: None
            MockSvc.return_value.resolve.return_value = ResolvedSources(
                visible=["abc"], rows={"abc": {"id": "abc"}},
                missing=[], invalid=[],
            )
            sp = StreamProcessor({"active_docs": "abc"}, {"sub": "u"})
            sp._configure_source()
        assert sp.source == {"active_docs": "abc"}

    def test_request_active_docs_malformed_uuid_raises_value_error(self):
        """Bug fix: malformed active_docs UUID used to fall through to
        Postgres ``CAST(:ids AS uuid[])`` and raise a generic cast error
        which the route translated to 400 'Malformed request body' via
        the broad except. Now SourceVisibilityService partitions
        malformed inputs into ``invalid`` and we raise ValueError with
        a specific message before any SQL runs. The route's existing
        ValueError branch keeps the 400 response.
        """
        import pytest as _pytest
        from unittest.mock import MagicMock, patch
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.services.source_visibility import ResolvedSources

        with patch(
            "application.api.answer.services.stream_processor.db_readonly"
        ) as mock_db_ro, patch(
            "application.api.answer.services.stream_processor.SourceVisibilityService"
        ) as MockSvc:
            mock_db_ro.return_value.__enter__ = lambda self: MagicMock()
            mock_db_ro.return_value.__exit__ = lambda *a: None
            MockSvc.return_value.resolve.return_value = ResolvedSources(
                visible=[], rows={}, missing=[], invalid=["not-a-uuid"],
            )
            sp = StreamProcessor(
                {"active_docs": ["not-a-uuid"]}, {"sub": "u"},
            )
            with _pytest.raises(ValueError, match="Malformed source ID"):
                sp._configure_source()

    def test_request_active_docs_invisible_raises_permission_error(self):
        """Well-formed UUID for a private source the user can't see —
        PermissionError -> 403 at the route layer."""
        import pytest as _pytest
        from unittest.mock import MagicMock, patch
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.services.source_visibility import ResolvedSources

        with patch(
            "application.api.answer.services.stream_processor.db_readonly"
        ) as mock_db_ro, patch(
            "application.api.answer.services.stream_processor.SourceVisibilityService"
        ) as MockSvc:
            mock_db_ro.return_value.__enter__ = lambda self: MagicMock()
            mock_db_ro.return_value.__exit__ = lambda *a: None
            MockSvc.return_value.resolve.return_value = ResolvedSources(
                visible=[], rows={},
                missing=["00000000-0000-0000-0000-000000000000"], invalid=[],
            )
            sp = StreamProcessor(
                {"active_docs": ["00000000-0000-0000-0000-000000000000"]},
                {"sub": "u"},
            )
            with _pytest.raises(PermissionError, match="not found or not visible"):
                sp._configure_source()

    def test_request_active_docs_default(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"active_docs": "default"}, {"sub": "u"})
        sp._configure_source()
        assert sp.source == {}

    def test_no_data_empty_source(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp._configure_source()
        assert sp.source == {}


class TestConfigureRetriever:
    def test_defaults(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp._configure_retriever()
        assert sp.retriever_config["retriever_name"] == "classic"
        assert sp.retriever_config["chunks"] == 2

    def test_agent_overrides(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp._agent_data = {"retriever": "hybrid_search", "chunks": 5}
        sp._configure_retriever()
        assert sp.retriever_config["retriever_name"] == "hybrid_search"
        assert sp.retriever_config["chunks"] == 5

    def test_request_overrides_agent(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor(
            {"retriever": "duckdb", "chunks": 7}, {"sub": "u"},
        )
        sp._agent_data = {"retriever": "hybrid_search", "chunks": 5}
        sp._configure_retriever()
        assert sp.retriever_config["retriever_name"] == "duckdb"
        assert sp.retriever_config["chunks"] == 7

    def test_invalid_agent_chunks_falls_back(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp._agent_data = {"chunks": "not-a-number"}
        sp._configure_retriever()
        assert sp.retriever_config["chunks"] == 2

    def test_invalid_request_chunks_falls_back(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"chunks": "abc"}, {"sub": "u"})
        sp._configure_retriever()
        assert sp.retriever_config["chunks"] == 2

    def test_isnonedoc_without_api_key_sets_chunks_to_0(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"isNoneDoc": True}, {"sub": "u"})
        sp.agent_key = None
        sp._configure_retriever()
        assert sp.retriever_config["chunks"] == 0


class TestGetPromptContent:
    def test_gets_from_agent_config(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        from application.storage.db.repositories.prompts import (
            PromptsRepository,
        )

        prompt = PromptsRepository(pg_conn).create(
            "u", "p1", "My prompt content",
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp.agent_config = {"prompt_id": str(prompt["id"])}

        with _patch_db(pg_conn):
            content = sp._get_prompt_content()
        assert content == "My prompt content"

    def test_returns_none_on_missing(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )

        sp = StreamProcessor({}, {"sub": "u"})
        sp.agent_config = {
            "prompt_id": "00000000-0000-0000-0000-000000000000",
        }
        with _patch_db(pg_conn):
            content = sp._get_prompt_content()
        assert content is None

    def test_caches_prompt_content(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp._prompt_content = "cached"
        # Even with no agent_config, cached value returned
        assert sp._get_prompt_content() == "cached"


class TestPreFetchDocs:
    def test_skips_when_no_active_docs(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp.source = {}
        docs, raw = sp.pre_fetch_docs("question")
        assert docs is None and raw is None

    def test_skips_when_isnonedoc_no_agent(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"isNoneDoc": True}, {"sub": "u"})
        sp.source = {"active_docs": "abc"}  # would normally be active
        sp.agent_id = None
        docs, raw = sp.pre_fetch_docs("q")
        assert docs is None and raw is None

    def test_handles_retriever_exception(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        sp.source = {"active_docs": "src"}
        with patch(
            "application.api.answer.services.stream_processor.StreamProcessor.create_retriever",
            side_effect=RuntimeError("boom"),
        ):
            docs, raw = sp.pre_fetch_docs("q")
        assert docs is None and raw is None


class TestConversationFetchCaching:
    """The conversation row + full message history must be fetched exactly
    once per request: ``_resolve_agent_id``, ``_load_conversation_history``
    and the compression threshold check all share the processor-level
    cache (``_get_conversation_once``)."""

    def _make_processor(self, conversation):
        from unittest.mock import MagicMock

        from application.api.answer.services.compression import (
            CompressionOrchestrator,
        )
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )

        sp = StreamProcessor(
            {"question": "q", "conversation_id": "conv-1"}, {"sub": "u1"},
        )
        sp.conversation_service = MagicMock()
        sp.conversation_service.get_conversation.return_value = conversation
        checker = MagicMock()
        checker.should_compress.return_value = False
        # Real orchestrator wired to the same mocked service: if
        # compress_if_needed ignored the pre-loaded conversation it would
        # re-fetch and the call-count assertion below would fail.
        sp.compression_orchestrator = CompressionOrchestrator(
            sp.conversation_service, threshold_checker=checker,
        )
        return sp

    def test_fetched_once_across_resolve_history_and_compression(self):
        conversation = {
            "_id": "conv-1",
            "agent_id": None,
            "queries": [
                {"prompt": "p1", "response": "r1", "metadata": {"k": "v"}},
            ],
        }
        sp = self._make_processor(conversation)
        with patch(
            "application.api.answer.services.stream_processor.settings."
            "ENABLE_CONVERSATION_COMPRESSION",
            True,
        ):
            assert sp._resolve_agent_id() is None  # fetch site 1
            sp._load_conversation_history()  # fetch sites 2 + 3
        assert sp.conversation_service.get_conversation.call_count == 1
        assert sp.history == [
            {"prompt": "p1", "response": "r1", "metadata": {"k": "v"}},
        ]

    def test_fetched_once_with_compression_disabled(self):
        conversation = {
            "_id": "conv-1",
            "agent_id": "agent-9",
            "queries": [{"prompt": "p1", "response": "r1"}],
        }
        sp = self._make_processor(conversation)
        with patch(
            "application.api.answer.services.stream_processor.settings."
            "ENABLE_CONVERSATION_COMPRESSION",
            False,
        ):
            assert sp._resolve_agent_id() == "agent-9"
            sp._load_conversation_history()
        assert sp.conversation_service.get_conversation.call_count == 1
        assert sp.history == [{"prompt": "p1", "response": "r1"}]

    def test_cache_is_identity_keyed(self):
        """``_configure_agent`` may rewrite ``initial_user_id`` to the
        agent owner for API-key callers AFTER ``_resolve_agent_id`` ran
        under the caller's identity. ``get_conversation`` enforces
        per-user access control, so a changed identity must re-fetch."""
        conversation = {"_id": "conv-1", "agent_id": None, "queries": []}
        sp = self._make_processor(conversation)
        assert sp._get_conversation_once() is conversation
        sp.initial_user_id = "owner-user"  # simulate api_key identity rewrite
        assert sp._get_conversation_once() is conversation
        assert sp.conversation_service.get_conversation.call_count == 2

    def test_not_found_is_not_cached(self):
        """A None result (missing/unauthorized) must not poison the cache;
        the failure path may re-fetch."""
        sp = self._make_processor(None)
        assert sp._get_conversation_once() is None
        assert sp._conversation is None
        assert sp._get_conversation_once() is None
        assert sp.conversation_service.get_conversation.call_count == 2


class TestPreFetchTools:
    def test_disabled_globally_returns_none(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "u"})
        with patch(
            "application.api.answer.services.stream_processor.settings.ENABLE_TOOL_PREFETCH",
            False,
        ):
            got = sp.pre_fetch_tools()
        assert got is None

    def test_disabled_per_request(self):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({"disable_tool_prefetch": True}, {"sub": "u"})
        with patch(
            "application.api.answer.services.stream_processor.settings.ENABLE_TOOL_PREFETCH",
            True,
        ):
            got = sp.pre_fetch_tools()
        assert got is None

    def test_no_user_tools_returns_none(self, pg_conn):
        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )
        sp = StreamProcessor({}, {"sub": "no-tools-user"})
        with _patch_db(pg_conn), patch(
            "application.api.answer.services.stream_processor.settings.ENABLE_TOOL_PREFETCH",
            True,
        ):
            got = sp.pre_fetch_tools()
        assert got is None
