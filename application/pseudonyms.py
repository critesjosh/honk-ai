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
  on that column because the sibling ``mcp_provider`` column already
  disambiguates the provider, and the ``user_id``-column prefix lives
  one column over.
- :func:`canonical_user_id` returns the **prefixed form**, e.g.
  ``"discord_p_v1:<32hex>"``. This is what every ``user_id`` column
  across the schema stores.
"""

import hashlib
import hmac as _hmac

# Public for callers that need to grep / filter by prefix in SQL.
DISCORD_PSEUDO_PREFIX = "discord_p_v1:"
SLACK_PSEUDO_PREFIX = "slack_p_v1:"

# Provider → ``user_id`` prefix. Adding a provider here is the ONLY place a
# new chat/MCP surface's pseudonym prefix is declared; the bot supplies the
# raw identity (for Slack a workspace-scoped ``team_id:user_id`` compound —
# see the Slack bot's ``slack_raw_identity``) and the backend never sees the
# plaintext id in storage.
_PROVIDER_PREFIXES = {
    "discord": DISCORD_PSEUDO_PREFIX,
    "slack": SLACK_PSEUDO_PREFIX,
}

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

    Supported providers are declared in ``_PROVIDER_PREFIXES``
    (``discord`` and ``slack`` today). Adding another provider means
    adding its prefix there AND updating the create/forget endpoints,
    the migration ``surface`` CHECK enum, and the smoke SQL. The
    provider allowlist is strict — an unknown provider raises rather
    than silently minting an un-erasable pseudonym.
    """
    prefix = _PROVIDER_PREFIXES.get(provider)
    if prefix is None:
        raise ValueError(f"unsupported provider: {provider!r}")
    return prefix + pseudonymize_provider_user_id(raw_id, pepper=pepper)


def resolve_requester_pseudonym(
    provider: object, raw_id: object, *, pepper: str
) -> str | None:
    """Lenient wrapper around :func:`canonical_user_id` for the request path.

    Used to tag a stored chat turn with the canonical pseudonym of the
    end-user who triggered it (so ``/forget-me`` can later erase it). Unlike
    :func:`canonical_user_id`, this NEVER raises — a malformed/absent/unknown
    identity must never break an in-flight answer; it just yields ``None`` (the
    row stays anonymous and un-erasable, ages out via retention).

    The raw id is the bot-supplied provider identity (Discord snowflake, or the
    Slack workspace-scoped ``team_id:user_id`` compound). It is HMAC'd here and
    never stored or logged in plaintext by callers.
    """
    if not pepper:
        return None
    if not isinstance(provider, str) or not isinstance(raw_id, str):
        return None
    provider = provider.strip()
    raw_id = raw_id.strip()
    if provider not in _PROVIDER_PREFIXES or not raw_id:
        return None
    try:
        return canonical_user_id(provider, raw_id, pepper=pepper)
    except ValueError:
        return None
