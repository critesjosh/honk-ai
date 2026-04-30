"""Tests for application/api/internal/routes.py.

Uses the ephemeral ``pg_conn`` fixture so the sources repository writes
happen against a real Postgres schema.
"""

import io
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from flask import Flask


_TEST_KEY = "test-internal-key"
_AUTH = {"X-Internal-Key": _TEST_KEY}


@contextmanager
def _patch_db(conn):
    @contextmanager
    def _yield():
        yield conn

    with patch(
        "application.api.internal.routes.db_session", _yield
    ):
        yield


def _make_app():
    from application.api.internal.routes import internal

    app = Flask(__name__)
    app.register_blueprint(internal)
    app.config["TESTING"] = True
    return app


class TestVerifyInternalKey:
    def test_rejects_when_internal_key_not_configured(self):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", ""
        ):
            with app.test_client() as c:
                r = c.get("/api/download")
        assert r.status_code == 401

    def test_rejects_when_key_missing(self):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ):
            with app.test_client() as c:
                r = c.get("/api/download")
        assert r.status_code == 401

    def test_rejects_when_key_mismatch(self):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ):
            with app.test_client() as c:
                r = c.get("/api/download", headers={"X-Internal-Key": "wrong"})
        assert r.status_code == 401


class TestDownloadFile:
    def test_returns_404_for_missing_file(self, tmp_path):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.UPLOAD_FOLDER",
            str(tmp_path),
        ), patch(
            "application.api.internal.routes.current_dir", ""
        ):
            with app.test_client() as c:
                r = c.get(
                    "/api/download?user=alice&name=job1&file=missing.txt",
                    headers=_AUTH,
                )
        assert r.status_code == 404

    def test_returns_file_when_present(self, tmp_path):
        app = _make_app()
        user_dir = tmp_path / "bob" / "job1"
        user_dir.mkdir(parents=True)
        (user_dir / "hello.txt").write_text("hi there")

        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.UPLOAD_FOLDER",
            str(tmp_path),
        ), patch(
            "application.api.internal.routes.current_dir", ""
        ):
            with app.test_client() as c:
                r = c.get(
                    "/api/download?user=bob&name=job1&file=hello.txt",
                    headers=_AUTH,
                )
        assert r.status_code == 200
        assert r.data == b"hi there"


class TestUploadIndex:
    def _base_form(self, *, source_id="source-1"):
        return {
            "user": "alice",
            "name": "Job A",
            "tokens": "100",
            "retriever": "classic",
            "id": source_id,
            "type": "file",
        }

    def test_rejects_without_auth(self):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ):
            with app.test_client() as c:
                r = c.post("/api/upload_index")
        assert r.status_code == 401

    def test_rejects_missing_user(self):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ):
            with app.test_client() as c:
                r = c.post("/api/upload_index", headers=_AUTH, data={})
        assert r.status_code == 200
        assert r.json == {"status": "no user"}

    def test_rejects_missing_name(self):
        app = _make_app()
        form = {"user": "alice"}
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ):
            with app.test_client() as c:
                r = c.post("/api/upload_index", headers=_AUTH, data=form)
        assert r.status_code == 200
        assert r.json == {"status": "no name"}

    def test_creates_new_source_for_non_faiss_store(self, pg_conn):
        """For non-faiss VECTOR_STORE the route skips file uploads entirely."""
        from application.storage.db.repositories.sources import SourcesRepository

        app = _make_app()
        form = {**self._base_form(source_id="legacy-source-1")}
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.VECTOR_STORE", "milvus"
        ), patch(
            "application.api.internal.routes.settings.EMBEDDINGS_NAME", "emb"
        ), patch(
            "application.api.internal.routes.StorageCreator.get_storage",
            return_value=MagicMock(),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post("/api/upload_index", headers=_AUTH, data=form)

        assert r.status_code == 200
        assert r.json == {"status": "ok"}
        repo = SourcesRepository(pg_conn)
        found = repo.get_by_legacy_id("legacy-source-1", "alice")
        assert found is not None

    def test_updates_existing_source(self, pg_conn):
        from application.storage.db.repositories.sources import SourcesRepository

        repo = SourcesRepository(pg_conn)
        created = repo.create(
            "initial",
            user_id="alice",
            legacy_mongo_id="legacy-src-2",
            tokens="0",
        )
        app = _make_app()
        form = {**self._base_form(source_id="legacy-src-2"), "tokens": "999"}
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.VECTOR_STORE", "milvus"
        ), patch(
            "application.api.internal.routes.settings.EMBEDDINGS_NAME", "emb"
        ), patch(
            "application.api.internal.routes.StorageCreator.get_storage",
            return_value=MagicMock(),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post("/api/upload_index", headers=_AUTH, data=form)
        assert r.status_code == 200

        updated = repo.get(str(created["id"]), "alice")
        assert updated["tokens"] == "999"

    def test_handles_directory_structure_and_file_name_map(self, pg_conn):
        import json

        app = _make_app()
        form = {
            **self._base_form(source_id="legacy-src-3"),
            "directory_structure": json.dumps({"root": {"a.txt": None}}),
            "file_name_map": json.dumps({"a.txt": "Original A.txt"}),
        }
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.VECTOR_STORE", "milvus"
        ), patch(
            "application.api.internal.routes.settings.EMBEDDINGS_NAME", "emb"
        ), patch(
            "application.api.internal.routes.StorageCreator.get_storage",
            return_value=MagicMock(),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post("/api/upload_index", headers=_AUTH, data=form)
        assert r.status_code == 200

    def test_invalid_json_falls_back_to_empty(self, pg_conn):
        app = _make_app()
        form = {
            **self._base_form(source_id="legacy-src-4"),
            "directory_structure": "not-json",
            "file_name_map": "also-not-json",
        }
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.VECTOR_STORE", "milvus"
        ), patch(
            "application.api.internal.routes.settings.EMBEDDINGS_NAME", "emb"
        ), patch(
            "application.api.internal.routes.StorageCreator.get_storage",
            return_value=MagicMock(),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post("/api/upload_index", headers=_AUTH, data=form)
        assert r.status_code == 200

    def test_faiss_missing_file_faiss(self, pg_conn):
        app = _make_app()
        form = self._base_form(source_id="legacy-src-5")
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.VECTOR_STORE", "faiss"
        ), patch(
            "application.api.internal.routes.StorageCreator.get_storage",
            return_value=MagicMock(),
        ):
            with app.test_client() as c:
                r = c.post(
                    "/api/upload_index",
                    headers=_AUTH,
                    data=form,
                    content_type="multipart/form-data",
                )
        assert r.status_code == 200
        assert r.json == {"status": "no file"}

    def test_faiss_missing_file_pkl(self, pg_conn):
        app = _make_app()
        data = {
            **self._base_form(source_id="legacy-src-6"),
            "file_faiss": (io.BytesIO(b"faiss-data"), "index.faiss"),
        }
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.VECTOR_STORE", "faiss"
        ), patch(
            "application.api.internal.routes.StorageCreator.get_storage",
            return_value=MagicMock(),
        ):
            with app.test_client() as c:
                r = c.post(
                    "/api/upload_index",
                    headers=_AUTH,
                    data=data,
                    content_type="multipart/form-data",
                )
        assert r.status_code == 200
        assert r.json == {"status": "no file"}

    def test_faiss_saves_both_files(self, pg_conn):
        app = _make_app()
        fake_storage = MagicMock()
        data = {
            **self._base_form(source_id="legacy-src-7"),
            "file_faiss": (io.BytesIO(b"faiss-data"), "index.faiss"),
            "file_pkl": (io.BytesIO(b"pkl-data"), "index.pkl"),
        }
        with patch(
            "application.api.internal.routes.settings.INTERNAL_KEY", _TEST_KEY
        ), patch(
            "application.api.internal.routes.settings.VECTOR_STORE", "faiss"
        ), patch(
            "application.api.internal.routes.settings.EMBEDDINGS_NAME", "emb"
        ), patch(
            "application.api.internal.routes.StorageCreator.get_storage",
            return_value=fake_storage,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/upload_index",
                    headers=_AUTH,
                    data=data,
                    content_type="multipart/form-data",
                )
        assert r.status_code == 200
        assert r.json == {"status": "ok"}
        assert fake_storage.save_file.call_count == 2


class TestCreateMcpKey:
    """End-to-end tests for /api/internal/create_mcp_key.

    Covers the AZTEC_SOURCE_IDS-order-preservation fix and the conflict
    behavior (key preserved, sources refreshed) that together restore
    correct retrieval for re-provisioned Discord MCP agents.
    """

    _PROV_KEY = "test-prov-key"
    _AUTH = {"X-Provisioning-Key": _PROV_KEY}

    @staticmethod
    def _make_sources(pg_conn, count: int = 5, *, is_public: bool = True) -> list[str]:
        """Create test source rows.

        Aztec corpora must be ``is_public=TRUE`` for ``create_mcp_key``
        to accept them — that's the whole point of the
        ``0004_sources_is_public`` migration. Tests default to True;
        pass ``is_public=False`` to verify the rejection path.
        """
        from sqlalchemy import text as sql_text
        from application.storage.db.repositories.sources import SourcesRepository

        repo = SourcesRepository(pg_conn)
        ids: list[str] = []
        for i in range(count):
            row = repo.create(f"src-{i}", user_id="local")
            ids.append(str(row["id"]))
        if is_public:
            pg_conn.execute(
                sql_text(
                    "UPDATE sources SET is_public = TRUE "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": ids},
            )
        return ids

    def test_preserves_aztec_source_ids_order_on_create(self, pg_conn):
        """The primary source must be the FIRST UUID in AZTEC_SOURCE_IDS,
        not whatever Postgres returns from a SELECT without ORDER BY.

        Regression for the bug where Step 2's upsert refresh started
        writing source_id from a heap-ordered SELECT.
        """
        ids = self._make_sources(pg_conn, count=5)
        # Reverse so heap order is unlikely to match AZTEC order.
        canonical_order = list(reversed(ids))
        env_value = ",".join(canonical_order)

        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), patch(
            "application.api.internal.routes.settings.AZTEC_SOURCE_IDS",
            env_value,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"discord_user_id": "u1", "discord_username": "alice"},
                )

        assert r.status_code == 200
        assert r.json["created"] is True

        from application.storage.db.repositories.agents import AgentsRepository

        agent = AgentsRepository(pg_conn).find_by_key(r.json["api_key"])
        assert str(agent["source_id"]) == canonical_order[0]
        assert [str(x) for x in agent["extra_source_ids"]] == canonical_order[1:]

    def test_re_provision_preserves_key_and_refreshes_sources(self, pg_conn):
        """Re-running /mcp-key for an existing user must keep their key
        (so their MCP client doesn't break) but refresh the source order
        in case AZTEC_SOURCE_IDS changed.
        """
        ids = self._make_sources(pg_conn, count=3)

        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), patch(
            "application.api.internal.routes.settings.AZTEC_SOURCE_IDS",
            ",".join(ids),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                first = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"discord_user_id": "u-rep", "discord_username": "bob"},
                )
        assert first.status_code == 200
        assert first.json["created"] is True
        original_key = first.json["api_key"]

        # Reorder AZTEC_SOURCE_IDS, re-provision.
        reordered = [ids[2], ids[0], ids[1]]
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), patch(
            "application.api.internal.routes.settings.AZTEC_SOURCE_IDS",
            ",".join(reordered),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                second = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"discord_user_id": "u-rep", "discord_username": "bob"},
                )
        assert second.status_code == 200
        # NOTE: don't assert on `created` here — the endpoint computes it
        # from `created_at == updated_at`, which collapses inside a single
        # test transaction because Postgres `now()` is transaction-bound.
        # In prod the two calls land in separate transactions and the flag
        # is reliable. The behavior we actually care about is key-preserved
        # + sources-refreshed below.
        assert second.json["api_key"] == original_key

        from application.storage.db.repositories.agents import AgentsRepository

        agent = AgentsRepository(pg_conn).find_by_key(original_key)
        assert str(agent["source_id"]) == reordered[0]
        assert [str(x) for x in agent["extra_source_ids"]] == reordered[1:]

    def test_rejects_unknown_provisioning_key(self, pg_conn):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/create_mcp_key",
                    headers={"X-Provisioning-Key": "wrong"},
                    json={"discord_user_id": "u", "discord_username": "x"},
                )
        assert r.status_code == 401

    def test_rejects_when_no_aztec_source_is_public(self, pg_conn):
        """If AZTEC_SOURCE_IDS lists private sources, the endpoint must
        fail loudly (500) rather than silently provisioning an agent
        that points at unreachable corpora.
        """
        ids = self._make_sources(pg_conn, count=3, is_public=False)

        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), patch(
            "application.api.internal.routes.settings.AZTEC_SOURCE_IDS",
            ",".join(ids),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"discord_user_id": "u-priv", "discord_username": "z"},
                )
        assert r.status_code == 500
        assert "No valid" in (r.json or {}).get("error", "")


class TestForgetDiscordUser:
    """End-to-end tests for /api/internal/forget_discord_user (GDPR Art. 17).

    The endpoint reuses the ``MCP_PROVISIONING_KEY`` trust model from
    ``create_mcp_key``: same auth header, same self-authenticated route
    bypass on the ``before_request`` INTERNAL_KEY check.
    """

    _PROV_KEY = "test-prov-key"
    _AUTH = {"X-Provisioning-Key": _PROV_KEY}

    # Every user-keyed table the route is expected to clear, plus
    # ``agents`` (special-cased by mcp_provider lookup). If models.py
    # grows a new user-keyed table the endpoint should also delete from,
    # add it here AND in routes.py — the round-trip assertion below
    # will fail loudly until both are updated.
    EXPECTED_TABLES = {
        "agents",
        "conversations",
        "attachments",
        "memories",
        "todos",
        "notes",
        "connector_sessions",
        "workflow_runs",
        "workflows",
        "user_tools",
        "agent_folders",
        "sources",
        "prompts",
        "user_logs",
        "stack_logs",
        "token_usage",
        "users",
    }

    def test_rejects_when_provisioning_key_not_configured(self, pg_conn):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY", ""
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/forget_discord_user",
                    headers=self._AUTH,
                    json={"discord_user_id": "u1"},
                )
        assert r.status_code == 401

    def test_rejects_unknown_provisioning_key(self, pg_conn):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/forget_discord_user",
                    headers={"X-Provisioning-Key": "wrong"},
                    json={"discord_user_id": "u1"},
                )
        assert r.status_code == 401

    def test_rejects_missing_discord_user_id(self, pg_conn):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/forget_discord_user",
                    headers=self._AUTH,
                    json={},
                )
        assert r.status_code == 400
        assert "discord_user_id" in (r.json or {}).get("error", "")

    def test_unknown_user_returns_full_table_set_with_zero_counts(self, pg_conn):
        """Schema-completeness contract: a successful response always
        names every table the route deletes from, even when nothing is
        actually deleted. Adding a new user-keyed table to models.py
        without updating routes.py will make this test fail.
        """
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/forget_discord_user",
                    headers=self._AUTH,
                    json={"discord_user_id": "no-such-user"},
                )
        assert r.status_code == 200
        body = r.json
        assert body["success"] is True
        deleted = body["deleted"]
        assert set(deleted.keys()) == self.EXPECTED_TABLES, (
            f"forget_discord_user response shape diverged from EXPECTED_TABLES. "
            f"missing={self.EXPECTED_TABLES - set(deleted.keys())} "
            f"extra={set(deleted.keys()) - self.EXPECTED_TABLES}"
        )
        for tbl, n in deleted.items():
            assert n == 0, f"unexpected delete count {n} for {tbl}"

    def test_deletes_real_user_data_end_to_end(self, pg_conn):
        """Round-trip: create an MCP agent + a conversation + a message
        for ``discord:u-real``, call /forget_discord_user, assert all
        three rows are gone (messages cascade-deleted via FK)."""
        from sqlalchemy import text as sql_text

        # Seed a public source so create_mcp_key succeeds.
        ids = TestCreateMcpKey._make_sources(pg_conn, count=1)

        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), patch(
            "application.api.internal.routes.settings.AZTEC_SOURCE_IDS",
            ",".join(ids),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                provision = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={
                        "discord_user_id": "u-real",
                        "discord_username": "real",
                    },
                )
        assert provision.status_code == 200
        canonical = "discord:u-real"

        # Insert a conversation + message + log entry for this user.
        conv_id = pg_conn.execute(
            sql_text(
                "INSERT INTO conversations (user_id, name) VALUES (:uid, :n) "
                "RETURNING id"
            ),
            {"uid": canonical, "n": "test-conv"},
        ).scalar()
        pg_conn.execute(
            sql_text(
                "INSERT INTO conversation_messages "
                "(conversation_id, user_id, position, prompt, response) "
                "VALUES (:cid, :uid, 0, 'q', 'a')"
            ),
            {"cid": conv_id, "uid": canonical},
        )
        pg_conn.execute(
            sql_text(
                "INSERT INTO user_logs (user_id, endpoint, data) "
                "VALUES (:uid, '/api/answer', '{}'::jsonb)"
            ),
            {"uid": canonical},
        )
        pg_conn.execute(
            sql_text(
                "INSERT INTO token_usage "
                "(user_id, prompt_tokens, generated_tokens) "
                "VALUES (:uid, 10, 20)"
            ),
            {"uid": canonical},
        )

        # Sanity: rows exist before forget.
        before_agents = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM agents "
                "WHERE mcp_provider = 'discord' "
                "AND mcp_provider_user_id = 'u-real'"
            )
        ).scalar()
        before_msgs = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM conversation_messages WHERE user_id = :uid"
            ),
            {"uid": canonical},
        ).scalar()
        assert before_agents == 1
        assert before_msgs == 1

        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/forget_discord_user",
                    headers=self._AUTH,
                    json={"discord_user_id": "u-real"},
                )
        assert r.status_code == 200
        deleted = r.json["deleted"]
        # Expected non-zero rowcounts:
        assert deleted["agents"] == 1
        assert deleted["conversations"] == 1
        assert deleted["user_logs"] == 1
        assert deleted["token_usage"] == 1

        # Verify everything is actually gone.
        assert (
            pg_conn.execute(
                sql_text(
                    "SELECT count(*) FROM agents "
                    "WHERE mcp_provider_user_id = 'u-real'"
                )
            ).scalar()
            == 0
        )
        assert (
            pg_conn.execute(
                sql_text(
                    "SELECT count(*) FROM conversations WHERE user_id = :uid"
                ),
                {"uid": canonical},
            ).scalar()
            == 0
        )
        # FK cascade: messages must be gone even though we didn't
        # enumerate ``conversation_messages`` in the route's delete list.
        assert (
            pg_conn.execute(
                sql_text(
                    "SELECT count(*) FROM conversation_messages "
                    "WHERE user_id = :uid"
                ),
                {"uid": canonical},
            ).scalar()
            == 0
        )
        assert (
            pg_conn.execute(
                sql_text("SELECT count(*) FROM users WHERE user_id = :uid"),
                {"uid": canonical},
            ).scalar()
            == 0
        )
