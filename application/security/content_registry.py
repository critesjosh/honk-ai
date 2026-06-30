"""Single source of truth for which columns hold user *content* and how each is
encrypted at rest (see PLAN-content-encryption.md).

The repositories call :func:`encrypt_value` / :func:`decrypt_value` with a
``(table, column)`` pair; the mode (text / whole-blob JSONB / named-leaf JSONB)
and any leaf paths live here, NOT in the repo SQL. The backfill and the
``scan_plaintext`` verifier enumerate the same registry, so a new content sink
is added in exactly one place and the verifier fails loudly if a classified
column is left plaintext.

**Gating:** *encryption* is gated on ``settings.CONTENT_ENCRYPTION_ENABLED`` (off
→ plaintext passthrough, so rollout can deploy the wiring before flipping it on).
*Decryption* is NOT gated — it always handles envelopes (read-both), so reads
keep working regardless of the flag once data is encrypted; a legacy-plaintext
value passes through untouched (no keyring needed unless an envelope is seen).

``conversation_messages.message_metadata`` uses the ``json_keep`` mode: the
structural keys in :data:`STRUCTURAL_METADATA_ALLOWLIST` (``type`` —
SQL-queried by the forget lookup; ``citation_filter``/``is_clarification`` —
read in-app) stay plaintext, and every other (content) key is encrypted under
the sentinel. So a content key like ``search_query`` is encrypted automatically
without enumerating content keys, and the scan additionally fails on any
non-structural plaintext key.
"""

import hashlib
import hmac as _hmac
import logging

from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from application.core.settings import settings
from application.security import content_encryption as ce

logger = logging.getLogger(__name__)

# Blind-index key for credential lookups (Class C). Once ``user_logs.data`` is a
# whole-blob ciphertext, the legacy ``data->>'api_key'`` filter can't reach the
# key, so equality lookups go through a deterministic HMAC fingerprint instead.
# Derived from ENCRYPTION_SECRET_KEY under a namespace distinct from both the
# content key and USER_ID_PEPPER. See PLAN-content-encryption.md §8.
_FP_NAMESPACE = b"honk-apikey-fingerprint-v1"
_fp_key: bytes | None = None


def _fingerprint_key() -> bytes:
    global _fp_key
    if _fp_key is None:
        _fp_key = HKDF(algorithm=SHA256(), length=32, salt=None, info=_FP_NAMESPACE).derive(
            (settings.ENCRYPTION_SECRET_KEY or "").encode("utf-8")
        )
    return _fp_key


def reset_fingerprint_cache() -> None:
    """Drop the cached fingerprint key (tests that mutate the secret call this)."""
    global _fp_key
    _fp_key = None


def api_key_fingerprint(api_key) -> str | None:
    """Deterministic HMAC-SHA256 hex of a bearer ``api_key`` for blind-index
    lookups. ``None``/empty → ``None`` (anonymous rows have no key)."""
    if not api_key:
        return None
    return _hmac.new(_fingerprint_key(), str(api_key).encode("utf-8"), hashlib.sha256).hexdigest()


# Keys allowed to remain plaintext in conversation_messages.message_metadata.
# ``type`` is read via SQL by the forget lookup (routes.py:501); the others are
# read in-app and are structural, not user content. Everything ELSE in the dict
# (e.g. ``search_query`` — a rephrased user query) is encrypted by the json_keep
# mode below. The scan also asserts no un-encrypted non-allowlisted key remains.
STRUCTURAL_METADATA_ALLOWLIST = {"type", "citation_filter", "is_clarification"}

# table -> column -> {"mode": ..., "leaf_paths"/"keep_keys": ...}
CONTENT_FIELDS: dict[str, dict[str, dict]] = {
    "conversation_messages": {
        "prompt": {"mode": "text"},
        "response": {"mode": "text"},
        "thought": {"mode": "text"},
        "tool_calls": {"mode": "json_blob"},
        # Open dict: structural keys stay plaintext, all other (content) keys
        # are encrypted under the sentinel. See encrypt_json_keep_keys.
        "message_metadata": {"mode": "json_keep", "keep_keys": STRUCTURAL_METADATA_ALLOWLIST},
    },
    "conversations": {
        "name": {"mode": "text"},
        "compression_metadata": {
            "mode": "json_leaf",
            "leaf_paths": ["compression_points.*.compressed_summary"],
        },
    },
    "user_logs": {
        "data": {"mode": "json_blob"},
        # Only the user's search question is content; the rest of metadata
        # (action / agent_id / counts) is structural and MCP-role-readable.
        "metadata": {"mode": "json_leaf", "leaf_paths": ["question"]},
    },
    "stack_logs": {
        "query": {"mode": "text"},
        "stacks": {"mode": "json_blob"},
    },
    "pending_tool_state": {
        "messages": {"mode": "json_blob"},
        "pending_tool_calls": {"mode": "json_blob"},
        "tools_dict": {"mode": "json_blob"},
        "tool_schemas": {"mode": "json_blob"},
        "agent_config": {"mode": "json_blob"},
        "client_tools": {"mode": "json_blob"},
    },
}


def _spec(table: str, column: str) -> dict:
    try:
        return CONTENT_FIELDS[table][column]
    except KeyError as exc:
        raise KeyError(f"{table}.{column} is not a registered content field") from exc


def encrypt_value(table: str, column: str, value):
    """Encrypt a content value per its registered mode. Plaintext passthrough
    when content encryption is disabled (rollout pre-flip)."""
    if not settings.CONTENT_ENCRYPTION_ENABLED:
        return value
    spec = _spec(table, column)
    mode = spec["mode"]
    if mode == "text":
        return ce.encrypt_text(value, table=table, column=column)
    if mode == "json_blob":
        return ce.encrypt_json_blob(value, table=table, column=column)
    if mode == "json_leaf":
        return ce.encrypt_json_leaves(value, table=table, column=column, leaf_paths=spec["leaf_paths"])
    if mode == "json_keep":
        return ce.encrypt_json_keep_keys(value, table=table, column=column, keep_keys=spec["keep_keys"])
    raise ValueError(f"unknown content mode {mode!r} for {table}.{column}")


def decrypt_value(table: str, column: str, value):
    """Decrypt a content value per its registered mode (read-both; never gated —
    a legacy-plaintext value passes through)."""
    spec = _spec(table, column)
    mode = spec["mode"]
    if mode == "text":
        return ce.decrypt_text(value, table=table, column=column)
    if mode == "json_blob":
        return ce.decrypt_json_blob(value, table=table, column=column)
    if mode == "json_leaf":
        return ce.decrypt_json_leaves(value, table=table, column=column, leaf_paths=spec["leaf_paths"])
    if mode == "json_keep":
        return ce.decrypt_json_keep_keys(value, table=table, column=column)
    raise ValueError(f"unknown content mode {mode!r} for {table}.{column}")


def safe_decrypt_value(table: str, column: str, value, *, default=None):
    """:func:`decrypt_value` that never raises (PLAN-content-encryption.md §6).

    A decrypt failure — corruption, a partially-completed key rotation, or a
    legacy-plaintext value seen while ``CONTENT_ENCRYPTION_LEGACY_READ`` is off —
    must NOT crash the answer path. Log the failure *class only* (never the
    bytes or the exception message — either could echo ciphertext) and return
    ``default`` so the surrounding read drops just that value/turn (like the
    ``erased_at`` filter) instead of collapsing the whole conversation to
    "not found". Used at the conversation read choke points
    (``_message_row_to_dict`` / ``_conversation_row_to_dict``)."""
    try:
        return decrypt_value(table, column, value)
    except Exception as exc:  # noqa: BLE001 - class only; never echo bytes/message
        logger.warning(
            "content decrypt failed for %s.%s (%s); substituting placeholder",
            table,
            column,
            type(exc).__name__,
        )
        return default


def encrypt_compression_point(point):
    """Encrypt the ``compressed_summary`` of a single compression point before it
    is pushed into ``conversations.compression_metadata.compression_points``.

    The leaf is at the point's *top level* here, but inside the stored column it
    lives at ``compression_points.*.compressed_summary``. The AAD is keyed on
    ``table:column`` only (not the leaf path), so encrypting here and decrypting
    via the column's registered leaf path round-trips. No-op when disabled."""
    if not settings.CONTENT_ENCRYPTION_ENABLED or point is None:
        return point
    return ce.encrypt_json_leaves(
        point, table="conversations", column="compression_metadata", leaf_paths=["compressed_summary"]
    )


def columns_by_mode(mode: str):
    """Yield ``(table, column, spec)`` for every registered field of ``mode`` —
    used by the scan and backfill to enumerate work without hardcoding."""
    for table, cols in CONTENT_FIELDS.items():
        for column, spec in cols.items():
            if spec["mode"] == mode:
                yield table, column, spec
