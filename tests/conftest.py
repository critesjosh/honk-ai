"""Root pytest fixtures for the DocsGPT backend suite.

Postgres fixture strategy
-------------------------

Regular unit tests get a Postgres connection from the ``pg_conn`` fixture
below, which is backed by ``pytest-postgresql``. That plugin spins up an
ephemeral ``pg_ctl``-managed cluster in a temp directory and tears it
down at the end of the session, so CI only needs Postgres *binaries*
installed, not a running service.

Schema setup is amortised across the session: ``alembic upgrade head``
runs ONCE into the cluster's *template* database (via the factory's
``load=`` hook), and every test that requests ``pg_engine`` / ``pg_conn``
gets its own throwaway database cloned from that template with
``CREATE DATABASE … TEMPLATE …`` — milliseconds instead of a full
per-test alembic subprocess. Tests that commit, or that open extra
connections straight off ``pg_engine``, stay hermetic because the clone
is dropped after each test.

Tests under ``tests/storage/db/`` use these same fixtures (see that
directory's conftest). Only the Alembic end-to-end test in
``test_migration_0005_pseudonymize_user_ids.py`` is marked
``@pytest.mark.integration``; it requests the *blank* ``postgresql``
fixture directly because it drives alembic itself from an empty DB.

No mongomock. The ``mock_mongo_db`` fixture that used to live here was
removed as part of the Phase 4/5 Mongo→Postgres cutover. Tests that
still reference it will fail with "fixture not found" until the
corresponding route handler is migrated to a repository read.
"""

from __future__ import annotations

import os

# Keep the repo-root ``.env`` (operator/prod values) out of every test
# run. ``application.core.settings`` constructs its module-level
# ``Settings(_env_file=…)`` at import time, so clearing the shell env is
# not enough — this flag makes settings skip the dotenv file entirely.
# Must be set before ANY ``application.*`` import. ``setdefault`` so a
# caller can explicitly opt back in with DOCSGPT_SETTINGS_SKIP_ENV_FILE="".
os.environ.setdefault("DOCSGPT_SETTINGS_SKIP_ENV_FILE", "1")

# Disable the app's self-bootstrap (AUTO_CREATE_DB / AUTO_MIGRATE) before
# any ``application.*`` module is imported. ``application/app.py`` runs
# ``ensure_database_ready`` at import time using whatever ``POSTGRES_URI``
# is set in the environment — which in dev is the operator's local DB, not
# the ephemeral ``pytest-postgresql`` cluster that the fixtures below spin
# up. Tests manage their own schema via the ``pg_engine`` fixture
# (``alembic upgrade head`` into the session's template DB), so the
# import-time bootstrap would at best be redundant and at worst would
# mutate the operator's dev DB. ``setdefault`` so a test run can still
# opt back in by setting the env var explicitly.
os.environ.setdefault("AUTO_MIGRATE", "false")
os.environ.setdefault("AUTO_CREATE_DB", "false")

# Pseudonymization pepper — required by ``application.core.settings``
# at construction time. Tests don't need a real one; this dummy value
# is 32 bytes (64 hex) so the entropy validator passes. ``setdefault``
# means a real shell-set ``USER_ID_PEPPER`` still wins, this only fills
# in for ad-hoc dev runs and CI. Not a security footgun: never written
# to a real DB, never used outside the ephemeral test process.
os.environ.setdefault("USER_ID_PEPPER", "0" * 64)

# Several upstream tests import ``application.api.answer.routes.base`` and
# call ``complete_stream`` with a mocked agent. The route still constructs
# an LLM via ``LLMCreator.create_llm`` for compression/summarization, and
# with the Aztec fork's ``LLM_PROVIDER=openrouter`` default, the OpenAI
# SDK refuses to instantiate without an API key. The mocked agent never
# actually exercises this LLM, so a dummy key is enough to get the client
# constructor to succeed. ``setdefault`` keeps any real shell-set key.
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key-not-used")
os.environ.setdefault("OPEN_ROUTER_API_KEY", "test-openrouter-key-not-used")

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from pytest_postgresql import factories
from sqlalchemy import create_engine

# ---------------------------------------------------------------------------
# Postgres fixtures (ephemeral cluster via pytest-postgresql)
# ---------------------------------------------------------------------------

_ALEMBIC_INI_PATH = Path(__file__).resolve().parent.parent / "application" / "alembic.ini"


def _migrate_template(host: str, port: int, user: str, dbname: str, password: str | None) -> None:
    """``load=`` hook: run ``alembic upgrade head`` ONCE into the template DB.

    pytest-postgresql calls this a single time per session, against the
    cluster's template database. Every per-test database handed out by the
    ``pg_migrated`` client fixture below is then cloned from that template,
    so no test pays the (multi-second) alembic subprocess cost itself.
    """
    url = f"postgresql+psycopg://{user}:{password or ''}@{host}:{port}/{dbname}"
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-c", str(_ALEMBIC_INI_PATH), "upgrade", "head"],
        timeout=120,
        env={**os.environ, "POSTGRES_URI": url},
    )


# Two session-scoped clusters, each lazily started only if requested:
#
# * ``postgresql_proc`` / ``postgresql`` — BLANK per-test databases. Kept
#   for tests that drive alembic themselves from an empty schema (the
#   migration-0005 integration test).
# * ``pg_proc_migrated`` / ``pg_migrated`` — per-test databases cloned
#   from a template that already has the full alembic schema applied.
#   This is what ``pg_engine`` / ``pg_conn`` (and therefore ~all
#   repository tests) ride on.
postgresql_proc = factories.postgresql_proc()
postgresql = factories.postgresql("postgresql_proc")

pg_proc_migrated = factories.postgresql_proc(load=[_migrate_template])
pg_migrated = factories.postgresql("pg_proc_migrated")


def _sqlalchemy_url(pg_conn_info) -> str:
    return (
        "postgresql+psycopg://"
        f"{pg_conn_info.user}:{pg_conn_info.password or ''}"
        f"@{pg_conn_info.host}:{pg_conn_info.port}/{pg_conn_info.dbname}"
    )


def pytest_collection_modifyitems(config, items):
    """Cleanly SKIP Postgres-backed tests on hosts without Postgres binaries.

    pytest-postgresql shells out to ``pg_config`` to locate ``pg_ctl``;
    without it every pg test ERRORs at fixture setup. Turn that into an
    explicit skip instead. A configured ``--postgresql-exec`` (or the
    ``postgresql_exec`` ini option) pointing at a real ``pg_ctl`` still
    counts as "binaries installed".
    """
    if shutil.which("pg_config") is not None:
        return
    exec_opt = config.getoption("postgresql_exec", default=None) or config.getini("postgresql_exec")
    if exec_opt and Path(exec_opt).exists():
        return
    skip_pg = pytest.mark.skip(reason="postgresql binaries not installed (pg_config not on PATH)")
    # The client fixtures request their proc fixture dynamically (via
    # ``request.getfixturevalue``), so the proc names never show up in an
    # item's static fixture closure — match on the client names too.
    pg_fixtures = {"postgresql_proc", "postgresql", "pg_proc_migrated", "pg_migrated"}
    for item in items:
        if pg_fixtures.intersection(getattr(item, "fixturenames", ())):
            item.add_marker(skip_pg)


@pytest.fixture()
def pg_engine(pg_migrated, monkeypatch):
    """Per-test SQLAlchemy engine against a fresh ephemeral Postgres DB.

    The database is cloned from the session's pre-migrated template, so
    the full schema is present without running alembic per test.
    ``POSTGRES_URI`` is patched in the environment for the duration of
    the test so any code that reads it via ``application.core.settings``
    sees the ephemeral DB.
    """
    url = _sqlalchemy_url(pg_migrated.info)
    monkeypatch.setenv("POSTGRES_URI", url)

    # Reset the settings cache so the new POSTGRES_URI is picked up if the
    # settings module is already imported.
    from application.core import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "POSTGRES_URI", url, raising=False)

    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_conn(pg_engine):
    """Per-test connection wrapped in a transaction that always rolls back."""
    conn = pg_engine.connect()
    txn = conn.begin()
    yield conn
    txn.rollback()
    conn.close()


# ---------------------------------------------------------------------------
# Generic unit-test fixtures (no DB, no Mongo)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm():
    llm = Mock()
    llm.gen_stream = Mock()
    llm._supports_tools = True
    llm._supports_structured_output = Mock(return_value=False)
    llm.__class__.__name__ = "MockLLM"
    return llm


@pytest.fixture
def mock_llm_handler():
    handler = Mock()
    handler.process_message_flow = Mock()
    return handler


@pytest.fixture
def mock_retriever():
    retriever = Mock()
    retriever.search = Mock(
        return_value=[
            {"text": "Test document 1", "filename": "doc1.txt", "source": "test"},
            {"text": "Test document 2", "title": "doc2.txt", "source": "test"},
        ]
    )
    return retriever


@pytest.fixture
def sample_chat_history():
    return [
        {"prompt": "What is Python?", "response": "Python is a programming language."},
        {"prompt": "Tell me more.", "response": "Python is known for simplicity."},
    ]


@pytest.fixture
def sample_tool_call():
    return {
        "tool_name": "test_tool",
        "call_id": "123",
        "action_name": "test_action",
        "arguments": {"arg1": "value1"},
        "result": "Tool executed successfully",
    }


@pytest.fixture
def decoded_token():
    return {"sub": "test_user", "email": "test@example.com"}


@pytest.fixture
def log_context():
    from application.logging import LogContext

    context = LogContext(
        endpoint="test_endpoint",
        activity_id="test_activity",
        user="test_user",
        api_key="test_key",
        query="test query",
    )
    return context


@pytest.fixture
def mock_llm_creator(mock_llm, monkeypatch):
    monkeypatch.setattr(
        "application.llm.llm_creator.LLMCreator.create_llm", Mock(return_value=mock_llm)
    )
    return mock_llm


@pytest.fixture
def mock_llm_handler_creator(mock_llm_handler, monkeypatch):
    monkeypatch.setattr(
        "application.llm.handlers.handler_creator.LLMHandlerCreator.create_handler",
        Mock(return_value=mock_llm_handler),
    )
    return mock_llm_handler


@pytest.fixture
def agent_base_params(decoded_token):
    return {
        "endpoint": "https://api.example.com",
        "llm_name": "openai",
        "model_id": "gpt-4",
        "api_key": "test_api_key",
        "user_api_key": None,
        "prompt": "You are a helpful assistant.",
        "chat_history": [],
        "decoded_token": decoded_token,
        "attachments": [],
        "json_schema": None,
    }


@pytest.fixture
def mock_tool():
    tool = Mock()
    tool.execute_action = Mock(return_value="Tool result")
    tool.get_actions_metadata = Mock(
        return_value=[
            {
                "name": "test_action",
                "description": "A test action",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "param1": {"type": "string", "description": "Test parameter"}
                    },
                    "required": ["param1"],
                },
            }
        ]
    )
    return tool


@pytest.fixture
def mock_tool_manager(mock_tool, monkeypatch):
    manager = Mock()
    manager.load_tool = Mock(return_value=mock_tool)
    monkeypatch.setattr(
        "application.agents.tool_executor.ToolManager", Mock(return_value=manager)
    )
    return manager


@pytest.fixture
def flask_app():
    from flask import Flask

    app = Flask(__name__)
    return app
