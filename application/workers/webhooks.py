"""Agent webhook executor.

Receives a webhook payload, looks up the agent (by UUID or legacy
Mongo ObjectId), runs ``run_agent_logic`` on the payload, returns the
agent's structured response.
"""

from __future__ import annotations

import json
import logging

from application.storage.db.base_repository import looks_like_uuid
from application.storage.db.repositories.agents import AgentsRepository
from application.storage.db.session import db_readonly
from application.workers.agent_runtime import run_agent_logic


def agent_webhook_worker(self, agent_id, payload):
    """Process the webhook payload for an agent.

    Args:
        self: Reference to the instance of the task.
        agent_id: Unique identifier for the agent.
        payload: The payload data from the webhook.
    """
    self.update_state(state="PROGRESS", meta={"current": 1})
    try:
        with db_readonly() as conn:
            repo = AgentsRepository(conn)
            agent_config = None
            if looks_like_uuid(str(agent_id)):
                # Access without user scoping — webhooks authenticate via
                # the incoming token, not a user context.
                from sqlalchemy import text as sql_text

                from application.storage.db.base_repository import row_to_dict
                result = conn.execute(
                    sql_text("SELECT * FROM agents WHERE id = CAST(:id AS uuid)"),
                    {"id": str(agent_id)},
                )
                row = result.fetchone()
                if row is not None:
                    agent_config = row_to_dict(row)
            if agent_config is None:
                agent_config = repo.get_by_legacy_id(str(agent_id))
        if not agent_config:
            raise ValueError(f"Agent with ID {agent_id} not found.")
        input_data = json.dumps(payload)
    except Exception as e:
        logging.error(f"Error processing agent webhook: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
    self.update_state(state="PROGRESS", meta={"current": 50})
    try:
        result = run_agent_logic(agent_config, input_data)
    except Exception as e:
        logging.error(f"Error running agent logic: {e}", exc_info=True)
        return {"status": "error"}
    else:
        logging.info(
            f"Webhook processed for agent {agent_id}", extra={"agent_id": agent_id},
        )
        return {"status": "success", "result": result}
    finally:
        self.update_state(state="PROGRESS", meta={"current": 100})
