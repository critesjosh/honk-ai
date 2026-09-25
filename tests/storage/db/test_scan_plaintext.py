"""Tests for scripts/db/scan_plaintext.py — the at-rest plaintext verifier."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

# scripts/ isn't on sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.db.scan_plaintext import _iter_leaf_strings, _scan_field  # noqa: E402

from application.core import settings as settings_mod  # noqa: E402
from application.security import content_encryption as ce  # noqa: E402
from application.security import content_registry as cr  # noqa: E402
from application.storage.db.repositories.conversations import ConversationsRepository  # noqa: E402
from application.storage.db.repositories.user_logs import UserLogsRepository  # noqa: E402

_PROMPT_SPEC = {"mode": "text"}
_DATA_SPEC = {"mode": "json_blob"}
_META_SPEC = {"mode": "json_keep", "keep_keys": cr.STRUCTURAL_METADATA_ALLOWLIST}


@pytest.fixture
def encrypted(monkeypatch):
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


@pytest.mark.unit
def test_iter_leaf_strings_wildcard():
    obj = {"compression_points": [{"compressed_summary": "a"}, {"compressed_summary": "b"}]}
    assert list(_iter_leaf_strings(obj, ["compression_points", "*", "compressed_summary"])) == ["a", "b"]
    assert list(_iter_leaf_strings({"x": 1}, ["missing"])) == []


class TestScanField:
    def test_detects_plaintext_text_column(self, pg_conn, encrypted):
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("u", "c")
        repo.append_message(conv["id"], {"prompt": "encrypted q", "response": "a"})  # encrypted
        # Inject a legacy plaintext row directly.
        pg_conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, user_id, position, prompt) "
                "VALUES (CAST(:c AS uuid), 'u', 1, :p)"
            ),
            {"c": conv["id"], "p": "leaked plaintext"},
        )
        assert _scan_field(pg_conn, "conversation_messages", "prompt", _PROMPT_SPEC) == 1

    def test_plaintext_masquerading_as_envelope_is_flagged(self, pg_conn, encrypted):
        """A row whose prompt merely *looks* like an envelope must FAIL the scan."""
        conv = ConversationsRepository(pg_conn).create("u", "c")
        pg_conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, user_id, position, prompt) "
                "VALUES (CAST(:c AS uuid), 'u', 0, :p)"
            ),
            {"c": conv["id"], "p": "honkenc:1:v1:g256:bm90LXJlYWw="},  # shaped like ciphertext, isn't
        )
        assert _scan_field(pg_conn, "conversation_messages", "prompt", _PROMPT_SPEC) == 1

    def test_clean_when_all_encrypted(self, pg_conn, encrypted):
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("u", "c")
        repo.append_message(conv["id"], {"prompt": "q", "response": "a"})
        assert _scan_field(pg_conn, "conversation_messages", "prompt", _PROMPT_SPEC) == 0

    def test_json_blob_plaintext_detected(self, pg_conn, encrypted):
        # Raw plaintext JSON into user_logs.data (bypasses the encrypting repo).
        pg_conn.execute(
            text("INSERT INTO user_logs (user_id, endpoint, data) VALUES ('u', '/x', CAST(:d AS jsonb))"),
            {"d": '{"question": "leaked"}'},
        )
        assert _scan_field(pg_conn, "user_logs", "data", _DATA_SPEC) == 1
        # An encrypted insert via the repo adds no violation.
        UserLogsRepository(pg_conn).insert(user_id="u", endpoint="/x", data={"question": "safe"})
        assert _scan_field(pg_conn, "user_logs", "data", _DATA_SPEC) == 1

    def test_json_keep_flags_non_structural_plaintext_key(self, pg_conn, encrypted):
        conv = ConversationsRepository(pg_conn).create("u", "c")
        # Structural-only metadata = clean.
        pg_conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, user_id, position, message_metadata) "
                "VALUES (CAST(:c AS uuid), 'u', 0, CAST(:m AS jsonb))"
            ),
            {"c": conv["id"], "m": '{"type": "ai", "citation_filter": {}}'},
        )
        assert _scan_field(pg_conn, "conversation_messages", "message_metadata", _META_SPEC) == 0
        # A plaintext content key (search_query) at top level = violation.
        pg_conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, user_id, position, message_metadata) "
                "VALUES (CAST(:c AS uuid), 'u', 1, CAST(:m AS jsonb))"
            ),
            {"c": conv["id"], "m": '{"type": "ai", "search_query": "leaked rephrase"}'},
        )
        assert _scan_field(pg_conn, "conversation_messages", "message_metadata", _META_SPEC) == 1
