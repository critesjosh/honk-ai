from datetime import timedelta
from unittest.mock import ANY, MagicMock, patch

import pytest


class TestIngestTask:
    @pytest.mark.unit
    @patch("application.api.user.tasks.ingest_worker")
    def test_calls_ingest_worker(self, mock_worker):
        from application.api.user.tasks import ingest

        mock_worker.return_value = {"status": "ok"}

        result = ingest("dir", ["pdf"], "job1", "user1", "/path", "file.pdf")

        mock_worker.assert_called_once_with(
            ANY, "dir", ["pdf"], "job1", "/path", "file.pdf", "user1",
            file_name_map=None,
        )
        assert result == {"status": "ok"}

    @pytest.mark.unit
    @patch("application.api.user.tasks.ingest_worker")
    def test_passes_file_name_map(self, mock_worker):
        from application.api.user.tasks import ingest

        mock_worker.return_value = {"status": "ok"}
        name_map = {"a.pdf": "b.pdf"}

        ingest("dir", ["pdf"], "job1", "user1", "/path", "file.pdf",
               file_name_map=name_map)

        mock_worker.assert_called_once_with(
            ANY, "dir", ["pdf"], "job1", "/path", "file.pdf", "user1",
            file_name_map=name_map,
        )


class TestIngestRemoteTask:
    @pytest.mark.unit
    @patch("application.api.user.tasks.remote_worker")
    def test_calls_remote_worker(self, mock_worker):
        from application.api.user.tasks import ingest_remote

        mock_worker.return_value = {"status": "ok"}

        result = ingest_remote({"url": "http://x"}, "job1", "user1", "web")

        mock_worker.assert_called_once_with(
            ANY, {"url": "http://x"}, "job1", "user1", "web"
        )
        assert result == {"status": "ok"}


class TestReingestSourceTask:
    @pytest.mark.unit
    @patch("application.worker.reingest_source_worker")
    def test_calls_reingest_worker(self, mock_worker):
        from application.api.user.tasks import reingest_source_task

        mock_worker.return_value = {"status": "ok"}

        result = reingest_source_task("source123", "user1")

        mock_worker.assert_called_once_with(ANY, "source123", "user1")
        assert result == {"status": "ok"}


class TestScheduleSyncsTask:
    @pytest.mark.unit
    @patch("application.api.user.tasks.sync_worker")
    def test_calls_sync_worker(self, mock_worker):
        from application.api.user.tasks import schedule_syncs

        mock_worker.return_value = {"status": "ok"}

        result = schedule_syncs("daily")

        mock_worker.assert_called_once_with(ANY, "daily")
        assert result == {"status": "ok"}


class TestSyncSourceTask:
    @pytest.mark.unit
    @patch("application.api.user.tasks.sync")
    def test_calls_sync(self, mock_sync):
        from application.api.user.tasks import sync_source

        mock_sync.return_value = {"status": "ok"}

        result = sync_source(
            {"data": 1}, "job1", "user1", "web", "daily", "classic", "doc1"
        )

        mock_sync.assert_called_once_with(
            ANY, {"data": 1}, "job1", "user1", "web", "daily", "classic", "doc1"
        )
        assert result == {"status": "ok"}


class TestStoreAttachmentTask:
    @pytest.mark.unit
    @patch("application.api.user.tasks.attachment_worker")
    def test_calls_attachment_worker(self, mock_worker):
        from application.api.user.tasks import store_attachment

        mock_worker.return_value = {"status": "ok"}

        result = store_attachment({"file": "info"}, "user1")

        mock_worker.assert_called_once_with(ANY, {"file": "info"}, "user1")
        assert result == {"status": "ok"}


class TestProcessAgentWebhookTask:
    @pytest.mark.unit
    @patch("application.api.user.tasks.agent_webhook_worker")
    def test_calls_agent_webhook_worker(self, mock_worker):
        from application.api.user.tasks import process_agent_webhook

        mock_worker.return_value = {"status": "ok"}

        result = process_agent_webhook("agent123", {"event": "test"})

        mock_worker.assert_called_once_with(ANY, "agent123", {"event": "test"})
        assert result == {"status": "ok"}


class TestIngestConnectorTask:
    @pytest.mark.unit
    @patch("application.worker.ingest_connector")
    def test_calls_ingest_connector_defaults(self, mock_worker):
        from application.api.user.tasks import ingest_connector_task

        mock_worker.return_value = {"status": "ok"}

        result = ingest_connector_task("job1", "user1", "gdrive")

        mock_worker.assert_called_once_with(
            ANY,
            "job1",
            "user1",
            "gdrive",
            session_token=None,
            file_ids=None,
            folder_ids=None,
            recursive=True,
            retriever="classic",
            operation_mode="upload",
            doc_id=None,
            sync_frequency="never",
        )
        assert result == {"status": "ok"}

    @pytest.mark.unit
    @patch("application.worker.ingest_connector")
    def test_calls_ingest_connector_custom(self, mock_worker):
        from application.api.user.tasks import ingest_connector_task

        mock_worker.return_value = {"status": "ok"}

        result = ingest_connector_task(
            "job1",
            "user1",
            "sharepoint",
            session_token="tok",
            file_ids=["f1"],
            folder_ids=["d1"],
            recursive=False,
            retriever="duckdb",
            operation_mode="sync",
            doc_id="doc1",
            sync_frequency="daily",
        )

        mock_worker.assert_called_once_with(
            ANY,
            "job1",
            "user1",
            "sharepoint",
            session_token="tok",
            file_ids=["f1"],
            folder_ids=["d1"],
            recursive=False,
            retriever="duckdb",
            operation_mode="sync",
            doc_id="doc1",
            sync_frequency="daily",
        )
        assert result == {"status": "ok"}


class TestSetupPeriodicTasks:
    @pytest.mark.unit
    def test_registers_periodic_tasks(self):
        from application.api.user.tasks import setup_periodic_tasks

        sender = MagicMock()

        setup_periodic_tasks(sender)

        assert sender.add_periodic_task.call_count == 5

        calls = sender.add_periodic_task.call_args_list

        # daily sync
        assert calls[0][0][0] == timedelta(days=1)
        # weekly sync
        assert calls[1][0][0] == timedelta(weeks=1)
        # monthly sync
        assert calls[2][0][0] == timedelta(days=30)
        # pending_tool_state TTL cleanup (60s)
        assert calls[3][0][0] == timedelta(seconds=60)
        # 1-year retention purge (privacy compliance)
        assert calls[4][0][0] == timedelta(days=1)
        assert calls[4][1].get("name") == "purge-old-user-data"


class TestPurgeOldUserData:
    """Coverage for the daily 1-year retention purge.

    Aligns with the Aztec Foundation privacy policy's 12-month window.
    The interesting behaviours to lock down here are: (a) the SQL
    queries actually run end-to-end against the real schema and return
    a per-table rowcount dict, (b) old messages are dropped while
    recent ones survive even when the parent conversation was touched
    yesterday (the codex-review fix), and (c) the safety guard returns
    a skip dict when ``POSTGRES_URI`` is unset (so the task never
    half-runs in a misconfigured worker).
    """

    @pytest.mark.unit
    def test_skip_path_when_postgres_uri_unset(self, monkeypatch):
        from application.api.user.tasks import purge_old_user_data
        from application.core import settings as settings_module

        monkeypatch.setattr(
            settings_module.settings, "POSTGRES_URI", None, raising=False
        )
        result = purge_old_user_data.run(retention_days=365)
        assert result == {"skipped": "POSTGRES_URI not set"}

    @pytest.mark.integration
    def test_returns_full_table_set_with_zero_counts_on_empty_db(
        self, pg_engine, monkeypatch
    ):
        """Empty DB ⇒ all five purge targets reachable, all zero."""
        from application.api.user.tasks import purge_old_user_data
        from application.storage.db import engine as engine_module

        monkeypatch.setattr(engine_module, "get_engine", lambda: pg_engine)

        result = purge_old_user_data.run(retention_days=99999)
        assert result["retention_days"] == 99999
        assert set(result["purged"].keys()) == {
            "conversation_messages",
            "conversations",
            "user_logs",
            "stack_logs",
            "token_usage",
        }
        for table, n in result["purged"].items():
            assert n == 0, f"{table} reported non-zero on empty DB"

    @pytest.mark.integration
    def test_purges_old_messages_keeps_recent_ones(
        self, pg_engine, monkeypatch
    ):
        """Codex-fix regression guard: the cursor is the message's own
        ``timestamp``, not the parent conversation's ``updated_at``. A
        thread with one new reply must NOT preserve its old messages.
        """
        from sqlalchemy import text
        from application.api.user.tasks import purge_old_user_data
        from application.storage.db import engine as engine_module

        monkeypatch.setattr(engine_module, "get_engine", lambda: pg_engine)

        # Insert: one conversation whose updated_at is "yesterday" but
        # which contains both an OLD (>1y) and a RECENT (<1d) message.
        with pg_engine.begin() as conn:
            conv_id = conn.execute(
                text(
                    "INSERT INTO conversations (user_id, name, updated_at) "
                    "VALUES ('test-user', 'mixed-age', now() - interval '1 day') "
                    "RETURNING id"
                )
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO conversation_messages "
                    "(conversation_id, user_id, position, prompt, response, timestamp) "
                    "VALUES (:cid, 'test-user', 0, 'old', 'old', "
                    "        now() - interval '400 days')"
                ),
                {"cid": conv_id},
            )
            conn.execute(
                text(
                    "INSERT INTO conversation_messages "
                    "(conversation_id, user_id, position, prompt, response, timestamp) "
                    "VALUES (:cid, 'test-user', 1, 'new', 'new', now())"
                ),
                {"cid": conv_id},
            )

        result = purge_old_user_data.run(retention_days=365)

        with pg_engine.begin() as conn:
            remaining = conn.execute(
                text(
                    "SELECT prompt FROM conversation_messages "
                    "WHERE conversation_id = :cid ORDER BY position"
                ),
                {"cid": conv_id},
            ).fetchall()

        assert result["purged"]["conversation_messages"] == 1
        assert [r[0] for r in remaining] == ["new"], (
            "old message should be purged, recent message should survive — "
            "if both are gone the cursor is wrongly keyed on conversation.updated_at"
        )

    @pytest.mark.integration
    def test_orphaned_conversation_cleanup(self, pg_engine, monkeypatch):
        """Once all messages of an old thread are purged, the parent
        conversation row should be swept up too — but only if it has
        no remaining messages.
        """
        from sqlalchemy import text
        from application.api.user.tasks import purge_old_user_data
        from application.storage.db import engine as engine_module

        monkeypatch.setattr(engine_module, "get_engine", lambda: pg_engine)

        with pg_engine.begin() as conn:
            # Conversation A: only old messages — should disappear.
            cid_a = conn.execute(
                text(
                    "INSERT INTO conversations (user_id, name, updated_at) "
                    "VALUES ('test-user', 'all-old', now() - interval '500 days') "
                    "RETURNING id"
                )
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO conversation_messages "
                    "(conversation_id, user_id, position, prompt, response, timestamp) "
                    "VALUES (:cid, 'test-user', 0, 'old', 'old', "
                    "        now() - interval '500 days')"
                ),
                {"cid": cid_a},
            )
            # Conversation B: has a recent message — must survive even
            # though updated_at is stale.
            cid_b = conn.execute(
                text(
                    "INSERT INTO conversations (user_id, name, updated_at) "
                    "VALUES ('test-user', 'has-recent', now() - interval '500 days') "
                    "RETURNING id"
                )
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO conversation_messages "
                    "(conversation_id, user_id, position, prompt, response, timestamp) "
                    "VALUES (:cid, 'test-user', 0, 'fresh', 'fresh', now())"
                ),
                {"cid": cid_b},
            )

        purge_old_user_data.run(retention_days=365)

        with pg_engine.begin() as conn:
            survivors = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT id FROM conversations WHERE id IN (:a, :b)"
                    ),
                    {"a": cid_a, "b": cid_b},
                ).fetchall()
            }

        assert cid_a not in survivors, "empty old conversation should be purged"
        assert cid_b in survivors, (
            "conversation with a recent message must survive — "
            "the empty-only filter (NOT EXISTS) regressed"
        )


class TestMcpOauthTask:
    @pytest.mark.unit
    @patch("application.api.user.tasks.mcp_oauth")
    def test_calls_mcp_oauth(self, mock_worker):
        from application.api.user.tasks import mcp_oauth_task

        mock_worker.return_value = {"url": "http://auth"}

        result = mcp_oauth_task({"server": "mcp"}, "user1")

        mock_worker.assert_called_once_with(ANY, {"server": "mcp"}, "user1")
        assert result == {"url": "http://auth"}


class TestMcpOauthStatusTask:
    @pytest.mark.unit
    @patch("application.api.user.tasks.mcp_oauth_status")
    def test_calls_mcp_oauth_status(self, mock_worker):
        from application.api.user.tasks import mcp_oauth_status_task

        mock_worker.return_value = {"status": "authorized"}

        result = mcp_oauth_status_task("task123")

        mock_worker.assert_called_once_with(ANY, "task123")
        assert result == {"status": "authorized"}
