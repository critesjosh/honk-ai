"""Tests for application/security/content_encryption.py.

Round-trip + envelope + AAD-binding + idempotency + legacy-read behavior for the
content-at-rest codec. See PLAN-content-encryption.md.
"""

import base64

import pytest
from application.security import content_encryption as ce
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@pytest.fixture
def codec(monkeypatch):
    """Configure a real, derived keyring and reset the process cache."""
    monkeypatch.setattr(ce.settings, "ENCRYPTION_SECRET_KEY", "x" * 40, raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_ACTIVE_KID", "v1", raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_KEYS", "", raising=False)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_LEGACY_READ", True, raising=False)
    ce.reset_keyring_cache()
    yield ce
    ce.reset_keyring_cache()


@pytest.mark.unit
def test_text_round_trip(codec):
    enc = codec.encrypt_text("how do I deploy a contract?", table="conversation_messages", column="prompt")
    assert enc.startswith("honkenc:1:v1:g256:")
    assert codec.is_encrypted(enc)
    assert codec.decrypt_text(enc, table="conversation_messages", column="prompt") == "how do I deploy a contract?"


@pytest.mark.unit
def test_none_passthrough(codec):
    assert codec.encrypt_text(None, table="t", column="c") is None
    assert codec.decrypt_text(None, table="t", column="c") is None
    assert codec.encrypt_json_blob(None, table="t", column="c") is None
    assert codec.decrypt_json_blob(None, table="t", column="c") is None


@pytest.mark.unit
def test_encrypt_is_idempotent(codec):
    once = codec.encrypt_text("hello", table="t", column="c")
    twice = codec.encrypt_text(once, table="t", column="c")
    assert once == twice  # already-encrypted returned unchanged


@pytest.mark.unit
def test_json_blob_round_trip(codec):
    value = {"b": 2, "a": [1, 2, {"nested": "secret"}]}
    enc = codec.encrypt_json_blob(value, table="user_logs", column="data")
    assert set(enc.keys()) == {"__enc__"}
    assert codec.is_encrypted(enc)
    assert codec.decrypt_json_blob(enc, table="user_logs", column="data") == value


@pytest.mark.unit
def test_json_leaves_encrypts_only_named_leaf(codec):
    value = {"type": "compression_summary", "text": "the user asked about X"}
    enc = codec.encrypt_json_leaves(
        value, table="conversation_messages", column="message_metadata", leaf_paths=["text"]
    )
    assert enc["type"] == "compression_summary"  # structural key untouched
    assert codec.is_encrypted(enc["text"])  # content leaf encrypted
    dec = codec.decrypt_json_leaves(enc, table="conversation_messages", column="message_metadata", leaf_paths=["text"])
    assert dec == value


@pytest.mark.unit
def test_json_leaves_wildcard_over_list(codec):
    value = {"compression_points": [{"compressed_summary": "s1"}, {"compressed_summary": "s2"}]}
    enc = codec.encrypt_json_leaves(
        value,
        table="conversations",
        column="compression_metadata",
        leaf_paths=["compression_points.*.compressed_summary"],
    )
    assert all(codec.is_encrypted(p["compressed_summary"]) for p in enc["compression_points"])
    dec = codec.decrypt_json_leaves(
        enc,
        table="conversations",
        column="compression_metadata",
        leaf_paths=["compression_points.*.compressed_summary"],
    )
    assert dec == value


@pytest.mark.unit
def test_json_keep_encrypts_non_structural_keys(codec):
    value = {"type": "ai", "citation_filter": {"strategy": "topk"}, "search_query": "rephrased user q"}
    enc = codec.encrypt_json_keep_keys(
        value, table="conversation_messages", column="message_metadata", keep_keys={"type", "citation_filter"}
    )
    assert enc["type"] == "ai"  # structural kept
    assert enc["citation_filter"] == {"strategy": "topk"}  # structural kept (even though a dict)
    assert "search_query" not in enc  # content moved into the sentinel
    assert codec.is_encrypted(enc)
    dec = codec.decrypt_json_keep_keys(enc, table="conversation_messages", column="message_metadata")
    assert dec == value


@pytest.mark.unit
def test_json_keep_all_structural_is_noop(codec):
    value = {"type": "ai", "citation_filter": {"strategy": "topk"}}
    enc = codec.encrypt_json_keep_keys(
        value, table="conversation_messages", column="message_metadata", keep_keys={"type", "citation_filter"}
    )
    assert enc == value  # nothing to encrypt → byte-identical
    assert codec.decrypt_json_keep_keys(enc, table="conversation_messages", column="message_metadata") == value


@pytest.mark.unit
def test_json_keep_is_idempotent(codec):
    value = {"type": "ai", "search_query": "q"}
    once = codec.encrypt_json_keep_keys(
        value, table="conversation_messages", column="message_metadata", keep_keys={"type"}
    )
    twice = codec.encrypt_json_keep_keys(
        once, table="conversation_messages", column="message_metadata", keep_keys={"type"}
    )
    assert once == twice


@pytest.mark.unit
def test_json_blob_tolerates_datetime(codec):
    """user_logs.data carries a 'timestamp'; encrypt must not choke on non-JSON types."""
    import datetime

    value = {"question": "q", "ts": datetime.datetime(2026, 6, 29, 12, 0, 0)}
    enc = codec.encrypt_json_blob(value, table="user_logs", column="data")
    dec = codec.decrypt_json_blob(enc, table="user_logs", column="data")
    assert dec["question"] == "q"
    assert dec["ts"] == "2026-06-29 12:00:00"  # serialized via default=str


@pytest.mark.unit
def test_plaintext_masquerading_as_envelope_is_encrypted(codec):
    """A user value that merely *looks* like an envelope must NOT be mistaken for
    ciphertext — it gets encrypted, and is_valid_envelope rejects it."""
    fake = "honkenc:1:v1:g256:bm90LXJlYWwtY2lwaGVydGV4dA=="  # well-formed shape, bogus payload
    assert codec.is_valid_envelope(fake, table="conversation_messages", column="prompt") is False
    enc = codec.encrypt_text(fake, table="conversation_messages", column="prompt")
    assert enc != fake  # actually encrypted, not skipped as "already encrypted"
    assert codec.is_valid_envelope(enc, table="conversation_messages", column="prompt") is True
    assert codec.decrypt_text(enc, table="conversation_messages", column="prompt") == fake


@pytest.mark.unit
def test_json_blob_with_extra_plaintext_key_is_not_valid(codec):
    """{__enc__: valid, leak: ...} must NOT pass as encrypted — exactly {__enc__} only."""
    real = codec.encrypt_json_blob({"a": 1}, table="user_logs", column="data")
    smuggled = {**real, "leak": "plaintext"}
    assert codec.is_valid_envelope(smuggled, table="user_logs", column="data") is False
    # Re-encrypting wraps the whole thing (the leak goes inside the ciphertext).
    enc = codec.encrypt_json_blob(smuggled, table="user_logs", column="data")
    assert set(enc.keys()) == {"__enc__"}
    assert codec.decrypt_json_blob(enc, table="user_logs", column="data") == smuggled
    # And decrypt refuses a malformed blob (sentinel + extra key) rather than
    # silently dropping the smuggled plaintext.
    with pytest.raises(ce.ContentDecryptError):
        codec.decrypt_json_blob(smuggled, table="user_logs", column="data")


@pytest.mark.unit
def test_json_keep_tampered_sentinel_raises(codec):
    """A tampered (envelope-shaped) message_metadata sentinel must raise, not pass through."""
    enc = codec.encrypt_json_keep_keys(
        {"type": "ai", "search_query": "q"},
        table="conversation_messages",
        column="message_metadata",
        keep_keys={"type"},
    )
    prefix, payload = enc["__enc__"].rsplit(":", 1)
    raw = bytearray(base64.urlsafe_b64decode(payload))
    raw[-1] ^= 0x01
    enc["__enc__"] = prefix + ":" + base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(ce.ContentDecryptError):
        codec.decrypt_json_keep_keys(enc, table="conversation_messages", column="message_metadata")


@pytest.mark.unit
def test_real_envelope_is_idempotent_via_validation(codec):
    enc = codec.encrypt_text("real", table="conversation_messages", column="prompt")
    assert codec.encrypt_text(enc, table="conversation_messages", column="prompt") == enc


@pytest.mark.unit
def test_json_keep_literal_enc_key_is_treated_as_content(codec):
    """A row carrying a literal __enc__ that isn't a valid envelope must not be
    accepted as encrypted — it's encrypted as content."""
    value = {"type": "ai", "__enc__": "not-an-envelope", "search_query": "leak"}
    # The whole dict isn't a valid blob (json_keep mixes structural keys), and a
    # bogus literal __enc__ string is not a valid envelope.
    assert codec.is_valid_envelope(value, table="conversation_messages", column="message_metadata") is False
    enc = codec.encrypt_json_keep_keys(
        value, table="conversation_messages", column="message_metadata", keep_keys={"type"}
    )
    assert enc["type"] == "ai"  # structural key preserved
    assert codec.is_valid_envelope(enc["__enc__"], table="conversation_messages", column="message_metadata") is True
    # Round-trips back to the original (the bogus __enc__ rode along as content).
    assert codec.decrypt_json_keep_keys(enc, table="conversation_messages", column="message_metadata") == value


@pytest.mark.unit
def test_json_leaves_missing_path_is_noop(codec):
    value = {"type": "ai"}  # no 'text' leaf on this row
    enc = codec.encrypt_json_leaves(value, table="t", column="c", leaf_paths=["text"])
    assert enc == value


@pytest.mark.unit
def test_aad_binds_to_table_and_column(codec):
    """Ciphertext for one column must not decrypt under another column's AAD."""
    enc = codec.encrypt_text("secret", table="conversation_messages", column="prompt")
    with pytest.raises(ce.ContentDecryptError):
        codec.decrypt_text(enc, table="conversation_messages", column="response")
    with pytest.raises(ce.ContentDecryptError):
        codec.decrypt_text(enc, table="stack_logs", column="query")


@pytest.mark.unit
def test_tamper_is_detected(codec):
    enc = codec.encrypt_text("secret", table="t", column="c")
    prefix, payload = enc.rsplit(":", 1)
    raw = bytearray(base64.urlsafe_b64decode(payload))
    raw[-1] ^= 0x01  # flip a tag bit
    tampered = prefix + ":" + base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(ce.ContentDecryptError):
        codec.decrypt_text(tampered, table="t", column="c")


@pytest.mark.unit
def test_unknown_kid_raises(codec):
    enc = codec.encrypt_text("secret", table="t", column="c")
    forged = enc.replace(":v1:", ":v9:", 1)
    with pytest.raises(ce.ContentDecryptError):
        codec.decrypt_text(forged, table="t", column="c")


@pytest.mark.unit
def test_legacy_plaintext_read(codec, monkeypatch):
    # legacy reads on: plaintext passes through
    assert codec.decrypt_text("legacy plaintext", table="t", column="c") == "legacy plaintext"
    # legacy reads off: plaintext in a classified column raises (strict mode)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_LEGACY_READ", False)
    with pytest.raises(ce.ContentDecryptError):
        codec.decrypt_text("legacy plaintext", table="t", column="c")


@pytest.mark.unit
def test_derived_key_is_hkdf_of_secret(codec):
    """The v1 key is HKDF(ENCRYPTION_SECRET_KEY) — decrypting independently confirms it."""
    enc = codec.encrypt_text("hi", table="t", column="c")
    _, ver, kid, alg, payload = enc.split(":", 4)
    raw = base64.urlsafe_b64decode(payload)
    nonce, ct = raw[:12], raw[12:]
    key = ce._derive_key("v1", "x" * 40)
    assert AESGCM(key).decrypt(nonce, ct, ce._aad("t", "c", "v1")) == b"hi"


@pytest.mark.unit
def test_missing_key_source_fails_closed(codec, monkeypatch):
    monkeypatch.setattr(ce.settings, "ENCRYPTION_SECRET_KEY", ce._DEFAULT_SECRET)
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_KEYS", "")
    ce.reset_keyring_cache()
    with pytest.raises(ce.ContentEncryptionError):
        codec.encrypt_text("x", table="t", column="c")


@pytest.mark.unit
def test_explicit_keyring_entry(codec, monkeypatch):
    key_b64 = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_ACTIVE_KID", "v2")
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_KEYS", f"v2:{key_b64}")
    ce.reset_keyring_cache()
    enc = codec.encrypt_text("rotated", table="t", column="c")
    assert ":v2:" in enc
    assert codec.decrypt_text(enc, table="t", column="c") == "rotated"


# ---- empty-blob passthrough (forget-tombstone / cutover safety, finding A) ----


@pytest.mark.unit
def test_empty_json_blob_passthrough_on_encrypt(codec):
    # Empty defaults carry no content — left bare, never wrapped in a sentinel,
    # so encrypt/backfill/scan all agree "empty == no content".
    assert codec.encrypt_json_blob([], table="conversation_messages", column="tool_calls") == []
    assert codec.encrypt_json_blob({}, table="user_logs", column="data") == {}


@pytest.mark.unit
def test_empty_json_blob_reads_under_strict_mode(codec, monkeypatch):
    # The forget tombstone writes raw ``tool_calls = '[]'`` (bypassing the choke
    # point). After LEGACY_READ is turned off, reading it must NOT raise — else a
    # conversation with an erased turn becomes unreadable.
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_LEGACY_READ", False)
    assert codec.decrypt_json_blob([], table="conversation_messages", column="tool_calls") == []
    assert codec.decrypt_json_blob({}, table="user_logs", column="data") == {}
    # A non-empty plaintext blob still fails closed under strict reads.
    with pytest.raises(ce.ContentDecryptError):
        codec.decrypt_json_blob({"a": 1}, table="user_logs", column="data")


# ---- kid charset (envelope-header integrity, finding D) ----


@pytest.mark.unit
def test_active_kid_with_colon_raises_in_keyring(codec, monkeypatch):
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_ACTIVE_KID", "v1:bad")
    ce.reset_keyring_cache()
    with pytest.raises(ce.ContentEncryptionError, match="kid"):
        codec.encrypt_text("x", table="t", column="c")


@pytest.mark.unit
def test_explicit_kid_with_bad_charset_raises(codec, monkeypatch):
    key_b64 = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_KEYS", f"bad kid:{key_b64}")
    ce.reset_keyring_cache()
    with pytest.raises(ce.ContentEncryptionError, match="kid"):
        codec.encrypt_text("x", table="t", column="c")


@pytest.mark.unit
def test_active_kid_whitespace_normalized(codec, monkeypatch):
    # A stray space/newline in the env must not desync the keyring lookup from
    # the envelope header (boot validator strips; the codec must too). " v1 "
    # behaves exactly as "v1" — no deferred failure, header carries the bare kid.
    monkeypatch.setattr(ce.settings, "CONTENT_ENCRYPTION_ACTIVE_KID", " v1 ")
    ce.reset_keyring_cache()
    enc = codec.encrypt_text("hi", table="t", column="c")
    assert enc.startswith("honkenc:1:v1:g256:")  # stripped in the header
    assert codec.decrypt_text(enc, table="t", column="c") == "hi"


# ---- safe_decrypt_value (graceful degradation, finding E / PLAN §6) ----


@pytest.mark.unit
def test_safe_decrypt_value_never_raises(codec, monkeypatch):
    from application.security import content_registry as cr

    # Strict reads: a raw plaintext value would raise in decrypt_value ...
    monkeypatch.setattr(cr.settings, "CONTENT_ENCRYPTION_LEGACY_READ", False, raising=False)
    with pytest.raises(Exception):
        cr.decrypt_value("conversation_messages", "prompt", "raw plaintext")
    # ... but the safe wrapper swallows it and returns the per-column placeholder.
    assert cr.safe_decrypt_value("conversation_messages", "prompt", "raw plaintext", default=None) is None
    assert cr.safe_decrypt_value("conversation_messages", "tool_calls", "not-json", default=[]) == []


@pytest.mark.unit
def test_safe_decrypt_value_passes_through_valid(codec):
    from application.security import content_registry as cr

    enc = codec.encrypt_text("hello", table="conversation_messages", column="prompt")
    assert cr.safe_decrypt_value("conversation_messages", "prompt", enc, default=None) == "hello"
