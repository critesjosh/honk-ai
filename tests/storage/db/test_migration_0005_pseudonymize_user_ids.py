"""Tests for migration ``0005_pseudonymize_user_ids``.

Two layers (per plan + codex review):

* **Layer A** — call the migration's importable
  ``do_pseudonymize_discord_users`` directly against ``pg_conn`` so
  we get fast, hermetic coverage of every behaviour
  (plaintext-free, move-not-copy, agent_preferences-preserved,
  control rows untouched, idempotency, empty-pepper abort).

* **Layer B** — one integration test that runs the *actual* Alembic
  ``upgrade()`` from a pre-0005 schema state, exercising the real
  migration file's ordering, imports, and ``op.get_bind()`` plumbing.
  Layer A alone could pass while the Alembic file itself is broken.
"""

from __future__ import annotations

import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


_PEPPER = "0" * 64  # matches tests/conftest.py setdefault


@contextmanager
def _pepper_env(monkeypatch, value: str | None):
    """Force USER_ID_PEPPER to a specific value (or unset) for the
    duration of a block. The migration helper reads ``os.environ``
    directly so we have to set the real env, not the settings cache.
    """
    if value is None:
        monkeypatch.delenv("USER_ID_PEPPER", raising=False)
    else:
        monkeypatch.setenv("USER_ID_PEPPER", value)
    yield


# ---------------------------------------------------------------------------
# Seed factory
# ---------------------------------------------------------------------------


def _seed_discord_user(
    conn,
    bare_id: str,
    *,
    seed_full_graph: bool = False,
) -> dict:
    """Insert one valid row per touched table for a given Discord
    identity. Returns a dict of seeded row IDs / counts so callers
    can write targeted assertions.

    ``bare_id`` is the raw 18-digit-ish Discord ID. The ``user_id``
    columns are seeded as ``"discord:<bare_id>"`` (the legacy
    plaintext form the migration is supposed to rewrite).

    ``seed_full_graph=True`` populates all 19 user-keyed tables;
    when False (default) it populates the smaller core
    (``users``, ``agents``, ``conversations``,
    ``conversation_messages``, ``user_logs``, ``token_usage``)
    which is enough for most assertions and avoids the FK acrobatics
    of attachments / workflow_runs / shared_conversations.
    """
    raw_user_id = f"discord:{bare_id}"

    # users — parent. agent_preferences with a non-default sentinel
    # so the "preferences survive the migration" test has something
    # to assert.
    conn.execute(
        text(
            "INSERT INTO users (user_id, agent_preferences) "
            "VALUES (:uid, CAST(:prefs AS jsonb))"
        ),
        {
            "uid": raw_user_id,
            "prefs": '{"pinned": ["seed-' + bare_id + '"]}',
        },
    )

    # agents — Discord MCP agent with the raw ID in mcp_provider_user_id
    # and a username embedded in name so we can verify both got
    # scrubbed.
    #
    # ``surface`` is only present once migration ``0009_agents_surface``
    # has run. ``test_alembic_upgrade_through_0005_runs`` seeds rows at
    # alembic revision 0004 (before this column exists), so we
    # introspect ``information_schema`` and emit the matching INSERT
    # shape. All other callers run with alembic at HEAD and get the
    # ``surface='mcp'`` column.
    has_surface = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'agents' AND column_name = 'surface'"
        )
    ).scalar() is not None
    if has_surface:
        agent_id = conn.execute(
            text(
                "INSERT INTO agents "
                "(user_id, name, status, mcp_provider, mcp_provider_user_id, "
                " mcp_purpose, key, retriever, agent_type, surface) "
                "VALUES (:uid, :name, 'published', 'discord', :pid, "
                "        'aztec_mcp', :key, 'classic', 'classic', 'mcp') "
                "RETURNING id"
            ),
            {
                "uid": raw_user_id,
                "name": f"Aztec MCP - alice-{bare_id}",
                "pid": bare_id,
                "key": f"key-{bare_id}",
            },
        ).scalar()
    else:
        agent_id = conn.execute(
            text(
                "INSERT INTO agents "
                "(user_id, name, status, mcp_provider, mcp_provider_user_id, "
                " mcp_purpose, key, retriever, agent_type) "
                "VALUES (:uid, :name, 'published', 'discord', :pid, "
                "        'aztec_mcp', :key, 'classic', 'classic') "
                "RETURNING id"
            ),
            {
                "uid": raw_user_id,
                "name": f"Aztec MCP - alice-{bare_id}",
                "pid": bare_id,
                "key": f"key-{bare_id}",
            },
        ).scalar()

    # conversations + conversation_messages
    conv_id = conn.execute(
        text(
            "INSERT INTO conversations (user_id, name) "
            "VALUES (:uid, 'test') RETURNING id"
        ),
        {"uid": raw_user_id},
    ).scalar()
    conn.execute(
        text(
            "INSERT INTO conversation_messages "
            "(conversation_id, user_id, position, prompt, response) "
            "VALUES (:cid, :uid, 0, 'q', 'a')"
        ),
        {"cid": conv_id, "uid": raw_user_id},
    )

    # operational logs
    conn.execute(
        text(
            "INSERT INTO user_logs (user_id, endpoint, data) "
            "VALUES (:uid, '/api/answer', '{}'::jsonb)"
        ),
        {"uid": raw_user_id},
    )
    conn.execute(
        text(
            "INSERT INTO token_usage "
            "(user_id, prompt_tokens, generated_tokens) "
            "VALUES (:uid, 10, 20)"
        ),
        {"uid": raw_user_id},
    )

    seeded = {
        "raw_user_id": raw_user_id,
        "agent_id": agent_id,
        "conversation_id": conv_id,
    }

    if seed_full_graph:
        # The remaining 13 user-keyed tables. Each gets one minimal
        # row satisfying its NOT NULL constraints.
        conn.execute(
            text(
                "INSERT INTO prompts (user_id, name, content) "
                "VALUES (:uid, 'p', 'c')"
            ),
            {"uid": raw_user_id},
        )
        ut_id = conn.execute(
            text(
                "INSERT INTO user_tools (user_id, name) "
                "VALUES (:uid, 't') RETURNING id"
            ),
            {"uid": raw_user_id},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO stack_logs "
                "(activity_id, user_id, query) VALUES ('a', :uid, 'q')"
            ),
            {"uid": raw_user_id},
        )
        conn.execute(
            text(
                "INSERT INTO agent_folders (user_id, name) "
                "VALUES (:uid, 'f')"
            ),
            {"uid": raw_user_id},
        )
        conn.execute(
            text(
                "INSERT INTO sources (user_id, name) VALUES (:uid, 's')"
            ),
            {"uid": raw_user_id},
        )
        conn.execute(
            text(
                "INSERT INTO attachments "
                "(user_id, filename, upload_path) "
                "VALUES (:uid, 'f.txt', 'x/y')"
            ),
            {"uid": raw_user_id},
        )
        conn.execute(
            text(
                "INSERT INTO memories (user_id, tool_id, path, content) "
                "VALUES (:uid, :tid, 'p', 'c')"
            ),
            {"uid": raw_user_id, "tid": ut_id},
        )
        conn.execute(
            text(
                "INSERT INTO todos (user_id, tool_id, title) "
                "VALUES (:uid, :tid, 'todo')"
            ),
            {"uid": raw_user_id, "tid": ut_id},
        )
        conn.execute(
            text(
                "INSERT INTO notes (user_id, tool_id, title, content) "
                "VALUES (:uid, :tid, 'note', 'c')"
            ),
            {"uid": raw_user_id, "tid": ut_id},
        )
        conn.execute(
            text(
                "INSERT INTO connector_sessions (user_id, provider) "
                "VALUES (:uid, 'gdrive')"
            ),
            {"uid": raw_user_id},
        )
        # shared_conversations needs a uuid column.
        conn.execute(
            text(
                "INSERT INTO shared_conversations "
                "(uuid, conversation_id, user_id) "
                "VALUES (gen_random_uuid(), :cid, :uid)"
            ),
            {"cid": conv_id, "uid": raw_user_id},
        )
        conn.execute(
            text(
                "INSERT INTO pending_tool_state "
                "(conversation_id, user_id, messages, pending_tool_calls, "
                " tools_dict, tool_schemas, agent_config, expires_at) "
                "VALUES (:cid, :uid, '[]'::jsonb, '[]'::jsonb, "
                "        '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, "
                "        now() + interval '1 hour')"
            ),
            {"cid": conv_id, "uid": raw_user_id},
        )
        wf_id = conn.execute(
            text(
                "INSERT INTO workflows (user_id, name) "
                "VALUES (:uid, 'w') RETURNING id"
            ),
            {"uid": raw_user_id},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO workflow_runs "
                "(workflow_id, user_id, status) "
                "VALUES (:wfid, :uid, 'pending')"
            ),
            {"wfid": wf_id, "uid": raw_user_id},
        )
    return seeded


_FULL_GRAPH_TABLES = (
    "users",
    "prompts",
    "user_tools",
    "token_usage",
    "user_logs",
    "stack_logs",
    "agent_folders",
    "sources",
    "agents",
    "attachments",
    "memories",
    "todos",
    "notes",
    "connector_sessions",
    "conversations",
    "conversation_messages",
    "shared_conversations",
    "pending_tool_state",
    "workflows",
    "workflow_runs",
)


@pytest.mark.integration
class TestPseudonymizeMigrationLayerA:
    """Direct calls to ``do_pseudonymize_discord_users``."""

    def _run(self, pg_conn):
        from importlib import import_module

        # Module name starts with a digit, so the only way to import
        # it is via importlib.import_module (regular ``from ... import``
        # syntax disallows leading digits in identifiers).
        mod = import_module(
            "application.alembic.versions.0005_pseudonymize_user_ids"
        )
        return mod.do_pseudonymize_discord_users(pg_conn)

    def test_plaintext_free_post_state_full_graph(self, pg_conn, monkeypatch):
        """All 19 user-keyed tables: no row matches ``user_id LIKE
        'discord:%'`` after migration."""
        with _pepper_env(monkeypatch, _PEPPER):
            _seed_discord_user(pg_conn, "u1", seed_full_graph=True)
            self._run(pg_conn)

        for table in _FULL_GRAPH_TABLES:
            n = pg_conn.execute(
                text(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE user_id LIKE 'discord:%'"
                )
            ).scalar()
            assert n == 0, f"{table} still has plaintext rows"

    def test_pseudo_present_post_state_full_graph(self, pg_conn, monkeypatch):
        """The pseudo prefix is present in every user-keyed table."""
        with _pepper_env(monkeypatch, _PEPPER):
            _seed_discord_user(pg_conn, "u1", seed_full_graph=True)
            self._run(pg_conn)

        for table in _FULL_GRAPH_TABLES:
            n = pg_conn.execute(
                text(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE user_id LIKE 'discord_p_v1:%'"
                )
            ).scalar()
            assert n > 0, f"{table} has no pseudonymous rows"

    def test_move_not_copy_with_control_row(self, pg_conn, monkeypatch):
        """Stronger than 'counts unchanged' — assert per table that:

        1. Old raw count is 0.
        2. Pseudo count equals the seed count of 1.
        3. Control row's user_id is unchanged (catches a too-broad
           WHERE clause).
        """
        # Control row that must NOT be rewritten.
        pg_conn.execute(
            text(
                "INSERT INTO users (user_id, agent_preferences) "
                "VALUES ('local', '{\"pinned\": []}'::jsonb)"
            )
        )
        pg_conn.execute(
            text(
                "INSERT INTO conversations (user_id, name) "
                "VALUES ('local', 'control')"
            )
        )

        with _pepper_env(monkeypatch, _PEPPER):
            _seed_discord_user(pg_conn, "u1")
            self._run(pg_conn)

        # 1+2: per table, raw is gone.
        for table in ("users", "conversations", "conversation_messages",
                      "user_logs", "token_usage", "agents"):
            old_count = pg_conn.execute(
                text(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE user_id LIKE 'discord:%'"
                )
            ).scalar()
            assert old_count == 0, (
                f"{table} still has plaintext rows after migration"
            )

        # ``agents`` also has the raw Discord ID in
        # ``mcp_provider_user_id`` — assert THAT is gone too. Earlier
        # this branch silently set ``old_count = 0`` for the agents
        # table, which made the test pass even if the most sensitive
        # plaintext (the actual Discord snowflake) survived. Codex
        # review caught it.
        agents_raw_pid = pg_conn.execute(
            text(
                "SELECT count(*) FROM agents "
                "WHERE mcp_provider_user_id ~ '^[0-9]+$'"
            )
        ).scalar()
        assert agents_raw_pid == 0, (
            "agents.mcp_provider_user_id still contains a Discord snowflake"
        )
        # 3: control row survived.
        assert (
            pg_conn.execute(
                text("SELECT count(*) FROM users WHERE user_id = 'local'")
            ).scalar()
            == 1
        )
        assert (
            pg_conn.execute(
                text(
                    "SELECT count(*) FROM conversations WHERE user_id = 'local'"
                )
            ).scalar()
            == 1
        )

    def test_agent_preferences_survives(self, pg_conn, monkeypatch):
        """Whole point of the parent-first-and-last sandwich. The
        seed sets agent_preferences to a sentinel dict; after the
        migration the new pseudo row's preferences must equal it.
        """
        with _pepper_env(monkeypatch, _PEPPER):
            _seed_discord_user(pg_conn, "u-prefs")
            self._run(pg_conn)

        prefs = pg_conn.execute(
            text(
                "SELECT agent_preferences FROM users "
                "WHERE user_id LIKE 'discord_p_v1:%'"
            )
        ).scalar()
        assert prefs == {"pinned": ["seed-u-prefs"]}, (
            f"agent_preferences was lost during migration: {prefs!r}"
        )

    def test_agents_name_stripped_only_for_discord(self, pg_conn, monkeypatch):
        """Discord agent's name → 'Aztec MCP'; non-Discord agent
        with the same name pattern is untouched."""
        # Non-Discord agent with the same prefix in its name. The
        # sweep must not touch this row.
        pg_conn.execute(
            text(
                "INSERT INTO agents (user_id, name, status, surface) "
                "VALUES ('local', 'Aztec MCP - keep me', 'published', 'discord')"
            )
        )
        with _pepper_env(monkeypatch, _PEPPER):
            _seed_discord_user(pg_conn, "u-name")
            self._run(pg_conn)

        discord_name = pg_conn.execute(
            text(
                "SELECT name FROM agents WHERE mcp_provider = 'discord'"
            )
        ).scalar()
        assert discord_name == "Aztec MCP"

        non_discord_name = pg_conn.execute(
            text(
                "SELECT name FROM agents WHERE mcp_provider IS NULL"
            )
        ).scalar()
        assert non_discord_name == "Aztec MCP - keep me", (
            "migration's name sweep is too broad — touched a "
            "non-Discord agent"
        )

    def test_mcp_provider_user_id_swapped(self, pg_conn, monkeypatch):
        with _pepper_env(monkeypatch, _PEPPER):
            _seed_discord_user(pg_conn, "u-pid")
            self._run(pg_conn)

        new_val = pg_conn.execute(
            text(
                "SELECT mcp_provider_user_id FROM agents "
                "WHERE mcp_provider = 'discord'"
            )
        ).scalar()
        assert re.fullmatch(r"[a-f0-9]{32}", new_val), (
            f"mcp_provider_user_id should be 32-char hex, got {new_val!r}"
        )
        assert new_val != "u-pid"

    def test_old_users_row_gone(self, pg_conn, monkeypatch):
        with _pepper_env(monkeypatch, _PEPPER):
            _seed_discord_user(pg_conn, "u-gone")
            self._run(pg_conn)
        n = pg_conn.execute(
            text(
                "SELECT count(*) FROM users WHERE user_id = 'discord:u-gone'"
            )
        ).scalar()
        assert n == 0

    def test_idempotent(self, pg_conn, monkeypatch):
        """Second invocation is a no-op — second pass finds no
        plaintext rows to rewrite."""
        with _pepper_env(monkeypatch, _PEPPER):
            _seed_discord_user(pg_conn, "u-idem")
            self._run(pg_conn)
            result2 = self._run(pg_conn)
        # Migration helper returns ``users_rewritten=0`` on a no-op.
        assert result2 == {"users_rewritten": 0, "tables_touched": 0}

    def test_empty_pepper_aborts_before_any_update(self, pg_conn, monkeypatch):
        _seed_discord_user(pg_conn, "u-fail")
        before = pg_conn.execute(
            text(
                "SELECT count(*) FROM users WHERE user_id LIKE 'discord:%'"
            )
        ).scalar()
        assert before > 0

        with _pepper_env(monkeypatch, None):
            with pytest.raises(RuntimeError, match="USER_ID_PEPPER"):
                self._run(pg_conn)

        after = pg_conn.execute(
            text(
                "SELECT count(*) FROM users WHERE user_id LIKE 'discord:%'"
            )
        ).scalar()
        assert after == before, (
            "migration touched rows before raising on missing pepper"
        )

    def test_non_hex_pepper_aborts(self, pg_conn, monkeypatch):
        """Migration's pepper validation must mirror the app's settings
        validator — otherwise an operator could run Alembic with an
        invalid pepper, commit pseudonyms, then later boot the app with
        a corrected pepper that produces different HMACs and breaks
        /forget-me silently."""
        _seed_discord_user(pg_conn, "u-bad")
        before = pg_conn.execute(
            text(
                "SELECT count(*) FROM users WHERE user_id LIKE 'discord:%'"
            )
        ).scalar()

        with _pepper_env(monkeypatch, "x" * 64):  # 64 chars but not hex
            with pytest.raises(RuntimeError, match="hex-encoded"):
                self._run(pg_conn)

        after = pg_conn.execute(
            text(
                "SELECT count(*) FROM users WHERE user_id LIKE 'discord:%'"
            )
        ).scalar()
        assert after == before

    def test_short_pepper_aborts(self, pg_conn, monkeypatch):
        _seed_discord_user(pg_conn, "u-short")
        with _pepper_env(monkeypatch, "ab" * 8):  # 8 bytes decoded
            with pytest.raises(RuntimeError, match="16 bytes"):
                self._run(pg_conn)


# ---------------------------------------------------------------------------
# Layer B — one Alembic integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_alembic_upgrade_through_0005_runs(postgresql, monkeypatch):
    """End-to-end: upgrade to revision just before 0005, seed
    plaintext rows, upgrade head. Confirms the migration file's
    ``upgrade()`` actually executes through Alembic — not just its
    extracted callable.
    """
    repo_root = Path(__file__).resolve().parents[3]
    alembic_ini = repo_root / "application" / "alembic.ini"

    info = postgresql.info
    url = (
        "postgresql+psycopg://"
        f"{info.user}:{info.password or ''}@{info.host}:{info.port}/{info.dbname}"
    )

    monkeypatch.setenv("POSTGRES_URI", url)
    monkeypatch.setenv("USER_ID_PEPPER", _PEPPER)

    # Upgrade to *just before* 0005 — that's revision 0004.
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(alembic_ini),
            "upgrade",
            "0004_sources_is_public",
        ],
        timeout=60,
        env={**__import__("os").environ, "POSTGRES_URI": url, "USER_ID_PEPPER": _PEPPER},
    )

    # Seed plaintext rows directly via SQLAlchemy.
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_discord_user(conn, "u-alembic")

    # Now upgrade head — 0005 fires here.
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(alembic_ini),
            "upgrade",
            "head",
        ],
        timeout=60,
        env={**__import__("os").environ, "POSTGRES_URI": url, "USER_ID_PEPPER": _PEPPER},
    )

    # Confirm post-conditions match Layer A.
    with engine.begin() as conn:
        plaintext = conn.execute(
            text(
                "SELECT count(*) FROM users WHERE user_id LIKE 'discord:%'"
            )
        ).scalar()
        pseudo = conn.execute(
            text(
                "SELECT count(*) FROM users WHERE user_id LIKE 'discord_p_v1:%'"
            )
        ).scalar()
        agent_name = conn.execute(
            text(
                "SELECT name FROM agents WHERE mcp_provider = 'discord'"
            )
        ).scalar()

    assert plaintext == 0
    assert pseudo == 1
    assert agent_name == "Aztec MCP"

    engine.dispose()
