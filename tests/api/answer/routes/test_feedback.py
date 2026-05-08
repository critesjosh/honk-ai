"""Tests for the relocated POST /api/feedback endpoint.

Lives under the ``answer`` blueprint as of the unused-component sweep
(originally in ``api/user/conversations/routes.py``). The Discord bot's
reaction-feedback flow depends on this endpoint, so the relocation
needs coverage that didn't move with the deleted ``api/user/`` tests.

Smoke surface:

- 401 when ``decoded_token`` is unset.
- 400 when required fields are missing.
- 404 when the conversation lookup misses (mismatched ``user_id``).
- 200 + repo write when everything lines up.
- Lowercase normalization: a payload of ``"LIKE"`` writes as ``"like"``
  so the analytics queries (which match lowercase) count rows correctly.
"""

import datetime
from unittest.mock import patch

import pytest

from application.app import app as flask_app


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


@pytest.fixture
def auth_local(monkeypatch):
    """Stub ``handle_auth`` so requests resolve as user_id='local'.

    Mirrors the production behaviour with ``AUTH_TYPE`` unset, which is
    what the Discord bot relies on for /api/feedback to round-trip.
    """
    monkeypatch.setattr(
        "application.app.handle_auth", lambda req: {"sub": "local"}
    )


def _post(client, body):
    return client.post(
        "/api/feedback",
        json=body,
        headers={"Content-Type": "application/json"},
    )


class TestSubmitFeedback:
    def test_401_when_no_decoded_token(self, client, monkeypatch):
        monkeypatch.setattr("application.app.handle_auth", lambda req: None)
        resp = _post(client, {"feedback": "like", "conversation_id": "x", "question_index": 0})
        assert resp.status_code == 401

    def test_400_when_required_fields_missing(self, client, auth_local):
        resp = _post(client, {})
        assert resp.status_code == 400

    def test_404_when_conversation_not_found(self, client, auth_local):
        # ``with db_session() as conn:`` opens a real Postgres connection
        # in the route handler before the repo mock takes effect, so we
        # have to neutralize the session ctx as well — CI doesn't set
        # POSTGRES_URI.
        from contextlib import contextmanager

        @contextmanager
        def fake_db_session():
            yield object()

        with patch(
            "application.api.answer.routes.feedback.db_session",
            new=fake_db_session,
        ), patch(
            "application.api.answer.routes.feedback.ConversationsRepository"
        ) as MockRepo:
            MockRepo.return_value.get_any.return_value = None
            resp = _post(
                client,
                {
                    "feedback": "like",
                    "conversation_id": "missing",
                    "question_index": 0,
                },
            )
            assert resp.status_code == 404

    def test_200_writes_normalized_lowercase_feedback(self, client, auth_local):
        from contextlib import contextmanager

        @contextmanager
        def fake_db_session():
            yield object()

        captured = {}

        def fake_set_feedback(conv_id, idx, payload):
            captured["conv_id"] = conv_id
            captured["idx"] = idx
            captured["payload"] = payload

        with patch(
            "application.api.answer.routes.feedback.db_session",
            new=fake_db_session,
        ), patch(
            "application.api.answer.routes.feedback.ConversationsRepository"
        ) as MockRepo:
            instance = MockRepo.return_value
            instance.get_any.return_value = {"id": "abc-123"}
            instance.set_feedback.side_effect = fake_set_feedback

            resp = _post(
                client,
                {
                    # Caller sends uppercase; we expect it normalized.
                    "feedback": "LIKE",
                    "conversation_id": "abc-123",
                    "question_index": 2,
                },
            )

        assert resp.status_code == 200
        assert captured["conv_id"] == "abc-123"
        assert captured["idx"] == 2
        assert captured["payload"]["text"] == "like"
        # Timestamp should be ISO-8601 with timezone.
        assert isinstance(captured["payload"]["timestamp"], str)
        datetime.datetime.fromisoformat(captured["payload"]["timestamp"])
