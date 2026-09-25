"""Tests for the provenance-free operator decrypt helpers in content_registry.

``decrypt_envelope_any_column`` / ``decrypt_deep`` restore content visibility for
operator surfaces (the MCP ``honk_sql`` tool, the honk-report export) that read
encrypted columns via raw SQL and so lose the originating ``(table, column)``.
They brute-force the registered AADs; GCM authentication makes the match
unambiguous. See PLAN-content-encryption.md.
"""

import pytest

from application.security import content_encryption as ce
from application.security import content_registry as cr


@pytest.fixture
def codec(monkeypatch):
    """Real derived keyring (kid v1 off a real secret); reset process caches."""
    monkeypatch.setattr(ce.settings, "ENCRYPTION_SECRET_KEY", "x" * 40, raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_ENABLED", True, raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_ACTIVE_KID", "v1", raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_KEYS", "", raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_LEGACY_READ", True, raising=False)
    ce.reset_keyring_cache()
    yield ce
    ce.reset_keyring_cache()


@pytest.mark.unit
def test_any_column_finds_right_aad_without_provenance(codec):
    enc = codec.encrypt_text("secret prompt", table="conversation_messages", column="prompt")
    assert cr.decrypt_envelope_any_column(enc) == "secret prompt"


@pytest.mark.unit
def test_any_column_rejects_non_and_fake_envelopes(codec):
    assert cr.decrypt_envelope_any_column("just plain text") is None
    assert cr.decrypt_envelope_any_column(None) is None
    # looks like an envelope but doesn't authenticate under any AAD
    assert cr.decrypt_envelope_any_column("honkenc:1:v1:g256:" + "A" * 24) is None


@pytest.mark.unit
def test_deep_text(codec):
    enc = codec.encrypt_text("hello", table="conversation_messages", column="response")
    assert cr.decrypt_deep(enc) == "hello"


@pytest.mark.unit
def test_deep_json_blob_dict(codec):
    val = {"a": 1, "b": ["x", "y"], "nested": {"k": "v"}}
    enc = codec.encrypt_json_blob(val, table="user_logs", column="data")
    assert cr.decrypt_deep(enc) == val


@pytest.mark.unit
def test_deep_json_blob_list(codec):
    val = [{"name": "search", "args": {"q": "deploy"}}]
    enc = codec.encrypt_json_blob(val, table="conversation_messages", column="tool_calls")
    assert cr.decrypt_deep(enc) == val


@pytest.mark.unit
def test_deep_json_keep_merges_structural_and_content(codec):
    val = {"type": "ai", "citation_filter": {"strategy": "topk"}, "search_query": "rephrased q"}
    enc = codec.encrypt_json_keep_keys(
        val,
        table="conversation_messages",
        column="message_metadata",
        keep_keys={"type", "citation_filter", "is_clarification"},
    )
    assert "__enc__" in enc and "search_query" not in enc  # content hidden at rest
    assert cr.decrypt_deep(enc) == val  # structural + content reunited


@pytest.mark.unit
def test_deep_json_leaf(codec):
    val = {"question": "what is noir?", "action": "api_search", "agent_id": "a1"}
    enc = codec.encrypt_json_leaves(val, table="user_logs", column="metadata", leaf_paths=["question"])
    assert cr.decrypt_deep(enc) == val


@pytest.mark.unit
def test_deep_passthrough_non_envelope(codec):
    assert cr.decrypt_deep("plain") == "plain"
    assert cr.decrypt_deep(123) == 123
    assert cr.decrypt_deep(None) is None
    assert cr.decrypt_deep(True) is True
    assert cr.decrypt_deep({"a": "b", "n": 1}) == {"a": "b", "n": 1}
    assert cr.decrypt_deep(["a", 1, None]) == ["a", 1, None]


@pytest.mark.unit
def test_deep_row_shape_mixed(codec):
    """A SQL row: a list of column values, some encrypted, some not."""
    p = codec.encrypt_text("q1", table="conversation_messages", column="prompt")
    r = codec.encrypt_text("a1", table="conversation_messages", column="response")
    row = ["widget", p, r, None, {"thumbs": "down"}]
    assert cr.decrypt_deep(row) == ["widget", "q1", "a1", None, {"thumbs": "down"}]


@pytest.mark.unit
def test_deep_masquerade_left_untouched(codec):
    fake = "honkenc:1:v1:g256:" + "A" * 24  # shaped like an envelope, not authentic
    assert cr.decrypt_deep(fake) == fake
    assert cr.decrypt_deep({"__enc__": fake, "type": "ai"}) == {"__enc__": fake, "type": "ai"}


@pytest.mark.unit
def test_operator_candidates_exclude_secret_columns(codec):
    cands = cr.operator_decrypt_candidates()
    assert ("user_logs", "data") not in cands
    assert not any(t == "pending_tool_state" for (t, _c) in cands)
    assert ("conversation_messages", "prompt") in cands and ("user_logs", "metadata") in cands

    # user_logs.data is denied to the MCP role → operator decrypt leaves it ciphertext
    secret = codec.encrypt_json_blob({"api_key": "k", "response": "r"}, table="user_logs", column="data")
    assert cr.decrypt_deep(secret, candidates=cands) == secret
    # ...but an allowed column still decrypts
    md = codec.encrypt_json_leaves({"question": "q"}, table="user_logs", column="metadata", leaf_paths=["question"])
    assert cr.decrypt_deep(md, candidates=cands) == {"question": "q"}
    # and with no restriction (default), the same blob DOES decrypt
    assert cr.decrypt_deep(secret) == {"api_key": "k", "response": "r"}


@pytest.mark.unit
def test_deep_unconfigured_keyring_returns_ciphertext(codec, monkeypatch):
    """If the keyring can't build (no key), operator reads degrade to ciphertext
    rather than crashing the query."""
    enc = codec.encrypt_text("hi", table="conversation_messages", column="prompt")
    monkeypatch.setattr(ce.settings, "ENCRYPTION_SECRET_KEY", ce._DEFAULT_SECRET)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_KEYS", "")
    ce.reset_keyring_cache()
    assert cr.decrypt_deep(enc) == enc  # unchanged, no exception
