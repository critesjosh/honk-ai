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
import json
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
# These are STRUCTURAL (not user content) and several are read/aggregated via raw
# SQL, so they must stay queryable: ``type`` (forget lookup, routes.py:501),
# ``citation_filter`` + ``is_clarification`` (read in-app), and
# ``literal_identifier_guard`` (``{mode,checked,corrected,scrubbed}`` counts —
# no addresses/content — aggregated by the honk-report skill). Everything ELSE in
# the dict (e.g. a rephrased ``search_query``) is content and is encrypted by the
# json_keep mode below. The scan asserts no un-encrypted non-allowlisted key
# remains. Adding a key here keeps it plaintext for NEW writes only — rows written
# before the change keep it inside the encrypted sentinel.
STRUCTURAL_METADATA_ALLOWLIST = {"type", "citation_filter", "is_clarification", "literal_identifier_guard"}

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


# --- Provenance-free decryption for authorized operator reads ------------------
#
# The normal read path decrypts at the repository ``_row_to_dict`` boundary,
# where the ``(table, column)`` is known. But operator surfaces — the MCP
# ``honk_sql`` tool and the honk-report export — read content via RAW SQL, so
# they never learn which registered column a value came from (aliases, joins,
# JSON sub-selects all erase it). These helpers decrypt WITHOUT that provenance
# by brute-forcing the registered AADs; AES-GCM authentication makes the match
# unambiguous. Use ONLY for already-authorized operator reads (the MCP role's
# grants + the host MCP auth are the access-control boundary, not these).


def _all_content_columns() -> list[tuple[str, str]]:
    return [(table, column) for table, cols in CONTENT_FIELDS.items() for column in cols]


# Content columns the ``docsgpt_mcp_ro`` role CANNOT read (migration 0006/0007
# audit policy): ``user_logs.data`` may carry bearer api_keys / response bodies,
# and ``pending_tool_state`` holds in-flight tool state. The operator decrypt
# helpers exclude these so they never decrypt a column the MCP least-privilege
# boundary deliberately withholds — keeping the decrypt surface in step with the
# SQL grants. Mirror the grants if either changes.
MCP_RO_DENIED_CONTENT: frozenset[tuple[str, str]] = frozenset(
    {("user_logs", "data")} | {("pending_tool_state", column) for column in CONTENT_FIELDS["pending_tool_state"]}
)


def operator_decrypt_candidates() -> frozenset[tuple[str, str]]:
    """The ``(table, column)`` AAD candidates an operator raw-SQL reader may
    decrypt: every content column EXCEPT :data:`MCP_RO_DENIED_CONTENT`. Pass this
    to :func:`decrypt_deep` from ``honk_sql`` / ``decrypt_export`` so they can't
    turn a secret-bearing blob into plaintext even with a privileged DB role."""
    return frozenset(c for c in _all_content_columns() if c not in MCP_RO_DENIED_CONTENT)


def decrypt_envelope_any_column(envelope, *, candidates=None) -> str | None:
    """Decrypt one ``honkenc:`` envelope without knowing its source column.

    Tries each candidate column's AAD (``table:column:kid:version``); a wrong AAD
    fails GCM integrity, so at most one authenticates and the match is
    unambiguous. ``candidates`` is an iterable of ``(table, column)`` — defaults
    to every registered content column; operator callers pass
    :func:`operator_decrypt_candidates` to exclude columns they aren't allowed to
    read. Returns the plaintext, or ``None`` when nothing authenticates (not our
    ciphertext, an excluded column, an unknown ``kid``, or the keyring is
    unavailable) so callers fall back to the original value and never crash."""
    if not isinstance(envelope, str) or not envelope.startswith(ce._PREFIX):
        return None
    cols = candidates if candidates is not None else _all_content_columns()
    for table, column in cols:
        try:
            return ce._decrypt_envelope(envelope, table, column)
        except Exception:  # noqa: BLE001 - wrong AAD / bad key / corrupt → try next column
            continue
    return None


def decrypt_deep(value, *, candidates=None):
    """Recursively decrypt every ``honkenc:`` envelope in a raw-SQL value.

    For AUTHORIZED operator reads only (MCP ``honk_sql`` / honk-report). Handles
    all registered storage shapes generically, mirroring the per-mode decoders:

    * bare envelope string (``text``) → plaintext;
    * whole-blob sentinel ``{"__enc__": <env>}`` (``json_blob``) → the parsed
      inner JSON (object/list);
    * ``json_keep`` dict (structural keys + sentinel) → structural keys merged
      with the decrypted content keys;
    * envelopes at nested leaves (``json_leaf``) → decrypted in place.

    ``candidates`` restricts which columns' AADs are tried (see
    :func:`decrypt_envelope_any_column`). Non-envelope values pass through
    unchanged (read-both); a value that only *looks* encrypted, or whose column
    is excluded, is left as-is."""
    if isinstance(value, str):
        if value.startswith(ce._PREFIX):
            plaintext = decrypt_envelope_any_column(value, candidates=candidates)
            return plaintext if plaintext is not None else value
        return value
    if isinstance(value, list):
        return [decrypt_deep(v, candidates=candidates) for v in value]
    if isinstance(value, dict):
        sentinel = value.get(ce._JSON_SENTINEL)
        if isinstance(sentinel, str) and sentinel.startswith(ce._PREFIX):
            plaintext = decrypt_envelope_any_column(sentinel, candidates=candidates)
            if plaintext is not None:
                try:
                    inner = json.loads(plaintext)
                except (ValueError, TypeError):
                    inner = plaintext
                others = {k: decrypt_deep(v, candidates=candidates) for k, v in value.items() if k != ce._JSON_SENTINEL}
                if isinstance(inner, dict):
                    return {**others, **inner}
                if others:  # structural keys beside a non-dict blob (unusual) — keep both
                    return {**others, ce._JSON_SENTINEL: inner}
                return inner
        return {k: decrypt_deep(v, candidates=candidates) for k, v in value.items()}
    return value
