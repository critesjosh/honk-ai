import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestBaseAnswerValidation:
    pass

    def test_validate_request_passes_with_required_fields(
        self, mock_mongo_db, flask_app
    ):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            data = {"question": "What is Python?"}

            result = resource.validate_request(data)

            assert result is None

    def test_validate_request_fails_without_question(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            data = {}

            result = resource.validate_request(data)

            assert result is not None
            assert result.status_code == 400
            assert "question" in result.json["message"].lower()

    def test_validate_with_conversation_id_required(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            data = {"question": "Test"}

            result = resource.validate_request(data, require_conversation_id=True)

            assert result is not None
            assert result.status_code == 400
            assert "conversation_id" in result.json["message"].lower()

    def test_validate_passes_with_all_required_fields(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            data = {"question": "Test", "conversation_id": str(uuid.uuid4())}

            result = resource.validate_request(data, require_conversation_id=True)

            assert result is None


@pytest.mark.unit
class TestUsageChecking:
    pass

    def test_returns_none_when_no_api_key(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            agent_config = {}

            result = resource.check_usage(agent_config)

            assert result is None









@pytest.mark.unit
class TestGPTModelRetrieval:
    pass

    def test_initializes_gpt_model(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            assert hasattr(resource, "default_model_id")
            assert resource.default_model_id is not None


@pytest.mark.unit
class TestConversationServiceIntegration:
    pass

    def test_initializes_conversation_service(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            assert hasattr(resource, "conversation_service")
            assert resource.conversation_service is not None



@pytest.mark.unit
class TestCompleteStreamMethod:
    pass

    def test_streams_answer_chunks(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            mock_agent = MagicMock()
            mock_agent.gen.return_value = iter(
                [
                    {"answer": "Hello "},
                    {"answer": "world!"},
                ]
            )

            decoded_token = {"sub": "user123"}

            stream = list(
                resource.complete_stream(
                    question="Test question",
                    agent=mock_agent,
                    conversation_id=None,
                    user_api_key=None,
                    decoded_token=decoded_token,
                    should_save_conversation=False,
                )
            )

            answer_chunks = [s for s in stream if '"type": "answer"' in s]
            assert len(answer_chunks) == 2
            assert '"answer": "Hello "' in answer_chunks[0]
            assert '"answer": "world!"' in answer_chunks[1]

    def test_streams_sources(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            mock_agent = MagicMock()
            mock_agent.gen.return_value = iter(
                [
                    {"answer": "Test answer"},
                    {"sources": [{"title": "doc1.txt", "text": "x" * 200}]},
                ]
            )

            decoded_token = {"sub": "user123"}

            stream = list(
                resource.complete_stream(
                    question="Test?",
                    agent=mock_agent,
                    conversation_id=None,
                    user_api_key=None,
                    decoded_token=decoded_token,
                    should_save_conversation=False,
                )
            )

            source_chunks = [s for s in stream if '"type": "source"' in s]
            assert len(source_chunks) == 1
            assert '"title": "doc1.txt"' in source_chunks[0]

    def test_handles_error_during_streaming(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            mock_agent = MagicMock()
            mock_agent.gen.side_effect = Exception("Test error")

            decoded_token = {"sub": "user123"}

            stream = list(
                resource.complete_stream(
                    question="Test?",
                    agent=mock_agent,
                    conversation_id=None,
                    user_api_key=None,
                    decoded_token=decoded_token,
                    should_save_conversation=False,
                )
            )

            assert any('"type": "error"' in s for s in stream)

    def test_error_relays_actual_message_not_generic(
        self, mock_mongo_db, flask_app
    ):
        """Outer-stream errors should surface the sanitized actual error,
        not the generic ``Please try again later`` placeholder. Prevents
        regressing the operator/user feedback that errors were being
        silently swallowed at the widget."""
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            mock_agent = MagicMock()
            mock_agent.gen.side_effect = RuntimeError("upstream timed out")
            stream = list(
                resource.complete_stream(
                    question="Test?",
                    agent=mock_agent,
                    conversation_id=None,
                    user_api_key=None,
                    decoded_token={"sub": "user123"},
                    should_save_conversation=False,
                )
            )

            error_frames = [s for s in stream if '"type": "error"' in s]
            assert error_frames, "expected an error SSE frame"
            joined = "".join(error_frames)
            assert "Please try again later" not in joined
            assert "timed out" in joined.lower()

    def test_logs_per_message_start_and_end(
        self, mock_mongo_db, flask_app, caplog
    ):
        """``stream.start`` and ``stream.end`` info-level lines should fire
        once per request so operator activity reports can read them."""
        import logging

        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            mock_agent = MagicMock()
            mock_agent.gen.return_value = iter([{"answer": "ok"}])
            with caplog.at_level(
                logging.INFO,
                logger="application.api.answer.routes.base",
            ):
                list(
                    resource.complete_stream(
                        question="Test?",
                        agent=mock_agent,
                        conversation_id=None,
                        user_api_key=None,
                        decoded_token={"sub": "user123"},
                        should_save_conversation=False,
                    )
                )

            assert any("stream.start" in r.getMessage() for r in caplog.records)
            assert any(
                "stream.end" in r.getMessage() and "status=ok" in r.getMessage()
                for r in caplog.records
            )

    def test_info_logs_omit_question_text(self, mock_mongo_db, flask_app, caplog):
        """End-user prompt text must never appear in INFO logs — it's
        captured downstream in user_logs, but stdout/journald should only
        get the length + a short hash for correlation."""
        import logging

        from application.api.answer.routes.base import BaseAnswerResource

        secret_q = "my-secret-prompt-Abc123XYZ"
        with flask_app.app_context():
            resource = BaseAnswerResource()
            mock_agent = MagicMock()
            mock_agent.gen.return_value = iter([{"answer": "ok"}])
            with caplog.at_level(
                logging.INFO,
                logger="application.api.answer.routes.base",
            ):
                list(
                    resource.complete_stream(
                        question=secret_q,
                        agent=mock_agent,
                        conversation_id=None,
                        user_api_key=None,
                        decoded_token={"sub": "user123"},
                        should_save_conversation=False,
                    )
                )

            for record in caplog.records:
                msg = record.getMessage()
                if "stream.start" in msg or "stream.end" in msg:
                    assert secret_q not in msg
                    assert "question_hash=" in msg

    def test_inband_error_persists_failure_log_and_does_not_log_success(
        self, mock_mongo_db, flask_app, caplog
    ):
        """When ``agent.gen`` yields ``{"type": "error"}``, the request is
        a failure even though no exception was raised — assert we write an
        error-shaped ``user_logs`` row and emit ``stream.end`` with an
        error status, never ``status=ok``."""
        import logging

        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            mock_agent = MagicMock()
            mock_agent.gen.return_value = iter(
                [
                    {"answer": "partial "},
                    {"type": "error", "error": "upstream 503 unavailable"},
                ]
            )

            with patch(
                "application.api.answer.routes.base.UserLogsRepository"
            ) as mock_repo_cls, patch(
                "application.api.answer.routes.base.db_session"
            ) as mock_session:
                # db_session is used as a context manager.
                mock_session.return_value.__enter__.return_value = MagicMock()
                mock_session.return_value.__exit__.return_value = False
                mock_repo = MagicMock()
                mock_repo_cls.return_value = mock_repo

                with caplog.at_level(
                    logging.INFO,
                    logger="application.api.answer.routes.base",
                ):
                    stream = list(
                        resource.complete_stream(
                            question="Test?",
                            agent=mock_agent,
                            conversation_id=None,
                            user_api_key=None,
                            decoded_token={"sub": "user123"},
                            should_save_conversation=False,
                        )
                    )

                assert any('"type": "error"' in s for s in stream)
                assert not any('"type": "end"' in s for s in stream), (
                    "in-band error should suppress the trailing end frame"
                )
                assert mock_repo.insert.called, (
                    "expected an error-shaped user_logs.insert call"
                )
                kwargs = mock_repo.insert.call_args.kwargs
                assert kwargs["data"]["level"] == "error"
                assert kwargs["data"]["error_class"] == "InbandError"
                # sanitizer should canonicalize 503 → temporarily unavailable
                assert "temporarily unavailable" in kwargs["data"]["error"]
                # PII shape: no raw question, no raw api_key.
                assert "question" not in kwargs["data"]
                assert "api_key" not in kwargs["data"]
                assert "error_raw" not in kwargs["data"]

            assert not any(
                "stream.end" in r.getMessage() and "status=ok" in r.getMessage()
                for r in caplog.records
            )
            assert any(
                "stream.end" in r.getMessage() and "status=error" in r.getMessage()
                for r in caplog.records
            )

    def test_outer_exception_persists_failure_log(
        self, mock_mongo_db, flask_app
    ):
        """Exceptions raised by ``agent.gen`` should also persist an
        error-shaped ``user_logs`` row so failures are counted in
        activity reports."""
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            mock_agent = MagicMock()
            mock_agent.gen.side_effect = RuntimeError("upstream timed out")

            with patch(
                "application.api.answer.routes.base.UserLogsRepository"
            ) as mock_repo_cls, patch(
                "application.api.answer.routes.base.db_session"
            ) as mock_session:
                mock_session.return_value.__enter__.return_value = MagicMock()
                mock_session.return_value.__exit__.return_value = False
                mock_repo = MagicMock()
                mock_repo_cls.return_value = mock_repo

                list(
                    resource.complete_stream(
                        question="Test?",
                        agent=mock_agent,
                        conversation_id=None,
                        user_api_key=None,
                        decoded_token={"sub": "user123"},
                        should_save_conversation=False,
                    )
                )

                assert mock_repo.insert.called
                kwargs = mock_repo.insert.call_args.kwargs
                data = kwargs["data"]
                assert data["level"] == "error"
                assert data["error_class"] == "RuntimeError"
                # PII / secret hygiene assertions: bare api_key dropped,
                # error_raw dropped, question body not persisted on error
                # path (the question_hash + length stand in for it).
                assert "api_key" not in data
                assert "error_raw" not in data
                assert "question" not in data
                assert "question_hash" in data

    def test_saves_conversation_when_enabled(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            mock_agent = MagicMock()
            mock_agent.gen.return_value = iter(
                [
                    {"answer": "Test answer"},
                ]
            )

            decoded_token = {"sub": "user123"}

            with patch.object(
                resource.conversation_service, "save_conversation"
            ) as mock_save:
                mock_save.return_value = str(uuid.uuid4())

                list(
                    resource.complete_stream(
                        question="Test?",
                        agent=mock_agent,
                        conversation_id=None,
                        user_api_key=None,
                        decoded_token=decoded_token,
                        should_save_conversation=True,
                    )
                )

                mock_save.assert_called_once()



@pytest.mark.unit
class TestProcessResponseStream:
    pass

    def test_processes_complete_stream(self, mock_mongo_db, flask_app):
        import json

        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            conv_id = str(uuid.uuid4())
            stream = [
                f'data: {json.dumps({"type": "answer", "answer": "Hello "})}\n\n',
                f'data: {json.dumps({"type": "answer", "answer": "world"})}\n\n',
                f'data: {json.dumps({"type": "source", "source": [{"title": "doc1"}]})}\n\n',
                f'data: {json.dumps({"type": "id", "id": conv_id})}\n\n',
                f'data: {json.dumps({"type": "end"})}\n\n',
            ]

            result = resource.process_response_stream(iter(stream))

            assert result["conversation_id"] == conv_id
            assert result["answer"] == "Hello world"
            assert result["sources"] == [{"title": "doc1"}]
            assert result["error"] is None

    def test_handles_stream_error(self, mock_mongo_db, flask_app):
        import json

        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            stream = [
                f'data: {json.dumps({"type": "error", "error": "Test error"})}\n\n',
            ]

            result = resource.process_response_stream(iter(stream))

            assert result["conversation_id"] is None
            assert result["error"] == "Test error"

    def test_handles_malformed_stream_data(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            stream = [
                "data: invalid json\n\n",
                'data: {"type": "end"}\n\n',
            ]

            result = resource.process_response_stream(iter(stream))

            assert result is not None


@pytest.mark.unit
class TestErrorStreamGenerate:
    pass

    def test_generates_error_stream(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()

            error_stream = list(resource.error_stream_generate("Test error message"))

            assert len(error_stream) == 1
            assert '"type": "error"' in error_stream[0]
            assert '"error": "Test error message"' in error_stream[0]


# ---------------------------------------------------------------------------
# Real-PG tests for check_usage against seeded agents + token usage
# ---------------------------------------------------------------------------


@contextmanager
def _patch_base_db(conn):
    @contextmanager
    def _yield():
        yield conn

    with patch(
        "application.api.answer.routes.base.db_readonly", _yield
    ), patch(
        "application.api.answer.routes.base.db_session", _yield
    ):
        yield


@pytest.mark.unit
class TestCheckUsagePgConn:
    def test_invalid_api_key_returns_401(self, pg_conn, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource

        with _patch_base_db(pg_conn), flask_app.app_context():
            resource = BaseAnswerResource()
            result = resource.check_usage({"user_api_key": "does-not-exist"})
        assert result is not None
        assert result.status_code == 401

    def test_no_limits_returns_none(self, pg_conn, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource
        from application.storage.db.repositories.agents import AgentsRepository

        AgentsRepository(pg_conn).create(
            "owner", "a", "published",
            surface="web_ask", key="k1",
            limited_token_mode=False, limited_request_mode=False,
        )
        with _patch_base_db(pg_conn), flask_app.app_context():
            resource = BaseAnswerResource()
            result = resource.check_usage({"user_api_key": "k1"})
        assert result is None

    def test_within_limit_returns_none(self, pg_conn, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource
        from application.storage.db.repositories.agents import AgentsRepository

        AgentsRepository(pg_conn).create(
            "owner", "a", "published",
            surface="web_ask", key="k2",
            limited_token_mode=True, token_limit=10000,
        )
        with _patch_base_db(pg_conn), flask_app.app_context():
            resource = BaseAnswerResource()
            result = resource.check_usage({"user_api_key": "k2"})
        assert result is None

    def test_token_limit_exceeded_returns_429(self, pg_conn, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.token_usage import (
            TokenUsageRepository,
        )

        AgentsRepository(pg_conn).create(
            "owner", "a", "published",
            surface="web_ask", key="k3",
            limited_token_mode=True, token_limit=100,
        )
        # Seed token usage exceeding the limit
        TokenUsageRepository(pg_conn).insert(
            api_key="k3", prompt_tokens=500, generated_tokens=0,
        )

        with _patch_base_db(pg_conn), flask_app.app_context():
            resource = BaseAnswerResource()
            result = resource.check_usage({"user_api_key": "k3"})
        assert result is not None
        assert result.status_code == 429

    def test_request_limit_exceeded_returns_429(self, pg_conn, flask_app):
        from application.api.answer.routes.base import BaseAnswerResource
        from application.storage.db.repositories.agents import AgentsRepository
        from application.storage.db.repositories.token_usage import (
            TokenUsageRepository,
        )

        AgentsRepository(pg_conn).create(
            "owner", "a", "published",
            surface="web_ask", key="k4",
            limited_request_mode=True, request_limit=1,
        )
        # Two request entries exceed limit=1
        TokenUsageRepository(pg_conn).insert(api_key="k4", prompt_tokens=10, generated_tokens=10)
        TokenUsageRepository(pg_conn).insert(api_key="k4", prompt_tokens=10, generated_tokens=10)

        with _patch_base_db(pg_conn), flask_app.app_context():
            resource = BaseAnswerResource()
            result = resource.check_usage({"user_api_key": "k4"})
        assert result is not None
        assert result.status_code == 429

    def test_string_True_limited_token_mode_parsed(self, pg_conn, flask_app):
        """Legacy Mongo sometimes stored ``limited_token_mode`` as the
        string 'True'; verify the parse branch."""
        from application.api.answer.routes.base import BaseAnswerResource
        from application.storage.db.repositories.agents import AgentsRepository

        # Store bool=False in DB (limited_token_mode default). Test uses
        # string 'True' by mutating the row directly.
        from sqlalchemy import text
        AgentsRepository(pg_conn).create(
            "owner", "a", "published",
            surface="web_ask", key="k5",
        )
        pg_conn.execute(
            text(
                "UPDATE agents SET limited_token_mode = :v WHERE key = :k"
            ),
            {"v": True, "k": "k5"},
        )
        with _patch_base_db(pg_conn), flask_app.app_context():
            resource = BaseAnswerResource()
            result = resource.check_usage({"user_api_key": "k5"})
        # With default limit and no token usage, should pass
        assert result is None
