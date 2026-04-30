"""Boot-guard tests for ``USER_ID_PEPPER`` in ``application.core.settings``.

The pepper is required at construction; an empty / non-hex / low-entropy
value must crash settings load before anything starts serving traffic.

Note: the settings module is a singleton instantiated at import time.
By the time pytest collects these tests, ``application.core.settings``
has already been imported (with the test pepper from
``tests/conftest.py``'s ``os.environ.setdefault``). To exercise the
negative paths we instantiate ``Settings()`` directly with an explicit
override rather than re-importing the module — codex re-review caught
that ``monkeypatch.delenv + reload`` would not work because the
singleton has already cached.
"""

import pytest
from pydantic import ValidationError


def _settings_cls():
    from application.core.settings import Settings

    return Settings


class TestUserIdPepperBootGuard:
    def test_unset_raises(self):
        Settings = _settings_cls()
        # ``_env_file=None`` so we don't pick up the project's .env.
        with pytest.raises(ValidationError) as excinfo:
            Settings(USER_ID_PEPPER="", _env_file=None)
        assert "USER_ID_PEPPER" in str(excinfo.value)

    def test_non_hex_raises(self):
        Settings = _settings_cls()
        with pytest.raises(ValidationError) as excinfo:
            Settings(USER_ID_PEPPER="x" * 64, _env_file=None)
        assert "hex-encoded" in str(excinfo.value)

    def test_short_decoded_raises(self):
        """A 16-character hex string only decodes to 8 bytes — too short."""
        Settings = _settings_cls()
        with pytest.raises(ValidationError) as excinfo:
            Settings(USER_ID_PEPPER="ab" * 8, _env_file=None)  # 8 bytes
        assert "16 bytes" in str(excinfo.value)

    def test_exactly_16_bytes_passes(self):
        """Boundary: 32 hex chars = 16 bytes is the minimum we accept."""
        Settings = _settings_cls()
        # No raise.
        s = Settings(USER_ID_PEPPER="ab" * 16, _env_file=None)
        assert s.USER_ID_PEPPER == "ab" * 16

    def test_recommended_32_bytes_passes(self):
        Settings = _settings_cls()
        s = Settings(USER_ID_PEPPER="cd" * 32, _env_file=None)
        assert s.USER_ID_PEPPER == "cd" * 32
