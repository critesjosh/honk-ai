"""Tests for application/api/internal/routes.py.

Uses the ephemeral ``pg_conn`` fixture so the sources repository writes
happen against a real Postgres schema.
"""

import re
from contextlib import contextmanager
from unittest.mock import patch

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
        # Bot chat turns are REDACTED in place (not deleted) and counted
        # under this key — see the redact-in-place block in routes.py +
        # TestForgetErasesChatTurns. user_id stays 'local', so the
        # plaintext-completeness check below trivially passes for it.
        "conversation_messages",
        # Paused tool-continuation state, deleted by requester_user_id (no
        # longer cascades since we tombstone the parent conversation).
        "pending_tool_state",
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


class TestSlackProvider:
    """The generalized (provider-aware) create/forget path used by Slack.

    Locks down: the new ``{provider, provider_user_id}`` request shape,
    the strict provider allowlist + no-fallback rule, the
    ``mcp_provider='slack'`` / ``surface='mcp'`` storage shape, and
    cross-provider isolation (a Slack forget must not touch a Discord row
    that shares the same raw id).
    """

    _PROV_KEY = "test-prov-key"
    _AUTH = {"X-Provisioning-Key": _PROV_KEY}
    # Slack compound identity: workspace-scoped ``team_id:user_id``.
    _SLACK_RAW = "T0AZTEC:U0HONK"

    def _provision_slack(self, pg_conn, *, raw_id=_SLACK_RAW):
        ids = TestCreateMcpKey._make_sources(pg_conn, count=2)
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), patch(
            "application.api.internal.routes.settings.AZTEC_SOURCE_IDS",
            ",".join(ids),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                return c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"provider": "slack", "provider_user_id": raw_id},
                )

    def test_create_slack_key_storage_shape(self, pg_conn):
        from sqlalchemy import text as sql_text

        r = self._provision_slack(pg_conn)
        assert r.status_code == 200, r.json

        # mcp_provider='slack', surface stays 'mcp', bare-hex (no raw id).
        row = pg_conn.execute(
            sql_text(
                "SELECT mcp_provider, surface, mcp_provider_user_id, user_id "
                "FROM agents WHERE key = :k"
            ),
            {"k": r.json["api_key"]},
        ).fetchone()
        assert row.mcp_provider == "slack"
        assert row.surface == "mcp"
        assert re.fullmatch(r"[a-f0-9]{32}", row.mcp_provider_user_id)
        assert row.user_id.startswith("slack_p_v1:")
        # Raw compound id must never appear in storage.
        leaked = pg_conn.execute(
            sql_text(
                "SELECT count(*) FROM agents WHERE mcp_provider_user_id = :raw"
            ),
            {"raw": self._SLACK_RAW},
        ).scalar()
        assert leaked == 0

    def test_slack_username_not_required(self, pg_conn):
        """Unlike the legacy Discord shape, the generalized shape does not
        require a username field."""
        r = self._provision_slack(pg_conn)
        assert r.status_code == 200

    def test_generalized_shape_requires_provider_user_id(self, pg_conn):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"provider": "slack"},
                )
        assert r.status_code == 400
        assert "provider_user_id" in (r.json or {}).get("error", "")

    def test_no_fallback_from_slack_to_discord_field(self, pg_conn):
        """A Slack caller that supplies only ``discord_user_id`` (no
        ``provider_user_id``) must 400 — NOT silently mint a Discord-scoped
        pseudonym from the stray field."""
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"provider": "slack", "discord_user_id": "T1:U1"},
                )
        assert r.status_code == 400

    def test_empty_provider_does_not_fall_back_to_discord(self, pg_conn):
        """An explicit empty/whitespace ``provider`` is a malformed
        generalized call — it must 400, NOT silently fall back to the
        Discord fields and mint a Discord-scoped pseudonym."""
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"provider": "  ", "discord_user_id": "u1", "discord_username": "x"},
                )
        assert r.status_code == 400
        assert "unsupported provider" in (r.json or {}).get("error", "")

    def test_unsupported_provider_rejected(self, pg_conn):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                r = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"provider": "github", "provider_user_id": "x"},
                )
        assert r.status_code == 400
        assert "unsupported provider" in (r.json or {}).get("error", "")

    def test_forget_slack_isolates_from_discord(self, pg_conn):
        """Provision the SAME raw id under both discord and slack, then
        forget only slack. The discord row must survive."""
        from sqlalchemy import text as sql_text

        ids = TestCreateMcpKey._make_sources(pg_conn, count=1)
        shared_raw = "COLLIDE:ME"
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), patch(
            "application.api.internal.routes.settings.AZTEC_SOURCE_IDS",
            ",".join(ids),
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                d = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"discord_user_id": shared_raw, "discord_username": "x"},
                )
                s = c.post(
                    "/api/internal/create_mcp_key",
                    headers=self._AUTH,
                    json={"provider": "slack", "provider_user_id": shared_raw},
                )
                assert d.status_code == 200 and s.status_code == 200
                # Two distinct rows despite the shared raw id.
                assert d.json["api_key"] != s.json["api_key"]

                forget = c.post(
                    "/api/internal/forget_discord_user",
                    headers=self._AUTH,
                    json={"provider": "slack", "provider_user_id": shared_raw},
                )
        assert forget.status_code == 200
        assert forget.json["deleted"]["agents"] == 1

        # Slack row gone, Discord row survives.
        assert (
            pg_conn.execute(
                sql_text("SELECT count(*) FROM agents WHERE mcp_provider = 'slack'")
            ).scalar()
            == 0
        )
        assert (
            pg_conn.execute(
                sql_text("SELECT count(*) FROM agents WHERE mcp_provider = 'discord'")
            ).scalar()
            == 1
        )


class TestForgetErasesChatTurns:
    """/forget-me must erase a bot user's own chat turns even though they are
    stored under the shared ``user_id='local'`` owner — via redact-in-place
    (keep row + position, NULL the content) plus scrubbing every off-message
    copy. See migration 0011 + the redact block in routes.py.
    """

    _PROV_KEY = "test-prov-key"
    _AUTH = {"X-Provisioning-Key": _PROV_KEY}
    _RAW = "discord-chatter-1"
    # conftest sets USER_ID_PEPPER to this for the test process.
    _PEPPER = "0" * 64

    def _pseudo(self, provider="discord", raw=None):
        from application.pseudonyms import canonical_user_id

        return canonical_user_id(provider, raw or self._RAW, pepper=self._PEPPER)

    def _forget(self, pg_conn, raw=None):
        app = _make_app()
        with patch(
            "application.api.internal.routes.settings.MCP_PROVISIONING_KEY",
            self._PROV_KEY,
        ), _patch_db(pg_conn):
            with app.test_client() as c:
                return c.post(
                    "/api/internal/forget_discord_user",
                    headers=self._AUTH,
                    json={"discord_user_id": raw or self._RAW},
                )

    def test_redacts_requester_turns_keeps_coparticipant_and_position(self, pg_conn):
        from sqlalchemy import text as sql_text

        pseudo = self._pseudo()
        # SHARED conversation owned by 'local' with a content-derived title.
        # pos 0 = the requester's turn; pos 1 = a co-participant (NULL tag).
        conv_id = pg_conn.execute(
            sql_text(
                "INSERT INTO conversations (user_id, name) "
                "VALUES ('local', 'secret title') RETURNING id"
            )
        ).scalar()
        pg_conn.execute(
            sql_text(
                "INSERT INTO conversation_messages "
                "(conversation_id, user_id, position, prompt, response, requester_user_id) "
                "VALUES (:cid, 'local', 0, 'my question', 'my answer', :p)"
            ),
            {"cid": conv_id, "p": pseudo},
        )
        pg_conn.execute(
            sql_text(
                "INSERT INTO conversation_messages "
                "(conversation_id, user_id, position, prompt, response) "
                "VALUES (:cid, 'local', 1, 'other question', 'other answer')"
            ),
            {"cid": conv_id},
        )
        # Off-message copies, tagged with the requester pseudonym.
        pg_conn.execute(
            sql_text(
                "INSERT INTO user_logs (user_id, endpoint, data, requester_user_id) "
                "VALUES ('local', 'stream_answer', '{\"question\": \"my question\"}'::jsonb, :p)"
            ),
            {"p": pseudo},
        )
        pg_conn.execute(
            sql_text(
                "INSERT INTO stack_logs (activity_id, endpoint, user_id, query, requester_user_id) "
                "VALUES ('act-1', 'stream', 'local', 'my question', :p)"
            ),
            {"p": pseudo},
        )

        r = self._forget(pg_conn)
        assert r.status_code == 200, r.json
        deleted = r.json["deleted"]
        assert deleted["conversation_messages"] == 1

        # Requester's turn: redacted in place (row + position kept, content gone).
        row = pg_conn.execute(
            sql_text(
                "SELECT prompt, response, requester_user_id, erased_at, position "
                "FROM conversation_messages WHERE conversation_id = :cid AND position = 0"
            ),
            {"cid": conv_id},
        ).fetchone()
        assert row is not None, "requester turn must NOT be deleted (position contract)"
        assert row.prompt is None and row.response is None
        assert row.requester_user_id is None
        assert row.erased_at is not None
        assert row.position == 0

        # Co-participant's turn: untouched.
        other = pg_conn.execute(
            sql_text(
                "SELECT prompt, erased_at FROM conversation_messages "
                "WHERE conversation_id = :cid AND position = 1"
            ),
            {"cid": conv_id},
        ).fetchone()
        assert other.prompt == "other question"
        assert other.erased_at is None

        # Conversation survives but its content-derived title is cleared.
        conv = pg_conn.execute(
            sql_text("SELECT name FROM conversations WHERE id = :cid"),
            {"cid": conv_id},
        ).fetchone()
        assert conv is not None and conv.name is None

        # Off-message log copies are deleted.
        assert (
            pg_conn.execute(
                sql_text("SELECT count(*) FROM user_logs WHERE requester_user_id = :p"),
                {"p": pseudo},
            ).scalar()
            == 0
        )
        assert (
            pg_conn.execute(
                sql_text("SELECT count(*) FROM stack_logs WHERE requester_user_id = :p"),
                {"p": pseudo},
            ).scalar()
            == 0
        )
        assert deleted["user_logs"] >= 1
        assert deleted["stack_logs"] >= 1

    def test_compression_summary_is_tombstoned(self, pg_conn):
        from sqlalchemy import text as sql_text

        pseudo = self._pseudo()
        conv_id = pg_conn.execute(
            sql_text(
                "INSERT INTO conversations (user_id, name, compression_metadata) "
                "VALUES ('local', 't', '{\"compressed_summary\": \"x\"}'::jsonb) RETURNING id"
            )
        ).scalar()
        pg_conn.execute(
            sql_text(
                "INSERT INTO conversation_messages "
                "(conversation_id, user_id, position, prompt, response, requester_user_id) "
                "VALUES (:cid, 'local', 0, 'q', 'a', :p)"
            ),
            {"cid": conv_id, "p": pseudo},
        )
        # Synthetic compression-summary message (carries the marker, no tag).
        pg_conn.execute(
            sql_text(
                "INSERT INTO conversation_messages "
                "(conversation_id, user_id, position, prompt, response, message_metadata) "
                "VALUES (:cid, 'local', 1, '[Context Compression Summary]', 'summary text', "
                "'{\"type\": \"compression_summary\"}'::jsonb)"
            ),
            {"cid": conv_id},
        )

        r = self._forget(pg_conn)
        assert r.status_code == 200, r.json

        summary = pg_conn.execute(
            sql_text(
                "SELECT response, erased_at FROM conversation_messages "
                "WHERE conversation_id = :cid AND position = 1"
            ),
            {"cid": conv_id},
        ).fetchone()
        assert summary.response is None
        assert summary.erased_at is not None
        meta = pg_conn.execute(
            sql_text("SELECT compression_metadata FROM conversations WHERE id = :cid"),
            {"cid": conv_id},
        ).scalar()
        assert meta is None

    def test_write_path_pseudonym_matches_forget(self):
        """Parity: the pseudonym the /stream write path stamps
        (resolve_requester_pseudonym) is byte-identical to what /forget-me
        computes (canonical_user_id) — otherwise erasure would miss."""
        from application.pseudonyms import (
            canonical_user_id,
            resolve_requester_pseudonym,
        )

        for provider, raw in (("discord", "1234567890"), ("slack", "T0:U0")):
            write_side = resolve_requester_pseudonym(provider, raw, pepper=self._PEPPER)
            forget_side = canonical_user_id(provider, raw, pepper=self._PEPPER)
            assert write_side == forget_side
            assert write_side is not None and raw not in write_side

    def test_resolve_requester_pseudonym_is_lenient(self):
        from application.pseudonyms import resolve_requester_pseudonym

        p = self._PEPPER
        assert resolve_requester_pseudonym(None, "x", pepper=p) is None
        assert resolve_requester_pseudonym("discord", None, pepper=p) is None
        assert resolve_requester_pseudonym("discord", "", pepper=p) is None
        assert resolve_requester_pseudonym("telegram", "x", pepper=p) is None
        assert resolve_requester_pseudonym("discord", "x", pepper="") is None
        assert resolve_requester_pseudonym(123, "x", pepper=p) is None
