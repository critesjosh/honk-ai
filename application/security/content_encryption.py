"""Application-layer encryption-at-rest for user *content* columns.

Distinct from :mod:`application.security.encryption` (which protects stored tool
credentials with AES-CBC keyed per-``user_id``). This module exists so Honk AI
can attest to Discord's Developer Policy that stored user message content is
encrypted at rest, **without** relying on disk/volume encryption (the host EBS
volume is unencrypted and the operator has no AWS access).

Design (see ``PLAN-content-encryption.md``):

- **AES-256-GCM** (authenticated — content is long-lived, so tamper/corruption
  must be detectable; the credential helper's unauthenticated CBC is the wrong
  primitive here).
- **One app key per ``kid``**, derived once via HKDF-SHA256 and cached. No
  PBKDF2-per-row: a single answer save touches title + prompt + response +
  thought + logs + compression, so per-row key derivation would add real
  latency on the hot path.
- **Keyring + ``kid``** in every envelope so the key can be rotated (mirrors the
  ``_v1`` rotation seam in :mod:`application.pseudonyms`). The launch key
  ``v1`` is HKDF-derived from ``ENCRYPTION_SECRET_KEY`` so no new secret needs
  provisioning; additional/rotated keys may be supplied via
  ``CONTENT_ENCRYPTION_KEYS``.
- **AAD = ``table:column:kid:version``** (no row id — content is legitimately
  re-emitted within a column, e.g. compression summaries synthesized from prior
  turns, so binding to a row id would break decryption of copied content). The
  table/column AAD still prevents moving ciphertext between columns.
- **Self-describing envelope** ``honkenc:1:<kid>:g256:<b64url(nonce|ct|tag)>``
  so reads can tell encrypted from legacy-plaintext values during rollout, and
  JSONB whole-blob fields wrap it as ``{"__enc__": "honkenc:..."}``.

The functions are intentionally permissive on *read* (legacy plaintext passes
through while ``CONTENT_ENCRYPTION_LEGACY_READ`` is on) and strict on
misconfiguration at *boot* (the settings validator fails closed). A decrypt
failure raises :class:`ContentDecryptError`; callers on the answer path MUST
catch it, drop/placeholder the value, and never surface ciphertext or log the
plaintext/exception message (only the exception class).
"""

import base64
import json
import logging
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from application.core.settings import settings

logger = logging.getLogger(__name__)

# Envelope: honkenc:<envelope_version>:<kid>:<alg>:<b64url payload>
_PREFIX = "honkenc:"
_ENVELOPE_VERSION = "1"
_ALG = "g256"  # AES-256-GCM
_NONCE_LEN = 12  # 96-bit GCM nonce
# Sentinel wrapper for whole-blob JSONB columns (a structured value can't hold a
# bare envelope string, and a root sentinel is cheap for a DB CHECK to assert).
_JSON_SENTINEL = "__enc__"

_DEFAULT_SECRET = "default-docsgpt-encryption-key"  # the settings default; never a valid key source

# A kid is embedded verbatim in the colon-delimited envelope header
# (``honkenc:1:<kid>:g256:…``), so it must not contain ':' (or whitespace) or
# the parser in :func:`_decrypt_envelope` would mis-split it and silently render
# every value undecryptable. Validated at boot (settings) AND here (defense in
# depth: tests/tooling that mutate settings bypass the boot validator).
_KID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Process-lifetime keyring cache: {kid: 32-byte key}. Built lazily so importing
# this module never fails at import time (tests, tooling) — only first use.
_keyring: dict[str, bytes] | None = None


class ContentEncryptionError(Exception):
    """Configuration / keyring error (fail-closed, raised on encrypt)."""


class ContentDecryptError(Exception):
    """A value could not be decrypted (bad key, tamper, truncation).

    Never carries the plaintext or the underlying exception message — callers
    log only the class. The answer path treats this like an erased turn.
    """


def _validate_kid(kid: str) -> str:
    """Return ``kid`` if it is charset-safe for the envelope header, else raise."""
    if not kid or not _KID_RE.match(kid):
        raise ContentEncryptionError(
            f"invalid content-encryption kid {kid!r}: must match [A-Za-z0-9_.-] "
            "(no ':' — it delimits the envelope header)"
        )
    return kid


def _active_kid() -> str:
    """The configured active kid, whitespace-normalized + charset-validated.

    Stripped so a stray space/newline in the env or secret file (common in
    ``.env`` / mounted secrets) can't desync the boot validator (which strips)
    from the keyring lookup and the envelope header — which would otherwise
    derive under one kid and look up under another. Used everywhere the active
    kid is read (keyring build + envelope write) so the value is identical."""
    return _validate_kid((settings.CONTENT_ENCRYPTION_ACTIVE_KID or "").strip())


def _derive_key(kid: str, source_secret: str) -> bytes:
    """HKDF-SHA256 → 32-byte content key, domain-separated per ``kid``."""
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=None,
        info=f"honk-content-encryption-{kid}".encode("utf-8"),
    ).derive(source_secret.encode("utf-8"))


def _parse_explicit_keys(raw: str) -> dict[str, bytes]:
    """Parse ``CONTENT_ENCRYPTION_KEYS`` = ``kid:<b64-32B>,kid2:<b64-32B>``.

    Explicit keys are base64-encoded 32-byte secrets (for rotation to an
    independent key, distinct from the HKDF-derived launch key). Raises on a
    malformed entry — a typo here must fail closed, not silently drop a key.
    """
    out: dict[str, bytes] = {}
    for chunk in (c.strip() for c in raw.split(",")):
        if not chunk:
            continue
        kid, _, b64 = chunk.partition(":")
        kid = kid.strip()
        if not kid or not b64:
            raise ContentEncryptionError(f"malformed CONTENT_ENCRYPTION_KEYS entry: {kid!r}")
        _validate_kid(kid)
        try:
            key = base64.b64decode(b64.strip(), validate=True)
        except Exception as exc:  # noqa: BLE001 - message must not echo the key
            raise ContentEncryptionError(f"CONTENT_ENCRYPTION_KEYS[{kid}] is not valid base64") from exc
        if len(key) != 32:
            raise ContentEncryptionError(f"CONTENT_ENCRYPTION_KEYS[{kid}] must decode to 32 bytes")
        out[kid] = key
    return out


def _build_keyring() -> dict[str, bytes]:
    """Resolve the keyring: explicit keys ∪ the HKDF-derived launch key.

    The active kid is always present: explicit (if supplied) else HKDF-derived
    from ``ENCRYPTION_SECRET_KEY``. Older kids referenced by stored envelopes
    must be supplied explicitly (rotation) — a derived kid only covers the one
    derived from the current secret.
    """
    active = _active_kid()
    keyring = _parse_explicit_keys(settings.CONTENT_ENCRYPTION_KEYS or "")
    if active not in keyring:
        source = settings.ENCRYPTION_SECRET_KEY or ""
        if not source or source == _DEFAULT_SECRET:
            raise ContentEncryptionError(
                "content encryption needs a real ENCRYPTION_SECRET_KEY to derive the "
                f"active key {active!r} (or supply it in CONTENT_ENCRYPTION_KEYS)."
            )
        keyring[active] = _derive_key(active, source)
    return keyring


def _get_keyring() -> dict[str, bytes]:
    global _keyring
    if _keyring is None:
        _keyring = _build_keyring()
    return _keyring


def reset_keyring_cache() -> None:
    """Drop the cached keyring (tests that mutate settings call this)."""
    global _keyring
    _keyring = None


def _aad(table: str, column: str, kid: str) -> bytes:
    return f"{table}:{column}:{kid}:{_ENVELOPE_VERSION}".encode("utf-8")


def is_encrypted(value: object) -> bool:
    """Cheap structural check: does ``value`` *look* like an envelope (prefix /
    sentinel)? Use for fast filtering only — NOT as proof a value is encrypted
    (a plaintext value shaped like an envelope would pass). The verifier and the
    idempotency guards use :func:`is_valid_envelope`, which actually decrypts."""
    if isinstance(value, str):
        return value.startswith(_PREFIX)
    if isinstance(value, dict):
        inner = value.get(_JSON_SENTINEL)
        return isinstance(inner, str) and inner.startswith(_PREFIX)
    return False


def _decrypt_envelope(value: str, table: str, column: str) -> str:
    """Decrypt one envelope string under its AAD, or raise. No legacy passthrough."""
    _, ver, kid, alg, payload = value.split(":", 4)
    if ver != _ENVELOPE_VERSION or alg != _ALG:
        raise ValueError("unsupported envelope")
    raw = base64.urlsafe_b64decode(payload.encode("ascii"))
    nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    key = _get_keyring().get(kid)
    if key is None:
        raise KeyError(kid)
    return AESGCM(key).decrypt(nonce, ct, _aad(table, column, kid)).decode("utf-8")


def is_valid_envelope(value, *, table: str, column: str) -> bool:
    """True iff ``value`` is a content envelope that **actually decrypts** under
    this column's AAD (string envelope, or ``{"__enc__": <envelope>}`` sentinel).

    This is the cryptographic definition of "encrypted" — a plaintext value that
    merely starts with ``honkenc:`` or carries a literal ``__enc__`` key returns
    ``False``, so it can't masquerade as ciphertext to the verifier or escape
    re-encryption in the idempotency guards. Requires the keyring (real key)."""
    if isinstance(value, dict):
        # A whole-blob ciphertext is EXACTLY {"__enc__": <valid envelope>}. Any
        # extra key means there is unprotected plaintext riding alongside, so it
        # is NOT a valid encrypted blob. (json_keep, which legitimately mixes
        # structural keys with the sentinel, validates the sentinel string itself
        # rather than calling this on the whole dict.)
        if set(value) != {_JSON_SENTINEL}:
            return False
        return is_valid_envelope(value[_JSON_SENTINEL], table=table, column=column)
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return False
    try:
        _decrypt_envelope(value, table, column)
        return True
    except Exception:  # noqa: BLE001 - any failure means "not a valid envelope"
        return False


def encrypt_text(value: str | None, *, table: str, column: str) -> str | None:
    """Encrypt a text value → envelope string. ``None`` passes through.

    Idempotent: an already-encrypted value is returned unchanged so backfills
    and double-writes are safe.
    """
    if value is None:
        return None
    if isinstance(value, str) and is_valid_envelope(value, table=table, column=column):
        return value  # already validly encrypted — idempotent
    if not isinstance(value, str):
        raise ContentEncryptionError(f"encrypt_text expected str, got {type(value).__name__}")
    keyring = _get_keyring()
    kid = _active_kid()
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(keyring[kid]).encrypt(nonce, value.encode("utf-8"), _aad(table, column, kid))
    payload = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
    return f"{_PREFIX}{_ENVELOPE_VERSION}:{kid}:{_ALG}:{payload}"


def decrypt_text(value: str | None, *, table: str, column: str) -> str | None:
    """Decrypt an envelope string → plaintext.

    ``None`` → ``None``. A non-envelope (legacy plaintext) is returned as-is
    when ``CONTENT_ENCRYPTION_LEGACY_READ`` is on, else raises. A malformed or
    undecryptable envelope raises :class:`ContentDecryptError`.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        if settings.CONTENT_ENCRYPTION_LEGACY_READ:
            return value if isinstance(value, str) else None
        raise ContentDecryptError(f"plaintext in {table}.{column} with legacy reads disabled")
    try:
        return _decrypt_envelope(value, table, column)
    except Exception as exc:  # noqa: BLE001 - class only; never echo bytes/message
        raise ContentDecryptError(f"decrypt failed for {table}.{column}") from exc


def encrypt_json_blob(value, *, table: str, column: str):
    """Encrypt a whole JSONB value → ``{"__enc__": "<envelope>"}``.

    ``None`` passes through; an already-wrapped value is returned unchanged.
    The structure is serialized with sorted keys so the ciphertext is stable
    for a given input (aids idempotent backfills, though the random nonce means
    re-encrypting still changes bytes).
    """
    if value is None:
        return None
    if value in ([], {}):
        # Empty default / forget tombstone (e.g. raw ``tool_calls = '[]'``): no
        # content to protect. Leave it bare so encrypt/decrypt/backfill/scan all
        # agree "empty == no content" — otherwise a strict-mode read of a bare
        # ``[]`` (which the backfill+scan skip) would raise. See decrypt_json_blob.
        return value
    if is_valid_envelope(value, table=table, column=column):
        return value  # already validly encrypted — idempotent
    serialized = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str, ensure_ascii=False)
    return {_JSON_SENTINEL: encrypt_text(serialized, table=table, column=column)}


def decrypt_json_blob(value, *, table: str, column: str):
    """Inverse of :func:`encrypt_json_blob`.

    ``None`` → ``None``. A wrapped sentinel is decrypted + parsed. A legacy
    plaintext JSON value (not wrapped) passes through when legacy reads are on.
    """
    if value is None:
        return None
    if value in ([], {}):
        # Empty default / forget tombstone — no content, never ciphertext. Pass it
        # through even under strict reads (LEGACY_READ=false): the backfill+scan
        # treat empty as "no content", so the runtime read must too, else reading
        # a conversation with an erased turn (tool_calls='[]') would raise.
        return value
    if isinstance(value, dict) and _JSON_SENTINEL in value:
        # An encrypted blob is EXACTLY {"__enc__": <envelope>}. Any extra key means
        # unprotected plaintext is riding alongside the sentinel — refuse rather
        # than decrypt-and-silently-drop it.
        if set(value) != {_JSON_SENTINEL} or not isinstance(value[_JSON_SENTINEL], str):
            raise ContentDecryptError(f"malformed encrypted blob in {table}.{column}")
        plaintext = decrypt_text(value[_JSON_SENTINEL], table=table, column=column)
        return json.loads(plaintext) if plaintext is not None else None
    if settings.CONTENT_ENCRYPTION_LEGACY_READ:
        return value
    raise ContentDecryptError(f"plaintext JSON in {table}.{column} with legacy reads disabled")


def _walk_leaf(obj, path: list[str], fn):
    """Apply ``fn`` to the string leaf(ves) at dotted ``path`` within ``obj``.

    Supports a ``*`` segment to map over list elements (e.g.
    ``compression_points.*.compressed_summary``). Missing intermediate keys are
    a no-op (the structure simply doesn't carry that leaf on this row).
    """
    if obj is None or not path:
        return obj
    head, rest = path[0], path[1:]
    if head == "*":
        if isinstance(obj, list):
            return [_walk_leaf(item, rest, fn) for item in obj]
        return obj
    if not isinstance(obj, dict) or head not in obj:
        return obj
    if rest:
        obj[head] = _walk_leaf(obj[head], rest, fn)
    else:
        leaf = obj[head]
        if isinstance(leaf, str):
            obj[head] = fn(leaf)
    return obj


def encrypt_json_leaves(value, *, table: str, column: str, leaf_paths: list[str]):
    """Encrypt only the named string leaves; leave structure/other keys plaintext.

    Used for ``message_metadata`` / ``compression_metadata`` / ``metadata`` where
    structural keys (``type``, flags) must stay readable for forget lookups and
    analytics, but content leaves must be encrypted. ``leaf_paths`` are dotted,
    ``*`` maps over a list.
    """
    if value is None:
        return None
    out = json.loads(json.dumps(value, default=str))  # deep copy (default=str: tolerate datetimes)
    for path in leaf_paths:
        out = _walk_leaf(out, path.split("."), lambda s: encrypt_text(s, table=table, column=column))
    return out


def decrypt_json_leaves(value, *, table: str, column: str, leaf_paths: list[str]):
    """Inverse of :func:`encrypt_json_leaves`. Permissive on legacy plaintext leaves."""
    if value is None:
        return None
    out = json.loads(json.dumps(value, default=str))
    for path in leaf_paths:
        out = _walk_leaf(out, path.split("."), lambda s: decrypt_text(s, table=table, column=column))
    return out


def encrypt_json_keep_keys(value, *, table: str, column: str, keep_keys):
    """Keep allowlisted top-level keys plaintext; encrypt all remaining keys as one
    blob under the JSON sentinel.

    For *open* dicts like ``conversation_messages.message_metadata`` where a few
    structural keys must stay SQL-queryable (``type`` for the forget lookup,
    ``citation_filter``/``is_clarification`` read in-app) but arbitrary
    agent-supplied keys (e.g. ``search_query``, a rephrased user query) are
    content and must not leak. A new content key is encrypted automatically —
    no enumeration of content keys needed. No-op when nothing non-structural is
    present (so today's structural-only rows stay byte-identical)."""
    if value is None or not isinstance(value, dict):
        return value
    # Already in encrypted form iff the ONLY non-structural key is a valid
    # sentinel envelope. A bogus/plaintext __enc__, or any other non-structural
    # key, means it is not yet (fully) encrypted → fall through and encrypt.
    non_struct = {k for k in value if k not in keep_keys}
    if non_struct == {_JSON_SENTINEL} and is_valid_envelope(value[_JSON_SENTINEL], table=table, column=column):
        return value
    rest = {k: v for k, v in value.items() if k not in keep_keys}
    if not rest:
        return value
    kept = {k: v for k, v in value.items() if k in keep_keys}
    serialized = json.dumps(rest, separators=(",", ":"), sort_keys=True, default=str, ensure_ascii=False)
    kept[_JSON_SENTINEL] = encrypt_text(serialized, table=table, column=column)
    return kept


def decrypt_json_keep_keys(value, *, table: str, column: str):
    """Inverse of :func:`encrypt_json_keep_keys`: decrypt the sentinel blob and
    merge the content keys back. Legacy plaintext (no sentinel) passes through."""
    if value is None or not isinstance(value, dict):
        return value
    sentinel = value.get(_JSON_SENTINEL)
    # A literal, non-envelope __enc__ key isn't our sentinel — leave it as content.
    # But an envelope-SHAPED sentinel must decrypt: a tampered/corrupt one raises
    # (decrypt_text → ContentDecryptError) rather than leaking ciphertext through.
    if not isinstance(sentinel, str) or not sentinel.startswith(_PREFIX):
        return value
    out = {k: v for k, v in value.items() if k != _JSON_SENTINEL}
    plaintext = decrypt_text(sentinel, table=table, column=column)
    if plaintext is not None:
        out.update(json.loads(plaintext))
    return out
