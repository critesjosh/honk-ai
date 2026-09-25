"""Tests for ``application.api.answer.routes.version:VersionResource``.

This endpoint is consumed by the @aztec/mcp-server's version-sync gate
(see PR #18). The contract: a 200 response with
``{"aztec_corpus_version": str, "source_count": int}``. MCP clients
treat the literal ``"unknown"`` as a "skip the version gate" signal,
so the AZTEC_CORPUS_VERSION-unset fallback is part of the contract.
"""

from unittest.mock import patch

import pytest


@pytest.mark.unit
class TestVersionResource:
    def test_returns_configured_version(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.version import VersionResource
        from application.core.settings import settings

        with flask_app.app_context(), patch.object(settings, "AZTEC_CORPUS_VERSION", "v4.3.0"):
            with flask_app.test_request_context():
                result = VersionResource().get()

        assert result.status_code == 200
        assert result.json["aztec_corpus_version"] == "v4.3.0"
        assert "source_count" in result.json
        assert isinstance(result.json["source_count"], int)

    def test_post_returns_same_payload_as_get(self, mock_mongo_db, flask_app):
        """The MCP client uses POST to bypass auth proxies (e.g. CF
        Access) that gate GET routes. The two verbs must return
        identical bodies."""
        from application.api.answer.routes.version import VersionResource
        from application.core.settings import settings

        with flask_app.app_context(), patch.object(settings, "AZTEC_CORPUS_VERSION", "v4.3.0"):
            with flask_app.test_request_context():
                get_result = VersionResource().get()
            with flask_app.test_request_context():
                post_result = VersionResource().post()

        assert get_result.status_code == post_result.status_code == 200
        assert get_result.json == post_result.json

    def test_returns_unknown_when_unset(self, mock_mongo_db, flask_app):
        from application.api.answer.routes.version import VersionResource
        from application.core.settings import settings

        with flask_app.app_context(), patch.object(
            settings, "AZTEC_CORPUS_VERSION", None
        ):
            with flask_app.test_request_context():
                result = VersionResource().get()

        assert result.status_code == 200
        assert result.json["aztec_corpus_version"] == "unknown"

    def test_returns_unknown_when_empty_string(self, mock_mongo_db, flask_app):
        """The MCP gate also treats `""` as `unknown`. Make sure the
        endpoint normalizes whitespace + empties to the literal string
        the client can match on."""
        from application.api.answer.routes.version import VersionResource
        from application.core.settings import settings

        with flask_app.app_context(), patch.object(
            settings, "AZTEC_CORPUS_VERSION", "   "
        ):
            with flask_app.test_request_context():
                result = VersionResource().get()

        assert result.status_code == 200
        assert result.json["aztec_corpus_version"] == "unknown"

    def test_source_count_falls_back_to_zero_on_db_error(self, mock_mongo_db, flask_app):
        """Source count is best-effort. A DB hiccup must NOT prevent
        the version response — the MCP gate depends on this endpoint
        always succeeding."""
        from application.api.answer.routes.version import VersionResource
        from application.core.settings import settings

        with flask_app.app_context(), patch.object(
            settings, "AZTEC_CORPUS_VERSION", "v4.3.0"
        ), patch.object(
            settings,
            "AZTEC_SOURCE_IDS",
            "11111111-1111-1111-1111-111111111111",
        ), patch(
            "application.api.answer.routes.version.db_readonly",
            side_effect=RuntimeError("db down"),
        ):
            with flask_app.test_request_context():
                result = VersionResource().get()

        assert result.status_code == 200
        assert result.json["aztec_corpus_version"] == "v4.3.0"
        assert result.json["source_count"] == 0
