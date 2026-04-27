"""Agent management routes.

HTTP-layer only: auth, request parsing, image upload, db-session
lifecycle, response shaping. All validation and persistence logic
lives in ``service.py``; response formatting helpers live in
``serializers.py``.
"""

import json

from flask import current_app, jsonify, make_response, request
from flask_restx import fields, Namespace, Resource

from application.api import api
from application.api.user.agents import service
from application.api.user.base import handle_image_upload, storage
from application.storage.db.repositories.agents import AgentsRepository
from application.storage.db.session import db_readonly, db_session


agents_ns = Namespace("agents", description="Agent management operations", path="/api")


@agents_ns.route("/get_agent")
class GetAgent(Resource):
    @api.doc(params={"id": "Agent ID"}, description="Get agent by ID")
    def get(self):
        if not (decoded_token := request.decoded_token):
            return {"success": False}, 401
        if not (agent_id := request.args.get("id")):
            return {"success": False, "message": "ID required"}, 400
        try:
            user = decoded_token["sub"]
            with db_readonly() as conn:
                payload = service.get_user_agent(conn, agent_id, user)
            if payload is None:
                return {"status": "Not found"}, 404
            return make_response(jsonify(payload), 200)
        except Exception as e:
            current_app.logger.error(f"Agent fetch error: {e}", exc_info=True)
            return {"success": False}, 400


@agents_ns.route("/get_agents")
class GetAgents(Resource):
    @api.doc(description="Retrieve agents for the user")
    def get(self):
        if not (decoded_token := request.decoded_token):
            return {"success": False}, 401
        user = decoded_token.get("sub")
        try:
            with db_session() as conn:
                payload = service.list_user_agents(conn, user)
        except Exception as err:
            current_app.logger.error(f"Error retrieving agents: {err}", exc_info=True)
            return make_response(jsonify({"success": False}), 400)
        return make_response(jsonify(payload), 200)


@agents_ns.route("/create_agent")
class CreateAgent(Resource):
    create_agent_model = api.model(
        "CreateAgentModel",
        {
            "name": fields.String(required=True, description="Name of the agent"),
            "description": fields.String(
                required=True, description="Description of the agent"
            ),
            "image": fields.Raw(
                required=False, description="Image file upload", type="file"
            ),
            "source": fields.String(
                required=False, description="Source ID (legacy single source)"
            ),
            "sources": fields.List(
                fields.String,
                required=False,
                description="List of source identifiers for multiple sources",
            ),
            "chunks": fields.Integer(required=False, description="Chunks count"),
            "retriever": fields.String(required=False, description="Retriever ID"),
            "prompt_id": fields.String(required=False, description="Prompt ID"),
            "tools": fields.List(
                fields.String, required=False, description="List of tool identifiers"
            ),
            "agent_type": fields.String(
                required=False,
                description="Type of the agent (classic, react, workflow). Defaults to 'classic' for backwards compatibility.",
            ),
            "status": fields.String(
                required=True, description="Status of the agent (draft or published)"
            ),
            "workflow": fields.String(
                required=False, description="Workflow ID for workflow-type agents"
            ),
            "json_schema": fields.Raw(
                required=False,
                description="JSON schema for enforcing structured output format",
            ),
            "limited_token_mode": fields.Boolean(
                required=False, description="Whether the agent is in limited token mode"
            ),
            "token_limit": fields.Integer(
                required=False, description="Token limit for the agent in limited mode"
            ),
            "limited_request_mode": fields.Boolean(
                required=False,
                description="Whether the agent is in limited request mode",
            ),
            "request_limit": fields.Integer(
                required=False,
                description="Request limit for the agent in limited mode",
            ),
            "models": fields.List(
                fields.String,
                required=False,
                description="List of available model IDs for this agent",
            ),
            "default_model_id": fields.String(
                required=False, description="Default model ID for this agent"
            ),
            "folder_id": fields.String(
                required=False, description="Folder ID to organize the agent"
            ),
            "allow_system_prompt_override": fields.Boolean(
                required=False,
                description="Allow API callers to override the system prompt via the v1 endpoint",
            ),
        },
    )

    @api.expect(create_agent_model)
    @api.doc(description="Create a new agent")
    def post(self):
        if not (decoded_token := request.decoded_token):
            return {"success": False}, 401
        user = decoded_token.get("sub")
        if request.content_type == "application/json":
            data = request.get_json()
        else:
            data = request.form.to_dict()
            if "tools" in data:
                try:
                    data["tools"] = json.loads(data["tools"])
                except json.JSONDecodeError:
                    data["tools"] = []
            if "sources" in data:
                try:
                    data["sources"] = json.loads(data["sources"])
                except json.JSONDecodeError:
                    data["sources"] = []
            if "json_schema" in data:
                try:
                    data["json_schema"] = json.loads(data["json_schema"])
                except json.JSONDecodeError:
                    data["json_schema"] = None
            if "models" in data:
                try:
                    data["models"] = json.loads(data["models"])
                except json.JSONDecodeError:
                    data["models"] = []

        if (err := service.validate_create_request(data)) is not None:
            return err

        image_url, error = handle_image_upload(request, "", user, storage)
        if error:
            return make_response(
                jsonify({"success": False, "message": "Image upload failed"}), 400
            )

        try:
            with db_session() as conn:
                payload, err = service.create_agent(conn, user, data, image_url)
                if err is not None:
                    return err
        except Exception as err:
            current_app.logger.error(f"Error creating agent: {err}", exc_info=True)
            return make_response(jsonify({"success": False}), 400)
        return make_response(jsonify(payload), 201)


@agents_ns.route("/update_agent/<string:agent_id>")
class UpdateAgent(Resource):
    update_agent_model = api.model(
        "UpdateAgentModel",
        {
            "name": fields.String(required=True, description="New name of the agent"),
            "description": fields.String(
                required=True, description="New description of the agent"
            ),
            "image": fields.String(
                required=False, description="New image URL or identifier"
            ),
            "source": fields.String(
                required=False, description="Source ID (legacy single source)"
            ),
            "sources": fields.List(
                fields.String,
                required=False,
                description="List of source identifiers for multiple sources",
            ),
            "chunks": fields.Integer(required=False, description="Chunks count"),
            "retriever": fields.String(required=False, description="Retriever ID"),
            "prompt_id": fields.String(required=False, description="Prompt ID"),
            "tools": fields.List(
                fields.String, required=False, description="List of tool identifiers"
            ),
            "agent_type": fields.String(
                required=False,
                description="Type of the agent (classic, react, workflow). Defaults to 'classic' for backwards compatibility.",
            ),
            "status": fields.String(
                required=True, description="Status of the agent (draft or published)"
            ),
            "workflow": fields.String(
                required=False, description="Workflow ID for workflow-type agents"
            ),
            "json_schema": fields.Raw(
                required=False,
                description="JSON schema for enforcing structured output format",
            ),
            "limited_token_mode": fields.Boolean(
                required=False, description="Whether the agent is in limited token mode"
            ),
            "token_limit": fields.Integer(
                required=False, description="Token limit for the agent in limited mode"
            ),
            "limited_request_mode": fields.Boolean(
                required=False,
                description="Whether the agent is in limited request mode",
            ),
            "request_limit": fields.Integer(
                required=False,
                description="Request limit for the agent in limited mode",
            ),
            "models": fields.List(
                fields.String,
                required=False,
                description="List of available model IDs for this agent",
            ),
            "default_model_id": fields.String(
                required=False, description="Default model ID for this agent"
            ),
            "folder_id": fields.String(
                required=False, description="Folder ID to organize the agent"
            ),
            "allow_system_prompt_override": fields.Boolean(
                required=False,
                description="Allow API callers to override the system prompt via the v1 endpoint",
            ),
        },
    )

    @api.expect(update_agent_model)
    @api.doc(description="Update an existing agent")
    def put(self, agent_id):
        if not (decoded_token := request.decoded_token):
            return make_response(
                jsonify({"success": False, "message": "Unauthorized"}), 401
            )
        user = decoded_token.get("sub")

        try:
            if request.content_type and "application/json" in request.content_type:
                data = request.get_json()
            else:
                data = request.form.to_dict()
                json_fields = ["tools", "sources", "json_schema", "models"]
                for field in json_fields:
                    if field in data and data[field]:
                        try:
                            data[field] = json.loads(data[field])
                        except json.JSONDecodeError:
                            return make_response(
                                jsonify(
                                    {
                                        "success": False,
                                        "message": f"Invalid JSON format for field: {field}",
                                    }
                                ),
                                400,
                            )
                if data.get("json_schema") == "":
                    data["json_schema"] = None
        except Exception as err:
            current_app.logger.error(
                f"Error parsing request data: {err}", exc_info=True
            )
            return make_response(
                jsonify({"success": False, "message": "Invalid request data"}), 400
            )

        try:
            with db_session() as conn:
                existing_agent = AgentsRepository(conn).get_any(agent_id, user)
                if not existing_agent:
                    return make_response(
                        jsonify(
                            {"success": False, "message": "Agent not found or not authorized"}
                        ),
                        404,
                    )
                image_url, image_error = handle_image_upload(
                    request, existing_agent.get("image", "") or "", user, storage,
                )
                if image_error:
                    return image_error

                payload, err = service.update_agent(
                    conn, user, existing_agent, data, image_url,
                )
                if err is not None:
                    return err
        except Exception as err:
            current_app.logger.error(
                f"Error updating agent {agent_id}: {err}", exc_info=True
            )
            return make_response(
                jsonify({"success": False, "message": "Database error during update"}),
                500,
            )

        return make_response(jsonify(payload), 200)


@agents_ns.route("/delete_agent")
class DeleteAgent(Resource):
    @api.doc(params={"id": "ID of the agent"}, description="Delete an agent by ID")
    def delete(self):
        decoded_token = request.decoded_token
        if not decoded_token:
            return make_response(jsonify({"success": False}), 401)
        user = decoded_token.get("sub")
        agent_id = request.args.get("id")
        if not agent_id:
            return make_response(
                jsonify({"success": False, "message": "ID is required"}), 400
            )
        try:
            with db_session() as conn:
                pg_agent_id, err = service.delete_agent(conn, user, agent_id)
                if err is not None:
                    return err
        except Exception as err:
            current_app.logger.error(f"Error deleting agent: {err}", exc_info=True)
            return make_response(jsonify({"success": False}), 400)
        return make_response(jsonify({"id": pg_agent_id}), 200)


@agents_ns.route("/pinned_agents")
class PinnedAgents(Resource):
    @api.doc(description="Get pinned agents for the user")
    def get(self):
        decoded_token = request.decoded_token
        if not decoded_token:
            return make_response(jsonify({"success": False}), 401)
        user_id = decoded_token.get("sub")

        try:
            with db_session() as conn:
                payload = service.list_pinned_agents(conn, user_id)
        except Exception as err:
            current_app.logger.error(f"Error retrieving pinned agents: {err}")
            return make_response(jsonify({"success": False}), 400)
        return make_response(jsonify(payload), 200)


@agents_ns.route("/template_agents")
class GetTemplateAgents(Resource):
    @api.doc(description="Get template/premade agents")
    def get(self):
        try:
            with db_readonly() as conn:
                payload = service.list_template_agents(conn)
            return make_response(jsonify(payload), 200)
        except Exception as e:
            current_app.logger.error(f"Template agents fetch error: {e}", exc_info=True)
            return make_response(jsonify({"success": False}), 400)


@agents_ns.route("/adopt_agent")
class AdoptAgent(Resource):
    @api.doc(params={"id": "Agent ID"}, description="Adopt an agent by ID")
    def post(self):
        if not (decoded_token := request.decoded_token):
            return make_response(jsonify({"success": False}), 401)
        if not (agent_id := request.args.get("id")):
            return make_response(
                jsonify({"success": False, "message": "ID required"}), 400
            )
        try:
            user = decoded_token["sub"]
            with db_session() as conn:
                payload, err = service.adopt_template(conn, user, agent_id)
                if err is not None:
                    return err
            return make_response(jsonify(payload), 200)
        except Exception as e:
            current_app.logger.error(f"Agent adopt error: {e}", exc_info=True)
            return make_response(jsonify({"success": False}), 400)


@agents_ns.route("/pin_agent")
class PinAgent(Resource):
    @api.doc(params={"id": "ID of the agent"}, description="Pin or unpin an agent")
    def post(self):
        decoded_token = request.decoded_token
        if not decoded_token:
            return make_response(jsonify({"success": False}), 401)
        user_id = decoded_token.get("sub")
        agent_id = request.args.get("id")

        if not agent_id:
            return make_response(
                jsonify({"success": False, "message": "ID is required"}), 400
            )
        try:
            with db_session() as conn:
                action, err = service.toggle_pin(conn, user_id, agent_id)
                if err is not None:
                    return err
        except Exception as err:
            current_app.logger.error(f"Error pinning/unpinning agent: {err}")
            return make_response(
                jsonify({"success": False, "message": "Server error"}), 500
            )
        return make_response(jsonify({"success": True, "action": action}), 200)


@agents_ns.route("/remove_shared_agent")
class RemoveSharedAgent(Resource):
    @api.doc(
        params={"id": "ID of the shared agent"},
        description="Remove a shared agent from the current user's shared list",
    )
    def delete(self):
        decoded_token = request.decoded_token
        if not decoded_token:
            return make_response(jsonify({"success": False}), 401)
        user_id = decoded_token.get("sub")
        agent_id = request.args.get("id")

        if not agent_id:
            return make_response(
                jsonify({"success": False, "message": "ID is required"}), 400
            )
        try:
            with db_session() as conn:
                _, err = service.remove_shared_agent(conn, user_id, agent_id)
                if err is not None:
                    return err
            return make_response(jsonify({"success": True, "action": "removed"}), 200)
        except Exception as err:
            current_app.logger.error(f"Error removing shared agent: {err}")
            return make_response(
                jsonify({"success": False, "message": "Server error"}), 500
            )
