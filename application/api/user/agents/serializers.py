"""Response shaping helpers for agent endpoints.

Translates Postgres agent rows (snake_case columns, raw UUIDs) into
the dict shape the React frontend consumes (string IDs, masked keys,
``source`` / ``sources`` naming preserved from the Mongo era).

Kept separate from ``service.py`` so the persistence-flavoured logic
in the service module doesn't leak Flask-image-url concerns and so
the formatters can be unit-tested without booting a DB session.
"""

from application.api.user.base import resolve_tool_details
from application.core.settings import settings
from application.utils import generate_image_url


def _format_agent_output(
    agent: dict,
    *,
    pinned: bool = False,
    include_key_masked: bool = True,
) -> dict:
    """Shape a PG agent row into the outward API response dict.

    Translates PG snake_case columns to the camelCase/frontend keys that
    the React client expects, preserving ``source``/``sources`` naming on
    the response even though storage uses ``source_id`` /
    ``extra_source_ids``.
    """
    source_id = agent.get("source_id")
    extra_source_ids = agent.get("extra_source_ids") or []
    source_value = str(source_id) if source_id else ""
    sources_list = [str(s) for s in extra_source_ids if s]

    out = {
        "id": str(agent["id"]),
        "name": agent.get("name", ""),
        "description": agent.get("description", "") or "",
        "image": (
            generate_image_url(agent["image"]) if agent.get("image") else ""
        ),
        "source": source_value,
        "sources": sources_list,
        "chunks": str(agent["chunks"]) if agent.get("chunks") is not None else "2",
        "retriever": agent.get("retriever", "") or "",
        "prompt_id": str(agent["prompt_id"]) if agent.get("prompt_id") else "",
        "tools": agent.get("tools", []) or [],
        "tool_details": resolve_tool_details(agent.get("tools", []) or []),
        "agent_type": agent.get("agent_type", "") or "",
        "status": agent.get("status", "") or "",
        "json_schema": agent.get("json_schema"),
        "limited_token_mode": bool(agent.get("limited_token_mode", False)),
        "token_limit": agent.get("token_limit") or settings.DEFAULT_AGENT_LIMITS["token_limit"],
        "limited_request_mode": bool(agent.get("limited_request_mode", False)),
        "request_limit": agent.get("request_limit") or settings.DEFAULT_AGENT_LIMITS["request_limit"],
        "created_at": agent.get("created_at", ""),
        "updated_at": agent.get("updated_at", ""),
        "last_used_at": agent.get("last_used_at", ""),
        "pinned": pinned,
        "shared": bool(agent.get("shared", False)),
        "shared_metadata": agent.get("shared_metadata", {}) or {},
        "shared_token": agent.get("shared_token", "") or "",
        "models": agent.get("models", []) or [],
        "default_model_id": agent.get("default_model_id", "") or "",
        "folder_id": str(agent["folder_id"]) if agent.get("folder_id") else None,
        "workflow": str(agent["workflow_id"]) if agent.get("workflow_id") else None,
        "allow_system_prompt_override": bool(
            agent.get("allow_system_prompt_override", False)
        ),
    }
    if include_key_masked:
        key_val = agent.get("key") or ""
        out["key"] = (
            f"{key_val[:4]}...{key_val[-4:]}" if key_val else ""
        )
    return out
