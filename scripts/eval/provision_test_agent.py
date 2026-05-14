"""Provision a throwaway "candidate" agent for eval-variant comparisons.

Use this to spin up an agent row with a custom prompt + source list +
model in the dev compose's Postgres, run ``eval_retrieval.py`` against
it, and then either reuse or delete it. The companion of
``scripts/eval/eval_retrieval.py`` and ``scripts/eval/compare.py``.

Every row this script writes is namespaced under
``user_id = 'eval-variant'`` so ``--list`` / ``--delete`` /
``--delete-all`` can find and clean them up without touching prod
agents.

Run inside the backend container so ``application.*`` imports resolve::

    docker compose -f deployment/docker-compose.yaml run --rm \\
        -v $(pwd)/scripts:/app/scripts:ro \\
        -e PYTHONPATH=/app \\
        backend python scripts/eval/provision_test_agent.py \\
            --name variant-new-prompt \\
            --prompt-file /app/scripts/eval/variants/new_prompt.txt \\
            --source-ids-from-env

The provisioner prints a single JSON line on stdout with the agent
key, id, prompt id, and model — pipe it through ``jq -r .key`` to
capture the bearer for ``eval_retrieval.py --api-key``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from application.core.settings import settings
from application.storage.db.session import db_session


VARIANT_USER_ID = "eval-variant"


def _parse_source_ids_arg(raw: str) -> list[str]:
    ids = [s.strip() for s in raw.split(",") if s.strip()]
    bad: list[str] = []
    for sid in ids:
        try:
            uuid.UUID(sid)
        except ValueError:
            bad.append(sid)
    if bad:
        print(f"non-UUID source ids: {bad}", file=sys.stderr)
        sys.exit(2)
    if not ids:
        print("source ids resolved to an empty list", file=sys.stderr)
        sys.exit(2)
    return ids


def _read_prompt_file(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        print(f"prompt file not found: {path}", file=sys.stderr)
        sys.exit(2)
    content = p.read_text(encoding="utf-8")
    if not content.strip():
        print(f"prompt file is empty: {path}", file=sys.stderr)
        sys.exit(2)
    return content


def _upsert_prompt(conn, name: str, content: str) -> str:
    """Insert or update a prompt row keyed on ``(user_id, name)``.

    Race-safe: the partial unique index ``prompts_eval_variant_name_uniq``
    (migration ``0008_eval_variant_unique``) gives ON CONFLICT a target,
    so two concurrent invocations can never produce duplicate rows.

    Returns the prompt id as a string.
    """
    pid = conn.execute(
        text(
            "INSERT INTO prompts (user_id, name, content)"
            " VALUES (:uid, :name, :content)"
            " ON CONFLICT (name) WHERE user_id = 'eval-variant'"
            " DO UPDATE SET content = EXCLUDED.content, updated_at = now()"
            " RETURNING id"
        ),
        {"uid": VARIANT_USER_ID, "name": name, "content": content},
    ).scalar_one()
    return str(pid)


def _upsert_agent(
    conn,
    name: str,
    primary: str,
    extras: list[str],
    prompt_id: str | None,
    model: str | None,
    key: str,
    chunks: int,
) -> tuple[str, str, bool]:
    """Insert or update an eval-variant agent keyed on ``(user_id, name)``.

    Race-safe via the ``agents_eval_variant_name_uniq`` partial unique
    index (migration ``0008_eval_variant_unique``). The existing ``key``
    is preserved on update so a previously-captured bearer keeps working;
    the passed ``key`` is used only on insert. Update also re-enforces
    the throwaway-agent guardrails (`tools='[]'`, `allow_system_prompt_override=false`)
    and explicitly clears any per-row rate limits so a re-provision can't
    inherit stale caps that would skew eval timing.

    Returns ``(agent_id, agent_key, created)``.
    """
    extras_literal = "{" + ",".join(extras) + "}"
    row = conn.execute(
        text(
            "INSERT INTO agents ("
            " user_id, name, description, agent_type, status, key,"
            " source_id, extra_source_ids, chunks, retriever, prompt_id,"
            " default_model_id, tools,"
            " limited_request_mode, limited_token_mode,"
            " request_limit, token_limit,"
            " allow_system_prompt_override"
            ") VALUES ("
            " :uid, :name, :desc, 'classic', 'published', :key,"
            " CAST(:src AS uuid), CAST(:extras AS uuid[]), :chunks,"
            " 'classic', CAST(:pid AS uuid), :model, '[]'::jsonb,"
            " false, false, NULL, NULL, false"
            ")"
            " ON CONFLICT (name) WHERE user_id = 'eval-variant'"
            " DO UPDATE SET"
            "   source_id = EXCLUDED.source_id,"
            "   extra_source_ids = EXCLUDED.extra_source_ids,"
            "   chunks = EXCLUDED.chunks,"
            "   retriever = 'classic',"
            "   agent_type = 'classic',"
            "   prompt_id = EXCLUDED.prompt_id,"
            "   default_model_id = EXCLUDED.default_model_id,"
            "   tools = '[]'::jsonb,"
            "   limited_request_mode = false,"
            "   limited_token_mode = false,"
            "   request_limit = NULL,"
            "   token_limit = NULL,"
            "   allow_system_prompt_override = false,"
            "   status = 'published',"
            "   updated_at = now()"
            " RETURNING id, key, (xmax = 0) AS inserted"
        ),
        {
            "uid": VARIANT_USER_ID,
            "name": name,
            "desc": "Eval variant agent (throwaway). Provisioned by scripts/eval/provision_test_agent.py.",
            "key": key,
            "src": primary,
            "extras": extras_literal,
            "chunks": chunks,
            "pid": prompt_id,
            "model": model,
        },
    ).fetchone()
    agent_id, agent_key, inserted = row
    return str(agent_id), str(agent_key), bool(inserted)


def _list_variants(conn) -> list[dict]:
    rows = conn.execute(
        text(
            "SELECT a.id, a.name, a.key, a.prompt_id, a.default_model_id,"
            " a.source_id, a.extra_source_ids, a.created_at, a.updated_at,"
            " p.name AS prompt_name"
            " FROM agents a"
            " LEFT JOIN prompts p ON p.id = a.prompt_id"
            " WHERE a.user_id = :uid"
            " ORDER BY a.created_at"
        ),
        {"uid": VARIANT_USER_ID},
    ).fetchall()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "key": str(r.key) if r.key else None,
            "prompt_id": str(r.prompt_id) if r.prompt_id else None,
            "prompt_name": r.prompt_name,
            "default_model_id": r.default_model_id,
            "source_id": str(r.source_id) if r.source_id else None,
            "extra_source_ids": [str(s) for s in (r.extra_source_ids or [])],
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


def _delete_variant(conn, name: str) -> tuple[int, int]:
    """Delete the named eval-variant agent and only its dedicated prompt.

    The agent goes first. A prompt row is only deleted if no remaining
    agent still references it via ``prompt_id`` — that protects shared
    prompts (`--prompt-id` reused across variants, or the live prod
    prompt linked into an eval row for "prompt held constant, vary
    sources" recipes) from being collateral.
    """
    agent_rows = conn.execute(
        text(
            "DELETE FROM agents WHERE user_id = :uid AND name = :name "
            "RETURNING id, prompt_id"
        ),
        {"uid": VARIANT_USER_ID, "name": name},
    ).fetchall()
    referenced_prompt_ids = {r.prompt_id for r in agent_rows if r.prompt_id}
    prompt_rows: list = []
    for pid in referenced_prompt_ids:
        deleted = conn.execute(
            text(
                "DELETE FROM prompts"
                " WHERE id = :pid"
                "   AND user_id = :uid"
                "   AND NOT EXISTS ("
                "     SELECT 1 FROM agents WHERE prompt_id = :pid"
                "   )"
                " RETURNING id"
            ),
            {"pid": pid, "uid": VARIANT_USER_ID},
        ).fetchall()
        prompt_rows.extend(deleted)
    return len(agent_rows), len(prompt_rows)


def _delete_all_variants(conn) -> tuple[int, int]:
    """Wipe every eval-variant agent and every prompt it owns.

    Mirrors ``_delete_variant``: agents first, then only those eval-variant
    prompts that no surviving agent references. A prompt owned by
    ``user_id='eval-variant'`` but linked from a non-eval agent
    (impossible today, defensive for future) is preserved.
    """
    agent_rows = conn.execute(
        text(
            "DELETE FROM agents WHERE user_id = :uid"
            " RETURNING id, prompt_id"
        ),
        {"uid": VARIANT_USER_ID},
    ).fetchall()
    prompt_rows = conn.execute(
        text(
            "DELETE FROM prompts"
            " WHERE user_id = :uid"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM agents WHERE agents.prompt_id = prompts.id"
            "   )"
            " RETURNING id"
        ),
        {"uid": VARIANT_USER_ID},
    ).fetchall()
    return len(agent_rows), len(prompt_rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision a throwaway eval-variant agent in Postgres."
    )
    parser.add_argument("--name", help="Variant name (used as agent + prompt name).")
    parser.add_argument(
        "--prompt-file",
        help="Path to a system prompt .txt file. Required for --name unless --prompt-id is given.",
    )
    parser.add_argument(
        "--prompt-id",
        help="Existing prompt UUID to reuse instead of inserting a new prompt row.",
    )
    parser.add_argument(
        "--source-ids",
        help="Comma-separated source UUIDs. First is the primary; rest become extra_source_ids.",
    )
    parser.add_argument(
        "--source-ids-from-env",
        action="store_true",
        help="Use settings.AZTEC_SOURCE_IDS (the same list the prod agents use).",
    )
    parser.add_argument(
        "--model",
        help="LLM model id (e.g. qwen/qwen3.6-flash). Defaults to settings.LLM_NAME at request time.",
    )
    parser.add_argument(
        "--chunks",
        type=int,
        default=4,
        help="Retriever chunk count (default: 4, same as prod agents).",
    )
    parser.add_argument(
        "--key",
        help="Bearer key for the new agent. Default: generate 64 hex chars. Ignored when refreshing an existing row.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all eval-variant agents and exit.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the agent + prompt with --name and exit.",
    )
    parser.add_argument(
        "--delete-all",
        action="store_true",
        help="Delete every eval-variant agent + prompt. Asks for confirmation.",
    )
    args = parser.parse_args()

    if args.list:
        with db_session() as conn:
            print(json.dumps(_list_variants(conn), indent=2))
        return 0

    if args.delete_all:
        confirm = os.environ.get("EVAL_VARIANT_CONFIRM") == "yes"
        if not confirm:
            print(
                "Refusing to delete all eval-variant rows without "
                "EVAL_VARIANT_CONFIRM=yes in the env.",
                file=sys.stderr,
            )
            return 2
        with db_session() as conn:
            a, p = _delete_all_variants(conn)
        print(f"Deleted {a} agent(s) and {p} prompt(s).")
        return 0

    if args.delete:
        if not args.name:
            print("--delete requires --name", file=sys.stderr)
            return 2
        with db_session() as conn:
            a, p = _delete_variant(conn, args.name)
        print(f"Deleted {a} agent(s) and {p} prompt(s) named {args.name!r}.")
        return 0

    if not args.name:
        print("--name is required", file=sys.stderr)
        return 2

    if args.prompt_id and args.prompt_file:
        print("Pass either --prompt-id or --prompt-file, not both.", file=sys.stderr)
        return 2
    if not args.prompt_id and not args.prompt_file:
        print("Provisioning requires --prompt-file or --prompt-id.", file=sys.stderr)
        return 2

    if args.source_ids and args.source_ids_from_env:
        print(
            "Pass either --source-ids or --source-ids-from-env, not both.",
            file=sys.stderr,
        )
        return 2
    if args.source_ids_from_env:
        raw = settings.AZTEC_SOURCE_IDS
        if not raw:
            print("settings.AZTEC_SOURCE_IDS is unset", file=sys.stderr)
            return 2
        source_ids = _parse_source_ids_arg(raw)
    elif args.source_ids:
        source_ids = _parse_source_ids_arg(args.source_ids)
    else:
        print("--source-ids or --source-ids-from-env required", file=sys.stderr)
        return 2

    primary, *extras = source_ids
    key = args.key or secrets.token_hex(32)

    with db_session() as conn:
        if args.prompt_file:
            content = _read_prompt_file(args.prompt_file)
            prompt_id = _upsert_prompt(conn, args.name, content)
        else:
            try:
                uuid.UUID(args.prompt_id)
            except ValueError:
                print(f"--prompt-id is not a UUID: {args.prompt_id}", file=sys.stderr)
                return 2
            prompt_id = args.prompt_id

        agent_id, agent_key, created = _upsert_agent(
            conn,
            name=args.name,
            primary=primary,
            extras=extras,
            prompt_id=prompt_id,
            model=args.model,
            key=key,
            chunks=args.chunks,
        )

    # `key_preserved` is true when the returned `key` is the one already on
    # the existing row (update path), false when it's the freshly-generated
    # `--key`/random key on a fresh insert. Operators read this to know
    # whether a cached bearer is still valid.
    print(
        json.dumps(
            {
                "name": args.name,
                "id": agent_id,
                "key": agent_key,
                "prompt_id": prompt_id,
                "model": args.model,
                "source_id": primary,
                "extra_source_ids": extras,
                "created": created,
                "key_preserved": not created,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
