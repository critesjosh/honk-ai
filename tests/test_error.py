import pytest
from application.error import bad_request, response_error, sanitize_api_error
from flask import Flask


@pytest.fixture
def app():
    app = Flask(__name__)
    return app


@pytest.mark.unit
def test_bad_request_with_message(app):
    with app.app_context():
        message = "Invalid input"
        response = bad_request(status_code=400, message=message)
        assert response.status_code == 400
        assert response.json == {"error": "Bad Request", "message": message}


@pytest.mark.unit
def test_bad_request_without_message(app):
    with app.app_context():
        response = bad_request(status_code=400)
        assert response.status_code == 400
        assert response.json == {"error": "Bad Request"}


@pytest.mark.unit
def test_response_error_with_message(app):
    with app.app_context():
        message = "Something went wrong"
        response = response_error(code_status=500, message=message)
        assert response.status_code == 500
        assert response.json == {"error": "Internal Server Error", "message": message}


@pytest.mark.unit
def test_response_error_without_message(app):
    with app.app_context():
        response = response_error(code_status=500)
        assert response.status_code == 500
        assert response.json == {"error": "Internal Server Error"}


@pytest.mark.unit
class TestSanitizeApiError:

    def test_503_unavailable(self):
        assert "temporarily unavailable" in sanitize_api_error("503 Service Unavailable")

    def test_high_demand(self):
        assert "temporarily unavailable" in sanitize_api_error("high demand")

    def test_429_rate_limit(self):
        assert "Rate limit" in sanitize_api_error("429 Too Many Requests")

    def test_quota_exceeded(self):
        assert "Rate limit" in sanitize_api_error("Quota exceeded")

    def test_401_unauthorized(self):
        assert "Authentication" in sanitize_api_error("401 Unauthorized")

    def test_invalid_api_key(self):
        assert "Authentication" in sanitize_api_error("Invalid API key provided")

    def test_timeout(self):
        assert "timed out" in sanitize_api_error("Request timed out")

    def test_connection_error(self):
        assert "Network" in sanitize_api_error("Connection refused")

    def test_long_message_sanitized(self):
        assert "error occurred" in sanitize_api_error("x" * 201)

    def test_traceback_sanitized(self):
        assert "error occurred" in sanitize_api_error("Traceback (most recent call)")

    def test_json_sanitized(self):
        assert "error occurred" in sanitize_api_error('{"error": "something"}')

    def test_short_safe_message_passed_through(self):
        assert sanitize_api_error("Something broke") == "Something broke"

    def test_bearer_token_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "Authorization: Bearer abcdef1234567890"
        )

    def test_sk_key_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "bad sk-proj-Abc123Xyz789Foo"
        )

    def test_anthropic_key_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "bad sk-ant-api03-AbcDef0123456789"
        )

    def test_jwt_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef.xyz"
        )

    def test_url_scrubbed(self):
        # Signed URL with query string — leaks key/expiry.
        assert "error occurred" in sanitize_api_error(
            "upload failed at https://oai-cf-sig.example.com/bucket/x?sig=AbCdE"
        )

    def test_internal_hostname_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "could not resolve backend.svc.cluster.local"
        )

    def test_filesystem_path_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "missing /app/application/secrets.json"
        )

    def test_ipv4_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "connect refused 10.0.42.17"
        )

    def test_org_id_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "org-id: org-AbCd0123456789XYZ"
        )

    def test_project_id_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "project_id=proj_ABcD0123456789xY"
        )

    def test_query_string_scrubbed(self):
        assert "error occurred" in sanitize_api_error(
            "bad ?api_key=AbCd0123456789xY"
        )

    def test_non_ascii_scrubbed(self):
        # Raw protocol bytes / unicode payloads shouldn't reach the widget.
        assert "error occurred" in sanitize_api_error("oh no \x00\xff binary")

    def test_known_category_wins_even_with_leak_shape(self):
        # 503 path is more useful than the generic fallback even if the
        # original happened to embed a URL — we still emit canned text.
        msg = sanitize_api_error(
            "503 unavailable at https://api.openai.com/v1/chat"
        )
        assert "temporarily unavailable" in msg
