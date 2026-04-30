"""HMAC-based pseudonymization for stored user identifiers.

Replaces direct plaintext provider IDs (e.g., ``discord:123456789012345678``)
with a deterministic HMAC over a server-side pepper. A database snapshot
without the pepper cannot be reversed back to the underlying provider
identity, while the bot/backend can still re-derive the same pseudonym
on every call to support ``/forget-me``, idempotent re-provisioning,
analytics aggregation, etc.

The format is **versioned by intent** — the ``_v1`` infix is a rotation
seam. If we ever change pepper, hash, or truncation, new IDs become
``_v2:`` and old + new can coexist while a backfill drains. Cheap to
add now; expensive to retrofit later.

Two output shapes:

- :func:`pseudonymize_provider_user_id` returns the **bare 32-char hex**
  HMAC. This is what ``agents.mcp_provider_user_id`` stores — no prefix
  on that column because the column itself only holds Discord (today)
  pseudonyms, and the prefix lives one column over in ``user_id``.
- :func:`canonical_user_id` returns the **prefixed form**, e.g.
  ``"discord_p_v1:<32hex>"``. This is what every ``user_id`` column
  across the schema stores.
"""

import hashlib
import hmac as _hmac

# Public for callers that need to grep / filter by prefix in SQL.
DISCORD_PSEUDO_PREFIX = "discord_p_v1:"

_HMAC_HEX_LEN = 32  # 32 hex chars = 128 bits


def pseudonymize_provider_user_id(raw_id: str, *, pepper: str) -> str:
    """Bare HMAC-SHA256 of ``raw_id`` keyed by ``pepper``, truncated to
    32 hex chars.

    Stored verbatim in ``agents.mcp_provider_user_id``. Both arguments
    are required — passing an empty string for either is a programming
    error and raises ``ValueError`` (defense-in-depth on top of the
    settings boot guard).
    """
    if not pepper:
        raise ValueError(
            "pepper must be a non-empty string (USER_ID_PEPPER not configured?)"
        )
    if not raw_id:
        raise ValueError("raw_id must be a non-empty string")
    digest = _hmac.new(
        pepper.encode("utf-8"), raw_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:_HMAC_HEX_LEN]


def canonical_user_id(provider: str, raw_id: str, *, pepper: str) -> str:
    """Prefixed pseudonym for storage in any ``user_id`` column.

    Today only ``provider="discord"`` is supported. Adding another
    provider means adding another prefix here AND updating the
    migration's table sweep, the smoke SQL, and the
    ``forget_<provider>_user`` endpoint.
    """
    if provider != "discord":
        raise ValueError(f"unsupported provider: {provider!r}")
    return DISCORD_PSEUDO_PREFIX + pseudonymize_provider_user_id(
        raw_id, pepper=pepper
    )
