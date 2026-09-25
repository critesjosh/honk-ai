"""MCP OAuth dance — runs the discovery handshake in a worker.

The Celery task wrappers ``mcp_oauth_task`` / ``mcp_oauth_status_task``
used to live in ``application/api/user/tasks.py``. That module was
deleted alongside the upstream admin SPA; the wrappers now live here
next to the worker functions they dispatch to.
``application/agents/tools/mcp_tool.py`` imports the task wrappers for
the agent-side OAuth flow, so we keep them registered.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from application.cache import get_redis_instance
from application.celery_init import celery


def mcp_oauth(self, config: Dict[str, Any], user_id: str = None) -> Dict[str, Any]:
    """Worker to handle MCP OAuth flow asynchronously."""

    try:
        import asyncio

        from application.agents.tools.mcp_tool import MCPTool

        task_id = self.request.id
        redis_client = get_redis_instance()

        def update_status(status_data: Dict[str, Any]):
            status_key = f"mcp_oauth_status:{task_id}"
            redis_client.setex(status_key, 600, json.dumps(status_data))

        update_status({
            "status": "in_progress",
            "message": "Starting OAuth...",
            "task_id": task_id,
        })

        tool_config = config.copy()
        tool_config["oauth_task_id"] = task_id
        mcp_tool = MCPTool(tool_config, user_id)

        async def run_oauth_discovery():
            if not mcp_tool._client:
                mcp_tool._setup_client()
            return await mcp_tool._execute_with_client("list_tools")

        update_status({
            "status": "awaiting_redirect",
            "message": "Awaiting OAuth redirect...",
            "task_id": task_id,
        })

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(run_oauth_discovery())
            tools = mcp_tool.get_actions_metadata()

            update_status({
                "status": "completed",
                "message": (
                    f"Connected — found {len(tools)} "
                    f"tool{'s' if len(tools) != 1 else ''}."
                ),
                "tools": tools,
                "tools_count": len(tools),
                "task_id": task_id,
            })

            return {"success": True, "tools": tools, "tools_count": len(tools)}
        except Exception as e:
            error_msg = f"OAuth failed: {str(e)}"
            logging.error("MCP OAuth discovery failed: %s", error_msg, exc_info=True)
            update_status({
                "status": "error",
                "message": error_msg,
                "task_id": task_id,
            })
            return {"success": False, "error": error_msg}
        finally:
            loop.close()
    except Exception as e:
        error_msg = f"OAuth init failed: {str(e)}"
        logging.error("MCP OAuth init failed: %s", error_msg, exc_info=True)
        update_status({
            "status": "error",
            "message": error_msg,
            "task_id": task_id,
        })
        return {"success": False, "error": error_msg}


def mcp_oauth_status(self, task_id: str) -> Dict[str, Any]:
    """Check the status of an MCP OAuth flow."""
    redis_client = get_redis_instance()
    status_key = f"mcp_oauth_status:{task_id}"

    status_data = redis_client.get(status_key)
    if status_data:
        return json.loads(status_data)
    return {"status": "not_found", "message": "Status not found"}


@celery.task(bind=True)
def mcp_oauth_task(self, config, user):
    return mcp_oauth(self, config, user)


@celery.task(bind=True)
def mcp_oauth_status_task(self, task_id):
    return mcp_oauth_status(self, task_id)
