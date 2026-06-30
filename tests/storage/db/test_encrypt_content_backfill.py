"""Tests for scripts/db/encrypt_content_backfill.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.db.encrypt_content_backfill import _backfill_table  # noqa: E402

from application.core import settings as settings_mod  # noqa: E402
from application.security import content_encryption as ce  # noqa: E402
from application.security import content_registry as cr  # noqa: E402
from application.storage.db.repositories.conversations import ConversationsRepository  # noqa: E402
from application.storage.db.repositories.user_logs import UserLogsRepository  # noqa: E402


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


def _backfill(conn, table):
    return _backfill_table(conn, table, cr.CONTENT_FIELDS[table], batch=500, dry_run=False)


class TestBackfill:
    def test_encrypts_legacy_message_and_is_decryptable(self, pg_conn, encrypted):
        repo = ConversationsRepository(pg_conn)
        conv = repo.create("u", "c")
        # Legacy plaintext row written before encryption (raw insert).
        pg_conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, user_id, position, prompt, response) "
                "VALUES (CAST(:c AS uuid), 'u', 0, :p, :r)"
            ),
            {"c": conv["id"], "p": "legacy question", "r": "legacy answer"},
        )
        seen, updated = _backfill(pg_conn, "conversation_messages")
        assert seen == 1 and updated == 1
        # At rest: ciphertext now.
        raw = pg_conn.execute(
            text("SELECT prompt FROM conversation_messages WHERE conversation_id = CAST(:c AS uuid)"),
            {"c": conv["id"]},
        ).scalar()
        assert raw.startswith("honkenc:")
        # Reads back as the original plaintext.
        assert repo.get_messages(conv["id"])[0]["prompt"] == "legacy question"

    def test_idempotent(self, pg_conn, encrypted):
        conv = ConversationsRepository(pg_conn).create("u", "c")
        pg_conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, user_id, position, prompt) "
                "VALUES (CAST(:c AS uuid), 'u', 0, :p)"
            ),
            {"c": conv["id"], "p": "q"},
        )
        assert _backfill(pg_conn, "conversation_messages")[1] == 1
        # Second pass changes nothing.
        assert _backfill(pg_conn, "conversation_messages")[1] == 0

    def test_user_logs_fingerprint_populated_and_lookup_works(self, pg_conn, encrypted):
        # Legacy plaintext user_logs row (data carries api_key + question).
        pg_conn.execute(
            text("INSERT INTO user_logs (user_id, endpoint, data) VALUES ('u', '/stream', CAST(:d AS jsonb))"),
            {"d": '{"api_key": "agent-123", "question": "legacy q"}'},
        )
        seen, updated = _backfill(pg_conn, "user_logs")
        assert seen == 1 and updated == 1
        raw = pg_conn.execute(text("SELECT data, api_key_fp FROM user_logs")).fetchone()
        assert "__enc__" in raw[0]  # data encrypted
        assert raw[1] == cr.api_key_fingerprint("agent-123")  # fp backfilled
        # Lookup by the bearer key still finds the row (via fp) and decrypts.
        found = UserLogsRepository(pg_conn).find_by_api_key("agent-123")
        assert len(found) == 1 and found[0]["data"]["question"] == "legacy q"
