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
        for the pseudo of ``u-real``, call /forget_discord_user with
        the *raw* ID, assert all three rows are gone (messages
        cascade-deleted via FK)."""
        from sqlalchemy import text as sql_text

        from application.pseudonyms import (
            canonical_user_id,
            pseudonymize_provider_user_id,
        )

        # The conftest sets USER_ID_PEPPER='0'*64 for the test process;
        # compute the pseudonyms the same way the route would.
        test_pepper = "0" * 64
        pseudo_uid = canonical_user_id(
            "discord", "u-real", pepper=test_pepper
        )
        bare_pseudo = pseudonymize_provider_user_id(
            "u-real", pepper=test_pepper
        )

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

        # Insert a conversation + message + log entry under the pseudo
        # (matching what the streaming path would have written).
        conv_id = pg_conn.execute(
            sql_text(
                "INSERT INTO conversations (user_id, name) VALUES (:uid, :n) "
                "RETURNING id"
            ),
            {"uid": pseudo_uid, "n": "test-conv"},
        ).scalar()
        pg_conn.execute(
            sql_text(
                "INSERT INTO conversation_messages "
                "(conversation_id, user_id, position, prompt, response) "
                "VALUES (:cid, :uid, 0, 'q', 'a')"
            ),
            {"cid": conv_id, "uid": pseudo_uid},
        )
        pg_conn.execute(
            sql_text(
                "INSERT INTO user_logs (user_id, endpoint, data) "
                "VALUES (:uid, '/api/answer', '{}'::jsonb)"
            ),
            {"uid": pseudo_uid},
        )
        pg_conn.execute(
            sql_text(
                "INSERT INTO token_usage "
                "(user_id, prompt_tokens, generated_tokens) "
                "VALUES (:uid, 10, 20)"
            ),
            {"uid": pseudo_uid},
        )

        # Sanity: rows exist under the pseudo, NOT under the raw ID.
        before_agents = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM agents "
                "WHERE mcp_provider = 'discord' "
                "AND mcp_provider_user_id = :pseudo"
            ),
            {"pseudo": bare_pseudo},
        ).scalar()
        before_raw_agents = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM agents "
                "WHERE mcp_provider_user_id = 'u-real'"
            )
        ).scalar()
        before_msgs = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM conversation_messages WHERE user_id = :uid"
            ),
            {"uid": pseudo_uid},
        ).scalar()
        assert before_agents == 1
        assert before_raw_agents == 0, (
            "raw Discord ID leaked into agents.mcp_provider_user_id"
        )
        assert before_msgs == 1

        # /forget-me is called with the RAW ID — endpoint contract is
        # unchanged. Storage representation is the only thing that
        # differs.
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

        # Verify everything is actually gone — query under the pseudo,
        # which is what was actually written.
        assert (
            pg_conn.execute(
                sql_text(
                    "SELECT count(*) FROM agents "
                    "WHERE mcp_provider_user_id = :pseudo"
                ),
                {"pseudo": bare_pseudo},
            ).scalar()
            == 0
        )
        assert (
            pg_conn.execute(
                sql_text(
                    "SELECT count(*) FROM conversations WHERE user_id = :uid"
                ),
                {"uid": pseudo_uid},
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
                {"uid": pseudo_uid},
            ).scalar()
            == 0
        )
        assert (
            pg_conn.execute(
                sql_text("SELECT count(*) FROM users WHERE user_id = :uid"),
                {"uid": pseudo_uid},
            ).scalar()
            == 0
        )


class TestPseudonymizationContract:
    """The privacy-anonymization contract.

    These tests are the regression guard for the change that
    HMAC-pseudonymizes Discord user identifiers across every
    user-keyed table. They assert the end-to-end shape (raw ID never
    reaches the DB) and the create/forget parity (the same helper is
    called on both the write and delete paths).
    """

    _PROV_KEY = "test-prov-key"
    _AUTH = {"X-Provisioning-Key": _PROV_KEY}
    # Realistic 18-digit Discord snowflake.
    _RAW = "123456789012345678"

    def _make_app_with_provisioning(self, pg_conn, sources_count=1):
        ids = TestCreateMcpKey._make_sources(pg_conn, count=sources_count)
        return ids

    def _provision(self, pg_conn, *, raw_id=_RAW, username="alice"):
        ids = self._make_app_with_provisioning(pg_conn)
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
                    json={
                        "discord_user_id": raw_id,
                        "discord_username": username,
                    },
                )
        return r

    def test_no_raw_discord_id_in_agents_table(self, pg_conn):
        """The single most important regression guard. An accidental
        revert of the pseudonymization is one logic error away from
        silently writing plaintext again — this test pins the
        contract at the SQL level."""
        from sqlalchemy import text as sql_text

        r = self._provision(pg_conn)
        assert r.status_code == 200

        # The raw 18-digit Discord ID must not appear anywhere in
        # mcp_provider_user_id.
        raw_count = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM agents "
                "WHERE mcp_provider_user_id = :raw"
            ),
            {"raw": self._RAW},
        ).scalar()
        assert raw_count == 0, "raw Discord ID leaked into agents.mcp_provider_user_id"

    def test_pseudo_present_in_agents_table(self, pg_conn):
        """``mcp_provider_user_id`` is the bare 32-char hex (no prefix)."""
        from sqlalchemy import text as sql_text

        self._provision(pg_conn)
        match_count = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM agents "
                "WHERE mcp_provider = 'discord' "
                "AND mcp_provider_user_id ~ '^[a-f0-9]{32}$'"
            )
        ).scalar()
        assert match_count == 1

    def test_user_id_carries_prefix(self, pg_conn):
        """``agents.user_id`` is the prefixed pseudo."""
        from sqlalchemy import text as sql_text

        self._provision(pg_conn)
        match_count = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM agents "
                "WHERE user_id ~ '^discord_p_v1:[a-f0-9]{32}$'"
            )
        ).scalar()
        assert match_count == 1

    def test_name_is_constant(self, pg_conn):
        """No Discord username embedded in agents.name."""
        from sqlalchemy import text as sql_text

        # Provision with a memorable username so the assertion is
        # meaningful — if the rename regresses the test sees it.
        self._provision(pg_conn, username="some-distinctive-name")
        names = pg_conn.execute(
            sql_text(
                "SELECT name FROM agents WHERE mcp_provider = 'discord'"
            )
        ).fetchall()
        assert [row[0] for row in names] == ["Aztec MCP"]

    def test_idempotency_under_different_username(self, pg_conn):
        """Calling /mcp-key twice with the same Discord ID and
        DIFFERENT usernames produces one row, and api_key /
        mcp_provider_user_id / user_id are all unchanged across the
        two calls. Catches drift in the upsert + helper paths.
        """
        from sqlalchemy import text as sql_text

        r1 = self._provision(pg_conn, username="alice-original")
        r2 = self._provision(pg_conn, username="alice-renamed")
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json["api_key"] == r2.json["api_key"]

        rows = pg_conn.execute(
            sql_text(
                "SELECT count(*), max(key), max(mcp_provider_user_id), "
                "       max(user_id), max(name) "
                "FROM agents WHERE mcp_provider = 'discord'"
            )
        ).fetchone()
        assert rows[0] == 1, "expected exactly one Discord agent row"
        assert rows[1] == r1.json["api_key"]
        # mcp_provider_user_id and user_id are deterministic from the
        # same raw ID + pepper, so they have to be identical.
        assert rows[2] is not None and len(rows[2]) == 32
        assert rows[3].startswith("discord_p_v1:")
        assert rows[4] == "Aztec MCP"

    def test_distinct_ids_produce_distinct_pseudos(self, pg_conn):
        """SQL-level collision sanity check on the helper."""
        from sqlalchemy import text as sql_text

        self._provision(pg_conn, raw_id="111111111111111111", username="a")
        self._provision(pg_conn, raw_id="222222222222222222", username="b")
        distinct = pg_conn.execute(
            sql_text(
                "SELECT count(DISTINCT mcp_provider_user_id) FROM agents "
                "WHERE mcp_provider = 'discord'"
            )
        ).scalar()
        assert distinct == 2

    def test_pseudonymize_create_forget_parity(self, pg_conn):
        """Three-property contract test (codex review — most
        important single test for this PR). If the helper drifts
        between the create and forget paths, /forget-me silently fails
        to find the rows and this test catches it.
        """
        from sqlalchemy import text as sql_text

        # 1. /mcp-key with raw ID.
        r = self._provision(pg_conn)
        assert r.status_code == 200
        # Agent row exists, raw ID NOT findable, pseudo IS findable.
        assert (
            pg_conn.execute(
                sql_text(
                    "SELECT count(*) FROM agents "
                    "WHERE mcp_provider_user_id = :raw"
                ),
                {"raw": self._RAW},
            ).scalar()
            == 0
        )
        assert (
            pg_conn.execute(
                sql_text(
                    "SELECT count(*) FROM agents "
                    "WHERE mcp_provider = 'discord'"
                )
            ).scalar()
            == 1
        )

        # 2. /forget-me with the same RAW ID.
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                forget = c.post(
                    "/api/internal/forget_discord_user",
                    headers=self._AUTH,
                    json={"discord_user_id": self._RAW},
                )
        assert forget.status_code == 200
        assert forget.json["deleted"]["agents"] == 1, (
            "/forget-me failed to find rows written by /mcp-key — "
            "create and forget paths are computing different pseudonyms"
        )

        # 3. SELECT for the raw ID returns 0 in EVERY user-keyed table.
        # Reuse the same EXPECTED_TABLES set the existing
        # TestForgetDiscordUser pins so the negative-lookup and
        # response-shape assertions stay in sync.
        for table in TestForgetDiscordUser.EXPECTED_TABLES:
            # ``agents`` has the raw ID in ``mcp_provider_user_id``;
            # every other table has it in ``user_id``.
            col = (
                "mcp_provider_user_id"
                if table == "agents"
                else "user_id"
            )
            count = pg_conn.execute(
                sql_text(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE {col} = :raw OR {col} = :prefixed"
                ),
                {"raw": self._RAW, "prefixed": f"discord:{self._RAW}"},
            ).scalar()
            assert count == 0, (
                f"{table} has plaintext rows after /forget-me; "
                f"either the migration missed it or the route does"
            )


class TestStreamPseudonymPropagation:
    """Verify the pseudonym propagates from the agent row into the
    stream's identity context (and therefore into ``conversations``
    + ``conversation_messages`` rows written downstream).

    The full ``/stream`` route depends on retired Mongo fixtures
    (``mock_mongo_db``) and an LLM stub that no longer exists in this
    fork — extending those tests is a trap. The useful seam is
    ``StreamProcessor._get_data_from_api_key``: that's the function
    that reads ``agents.user_id`` and exposes it as the ``data["user"]``
    that downstream code propagates into ``decoded_token["sub"]``,
    which is what ``ConversationsRepository.create`` writes as
    ``conversations.user_id``. If this single hop preserves the
    pseudo, the rest is trivial.
    """

    _PROV_KEY = "test-prov-key"
    _AUTH = {"X-Provisioning-Key": _PROV_KEY}

    def test_get_data_from_api_key_returns_pseudonymous_user(self, pg_conn):
        import re

        from application.api.answer.services.stream_processor import (
            StreamProcessor,
        )

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
                r = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={
                        "discord_user_id": "42",
                        "discord_username": "x",
                    },
                )
        assert r.status_code == 200
        api_key = r.json["api_key"]

        # Build a minimal StreamProcessor and call _get_data_from_api_key.
        # The class is instantiated with bare-minimum args; we only need
        # the method's logic.
        with _patch_db(pg_conn), patch(
            "application.api.answer.services.stream_processor.db_readonly"
        ) as db_readonly_patch:
            from contextlib import contextmanager

            @contextmanager
            def _yield_pg_conn():
                yield pg_conn

            db_readonly_patch.side_effect = lambda: _yield_pg_conn()

            sp = StreamProcessor.__new__(StreamProcessor)  # bypass __init__
            data = sp._get_data_from_api_key(api_key)

        # data["user"] is what propagates into decoded_token["sub"]
        # which conversations.user_id derives from.
        assert re.fullmatch(r"discord_p_v1:[a-f0-9]{32}", data["user"]), (
            f"agent identity not pseudonymized: data['user']={data['user']!r}"
        )
