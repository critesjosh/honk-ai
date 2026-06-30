"""Boot-guard / normalization tests for the decoding-policy settings in
``application.core.settings`` (SAMPLING_* fields, see PR #203 / WS3).

Two failure modes are covered:
  * blank ``SAMPLING_TEMPERATURE=`` / ``SAMPLING_TOP_P=`` (the unset form the
    .env-template documents) must NOT crash boot — Pydantic rejects "" for
    Optional[float], so a before-validator normalizes blank → None.
  * out-of-range numeric values are clamped to the provider-accepted ranges
    so a typo doesn't turn every scoped call into a provider-side error.

We instantiate ``Settings()`` directly with explicit overrides and
``_env_file=None`` so the project's .env can't leak in (same approach as
``test_settings_pepper.py``).
"""

import pytest

# Valid pepper so the unrelated USER_ID_PEPPER boot-guard doesn't fire.
_PEPPER = "ab" * 16


def _settings(**overrides):
    from application.core.settings import Settings

    return Settings(USER_ID_PEPPER=_PEPPER, _env_file=None, **overrides)


class TestBlankSamplingFloatNormalization:
    @pytest.mark.parametrize("field", ["SAMPLING_TEMPERATURE", "SAMPLING_TOP_P"])
    @pytest.mark.parametrize("blank", ["", "   ", "none", "None"])
    def test_blank_normalizes_to_none(self, field, blank):
        # Regression: a bare ``SAMPLING_TEMPERATURE=`` in .env used to raise a
        # ValidationError at boot.
        s = _settings(**{field: blank})
        assert getattr(s, field) is None

    @pytest.mark.parametrize("field", ["SAMPLING_TEMPERATURE", "SAMPLING_TOP_P"])
    def test_unset_defaults_to_none(self, field):
        assert getattr(_settings(), field) is None


class TestNumericClamping:
    def test_presence_penalty_clamped_high(self):
        assert _settings(SAMPLING_PRESENCE_PENALTY=3).SAMPLING_PRESENCE_PENALTY == 2.0

    def test_presence_penalty_clamped_low(self):
        assert _settings(SAMPLING_PRESENCE_PENALTY=-5).SAMPLING_PRESENCE_PENALTY == -2.0

    def test_presence_penalty_in_range_unchanged(self):
        assert _settings(SAMPLING_PRESENCE_PENALTY=0.3).SAMPLING_PRESENCE_PENALTY == 0.3

    def test_temperature_clamped(self):
        assert _settings(SAMPLING_TEMPERATURE=5).SAMPLING_TEMPERATURE == 2.0
        assert _settings(SAMPLING_TEMPERATURE=-1).SAMPLING_TEMPERATURE == 0.0

    def test_temperature_in_range_unchanged(self):
        assert _settings(SAMPLING_TEMPERATURE=0.4).SAMPLING_TEMPERATURE == 0.4

    def test_top_p_clamped(self):
        assert _settings(SAMPLING_TOP_P=2).SAMPLING_TOP_P == 1.0
        assert _settings(SAMPLING_TOP_P=-0.5).SAMPLING_TOP_P == 0.0

    def test_top_p_in_range_unchanged(self):
        assert _settings(SAMPLING_TOP_P=0.9).SAMPLING_TOP_P == 0.9


class TestNonFiniteRejected:
    """NaN/inf would clamp to an extreme (NaN -> upper bound) and silently
    hide a corrupt config — reject loudly at boot instead."""

    @pytest.mark.parametrize(
        "field", ["SAMPLING_PRESENCE_PENALTY", "SAMPLING_TEMPERATURE", "SAMPLING_TOP_P"]
    )
    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
    def test_non_finite_raises(self, field, bad):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as excinfo:
            _settings(**{field: bad})
        assert field in str(excinfo.value)
