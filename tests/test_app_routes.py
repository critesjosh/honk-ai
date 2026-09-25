"""Tests for application/app.py route handlers."""

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def app():
    """Import the Flask app with auth mocked to avoid JWT setup issues."""
    with patch("application.app.handle_auth", return_value={"sub": "test_user"}):
        from application.app import app as flask_app
        flask_app.config["TESTING"] = True
        yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


class TestHomeRoute:

    @pytest.mark.unit
    def test_root_returns_200(self, client):
        """Root serves Swagger UI via Flask-RESTX."""
        response = client.get("/")
        assert response.status_code == 200


class TestHealthRoute:

    @pytest.mark.unit
    def test_returns_ok(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["status"] == "ok"


class TestConfigRoute:

    @pytest.mark.unit
    def test_returns_auth_config(self, client):
        response = client.get("/api/config")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "auth_type" in data
        assert "requires_auth" in data


class TestGenerateTokenRoute:

    @pytest.mark.unit
    def test_session_jwt_generates_token(self, client, app):
        with patch("application.app.settings") as mock_settings:
            mock_settings.AUTH_TYPE = "session_jwt"
            mock_settings.JWT_SECRET_KEY = "test_secret"
            response = client.get("/api/generate_token")
            assert response.status_code == 200
            data = json.loads(response.data)
            assert "token" in data

    @pytest.mark.unit
    def test_non_session_jwt_returns_error(self, client, app):
        with patch("application.app.settings") as mock_settings:
            mock_settings.AUTH_TYPE = "none"
            response = client.get("/api/generate_token")
            assert response.status_code == 400


class TestAuthenticateRequest:

    @pytest.mark.unit
    def test_options_returns_200(self, client):
        response = client.options("/api/health")
        assert response.status_code == 200

    @pytest.mark.unit
    def test_auth_error_returns_401(self, client, app):
        with patch("application.app.handle_auth", return_value={"error": "Invalid token"}):
            response = client.get("/api/health")
            assert response.status_code == 401

    @pytest.mark.unit
    def test_no_token_sets_none(self, client, app):
        with patch("application.app.handle_auth", return_value=None):
            response = client.get("/api/health")
            assert response.status_code == 200


class TestAfterRequest:
    """The Aztec fork gates CORS headers on CORS_ALLOWED_ORIGINS
    (see application/app.py:after_request). Default is unset → no headers."""

    @pytest.mark.unit
    def test_no_cors_headers_when_unset(self, client):
        # Patch settings explicitly — otherwise an ambient
        # CORS_ALLOWED_ORIGINS in the pytest env would flip the branch.
        # Unset means after_request returns before emitting any CORS
        # headers, not just the origin one.
        with patch("application.app.settings") as mock_settings:
            mock_settings.CORS_ALLOWED_ORIGINS = ""
            response = client.get("/api/health")
        assert "Access-Control-Allow-Origin" not in response.headers
        assert "Access-Control-Allow-Headers" not in response.headers
        assert "Access-Control-Allow-Methods" not in response.headers

    @pytest.mark.unit
    def test_wildcard_emits_star_and_methods(self, client):
        with patch("application.app.settings") as mock_settings:
            mock_settings.CORS_ALLOWED_ORIGINS = "*"
            response = client.get("/api/health")
        assert response.headers.get("Access-Control-Allow-Origin") == "*"
        assert "Content-Type" in response.headers.get("Access-Control-Allow-Headers", "")
        assert "GET" in response.headers.get("Access-Control-Allow-Methods", "")

    @pytest.mark.unit
    def test_glob_pattern_echoes_matching_origin(self, client):
        with patch("application.app.settings") as mock_settings:
            mock_settings.CORS_ALLOWED_ORIGINS = "https://*.example.com"
            response = client.get(
                "/api/health",
                headers={"Origin": "https://preview-7.example.com"},
            )
        assert (
            response.headers.get("Access-Control-Allow-Origin")
            == "https://preview-7.example.com"
        )
        assert response.headers.get("Vary") == "Origin"

    @pytest.mark.unit
    def test_non_matching_origin_gets_no_header(self, client):
        with patch("application.app.settings") as mock_settings:
            mock_settings.CORS_ALLOWED_ORIGINS = "https://*.example.com"
            response = client.get(
                "/api/health",
                headers={"Origin": "https://evil.test"},
            )
        assert "Access-Control-Allow-Origin" not in response.headers
