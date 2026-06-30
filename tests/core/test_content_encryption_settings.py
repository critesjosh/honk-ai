"""Boot-validator tests for content-encryption settings.

The validator must fail closed when content encryption is enabled but the active
key has no source — mirroring the USER_ID_PEPPER guard.
"""

import base64

import pytest
from application.core.settings import Settings
from pydantic import ValidationError

_GOOD_PEPPER = "0" * 64  # 32 bytes hex — passes the pepper validator


def _make(**overrides):
    base = {"USER_ID_PEPPER": _GOOD_PEPPER, "_env_file": None}
    base.update(overrides)
    return Settings(**base)


@pytest.mark.unit
def test_disabled_does_not_require_a_key():
    s = _make(CONTENT_ENCRYPTION_ENABLED=False, ENCRYPTION_SECRET_KEY="default-docsgpt-encryption-key")
    assert s.CONTENT_ENCRYPTION_ENABLED is False


@pytest.mark.unit
def test_enabled_with_default_secret_fails_closed():
    with pytest.raises(ValidationError, match="content encryption is enabled"):
        _make(CONTENT_ENCRYPTION_ENABLED=True, ENCRYPTION_SECRET_KEY="default-docsgpt-encryption-key")


@pytest.mark.unit
def test_enabled_with_real_secret_passes():
    s = _make(CONTENT_ENCRYPTION_ENABLED=True, ENCRYPTION_SECRET_KEY="x" * 40)
    assert s.CONTENT_ENCRYPTION_ENABLED is True


@pytest.mark.unit
def test_enabled_with_explicit_key_still_requires_real_secret():
    """Even with an explicit active content key, a real ENCRYPTION_SECRET_KEY is
    required: the user_logs api_key blind index is HKDF'd off it unconditionally,
    so booting on the default secret would orphan api_key_fp lookups later."""
    key = base64.b64encode(b"k" * 32).decode()
    with pytest.raises(ValidationError, match="ENCRYPTION_SECRET_KEY"):
        _make(
            CONTENT_ENCRYPTION_ENABLED=True,
            ENCRYPTION_SECRET_KEY="default-docsgpt-encryption-key",
            CONTENT_ENCRYPTION_ACTIVE_KID="v2",
            CONTENT_ENCRYPTION_KEYS=f"v2:{key}",
        )


@pytest.mark.unit
def test_enabled_with_explicit_key_and_real_secret_passes():
    key = base64.b64encode(b"k" * 32).decode()
    s = _make(
        CONTENT_ENCRYPTION_ENABLED=True,
        ENCRYPTION_SECRET_KEY="x" * 40,
        CONTENT_ENCRYPTION_ACTIVE_KID="v2",
        CONTENT_ENCRYPTION_KEYS=f"v2:{key}",
    )
    assert s.CONTENT_ENCRYPTION_ACTIVE_KID == "v2"


@pytest.mark.unit
def test_malformed_inactive_key_fails_closed_at_boot():
    """A bad *inactive* key must fail at boot, not on the first encrypt where it
    would crash mid-stream (the keyring parses every entry)."""
    good = base64.b64encode(b"k" * 32).decode()
    with pytest.raises(ValidationError, match="not valid base64"):
        _make(
            CONTENT_ENCRYPTION_ENABLED=True,
            ENCRYPTION_SECRET_KEY="x" * 40,
            CONTENT_ENCRYPTION_ACTIVE_KID="v2",
            CONTENT_ENCRYPTION_KEYS=f"v2:{good},v1:not-base64!!!",
        )


@pytest.mark.unit
def test_inactive_key_wrong_length_fails_closed():
    good = base64.b64encode(b"k" * 32).decode()
    short = base64.b64encode(b"k" * 16).decode()
    with pytest.raises(ValidationError, match="32 bytes"):
        _make(
            CONTENT_ENCRYPTION_ENABLED=True,
            ENCRYPTION_SECRET_KEY="x" * 40,
            CONTENT_ENCRYPTION_ACTIVE_KID="v2",
            CONTENT_ENCRYPTION_KEYS=f"v2:{good},v1:{short}",
        )


@pytest.mark.unit
def test_active_kid_with_colon_rejected():
    """A kid with ':' would corrupt the colon-delimited envelope header and make
    every value undecryptable; reject it at boot."""
    with pytest.raises(ValidationError, match="A-Za-z0-9"):
        _make(
            CONTENT_ENCRYPTION_ENABLED=True,
            ENCRYPTION_SECRET_KEY="x" * 40,
            CONTENT_ENCRYPTION_ACTIVE_KID="v1:bad",
        )


@pytest.mark.unit
def test_explicit_kid_with_colon_rejected():
    key = base64.b64encode(b"k" * 32).decode()
    # 'bad:kid' partitions to kid='bad' (charset-ok) so force the bad char into
    # an entry whose kid clearly violates the charset.
    with pytest.raises(ValidationError, match="A-Za-z0-9"):
        _make(
            CONTENT_ENCRYPTION_ENABLED=True,
            ENCRYPTION_SECRET_KEY="x" * 40,
            CONTENT_ENCRYPTION_ACTIVE_KID="v2",
            CONTENT_ENCRYPTION_KEYS=f"v2:{key},bad kid:{key}",
        )


@pytest.mark.unit
def test_llm_cache_defaults_off():
    assert _make().LLM_CACHE_ENABLED is False
