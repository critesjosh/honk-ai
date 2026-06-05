"""Provision (or refresh) the Aztec + Ethereum network-status agent tools.

Creates two ``user_tools`` rows under ``user_id='local'`` — one
``aztec_network``, one ``ethereum_network`` — with every action flagged
``active=true``. With ``--attach-to-agents``, also appends both tool
UUIDs to the ``agents.tools`` JSONB array for every agent matching
``user_id='local' AND surface IN ('discord','widget')``.

Idempotent. Re-running refreshes the action lists (in case we add new
actions later) and is a no-op for already-attached agents. Tool UUIDs
are preserved across runs.

Usage::

    docker compose -f deployment/docker-compose-hub.yaml run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro \\
        -e PYTHONPATH=/app backend \\
        python scripts/db/create_network_tools.py --attach-to-agents

Or with ``--dry-run`` to print what would change without writing.

Rollback: a successful run prints the exact UUID-scoped rollback SQL for
the two tools it provisioned — prefer that over a blanket reset. The
UUID-scoped form removes ONLY these two tools from ``agents.tools`` and
leaves any other attached tools intact::

    UPDATE agents SET tools = (
        SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb)
        FROM jsonb_array_elements_text(tools) elem
        WHERE elem NOT IN ('<aztec_uuid>', '<ethereum_uuid>')
    )
    WHERE user_id = 'local' AND surface IN ('discord','widget');
    UPDATE user_tools SET status = false
        WHERE user_id = 'local' AND name IN ('aztec_network', 'ethereum_network');
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from application.agents.tools.aztec_network import AztecNetworkTool
from application.agents.tools.ethereum_network import EthereumNetworkTool
from application.storage.db.session import db_session


TOOL_USER_ID = "local"

# (name, display_name, description, tool_class). The display_name and
# description show up in the operator's tool listing only — agents see
# the per-action descriptions from get_actions_metadata().
_TOOLS = [
    (
        "aztec_network",
        "Aztec Network Status",
        "Query Aztec L2 chain head, finalization, validators, and RPC nodes via Aztecscan.",
        AztecNetworkTool,
    ),
    (
        "ethereum_network",
        "Ethereum Network Status",
        "Query Ethereum L1 mainnet/Sepolia block number, gas price, chain id, sync status via JSON-RPC.",
        EthereumNetworkTool,
    ),
]


def _actions_payload(tool_cls) -> list[dict]:
    """Build the ``user_tools.actions`` JSONB body from a Tool class."""
    instance = tool_cls(config={})
    actions = []
    for meta in instance.get_actions_metadata():
        actions.append(
            {
                "name": meta["name"],
                "description": meta.get("description", ""),
                "parameters": meta.get("parameters", {}),
                "active": True,
            }
        )
    return actions


def _config_requirements_payload(tool_cls) -> dict:
    """Build the ``user_tools.config_requirements`` JSONB from a Tool class.

    Persisting this makes the per-tool override fields (base URL / API
    key / RPC URLs, and their secret flags) discoverable to any operator
    inspecting the row — without it the override surface is invisible.
    """
    return tool_cls(config={}).get_config_requirements()


def _upsert_tool(
    conn, name: str, display_name: str, description: str, actions: list[dict], config_reqs: dict
) -> str:
    """Insert or refresh a ``user_tools`` row. Returns its UUID."""
    existing = conn.execute(
        text(
            "SELECT id FROM user_tools WHERE user_id = :uid AND name = :name LIMIT 1"
        ),
        {"uid": TOOL_USER_ID, "name": name},
    ).fetchone()

    if existing is None:
        row_id = conn.execute(
            text(
                """
                INSERT INTO user_tools (
                    user_id, name, custom_name, display_name, description,
                    config, config_requirements, actions, status
                )
                VALUES (
                    :uid, :name, :name, :display_name, :description,
                    '{}'::jsonb, CAST(:config_requirements AS jsonb),
                    CAST(:actions AS jsonb), true
                )
                RETURNING id
                """
            ),
            {
                "uid": TOOL_USER_ID,
                "name": name,
                "display_name": display_name,
                "description": description,
                "config_requirements": json.dumps(config_reqs),
                "actions": json.dumps(actions),
            },
        ).scalar_one()
        print(f"Created user_tools row {name}={row_id}")
        return str(row_id)

    row_id = existing[0]
    conn.execute(
        text(
            """
            UPDATE user_tools
            SET actions = CAST(:actions AS jsonb),
                config_requirements = CAST(:config_requirements AS jsonb),
                display_name = :display_name,
                description = :description,
                status = true,
                updated_at = now()
            WHERE id = :id
            """
        ),
        {
            "id": row_id,
            "display_name": display_name,
            "description": description,
            "config_requirements": json.dumps(config_reqs),
            "actions": json.dumps(actions),
        },
    )
    print(f"Refreshed user_tools row {name}={row_id} ({len(actions)} actions)")
    return str(row_id)


def _attach_to_agents(conn, tool_ids: list[str]) -> None:
    """Append each tool UUID to ``agents.tools`` for Discord + widget
    agents, only when not already present. UUIDs are stored as JSONB
    strings, so the duplicate check uses JSON containment."""
    agents = conn.execute(
        text(
            "SELECT id, name, tools FROM agents "
            "WHERE user_id = :uid AND surface IN ('discord','widget')"
        ),
        {"uid": TOOL_USER_ID},
    ).fetchall()
    if not agents:
        print("No discord/widget agents found under user_id='local' — skipping attach.")
        return
    for agent_id, agent_name, current_tools in agents:
        current = list(current_tools or [])
        added = []
        for tid in tool_ids:
            if tid not in current:
                current.append(tid)
                added.append(tid)
        if not added:
            print(f"Agent '{agent_name}' ({agent_id}) already has both tools — no change.")
            continue
        conn.execute(
            text(
                "UPDATE agents SET tools = CAST(:tools AS jsonb), updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": agent_id, "tools": json.dumps(current)},
        )
        print(f"Agent '{agent_name}' ({agent_id}) — attached {len(added)} tool(s).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--attach-to-agents",
        action="store_true",
        help="Also append the tool UUIDs to agents.tools for discord+widget agents.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without committing.",
    )
    args = parser.parse_args()

    with db_session() as conn:
        tool_ids: list[str] = []
        for name, display_name, description, tool_cls in _TOOLS:
            actions = _actions_payload(tool_cls)
            config_reqs = _config_requirements_payload(tool_cls)
            tool_ids.append(
                _upsert_tool(conn, name, display_name, description, actions, config_reqs)
            )

        if args.attach_to_agents:
            _attach_to_agents(conn, tool_ids)

        if args.dry_run:
            print("--dry-run set — rolling back.", file=sys.stderr)
            conn.rollback()
            return 0

    print("Done. tool_ids:", ", ".join(tool_ids))
    # Print a precise, UUID-scoped rollback so an operator never has to
    # fall back to the docstring's blanket `tools = '[]'` (which would
    # also strip any OTHER tools an agent had).
    quoted = ", ".join(f"'{tid}'" for tid in tool_ids)
    print("\nTo roll back, run:")
    print(
        "  UPDATE agents SET tools = (\n"
        "      SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb)\n"
        "      FROM jsonb_array_elements_text(tools) elem\n"
        f"      WHERE elem NOT IN ({quoted})\n"
        "  )\n"
        "  WHERE user_id = 'local' AND surface IN ('discord','widget');\n"
        "  UPDATE user_tools SET status = false\n"
        "      WHERE user_id = 'local' AND name IN ('aztec_network', 'ethereum_network');"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
