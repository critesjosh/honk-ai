"""Provision (or refresh) the Honk AI Slack chat agent.

This is the singleton agent whose ``key`` becomes the Slack bot's
``API_KEY`` (the bot posts every workspace's questions to it via
``/stream``, exactly like the Discord bot posts to the ``Aztec 4.x``
agent). It carries ``surface='slack'`` so per-surface analytics can
distinguish Slack traffic — see migration ``0010_agents_surface_slack``
and the ``agents.surface`` rule in CLAUDE.md.

Per-user Slack MCP keys are a DIFFERENT thing: they're provisioned at
request time by ``/api/internal/create_mcp_key`` with ``surface='mcp'``
and ``mcp_provider='slack'``. This script only creates the shared chat
agent.

Caps: left OFF by default. The real spend brake for the bot path is the
bot-side per-workspace daily USD cap (``SLACK_TEAM_DAILY_USD_CAPS``),
which — like the Discord guild cap — exists because every workspace
shares this one agent key, so the agent-level ``limited_token_mode``
can't gate one workspace without gating all of them. Set
``SLACK_AGENT_*_LIMIT`` env vars to additionally enable the coarse
agent-level cap if desired.

Idempotent: look up by ``(user_id, name)``; refresh source/prompt on
update, preserve ``key`` so the deployed bot keeps working (rotating the
key silences the bot until its ``.env`` is updated + service recreated).

Usage (scripts/ is NOT in the backend image — bind-mount it; see
CLAUDE.md)::

    # First-time provisioning. Capture the printed key into .env as the
    # slack-bot service's API_KEY before recreating the bot.
    docker compose -f deployment/docker-compose-hub.yaml run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro \\
        -e SLACK_AGENT_KEY="$(uuidgen)" \\
        backend python scripts/db/create_slack_chat_agent.py

    # Refresh source list / prompt (re-uses the existing key):
    docker compose -f deployment/docker-compose-hub.yaml run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro \\
        backend python scripts/db/create_slack_chat_agent.py
"""

from __future__ import annotations

import os
import sys
import uuid

from sqlalchemy import text

from application.core.settings import settings
from application.storage.db.session import db_session


AGENT_USER_ID = "local"
AGENT_NAME = "Honk AI — Slack"


def _parse_source_ids(raw: str | None) -> list[str]:
    if not raw:
        print("AZTEC_SOURCE_IDS is not set in the backend env", file=sys.stderr)
        sys.exit(2)
    ids = [s.strip() for s in raw.split(",") if s.strip()]
    bad: list[str] = []
    for sid in ids:
        try:
            uuid.UUID(sid)
        except ValueError:
            bad.append(sid)
    if bad:
        print(f"AZTEC_SOURCE_IDS contains non-UUID entries: {bad}", file=sys.stderr)
        sys.exit(2)
    if not ids:
        print("AZTEC_SOURCE_IDS resolved to an empty list", file=sys.stderr)
        sys.exit(2)
    return ids


def main() -> int:
    source_ids = _parse_source_ids(settings.AZTEC_SOURCE_IDS)
    primary, *extras = source_ids

    # Reuse the Discord-grounded prompt for the MVP unless an operator
    # points SLACK_PROMPT_ID at a Slack-specific (mrkdwn) variant. Note
    # the prompt body lives in Postgres (CLAUDE.md) — this only sets the
    # FK.
    prompt_id = os.environ.get("SLACK_PROMPT_ID") or None
    default_model_id = os.environ.get("SLACK_DEFAULT_MODEL_ID") or None
    fresh_key = os.environ.get("SLACK_AGENT_KEY") or None

    # Optional coarse agent-level caps (off unless both the *_MODE intent
    # and a limit are provided). The bot-side per-workspace cap is the
    # primary brake; these are belt-and-suspenders.
    request_limit = os.environ.get("SLACK_AGENT_REQUEST_LIMIT")
    token_limit = os.environ.get("SLACK_AGENT_TOKEN_LIMIT")
    limited_request = bool(request_limit)
    limited_token = bool(token_limit)

    with db_session() as conn:
        existing = conn.execute(
            text(
                "SELECT id, key FROM agents WHERE user_id = :uid AND name = :name "
                "ORDER BY created_at LIMIT 1"
            ),
            {"uid": AGENT_USER_ID, "name": AGENT_NAME},
        ).fetchone()

        if existing is None:
            if not fresh_key:
                print(
                    "No existing Slack chat agent found. Set SLACK_AGENT_KEY "
                    "(suggest: uuidgen) and re-run.",
                    file=sys.stderr,
                )
                return 2
            new_id = conn.execute(
                text(
                    "INSERT INTO agents ("
                    " user_id, name, description, agent_type, status, key,"
                    " source_id, extra_source_ids, chunks, retriever, prompt_id,"
                    " default_model_id, tools,"
                    " limited_request_mode, request_limit,"
                    " limited_token_mode, token_limit,"
                    " allow_system_prompt_override, surface"
                    ") VALUES ("
                    " :uid, :name, :desc, 'classic', 'published', :key,"
                    " CAST(:src AS uuid), CAST(:extras AS uuid[]), :chunks,"
                    " 'classic', CAST(:pid AS uuid), :model, '[]'::jsonb,"
                    " :lrm, :rl, :ltm, :tl, false, 'slack'"
                    ") RETURNING id"
                ),
                {
                    "uid": AGENT_USER_ID,
                    "name": AGENT_NAME,
                    "desc": "Honk AI Slack bot chat agent (Aztec docs RAG).",
                    "key": fresh_key,
                    "src": primary,
                    "extras": "{" + ",".join(extras) + "}",
                    "chunks": 4,
                    "pid": prompt_id,
                    "model": default_model_id,
                    "lrm": limited_request,
                    "rl": int(request_limit) if request_limit else 0,
                    "ltm": limited_token,
                    "tl": int(token_limit) if token_limit else 0,
                },
            ).scalar_one()
            print(f"Created agent id={new_id}")
            print(f"Agent key (set as the slack-bot service API_KEY): {fresh_key}")
            return 0

        agent_id, current_key = existing
        conn.execute(
            text(
                "UPDATE agents SET"
                " source_id = CAST(:src AS uuid),"
                " extra_source_ids = CAST(:extras AS uuid[]),"
                " chunks = :chunks,"
                " retriever = 'classic',"
                " agent_type = 'classic',"
                " prompt_id = COALESCE(CAST(:pid AS uuid), prompt_id),"
                " default_model_id = COALESCE(:model, default_model_id),"
                " tools = '[]'::jsonb,"
                " allow_system_prompt_override = false,"
                " surface = 'slack',"
                " updated_at = now()"
                " WHERE id = :id"
            ),
            {
                "id": agent_id,
                "src": primary,
                "extras": "{" + ",".join(extras) + "}",
                "chunks": 4,
                "pid": prompt_id,
                "model": default_model_id,
            },
        )
        print(f"Refreshed agent id={agent_id} (existing key preserved)")
        if fresh_key and fresh_key != current_key:
            print(
                "Note: SLACK_AGENT_KEY was set but did NOT replace the existing key. "
                "Rotation requires a deliberate UPDATE; rotating silences the bot "
                "until its .env API_KEY is updated and the service recreated.",
                file=sys.stderr,
            )
        return 0


if __name__ == "__main__":
    sys.exit(main())
