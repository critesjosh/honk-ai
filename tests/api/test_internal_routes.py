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
    def _make_sources(pg_conn, count: int = 5) -> list[str]:
        from application.storage.db.repositories.sources import SourcesRepository

        repo = SourcesRepository(pg_conn)
        return [str(repo.create(f"src-{i}", user_id="local")["id"]) for i in range(count)]

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
