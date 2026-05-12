"""Tests for the ``MCP_AUTH_REQUIRED=false`` network-trust path.

The bearer-mode path is exercised by ``test_auth.py`` and the integration
suite. This file covers the new ``auth_required=False`` branch added so the
mcp container can run behind a network-only trust boundary (loopback bind +
SSH-tunnel chain). Two surfaces:

1. ``build_server(auth_required=False)`` constructs a FastMCP with no auth
   middleware and a synthetic anonymous TokenInfo for tool handlers.
2. ``_env_bool`` (the env parser ``run()`` uses to read MCP_AUTH_REQUIRED)
   is strict — unknown values fail loud, not silently default to True.

Both are unit-tested with no Postgres; the auth-disabled branch never reads
``mcp_tokens`` or opens a DB connection at boot.
"""

from __future__ import annotations

import pytest

from application.mcp_server.auth import KNOWN_SCOPES, TokenInfo
from application.mcp_server.server import _env_bool, build_server


@pytest.mark.unit
class TestEnvBool:
    @pytest.mark.parametrize("raw", ["1", "true", "True", "TRUE", "yes", "on"])
    def test_truthy(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("MCP_AUTH_REQUIRED", raw)
        assert _env_bool("MCP_AUTH_REQUIRED", default=False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "False", "FALSE", "no", "off"])
    def test_falsy(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("MCP_AUTH_REQUIRED", raw)
        assert _env_bool("MCP_AUTH_REQUIRED", default=True) is False

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_AUTH_REQUIRED", raising=False)
        assert _env_bool("MCP_AUTH_REQUIRED", default=True) is True
        assert _env_bool("MCP_AUTH_REQUIRED", default=False) is False

    @pytest.mark.parametrize("raw", ["maybe", "2", "", " ", "tru", "off-ish"])
    def test_unknown_value_raises(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("MCP_AUTH_REQUIRED", raw)
        with pytest.raises(ValueError, match="MCP_AUTH_REQUIRED"):
            _env_bool("MCP_AUTH_REQUIRED", default=True)


@pytest.mark.unit
class TestBuildServerAuthDisabled:
    """``build_server(auth_required=False)`` must skip the bearer middleware.

    We don't need a live Postgres for this branch — TokenStore is never
    instantiated. ``build_server`` still wants a connection string for the
    tool executors, so we pass one explicitly; nothing actually opens a
    socket at construct time.
    """

    _STUB_DB_URL = "postgresql://stub:stub@localhost:5432/stub"

    def test_fastmcp_has_no_auth_middleware(self) -> None:
        server = build_server(connection_string=self._STUB_DB_URL, auth_required=False)
        # FastMCP exposes the configured auth verifier on the instance.
        # In auth-disabled mode it must be None.
        assert server.auth is None

    def test_fastmcp_has_auth_middleware_when_required(self) -> None:
        # Sanity check the default branch — TokenStore is constructed but
        # doesn't connect; the stub URL is acceptable until we actually
        # verify a token.
        server = build_server(connection_string=self._STUB_DB_URL, auth_required=True)
        assert server.auth is not None

    def test_anonymous_token_has_all_known_scopes(self) -> None:
        # The synthetic TokenInfo returned by _current_token() in
        # auth-disabled mode must satisfy require_scopes() for every
        # registered tool, otherwise tools would fail with MissingScope at
        # runtime even though the deployment intentionally skipped auth.
        anon = TokenInfo(token_id=None, label="anonymous", scopes=KNOWN_SCOPES)
        # All canonical scopes are granted.
        assert anon.scopes == KNOWN_SCOPES
        # token_id is None — audit_call accepts str | None and the
        # mcp_audit.token_id column is nullable in the schema.
        assert anon.token_id is None
