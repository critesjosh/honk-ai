"""Provision (or refresh) the public-facing "Ask Aztec" web agent.

The static-bearer ``key`` for this agent is shipped in the public web
bundle as ``VITE_ASK_AZTEC_AGENT_KEY`` and is therefore a known-public
capability.  Anyone who reads the page's JS can replay it against
``POST /stream``.  We compensate with three guardrails configured here:

* ``limited_request_mode`` + ``request_limit`` — caps daily request
  count at the application layer.
* ``limited_token_mode`` + ``token_limit`` — caps daily LLM token
  spend.
* ``allow_system_prompt_override`` is left ``False`` so callers cannot
  override the canonical Aztec system prompt via ``passthrough``.

Tools are explicitly empty — the public agent must never gain a tool
that could exfiltrate or write data on a logged-in user's behalf.

Idempotent: look up by ``(user_id, name)``; refresh source/prompt/caps
on update, preserve ``key`` so the deployed bundle keeps working.

Usage::

    # First-time provisioning. Capture the printed key into .env as
    # VITE_ASK_AZTEC_AGENT_KEY before rebuilding the frontend-ask image.
    docker compose -f deployment/docker-compose-hub.yaml run --rm \\
        -e ASK_AZTEC_AGENT_KEY="$(openssl rand -hex 32)" \\
        backend python scripts/db/create_ask_aztec_public_agent.py

    # Refresh source list / caps (re-uses the existing key):
    docker compose -f deployment/docker-compose-hub.yaml run --rm \\
        backend python scripts/db/create_ask_aztec_public_agent.py
"""

from __future__ import annotations

import os
import sys
import uuid

from sqlalchemy import text

from application.core.settings import settings
from application.storage.db.session import db_session


AGENT_USER_ID = "public-web"
AGENT_NAME = "Ask Aztec — public web"

# Daily caps. Tunable. Sized for ~1 question every few seconds across
# the whole public surface; revisit if abuse alerts fire.
DEFAULT_REQUEST_LIMIT = 10_000
DEFAULT_TOKEN_LIMIT = 5_000_000


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

    prompt_id = os.environ.get("ASK_AZTEC_PROMPT_ID") or None
    default_model_id = os.environ.get("ASK_AZTEC_DEFAULT_MODEL_ID") or None
    request_limit = int(os.environ.get("ASK_AZTEC_REQUEST_LIMIT", DEFAULT_REQUEST_LIMIT))
    token_limit = int(os.environ.get("ASK_AZTEC_TOKEN_LIMIT", DEFAULT_TOKEN_LIMIT))
    fresh_key = os.environ.get("ASK_AZTEC_AGENT_KEY") or None

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
                    "No existing public-web agent found. Set ASK_AZTEC_AGENT_KEY "
                    "(suggest: openssl rand -hex 32) and re-run.",
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
                    " true, :rl, true, :tl, false, 'web_ask'"
                    ") RETURNING id"
                ),
                {
                    "uid": AGENT_USER_ID,
                    "name": AGENT_NAME,
                    "desc": "Public-facing Aztec docs RAG (web). Bearer key shipped in /ask bundle.",
                    "key": fresh_key,
                    "src": primary,
                    "extras": "{" + ",".join(extras) + "}",
                    "chunks": 4,
                    "pid": prompt_id,
                    "model": default_model_id,
                    "rl": request_limit,
                    "tl": token_limit,
                },
            ).scalar_one()
            print(f"Created agent id={new_id}")
            print(f"Agent key (set as VITE_ASK_AZTEC_AGENT_KEY): {fresh_key}")
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
                " limited_request_mode = true, request_limit = :rl,"
                " limited_token_mode = true, token_limit = :tl,"
                " allow_system_prompt_override = false,"
                " surface = 'web_ask',"
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
                "rl": request_limit,
                "tl": token_limit,
            },
        )
        print(f"Refreshed agent id={agent_id} (existing key preserved)")
        if fresh_key and fresh_key != current_key:
            print(
                "Note: ASK_AZTEC_AGENT_KEY was set but did NOT replace the existing key. "
                "Rotation requires a deliberate UPDATE; rotating breaks every deployed bundle.",
                file=sys.stderr,
            )
        return 0


if __name__ == "__main__":
    sys.exit(main())
