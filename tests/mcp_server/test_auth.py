"""Tests for ``application.mcp_server.auth``.

Covers the parts that don't need a real Postgres:

* SHA-256 hashing matches between the issuance script and the verifier.
* Scope checks raise on mismatches.
* The connection-string resolver respects the documented env-var
  precedence (``MCP_DB_URL`` > ``PGVECTOR_CONNECTION_STRING`` >
  ``POSTGRES_URI``).

The actual ``TokenStore.verify`` path needs a Postgres fixture to be
meaningful and is exercised in integration tests under
``tests/mcp_server/integration/``.
"""

from __future__ import annotations

import pytest

from application.mcp_server.auth import (
    KNOWN_SCOPES,
    SCOPE_DB_READ,
    SCOPE_LOGS_READ,
    SCOPE_RAG_READ,
    MissingScope,
    TokenInfo,
    _resolve_connection_string_from_env,
    hash_token,
    require_scopes,
)


@pytest.mark.unit
class TestHashToken:
    def test_hash_is_deterministic(self) -> None:
        a = hash_token("super-secret")
        b = hash_token("super-secret")
        assert a == b
        assert len(a) == 64
        # Hex
        int(a, 16)

    def test_hash_differs_per_input(self) -> None:
        a = hash_token("alpha")
        b = hash_token("beta")
        assert a != b


@pytest.mark.unit
class TestScopeCheck:
    def test_passes_when_required_scopes_present(self) -> None:
        token = TokenInfo(
            token_id="00000000-0000-0000-0000-000000000000",
            label="t",
            scopes=frozenset({SCOPE_DB_READ, SCOPE_LOGS_READ}),
        )
        require_scopes(token, [SCOPE_DB_READ])
        require_scopes(token, [SCOPE_DB_READ, SCOPE_LOGS_READ])

    def test_raises_with_missing_named(self) -> None:
        token = TokenInfo(
            token_id="00000000-0000-0000-0000-000000000000",
            label="t",
            scopes=frozenset({SCOPE_DB_READ}),
        )
        with pytest.raises(MissingScope) as ei:
            require_scopes(token, [SCOPE_DB_READ, SCOPE_RAG_READ])
        assert SCOPE_RAG_READ in ei.value.missing
        assert SCOPE_DB_READ not in ei.value.missing


@pytest.mark.unit
class TestKnownScopes:
    def test_known_scope_set_is_consistent(self) -> None:
        # The constants must all be in KNOWN_SCOPES so issue_token.py
        # can validate operator input against the same set.
        for name in (
            SCOPE_DB_READ,
            SCOPE_RAG_READ,
            SCOPE_LOGS_READ,
        ):
            assert name in KNOWN_SCOPES


@pytest.mark.unit
class TestResolveConnectionStringFromEnv:
    """The env-var ladder is the authoritative mapping; lock it down."""

    def test_explicit_mcp_db_url_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_DB_URL", "postgres://mcp:x@h/d")
        monkeypatch.setenv("PGVECTOR_CONNECTION_STRING", "postgres://other:x@h/d")
        monkeypatch.setenv("POSTGRES_URI", "postgresql+psycopg://other2:x@h/d")
        assert _resolve_connection_string_from_env() == "postgres://mcp:x@h/d"

    def test_pgvector_used_when_no_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_DB_URL", raising=False)
        monkeypatch.setenv("PGVECTOR_CONNECTION_STRING", "postgres://pg:x@h/d")
        monkeypatch.setenv("POSTGRES_URI", "postgresql+psycopg://other:x@h/d")
        # Already libpq-friendly, so it passes through.
        assert _resolve_connection_string_from_env() == "postgres://pg:x@h/d"

    def test_postgres_uri_normalized_for_libpq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_DB_URL", raising=False)
        monkeypatch.delenv("PGVECTOR_CONNECTION_STRING", raising=False)
        monkeypatch.setenv("POSTGRES_URI", "postgresql+psycopg://u:p@h/d")
        # SQLAlchemy dialect prefix is rewritten so libpq accepts it.
        assert _resolve_connection_string_from_env() == "postgresql://u:p@h/d"

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_DB_URL", raising=False)
        monkeypatch.delenv("PGVECTOR_CONNECTION_STRING", raising=False)
        monkeypatch.delenv("POSTGRES_URI", raising=False)
        assert _resolve_connection_string_from_env() is None
