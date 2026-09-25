"""honk_sql decrypts encrypted content in its results.

The MCP SQL path reads via raw SQL (bypassing the repo decrypt boundary), so
``SQLExecutor.execute`` post-processes results with ``decrypt_deep`` to restore
operator content visibility. Here we stub psycopg so no live DB is needed.
"""

import pytest

from application.security import content_encryption as ce
from application.mcp_server.tools import sql as sql_mod
from application.mcp_server.tools.sql import SQLExecutor


@pytest.fixture
def keyring(monkeypatch):
    monkeypatch.setattr(ce.settings, "ENCRYPTION_SECRET_KEY", "x" * 40, raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_ENABLED", True, raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_ACTIVE_KID", "v1", raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_KEYS", "", raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_LEGACY_READ", True, raising=False)
    ce.reset_keyring_cache()
    yield
    ce.reset_keyring_cache()


class _Col:
    def __init__(self, name):
        self.name = name


class _FakeCursor:
    def __init__(self, rows, names):
        self._rows = rows
        self.description = [_Col(n) for n in names]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):  # SET LOCAL ... + the statement — all no-ops here
        return None

    def fetchmany(self, n):
        return self._rows


class _FakeConn:
    def __init__(self, rows, names):
        self._rows = rows
        self._names = names
        self.read_only = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self._rows, self._names)


def _patch_conn(monkeypatch, rows, names):
    monkeypatch.setattr(sql_mod.psycopg, "connect", lambda *a, **k: _FakeConn(rows, names))


@pytest.mark.unit
def test_execute_decrypts_content_columns(keyring, monkeypatch):
    prompt = ce.encrypt_text("how do I deploy?", table="conversation_messages", column="prompt")
    tool_calls = ce.encrypt_json_blob(
        [{"name": "search"}], table="conversation_messages", column="tool_calls"
    )
    _patch_conn(monkeypatch, rows=[["widget", prompt, tool_calls]], names=["surface", "prompt", "tool_calls"])

    out = SQLExecutor("postgresql://stub").execute("SELECT surface, prompt, tool_calls FROM conversation_messages")

    assert out["columns"] == ["surface", "prompt", "tool_calls"]
    assert out["rows"][0][0] == "widget"  # untouched
    assert out["rows"][0][1] == "how do I deploy?"  # decrypted text
    assert out["rows"][0][2] == [{"name": "search"}]  # decrypted blob → parsed JSON


@pytest.mark.unit
def test_execute_decrypt_false_leaves_ciphertext(keyring, monkeypatch):
    prompt = ce.encrypt_text("secret", table="conversation_messages", column="prompt")
    _patch_conn(monkeypatch, rows=[[prompt]], names=["prompt"])

    out = SQLExecutor("postgresql://stub").execute("SELECT prompt FROM conversation_messages", decrypt=False)

    assert out["rows"][0][0].startswith("honkenc:")  # opt-out keeps ciphertext


@pytest.mark.unit
def test_execute_does_not_decrypt_denied_columns(keyring, monkeypatch):
    # user_logs.data is excluded from operator decrypt candidates (the MCP role
    # can't read it anyway) — so even if a row carries it, honk_sql leaves it
    # ciphertext rather than acting as a decrypt oracle for secret-bearing blobs.
    data = ce.encrypt_json_blob({"api_key": "secret-bearer"}, table="user_logs", column="data")
    _patch_conn(monkeypatch, rows=[[data]], names=["data"])

    out = SQLExecutor("postgresql://stub").execute("SELECT data FROM user_logs")

    assert "__enc__" in out["rows"][0][0]
    assert "secret-bearer" not in str(out["rows"][0][0])


@pytest.mark.unit
def test_execute_passes_through_plaintext(keyring, monkeypatch):
    _patch_conn(monkeypatch, rows=[["legacy plaintext answer"]], names=["response"])

    out = SQLExecutor("postgresql://stub").execute("SELECT response FROM conversation_messages")

    assert out["rows"][0][0] == "legacy plaintext answer"  # read-both: plaintext untouched
