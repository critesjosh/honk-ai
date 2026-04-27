"""Business logic for agent CRUD endpoints.

Routes in ``routes.py`` are kept thin: they handle HTTP concerns
(auth, request body parsing, image upload, JSON-vs-form payloads,
``db_session`` lifecycle, response shaping) and delegate persistence
+ validation here.

Service functions follow the existing
``(value, error_response)`` tuple convention used elsewhere in this
package — on failure they return a fully-formed Flask response
(status + JSON body) so the route can ``return err`` without
knowing which 4xx applies. This pre-dates a typed exception
hierarchy; once item 3 of ``PLAN-simplify.md`` lands, these
tuples should fold into ``raise``.
"""

import datetime
import json
import uuid

from flask import current_app, jsonify, make_response

from application.api.user.agents.serializers import _format_agent_output
from application.core.json_schema_utils import (
    JsonSchemaValidationError,
    normalize_json_schema_payload,
)
from application.services.source_visibility import SourceVisibilityService
from application.storage.db.base_repository import looks_like_uuid
from application.storage.db.repositories.agent_folders import AgentFoldersRepository
from application.storage.db.repositories.agents import AgentsRepository
from application.storage.db.repositories.users import UsersRepository
from application.storage.db.repositories.workflow_edges import WorkflowEdgesRepository
from application.storage.db.repositories.workflow_nodes import WorkflowNodesRepository
from application.storage.db.repositories.workflows import WorkflowsRepository
from application.utils import (
    check_required_fields,
    generate_image_url,
    validate_required_fields,
)


AGENT_TYPE_SCHEMAS = {
    "classic": {
        "required_published": [
            "name",
            "description",
            "chunks",
            "retriever",
            "prompt_id",
        ],
        "required_draft": ["name"],
        "validate_published": ["name", "description", "prompt_id"],
        "validate_draft": [],
        "require_source": True,
        "fields": [
            "name",
            "description",
            "agent_type",
            "status",
            "key",
            "image",
            "source_id",
            "extra_source_ids",
            "chunks",
            "retriever",
            "prompt_id",
            "tools",
            "json_schema",
            "models",
            "default_model_id",
            "folder_id",
            "limited_token_mode",
            "token_limit",
            "limited_request_mode",
            "request_limit",
            "allow_system_prompt_override",
        ],
    },
    "workflow": {
        "required_published": ["name", "workflow"],
        "required_draft": ["name"],
        "validate_published": ["name", "workflow"],
        "validate_draft": [],
        "fields": [
            "name",
            "description",
            "agent_type",
            "status",
            "key",
            "workflow_id",
            "folder_id",
            "limited_token_mode",
            "token_limit",
            "limited_request_mode",
            "request_limit",
            "allow_system_prompt_override",
        ],
    },
}

AGENT_TYPE_SCHEMAS["react"] = AGENT_TYPE_SCHEMAS["classic"]
AGENT_TYPE_SCHEMAS["agentic"] = AGENT_TYPE_SCHEMAS["classic"]
AGENT_TYPE_SCHEMAS["research"] = AGENT_TYPE_SCHEMAS["classic"]
AGENT_TYPE_SCHEMAS["openai"] = AGENT_TYPE_SCHEMAS["classic"]


# ---------------------------------------------------------------------------
# Reference resolvers
# ---------------------------------------------------------------------------


def normalize_workflow_reference(workflow_value):
    """Normalize workflow references from form/json payloads into a string id."""
    if workflow_value is None:
        return None
    if isinstance(workflow_value, dict):
        return (
            workflow_value.get("id")
            or workflow_value.get("_id")
            or workflow_value.get("workflow_id")
        )
    if isinstance(workflow_value, str):
        value = workflow_value.strip()
        if not value:
            return ""
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return parsed.strip()
            if isinstance(parsed, dict):
                return (
                    parsed.get("id") or parsed.get("_id") or parsed.get("workflow_id")
                )
        except json.JSONDecodeError:
            pass
        return value
    return str(workflow_value)


def _resolve_workflow_for_user(conn, workflow_value, user):
    """Resolve and ownership-check a workflow value, returning its PG UUID."""
    workflow_id = normalize_workflow_reference(workflow_value)
    if not workflow_id:
        return None, None
    repo = WorkflowsRepository(conn)
    if looks_like_uuid(workflow_id):
        workflow = repo.get(workflow_id, user)
    else:
        workflow = repo.get_by_legacy_id(workflow_id, user)
    if workflow is None:
        return None, make_response(
            jsonify({"success": False, "message": "Workflow not found"}), 404
        )
    return str(workflow["id"]), None


def _authorize_sources_or_error(conn, user, source_uuids):
    """Resolve ``source_uuids`` for agent CRUD (untrusted request input).

    Returns ``(visible_ids, error_response)``. On success ``error_response``
    is None and ``visible_ids`` is the input-order list of UUIDs the user
    can see. On any malformed or invisible ID the function returns the
    ready-to-return Flask error response (400 for malformed, 403 for
    invisible) so the caller can ``return err`` directly.

    Centralises agent-CRUD source-validation policy in one place — both
    create and update used to inline this differently, with create
    silently dropping non-UUIDs while update returned 400 for them.
    """
    resolved = SourceVisibilityService(conn).resolve(user, source_uuids)
    if resolved.invalid:
        return [], make_response(
            jsonify({
                "success": False,
                "message": f"Invalid source ID format: {resolved.invalid[0]}",
            }),
            400,
        )
    if resolved.missing:
        return [], make_response(
            jsonify({
                "success": False,
                "message": (
                    "Source not found or not visible: "
                    f"{resolved.missing[0]}"
                ),
            }),
            403,
        )
    return resolved.visible, None


def _resolve_folder_id(conn, folder_id, user):
    """Resolve a folder id (UUID or legacy) to its PG UUID; error response otherwise."""
    if not folder_id:
        return None, None
    repo = AgentFoldersRepository(conn)
    folder = None
    if looks_like_uuid(folder_id):
        folder = repo.get(folder_id, user)
    if folder is None:
        folder = repo.get_by_legacy_id(folder_id, user)
    if folder is None:
        return None, make_response(
            jsonify({"success": False, "message": "Folder not found"}), 404
        )
    return str(folder["id"]), None


def _build_create_kwargs(data: dict, *, image_url: str, agent_type: str) -> dict:
    """Translate request data + resolved references into AgentsRepository.create kwargs."""
    kwargs: dict = {}

    schema = AGENT_TYPE_SCHEMAS.get(agent_type, AGENT_TYPE_SCHEMAS["classic"])
    allowed_fields = set(schema["fields"])

    for key in (
        "description", "agent_type", "key", "image", "retriever",
        "default_model_id",
    ):
        if key in allowed_fields and data.get(key) not in (None, ""):
            kwargs[key] = data[key]

    if image_url and "image" in allowed_fields:
        kwargs["image"] = image_url

    if "source_id" in allowed_fields and data.get("source_id"):
        kwargs["source_id"] = data["source_id"]
    if "extra_source_ids" in allowed_fields and data.get("extra_source_ids"):
        kwargs["extra_source_ids"] = data["extra_source_ids"]

    if "prompt_id" in allowed_fields:
        prompt_val = data.get("prompt_id")
        if prompt_val and prompt_val != "default" and looks_like_uuid(prompt_val):
            kwargs["prompt_id"] = prompt_val

    if "folder_id" in allowed_fields and data.get("folder_id"):
        kwargs["folder_id"] = data["folder_id"]

    if "workflow_id" in allowed_fields and data.get("workflow_id"):
        kwargs["workflow_id"] = data["workflow_id"]

    if "chunks" in allowed_fields:
        chunks_val = data.get("chunks")
        if chunks_val not in (None, ""):
            try:
                kwargs["chunks"] = int(chunks_val)
            except (TypeError, ValueError):
                current_app.logger.debug(
                    "Ignoring invalid 'chunks' value while building agent create kwargs: %r",
                    chunks_val,
                )

    for key in ("limited_token_mode", "limited_request_mode", "allow_system_prompt_override"):
        if key in allowed_fields and key in data:
            raw = data[key]
            kwargs[key] = raw == "True" if isinstance(raw, str) else bool(raw)

    for key in ("token_limit", "request_limit"):
        if key in allowed_fields and data.get(key) not in (None, ""):
            try:
                kwargs[key] = int(data[key])
            except (TypeError, ValueError):
                current_app.logger.debug(
                    "Ignoring invalid %s value while building agent create kwargs: %r",
                    key,
                    data.get(key),
                )

    if "tools" in allowed_fields and data.get("tools") is not None:
        kwargs["tools"] = data["tools"]
    if "json_schema" in allowed_fields and data.get("json_schema") is not None:
        kwargs["json_schema"] = data["json_schema"]
    if "models" in allowed_fields and data.get("models") is not None:
        kwargs["models"] = data["models"]

    return kwargs


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------


def get_user_agent(conn, agent_id, user):
    """Return the formatted agent dict, or None if invisible to ``user``."""
    agent = AgentsRepository(conn).get_any(agent_id, user)
    if not agent:
        return None
    return _format_agent_output(agent)


def list_user_agents(conn, user):
    """Return the formatted, filtered agent list for ``user``.

    Filters out agents that have neither sources nor a retriever and
    aren't workflow agents — these would be unusable from the UI.
    """
    users_repo = UsersRepository(conn)
    user_doc = users_repo.upsert(user)
    pinned_ids = set(
        user_doc.get("agent_preferences", {}).get("pinned", [])
        if isinstance(user_doc.get("agent_preferences"), dict)
        else []
    )
    agents = AgentsRepository(conn).list_for_user(user)
    return [
        _format_agent_output(agent, pinned=str(agent["id"]) in pinned_ids)
        for agent in agents
        if agent.get("source_id")
        or (agent.get("extra_source_ids") or [])
        or agent.get("retriever")
        or agent.get("agent_type") == "workflow"
    ]


def list_pinned_agents(conn, user_id):
    """Return formatted pinned agents for ``user_id`` and clean stale pin ids."""
    from sqlalchemy import text as _sql_text

    users_repo = UsersRepository(conn)
    user_doc = users_repo.upsert(user_id)
    pinned_ids = (
        user_doc.get("agent_preferences", {}).get("pinned", [])
        if isinstance(user_doc.get("agent_preferences"), dict)
        else []
    )
    if not pinned_ids:
        return []

    uuid_pinned = [pid for pid in pinned_ids if looks_like_uuid(pid)]
    non_uuid = [pid for pid in pinned_ids if not looks_like_uuid(pid)]

    if uuid_pinned:
        result = conn.execute(
            _sql_text(
                "SELECT * FROM agents "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": uuid_pinned},
        )
        pinned_agents = [dict(row._mapping) for row in result.fetchall()]
    else:
        pinned_agents = []

    existing_ids = {str(a["id"]) for a in pinned_agents}
    stale = [pid for pid in uuid_pinned if pid not in existing_ids]
    stale.extend(non_uuid)
    if stale:
        users_repo.remove_pinned_bulk(user_id, stale)

    formatted = []
    for agent in pinned_agents:
        source_id = agent.get("source_id")
        if not source_id and not agent.get("retriever"):
            continue
        from application.api.user.base import resolve_tool_details

        formatted.append(
            {
                "id": str(agent["id"]),
                "name": agent.get("name", ""),
                "description": agent.get("description", ""),
                "image": (
                    generate_image_url(agent["image"]) if agent.get("image") else ""
                ),
                "source": str(source_id) if source_id else "",
                "chunks": str(agent["chunks"]) if agent.get("chunks") is not None else "",
                "retriever": agent.get("retriever", "") or "",
                "prompt_id": str(agent["prompt_id"]) if agent.get("prompt_id") else "",
                "tools": agent.get("tools", []) or [],
                "tool_details": resolve_tool_details(agent.get("tools", []) or []),
                "agent_type": agent.get("agent_type", "") or "",
                "status": agent.get("status", "") or "",
                "created_at": agent.get("created_at", ""),
                "updated_at": agent.get("updated_at", ""),
                "last_used_at": agent.get("last_used_at", ""),
                "key": (
                    f"{agent['key'][:4]}...{agent['key'][-4:]}"
                    if agent.get("key")
                    else ""
                ),
                "pinned": True,
            }
        )
    return formatted


def list_template_agents(conn):
    """Return the public template agents (system-owned)."""
    from sqlalchemy import text as _sql_text

    result = conn.execute(
        _sql_text(
            "SELECT * FROM agents "
            "WHERE user_id IN ('system', '__system__') "
            "ORDER BY name"
        ),
    )
    template_rows = [dict(row._mapping) for row in result.fetchall()]
    return [
        {
            "id": str(agent["id"]),
            "name": agent.get("name"),
            "description": agent.get("description") or "",
            "image": agent.get("image") or "",
        }
        for agent in template_rows
    ]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def validate_create_request(data):
    """Pre-DB validation for the create-agent payload.

    Mutates ``data`` in place to normalise ``json_schema``. Returns a
    Flask error response on failure or ``None`` on success.

    Kept separate from :func:`create_agent` so the route can fail fast
    before performing image upload — otherwise a malformed payload (bad
    JSON schema / missing required field / unsupported status) would
    leave an uploaded image on disk that the caller never persisted.
    DB-dependent checks (source visibility, folder/workflow ownership)
    still happen inside ``create_agent`` after upload.
    """
    if "json_schema" in data:
        try:
            data["json_schema"] = normalize_json_schema_payload(
                data.get("json_schema")
            )
        except JsonSchemaValidationError:
            return make_response(
                jsonify({"success": False, "message": "Invalid JSON schema"}),
                400,
            )

    if data.get("status") not in ["draft", "published"]:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Status must be either 'draft' or 'published'",
                }
            ),
            400,
        )

    agent_type = data.get("agent_type", "")
    if not agent_type or agent_type not in AGENT_TYPE_SCHEMAS:
        schema = AGENT_TYPE_SCHEMAS["classic"]
    else:
        schema = AGENT_TYPE_SCHEMAS[agent_type]

    is_published = data.get("status") == "published"
    if is_published:
        required_fields = schema["required_published"]
        validate_fields = schema["validate_published"]
        if (
            schema.get("require_source")
            and not data.get("source")
            and not data.get("sources")
        ):
            return make_response(
                jsonify(
                    {
                        "success": False,
                        "message": "Either 'source' or 'sources' field is required for published agents",
                    }
                ),
                400,
            )
    else:
        required_fields = schema["required_draft"]
        validate_fields = schema["validate_draft"]

    missing = check_required_fields(data, required_fields)
    if missing:
        return missing
    invalid = validate_required_fields(data, validate_fields)
    if invalid:
        return invalid
    return None


def create_agent(conn, user, data, image_url):
    """Create an agent for ``user`` from a *pre-validated* request payload.

    The caller is expected to have run :func:`validate_create_request`
    against ``data`` first (the route does so before image upload). This
    function only handles checks that need a DB connection: source
    visibility, folder ownership, workflow ownership.

    Returns ``(payload, err)`` where ``payload`` is ``{"id": ..., "key": ...}``
    on success and ``err`` is a Flask error response on failure.
    """
    agent_type = data.get("agent_type") or "classic"
    if agent_type not in AGENT_TYPE_SCHEMAS:
        agent_type = "classic"
    is_published = data.get("status") == "published"

    key = str(uuid.uuid4()) if is_published else ""

    pg_folder_id = None
    if data.get("folder_id"):
        pg_folder_id, err = _resolve_folder_id(conn, data["folder_id"], user)
        if err:
            return None, err

    pg_workflow_id = None
    if agent_type == "workflow":
        pg_workflow_id, err = _resolve_workflow_for_user(
            conn, data.get("workflow"), user,
        )
        if err and is_published:
            return None, err
        if pg_workflow_id is None and is_published:
            return None, make_response(
                jsonify({"success": False, "message": "Workflow is required"}),
                400,
            )

    # Resolve sources via SourceVisibilityService. The service partitions
    # inputs into invalid (malformed UUID) / missing (well-formed but
    # not visible) / visible, so both create and update reject malformed
    # entries with 400 (previously create silently dropped non-UUIDs
    # while update returned 400 — divergent behaviour).
    #
    # Container shape (list vs single string) is the caller's
    # responsibility — the service iterates whatever it's given, and a
    # string would be iterated character-by-character. Validate the
    # contract here before handing off.
    if data.get("sources") is not None:
        if not isinstance(data["sources"], list):
            return None, make_response(
                jsonify({
                    "success": False,
                    "message": (
                        "Field 'sources' must be a list of source UUIDs."
                    ),
                }),
                400,
            )
        requested = data["sources"]
        extras_only = True
    else:
        extras_only = False
        source_value = data.get("source", "")
        requested = [source_value] if source_value else []

    visible_ids, err = _authorize_sources_or_error(conn, user, requested)
    if err is not None:
        return None, err

    source_id_resolved = None
    extra_source_ids: list[str] = []
    if extras_only:
        extra_source_ids = visible_ids
    elif visible_ids:
        source_id_resolved = visible_ids[0]

    build_data = dict(data)
    build_data["folder_id"] = pg_folder_id
    build_data["workflow_id"] = pg_workflow_id
    build_data["source_id"] = source_id_resolved
    build_data["extra_source_ids"] = extra_source_ids
    build_data["key"] = key
    build_data["agent_type"] = agent_type

    # Classic agents: default chunks/retriever if nothing else supplied.
    if agent_type != "workflow":
        if build_data.get("chunks") in (None, ""):
            build_data["chunks"] = 2
        if (
            not source_id_resolved
            and not extra_source_ids
            and not build_data.get("retriever")
        ):
            build_data["retriever"] = "classic"

    kwargs = _build_create_kwargs(
        build_data, image_url=image_url, agent_type=agent_type,
    )
    agent_row = AgentsRepository(conn).create(
        user,
        data["name"],
        data["status"],
        **kwargs,
    )
    new_id = str(agent_row["id"])
    return {"id": new_id, "key": key}, None


def update_agent(conn, user, existing_agent, data, image_url):
    """Update an already-fetched agent row from a parsed payload.

    The route looks up ``existing_agent`` (it needs the current image
    path for ``handle_image_upload`` anyway) and passes it through here
    so we don't double-query.

    Returns ``(payload, err)`` where ``payload`` is the success response
    body (without the HTTP wrapper) and ``err`` is a Flask error response
    on failure.
    """
    agents_repo = AgentsRepository(conn)
    pg_agent_id = str(existing_agent["id"])

    update_fields: dict = {}
    allowed_fields = [
        "name",
        "description",
        "image",
        "source",
        "sources",
        "chunks",
        "retriever",
        "prompt_id",
        "tools",
        "agent_type",
        "status",
        "json_schema",
        "limited_token_mode",
        "token_limit",
        "limited_request_mode",
        "request_limit",
        "models",
        "default_model_id",
        "folder_id",
        "workflow",
        "allow_system_prompt_override",
    ]

    for field in allowed_fields:
        if field not in data:
            continue
        if field == "status":
            new_status = data.get("status")
            if new_status not in ["draft", "published"]:
                return None, make_response(
                    jsonify(
                        {
                            "success": False,
                            "message": "Invalid status value. Must be 'draft' or 'published'",
                        }
                    ),
                    400,
                )
            update_fields["status"] = new_status
        elif field == "source":
            source_id = data.get("source")
            if not source_id or source_id == "default":
                update_fields["source_id"] = None
                continue
            visible, err = _authorize_sources_or_error(conn, user, [source_id])
            if err is not None:
                return None, err
            update_fields["source_id"] = visible[0] if visible else None
        elif field == "sources":
            sources_list = data.get("sources", []) or []
            if not isinstance(sources_list, list):
                return None, make_response(
                    jsonify({
                        "success": False,
                        "message": (
                            "Field 'sources' must be a list of source UUIDs."
                        ),
                    }),
                    400,
                )
            visible, err = _authorize_sources_or_error(conn, user, sources_list)
            if err is not None:
                return None, err
            update_fields["extra_source_ids"] = visible
        elif field == "chunks":
            chunks_value = data.get("chunks")
            if chunks_value in ("", None):
                update_fields["chunks"] = 2
            else:
                try:
                    chunks_int = int(chunks_value)
                    if chunks_int < 0:
                        return None, make_response(
                            jsonify(
                                {
                                    "success": False,
                                    "message": "Chunks value must be a non-negative integer",
                                }
                            ),
                            400,
                        )
                    update_fields["chunks"] = chunks_int
                except (ValueError, TypeError):
                    return None, make_response(
                        jsonify(
                            {
                                "success": False,
                                "message": f"Invalid chunks value: {chunks_value}",
                            }
                        ),
                        400,
                    )
        elif field == "tools":
            tools_list = data.get("tools", [])
            if not isinstance(tools_list, list):
                return None, make_response(
                    jsonify({"success": False, "message": "Tools must be a list"}),
                    400,
                )
            update_fields["tools"] = tools_list
        elif field == "json_schema":
            json_schema = data.get("json_schema")
            if json_schema is not None:
                try:
                    update_fields["json_schema"] = normalize_json_schema_payload(
                        json_schema
                    )
                except JsonSchemaValidationError:
                    return None, make_response(
                        jsonify({"success": False, "message": "Invalid JSON schema"}),
                        400,
                    )
            else:
                update_fields["json_schema"] = None
        elif field == "limited_token_mode":
            raw_value = data.get("limited_token_mode", False)
            bool_value = (
                raw_value == "True"
                if isinstance(raw_value, str)
                else bool(raw_value)
            )
            update_fields["limited_token_mode"] = bool_value
            if bool_value and data.get("token_limit") is None:
                return None, make_response(
                    jsonify(
                        {
                            "success": False,
                            "message": "Token limit must be provided when limited token mode is enabled",
                        }
                    ),
                    400,
                )
        elif field == "limited_request_mode":
            raw_value = data.get("limited_request_mode", False)
            bool_value = (
                raw_value == "True"
                if isinstance(raw_value, str)
                else bool(raw_value)
            )
            update_fields["limited_request_mode"] = bool_value
            if bool_value and data.get("request_limit") is None:
                return None, make_response(
                    jsonify(
                        {
                            "success": False,
                            "message": "Request limit must be provided when limited request mode is enabled",
                        }
                    ),
                    400,
                )
        elif field == "token_limit":
            token_limit = data.get("token_limit")
            update_fields["token_limit"] = int(token_limit) if token_limit else 0
            if update_fields["token_limit"] > 0 and not data.get("limited_token_mode"):
                return None, make_response(
                    jsonify(
                        {
                            "success": False,
                            "message": "Token limit cannot be set when limited token mode is disabled",
                        }
                    ),
                    400,
                )
        elif field == "request_limit":
            request_limit = data.get("request_limit")
            update_fields["request_limit"] = int(request_limit) if request_limit else 0
            if update_fields["request_limit"] > 0 and not data.get("limited_request_mode"):
                return None, make_response(
                    jsonify(
                        {
                            "success": False,
                            "message": "Request limit cannot be set when limited request mode is disabled",
                        }
                    ),
                    400,
                )
        elif field == "folder_id":
            folder_input = data.get("folder_id")
            if folder_input:
                pg_folder_id, folder_err = _resolve_folder_id(
                    conn, folder_input, user,
                )
                if folder_err:
                    return None, folder_err
                update_fields["folder_id"] = pg_folder_id
            else:
                update_fields["folder_id"] = None
        elif field == "workflow":
            workflow_required = (
                data.get("status", existing_agent.get("status")) == "published"
                and data.get("agent_type", existing_agent.get("agent_type"))
                == "workflow"
            )
            workflow_input = data.get("workflow")
            normalized = normalize_workflow_reference(workflow_input)
            if not normalized:
                if workflow_required:
                    return None, make_response(
                        jsonify({"success": False, "message": "Workflow is required"}),
                        400,
                    )
                update_fields["workflow_id"] = None
            else:
                pg_workflow_id, wf_err = _resolve_workflow_for_user(
                    conn, workflow_input, user,
                )
                if wf_err:
                    return None, wf_err
                update_fields["workflow_id"] = pg_workflow_id
        elif field == "prompt_id":
            value = data["prompt_id"]
            if not value or value == "default":
                update_fields["prompt_id"] = None
            elif looks_like_uuid(value):
                update_fields["prompt_id"] = value
            else:
                return None, make_response(
                    jsonify(
                        {"success": False, "message": f"Invalid prompt_id: {value}"}
                    ),
                    400,
                )
        elif field == "allow_system_prompt_override":
            raw_value = data.get("allow_system_prompt_override", False)
            update_fields["allow_system_prompt_override"] = (
                raw_value == "True"
                if isinstance(raw_value, str)
                else bool(raw_value)
            )
        else:
            value = data[field]
            if field in ["name", "description", "agent_type"]:
                if not value or not str(value).strip():
                    return None, make_response(
                        jsonify(
                            {
                                "success": False,
                                "message": f"Field '{field}' cannot be empty",
                            }
                        ),
                        400,
                    )
            update_fields[field] = value
    if image_url:
        update_fields["image"] = image_url
    if not update_fields:
        return None, make_response(
            jsonify(
                {
                    "success": False,
                    "message": "No valid update data provided",
                }
            ),
            400,
        )

    newly_generated_key = None
    final_status = update_fields.get("status", existing_agent.get("status"))
    final_agent_type = update_fields.get(
        "agent_type", existing_agent.get("agent_type")
    )

    if final_status == "published":
        if final_agent_type == "workflow":
            missing_published_fields = []
            if not update_fields.get("name", existing_agent.get("name")):
                missing_published_fields.append("Agent name")
            workflow_final = update_fields.get(
                "workflow_id", existing_agent.get("workflow_id"),
            )
            if not workflow_final:
                missing_published_fields.append("Workflow")
            if missing_published_fields:
                return None, make_response(
                    jsonify(
                        {
                            "success": False,
                            "message": f"Cannot publish workflow agent. Missing required fields: {', '.join(missing_published_fields)}",
                        }
                    ),
                    400,
                )
        else:
            missing_published_fields = []
            for req_field, field_label in (
                ("name", "Agent name"),
                ("description", "Agent description"),
                ("chunks", "Chunks count"),
                ("prompt_id", "Prompt"),
                ("agent_type", "Agent type"),
            ):
                final_value = update_fields.get(
                    req_field, existing_agent.get(req_field)
                )
                if not final_value:
                    missing_published_fields.append(field_label)
            source_final = update_fields.get(
                "source_id", existing_agent.get("source_id"),
            )
            extra_final = update_fields.get(
                "extra_source_ids", existing_agent.get("extra_source_ids") or [],
            )
            if not source_final and not extra_final:
                missing_published_fields.append("Source")
            if missing_published_fields:
                return None, make_response(
                    jsonify(
                        {
                            "success": False,
                            "message": f"Cannot publish agent. Missing or invalid required fields: {', '.join(missing_published_fields)}",
                        }
                    ),
                    400,
                )
        if not existing_agent.get("key"):
            newly_generated_key = str(uuid.uuid4())
            update_fields["key"] = newly_generated_key

    updated = agents_repo.update(pg_agent_id, user, update_fields)
    if not updated:
        return None, make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Agent not found or update failed",
                }
            ),
            404,
        )

    response_data = {
        "success": True,
        "id": pg_agent_id,
        "message": "Agent updated successfully",
    }
    if newly_generated_key:
        response_data["key"] = newly_generated_key
    return response_data, None


def delete_agent(conn, user, agent_id):
    """Delete an agent (and its workflow, if any). Returns ``(pg_id, err)``."""
    agents_repo = AgentsRepository(conn)
    agent = agents_repo.get_any(agent_id, user)
    if not agent:
        return None, make_response(
            jsonify({"success": False, "message": "Agent not found"}), 404
        )
    pg_agent_id = str(agent["id"])
    workflow_id = agent.get("workflow_id")
    # For workflow-type agents, delete the owned workflow in the same
    # transaction. workflow_nodes/workflow_edges cascade via ON DELETE
    # CASCADE so a single workflow delete suffices.
    if agent.get("agent_type") == "workflow" and workflow_id:
        try:
            WorkflowNodesRepository(conn).delete_by_workflow(str(workflow_id))
            WorkflowEdgesRepository(conn).delete_by_workflow(str(workflow_id))
            WorkflowsRepository(conn).delete(str(workflow_id), user)
        except Exception as wf_err:
            current_app.logger.warning(
                f"Workflow cleanup failed for agent {pg_agent_id}: {wf_err}"
            )
    agents_repo.delete(pg_agent_id, user)
    UsersRepository(conn).remove_agent_from_all(user, pg_agent_id)
    return pg_agent_id, None


def adopt_template(conn, user, agent_id):
    """Copy a system-template agent into ``user``'s account.

    Returns ``({"success": True, "agent": {...}}, None)`` on success.
    Filters template source attachments through the adopter's visibility
    scope: a template SHOULD only reference public sources, but if a
    template was misconfigured (or shipped from a different environment)
    we drop the invisible UUIDs rather than persist them onto the new row.
    """
    from sqlalchemy import text as _sql_text

    if looks_like_uuid(agent_id):
        template_row = conn.execute(
            _sql_text(
                "SELECT * FROM agents "
                "WHERE id = CAST(:id AS uuid) "
                "AND user_id IN ('system', '__system__')"
            ),
            {"id": agent_id},
        ).fetchone()
    else:
        template_row = conn.execute(
            _sql_text(
                "SELECT * FROM agents "
                "WHERE legacy_mongo_id = :id "
                "AND user_id IN ('system', '__system__')"
            ),
            {"id": agent_id},
        ).fetchone()
    if template_row is None:
        return None, make_response(jsonify({"status": "Not found"}), 404)
    template = dict(template_row._mapping)

    now = datetime.datetime.now(datetime.timezone.utc)
    new_key = str(uuid.uuid4())
    create_kwargs: dict = {}
    template_primary = template.get("source_id")
    template_extras = list(template.get("extra_source_ids") or [])
    template_source_ids = [
        str(s) for s in [template_primary, *template_extras] if s
    ]
    if template_source_ids:
        resolved = SourceVisibilityService(conn).resolve(
            user, template_source_ids,
        )
        if resolved.missing or resolved.invalid:
            current_app.logger.warning(
                f"AdoptAgent: template {agent_id} references "
                "sources not visible to "
                f"{user}: missing={resolved.missing} "
                f"invalid={resolved.invalid}"
            )
        if (
            template_primary
            and str(template_primary) in resolved.rows
        ):
            create_kwargs["source_id"] = str(template_primary)
        create_kwargs["extra_source_ids"] = [
            str(s) for s in template_extras
            if str(s) in resolved.rows
        ]
    for col in (
        "description", "agent_type", "image", "retriever",
        "default_model_id",
        "prompt_id", "folder_id", "workflow_id",
    ):
        val = template.get(col)
        if val not in (None, ""):
            create_kwargs[col] = val
    for col in ("tools", "json_schema", "models", "shared_metadata"):
        if template.get(col) is not None:
            create_kwargs[col] = template[col]
    for col in ("chunks", "token_limit", "request_limit"):
        if template.get(col) is not None:
            create_kwargs[col] = template[col]
    for col in (
        "limited_token_mode", "limited_request_mode",
        "allow_system_prompt_override",
    ):
        if template.get(col) is not None:
            create_kwargs[col] = bool(template[col])

    create_kwargs["key"] = new_key
    create_kwargs["last_used_at"] = now

    new_agent = AgentsRepository(conn).create(
        user,
        template.get("name") or "",
        "published",
        **create_kwargs,
    )

    response_agent = _format_agent_output(new_agent, include_key_masked=False)
    response_agent["key"] = new_key
    return {"success": True, "agent": response_agent}, None


def toggle_pin(conn, user_id, agent_id):
    """Toggle pin state for ``agent_id`` in ``user_id``'s prefs.

    Returns ``("pinned" | "unpinned", None)`` on success. Any user can
    pin any agent they can see — including shared ones — so we use the
    non-user-scoped lookup here rather than restricting to owner.
    """
    from sqlalchemy import text as _sql_text

    if looks_like_uuid(agent_id):
        agent_row = conn.execute(
            _sql_text("SELECT id FROM agents WHERE id = CAST(:id AS uuid)"),
            {"id": agent_id},
        ).fetchone()
    else:
        agent_row = conn.execute(
            _sql_text("SELECT id FROM agents WHERE legacy_mongo_id = :id"),
            {"id": agent_id},
        ).fetchone()
    if agent_row is None:
        return None, make_response(
            jsonify({"success": False, "message": "Agent not found"}),
            404,
        )
    pg_agent_id = str(agent_row._mapping["id"])

    users_repo = UsersRepository(conn)
    user_doc = users_repo.upsert(user_id)
    pinned_list = (
        user_doc.get("agent_preferences", {}).get("pinned", [])
        if isinstance(user_doc.get("agent_preferences"), dict)
        else []
    )
    if pg_agent_id in pinned_list:
        users_repo.remove_pinned(user_id, pg_agent_id)
        return "unpinned", None
    users_repo.add_pinned(user_id, pg_agent_id)
    return "pinned", None


def remove_shared_agent(conn, user_id, agent_id):
    """Remove a shared agent from ``user_id``'s prefs. Returns ``(_, err)``."""
    from sqlalchemy import text as _sql_text

    if looks_like_uuid(agent_id):
        agent_row = conn.execute(
            _sql_text(
                "SELECT id FROM agents "
                "WHERE id = CAST(:id AS uuid) AND shared = true"
            ),
            {"id": agent_id},
        ).fetchone()
    else:
        agent_row = conn.execute(
            _sql_text(
                "SELECT id FROM agents "
                "WHERE legacy_mongo_id = :id AND shared = true"
            ),
            {"id": agent_id},
        ).fetchone()
    if agent_row is None:
        return None, make_response(
            jsonify({"success": False, "message": "Shared agent not found"}),
            404,
        )
    pg_agent_id = str(agent_row._mapping["id"])
    users_repo = UsersRepository(conn)
    users_repo.upsert(user_id)
    users_repo.remove_agent_from_all(user_id, pg_agent_id)
    return pg_agent_id, None
