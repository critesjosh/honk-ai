"""Integration tests: content encryption through the repository layer.

Verifies the choke-point wiring (PLAN-content-encryption.md §7): with encryption
ENABLED, writes store ciphertext at rest, reads return plaintext, the
``api_key_fp`` blind-index lookup works once ``data`` is opaque, and legacy
plaintext rows still read back (read-both).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from application.core import settings as settings_mod
from application.security import content_encryption as ce
from application.security import content_registry as cr
from application.storage.db.repositories.conversations import ConversationsRepository
from application.storage.db.repositories.pending_tool_state import PendingToolStateRepository
from application.storage.db.repositories.stack_logs import StackLogsRepository
from application.storage.db.repositories.user_logs import UserLogsRepository


@pytest.fixture
def encrypted(monkeypatch):
    """Turn content encryption on with a real derived key for the duration."""
    monkeypatch.setattr(settings_mod.settings, "ENCRYPTION_SECRET_KEY", "x" * 40)
    monkeypatch.setattr(settings_mod.settings, "CONTENT_ENCRYPTION_ENABLED", True)
    monkeypatch.setattr(settings_mod.settings, "CONTENT_ENCRYPTION_ACTIVE_KID", "v1")
    monkeypatch.setattr(settings_mod.settings, "CONTENT_ENCRYPTION_KEYS", "")
    monkeypatch.setattr(settings_mod.settings, "CONTENT_ENCRYPTION_LEGACY_READ", True)
    ce.reset_keyring_cache()
    cr.reset_fingerprint_cache()
    yield
    ce.reset_keyring_cache()
    cr.reset_fingerprint_cache()


class TestConversationMessages:
    def test_message_round_trip_and_ciphertext_at_rest(self, pg_conn, encrypted):
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("user-1", "My Chat")
        repo.append_message(
            conv["id"],
            {
                "prompt": "how do I deploy?",
                "response": "use aztec-up",
                "thought": "reasoning",
                "tool_calls": [{"name": "search", "args": {"q": "deploy"}}],
            },
        )
        # Read path returns plaintext.
        msgs = repo.get_messages(conv["id"])
        assert msgs[0]["prompt"] == "how do I deploy?"
        assert msgs[0]["response"] == "use aztec-up"
        assert msgs[0]["thought"] == "reasoning"
        assert msgs[0]["tool_calls"] == [{"name": "search", "args": {"q": "deploy"}}]
        # Raw values at rest are ciphertext.
        row = pg_conn.execute(
            text(
                "SELECT prompt, response, thought, tool_calls FROM conversation_messages "
                "WHERE conversation_id = CAST(:c AS uuid)"
            ),
            {"c": conv["id"]},
        ).fetchone()
        assert row[0].startswith("honkenc:")
        assert row[1].startswith("honkenc:")
        assert row[2].startswith("honkenc:")
        assert "__enc__" in row[3]  # tool_calls jsonb sentinel

    def test_message_metadata_keeps_structural_encrypts_content(self, pg_conn, encrypted):
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("user-1", "c")
        repo.append_message(
            conv["id"],
            {
                "prompt": "q",
                "response": "a",
                "metadata": {"type": "ai", "citation_filter": {"strategy": "topk"}, "search_query": "rephrased q"},
            },
        )
        # Read returns the full metadata (decrypted).
        md = repo.get_messages(conv["id"])[0]["metadata"]
        assert md == {"type": "ai", "citation_filter": {"strategy": "topk"}, "search_query": "rephrased q"}
        # At rest: type stays plaintext + SQL-queryable; search_query is gone from plaintext.
        raw = pg_conn.execute(
            text(
                "SELECT message_metadata, message_metadata->>'type' t FROM conversation_messages "
                "WHERE conversation_id = CAST(:c AS uuid)"
            ),
            {"c": conv["id"]},
        ).fetchone()
        assert raw[1] == "ai"  # forget's ->>'type' lookup still works
        assert "__enc__" in raw[0]
        assert "rephrased q" not in str(raw[0])  # content not in plaintext at rest

    def test_conversation_name_encrypted_at_rest(self, pg_conn, encrypted):
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("user-1", "Secret Title")
        assert repo.get(conv["id"], "user-1")["name"] == "Secret Title"  # decrypts
        raw = pg_conn.execute(
            text("SELECT name FROM conversations WHERE id = CAST(:c AS uuid)"), {"c": conv["id"]}
        ).scalar()
        assert raw.startswith("honkenc:")


class TestUserLogs:
    def test_data_encrypted_and_api_key_fp_lookup(self, pg_conn, encrypted):
        repo = UserLogsRepository(pg_conn)
        repo.insert(
            user_id="local",
            endpoint="/stream",
            data={"api_key": "agent-uuid-123", "question": "what is noir?", "response": "a language"},
            metadata={"action": "api_search", "question": "what is noir?"},
        )
        # Lookup by api_key works even though `data` is an opaque blob (proves fp).
        rows = repo.find_by_api_key("agent-uuid-123")
        assert len(rows) == 1
        assert rows[0]["data"]["question"] == "what is noir?"  # decrypts
        assert rows[0]["data"]["response"] == "a language"
        # metadata.question encrypted; structural keys plaintext.
        assert rows[0]["metadata"]["question"] == "what is noir?"
        # Raw at rest: data is sentinel, metadata.question is ciphertext, action plaintext.
        raw = pg_conn.execute(
            text(
                "SELECT data, metadata->>'question' q, metadata->>'action' a, api_key_fp "
                "FROM user_logs WHERE user_id = 'local'"
            ),
        ).fetchone()
        assert "__enc__" in raw[0]
        assert raw[1].startswith("honkenc:")
        assert raw[2] == "api_search"
        assert raw[3] is not None and not raw[3].startswith("honkenc:")  # fp is a hex hash


class TestStackLogs:
    def test_query_and_stacks_encrypted_at_rest(self, pg_conn, encrypted):
        StackLogsRepository(pg_conn).insert(
            activity_id="act-1",
            endpoint="/stream",
            query="my secret prompt",
            stacks=[{"step": "retrieval", "detail": "found docs"}],
        )
        raw = pg_conn.execute(text("SELECT query, stacks FROM stack_logs WHERE activity_id = 'act-1'")).fetchone()
        assert raw[0].startswith("honkenc:")
        assert "__enc__" in raw[1]


class TestPendingToolState:
    def test_round_trip_and_ciphertext(self, pg_conn, encrypted):
        conv = ConversationsRepository(pg_conn).create("user-1", "c")
        repo = PendingToolStateRepository(pg_conn)
        repo.save_state(
            conv["id"],
            "user-1",
            messages=[{"role": "user", "content": "secret question"}],
            pending_tool_calls=[{"id": "1"}],
            tools_dict={"t": 1},
            tool_schemas=[{"s": 1}],
            agent_config={"model": "x"},
            client_tools=["ct"],
        )
        loaded = repo.load_state(conv["id"], "user-1")
        assert loaded["messages"] == [{"role": "user", "content": "secret question"}]
        assert loaded["client_tools"] == ["ct"]
        raw = pg_conn.execute(
            text("SELECT messages, client_tools FROM pending_tool_state WHERE conversation_id = CAST(:c AS uuid)"),
            {"c": conv["id"]},
        ).fetchone()
        assert "__enc__" in raw[0]
        assert "__enc__" in raw[1]


class TestReadBoth:
    def test_legacy_plaintext_message_still_reads(self, pg_conn, encrypted):
        """A row written before encryption (plaintext) must read back untouched."""
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("user-1", "c")
        # Insert a plaintext message directly, bypassing the encrypting repo write.
        pg_conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, user_id, position, prompt, response) "
                "VALUES (CAST(:c AS uuid), 'user-1', 0, :p, :r)"
            ),
            {"c": conv["id"], "p": "legacy plaintext q", "r": "legacy plaintext a"},
        )
        msgs = repo.get_messages(conv["id"])
        assert msgs[0]["prompt"] == "legacy plaintext q"
        assert msgs[0]["response"] == "legacy plaintext a"


class TestCutoverAndDegradation:
    """Strict-mode (LEGACY_READ=false) safety: the forget tombstone and corrupt
    rows must not brick conversation reads. Findings A + E."""

    def test_forget_tombstone_reads_under_strict_mode(self, pg_conn, encrypted, monkeypatch):
        """``/forget-me`` redacts via raw SQL (``tool_calls = '[]'``, bypassing the
        choke point). After the cutover flips LEGACY_READ off, reading the erased
        turn must still work, not raise."""
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("user-1", "c")
        repo.append_message(conv["id"], {"prompt": "q", "response": "a", "tool_calls": [{"name": "search"}]})
        # Simulate the forget tombstone: NULL content, bare empty defaults.
        pg_conn.execute(
            text(
                "UPDATE conversation_messages SET prompt = NULL, response = NULL, thought = NULL, "
                "tool_calls = '[]'::jsonb, message_metadata = '{}'::jsonb "
                "WHERE conversation_id = CAST(:c AS uuid)"
            ),
            {"c": conv["id"]},
        )
        monkeypatch.setattr(settings_mod.settings, "CONTENT_ENCRYPTION_LEGACY_READ", False)
        msgs = repo.get_messages(conv["id"])  # must not raise
        assert msgs[0]["tool_calls"] == []
        assert msgs[0]["prompt"] is None
        assert msgs[0]["metadata"] == {}

    def test_corrupt_ciphertext_degrades_to_placeholder(self, pg_conn, encrypted, monkeypatch):
        """A single undecryptable value drops to a placeholder; the rest of the
        conversation (and its other messages) still read — no whole-conversation
        collapse (PLAN §6)."""
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("user-1", "c")
        repo.append_message(conv["id"], {"prompt": "good q", "response": "good a"})
        # Corrupt only the prompt at rest: a well-formed envelope with a payload
        # that can't authenticate.
        pg_conn.execute(
            text(
                "UPDATE conversation_messages SET prompt = :bad WHERE conversation_id = CAST(:c AS uuid)"
            ),
            {"c": conv["id"], "bad": "honkenc:1:v1:g256:QUFBQQ"},
        )
        monkeypatch.setattr(settings_mod.settings, "CONTENT_ENCRYPTION_LEGACY_READ", False)
        msgs = repo.get_messages(conv["id"])  # must not raise
        assert msgs[0]["prompt"] is None  # placeholder
        assert msgs[0]["response"] == "good a"  # sibling value intact
