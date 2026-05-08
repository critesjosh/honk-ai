"""Tests for the new corpus-ingest blueprint.

POST /api/upload + GET /api/task_status are the two endpoints
``scripts/ingest/upload.py`` depends on. They were carved out of the
deleted ``api/user/sources/upload.py`` blueprint when the upstream
admin SPA was removed.
"""

from unittest.mock import MagicMock, patch

import pytest

from application.app import app as flask_app


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


@pytest.fixture
def auth_local(monkeypatch):
    monkeypatch.setattr(
        "application.app.handle_auth", lambda req: {"sub": "local"}
    )


class TestUploadFile:
    def test_401_when_no_decoded_token(self, client, monkeypatch):
        monkeypatch.setattr("application.app.handle_auth", lambda req: None)
        resp = client.post("/api/upload")
        assert resp.status_code == 401

    def test_400_when_no_files(self, client, auth_local):
        resp = client.post(
            "/api/upload",
            data={"user": "local", "name": "foo"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_400_when_required_fields_missing(self, client, auth_local):
        # Even with a file attached, missing the form fields is a 400.
        resp = client.post(
            "/api/upload",
            data={"file": (b"data", "x.txt")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400


class TestTaskStatus:
    def test_400_without_task_id(self, client, auth_local):
        resp = client.get("/api/task_status")
        assert resp.status_code == 400

    def test_returns_task_meta(self, client, auth_local):
        # Patch the lazy ``celery`` import inside the route handler.
        fake_task = MagicMock()
        fake_task.status = "SUCCESS"
        fake_task.info = {"directory": "/app/application/inputs"}

        fake_celery = MagicMock()
        fake_celery.AsyncResult.return_value = fake_task

        with patch.dict(
            "sys.modules", {"application.celery_init": MagicMock(celery=fake_celery)}
        ):
            resp = client.get("/api/task_status?task_id=abc-123")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "SUCCESS"
        assert body["result"] == {"directory": "/app/application/inputs"}
