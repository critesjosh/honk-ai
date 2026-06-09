"""Unit tests for ``application.pseudonyms``.

No DB. The helper is pure — every test here runs in microseconds.
"""

import re

import pytest

from application.pseudonyms import (
    DISCORD_PSEUDO_PREFIX,
    SLACK_PSEUDO_PREFIX,
    canonical_user_id,
    pseudonymize_provider_user_id,
)


# Pinned golden vector. Recomputed once during initial implementation
# from `hmac_sha256("a"*64, "123456789012345678").hexdigest()[:32]`.
# If this assertion ever fails, **stop**: either the algorithm,
# encoding, truncation, or prefix changed — and any of those silently
# break /forget-me on existing rows because the helper now produces a
# different pseudo than the migration wrote.
GOLDEN_PEPPER = "a" * 64
GOLDEN_RAW_ID = "123456789012345678"
GOLDEN_BARE_HEX = "7e1e77fbd263326e7e8984b4a4299833"


class TestPseudonymizeProviderUserId:
    def test_golden_vector(self):
        assert (
            pseudonymize_provider_user_id(GOLDEN_RAW_ID, pepper=GOLDEN_PEPPER)
            == GOLDEN_BARE_HEX
        )

    def test_format_is_32_lowercase_hex(self):
        out = pseudonymize_provider_user_id("42", pepper=GOLDEN_PEPPER)
        assert re.fullmatch(r"[a-f0-9]{32}", out)

    def test_pepper_sensitivity(self):
        a = pseudonymize_provider_user_id("42", pepper=GOLDEN_PEPPER)
        b = pseudonymize_provider_user_id("42", pepper="b" * 64)
        assert a != b, (
            "two different peppers must produce different pseudos — "
            "core security property"
        )

    def test_input_sensitivity(self):
        a = pseudonymize_provider_user_id("42", pepper=GOLDEN_PEPPER)
        b = pseudonymize_provider_user_id("43", pepper=GOLDEN_PEPPER)
        assert a != b

    def test_empty_pepper_raises(self):
        with pytest.raises(ValueError, match="pepper"):
            pseudonymize_provider_user_id("42", pepper="")

    def test_none_pepper_raises(self):
        with pytest.raises(ValueError, match="pepper"):
            pseudonymize_provider_user_id("42", pepper=None)  # type: ignore[arg-type]

    def test_empty_raw_id_raises(self):
        with pytest.raises(ValueError, match="raw_id"):
            pseudonymize_provider_user_id("", pepper=GOLDEN_PEPPER)


class TestCanonicalUserId:
    def test_prefixed_form(self):
        out = canonical_user_id(
            "discord", GOLDEN_RAW_ID, pepper=GOLDEN_PEPPER
        )
        assert out == DISCORD_PSEUDO_PREFIX + GOLDEN_BARE_HEX

    def test_format_matches_migration_regex(self):
        """Must satisfy the pattern the migration + smoke SQL filter on."""
        out = canonical_user_id("discord", "42", pepper=GOLDEN_PEPPER)
        assert re.fullmatch(r"discord_p_v1:[a-f0-9]{32}", out)

    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="unsupported provider"):
            canonical_user_id("github", "42", pepper=GOLDEN_PEPPER)

    def test_propagates_helper_validation(self):
        """Empty pepper / raw_id must still raise via the canonical wrapper."""
        with pytest.raises(ValueError):
            canonical_user_id("discord", "", pepper=GOLDEN_PEPPER)
        with pytest.raises(ValueError):
            canonical_user_id("discord", "42", pepper="")

    def test_slack_prefixed_form(self):
        """Slack uses its own ``user_id`` prefix; the bare HMAC is shared
        (provider-blind) — the ``mcp_provider`` column + prefix are what
        disambiguate provider."""
        out = canonical_user_id("slack", GOLDEN_RAW_ID, pepper=GOLDEN_PEPPER)
        assert out == SLACK_PSEUDO_PREFIX + GOLDEN_BARE_HEX
        assert re.fullmatch(r"slack_p_v1:[a-f0-9]{32}", out)

    def test_provider_distinguished_by_prefix_only(self):
        """Same raw id under discord vs slack share the bare HMAC but get
        distinct prefixed canonical ids — so the two never collide in any
        ``user_id`` column."""
        d = canonical_user_id("discord", GOLDEN_RAW_ID, pepper=GOLDEN_PEPPER)
        s = canonical_user_id("slack", GOLDEN_RAW_ID, pepper=GOLDEN_PEPPER)
        assert d != s
        assert d.split(":", 1)[1] == s.split(":", 1)[1]  # same bare hex
