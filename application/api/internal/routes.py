import os
import datetime
import json
import uuid
from flask import Blueprint, request, send_from_directory, jsonify
from sqlalchemy import text
from werkzeug.utils import secure_filename
import logging

from application.core.settings import settings
from application.pseudonyms import (
    canonical_user_id,
    pseudonymize_provider_user_id,
)
from application.storage.db.base_repository import looks_like_uuid
from application.storage.db.repositories.agents import AgentsRepository
from application.storage.db.repositories.sources import SourcesRepository
from application.storage.db.session import db_session


logger = logging.getLogger(__name__)

current_dir = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


internal = Blueprint("internal", __name__)


SELF_AUTHENTICATED_ROUTES = {
    "/api/internal/create_mcp_key",
    "/api/internal/forget_discord_user",
}


@internal.before_request
def verify_internal_key():
    """Verify INTERNAL_KEY for all internal endpoint requests.

    Deny by default: if INTERNAL_KEY is not configured, reject all requests.
    Routes listed in ``SELF_AUTHENTICATED_ROUTES`` authenticate themselves
    (e.g. MCP key provisioning uses MCP_PROVISIONING_KEY so the Discord
    bot never holds the full INTERNAL_KEY).
    """
    if request.path in SELF_AUTHENTICATED_ROUTES:
        return None
    if not settings.INTERNAL_KEY:
        logger.warning(
            f"Internal API request rejected from {request.remote_addr}: "
            "INTERNAL_KEY is not configured"
        )
        return jsonify({"error": "Unauthorized", "message": "Internal API is not configured"}), 401
    internal_key = request.headers.get("X-Internal-Key")
    if not internal_key or internal_key != settings.INTERNAL_KEY:
        logger.warning(f"Unauthorized internal API access attempt from {request.remote_addr}")
        return jsonify({"error": "Unauthorized", "message": "Invalid or missing internal key"}), 401


@internal.route("/api/download", methods=["get"])
def download_file():
    user = secure_filename(request.args.get("user"))
    job_name = secure_filename(request.args.get("name"))
    filename = secure_filename(request.args.get("file"))
    save_dir = os.path.join(current_dir, settings.UPLOAD_FOLDER, user, job_name)
    return send_from_directory(save_dir, filename, as_attachment=True)


@internal.route("/api/upload_index", methods=["POST"])
def upload_index_files():
    """Upload two files(index.faiss, index.pkl) to the user's folder."""
    if "user" not in request.form:
        return {"status": "no user"}
    user = request.form["user"]
    if "name" not in request.form:
        return {"status": "no name"}
    job_name = request.form["name"]
    tokens = request.form["tokens"]
    retriever = request.form["retriever"]
    source_id = request.form["id"]
    type = request.form["type"]
    remote_data = request.form["remote_data"] if "remote_data" in request.form else None
    sync_frequency = request.form["sync_frequency"] if "sync_frequency" in request.form else None

    file_path = request.form.get("file_path")
    directory_structure = request.form.get("directory_structure")
    file_name_map = request.form.get("file_name_map")

    if directory_structure:
        try:
            directory_structure = json.loads(directory_structure)
        except Exception:
            logger.error("Error parsing directory_structure")
            directory_structure = {}
    else:
        directory_structure = {}
    if file_name_map:
        try:
            file_name_map = json.loads(file_name_map)
        except Exception:
            logger.error("Error parsing file_name_map")
            file_name_map = None
    else:
        file_name_map = None

    # Aztec fork uses pgvector exclusively; embeddings are written
    # directly to the ``documents`` table by the worker. The upstream
    # FAISS-file branch (which stored ``index.faiss`` / ``index.pkl``
    # under ``indexes/<source_id>/``) was removed.
    now = datetime.datetime.now(datetime.timezone.utc)
    update_fields = {
        "name": job_name,
        "type": type,
        "language": job_name,
        "date": now,
        "model": settings.EMBEDDINGS_NAME,
        "tokens": tokens,
        "retriever": retriever,
        "remote_data": remote_data,
        "sync_frequency": sync_frequency,
        "file_path": file_path,
        "directory_structure": directory_structure,
    }
    if file_name_map is not None:
        update_fields["file_name_map"] = file_name_map

    with db_session() as conn:
        repo = SourcesRepository(conn)
        existing = None
        if looks_like_uuid(source_id):
            existing = repo.get(source_id, user)
        if existing is None:
            existing = repo.get_by_legacy_id(source_id, user)
        if existing is not None:
            repo.update(str(existing["id"]), user, update_fields)
        else:
            repo.create(
                job_name,
                source_id=source_id if looks_like_uuid(source_id) else None,
                user_id=user,
                type=type,
                tokens=tokens,
                retriever=retriever,
                remote_data=remote_data,
                sync_frequency=sync_frequency,
                file_path=file_path,
                directory_structure=directory_structure,
                file_name_map=file_name_map,
                language=job_name,
                model=settings.EMBEDDINGS_NAME,
                date=now,
                legacy_mongo_id=None if looks_like_uuid(source_id) else str(source_id),
            )
    return {"status": "ok"}


# Providers whose bots may provision MCP keys / erase data. Mirrors the
# strict allowlist in ``application/pseudonyms.py`` — an unknown provider
# is a 400, never a silently-Discord-scoped pseudonym.
_SUPPORTED_PROVIDERS = ("discord", "slack")


def _resolve_provider_identity(data: dict) -> tuple[str, str, str]:
    """Resolve ``(provider, raw_user_id, username)`` from a request body.

    Two accepted shapes, chosen by whether ``provider`` is present so the
    legacy Discord bot's payload keeps working byte-for-byte:

    - **Legacy (no ``provider``):** requires ``discord_user_id`` (and, for
      ``create_mcp_key`` only, ``discord_username``). Resolves to provider
      ``discord``. This is exactly the old contract.
    - **Generalized (``provider`` present):** requires ``provider`` in the
      allowlist and ``provider_user_id``; ``provider_username`` is optional.
      There is deliberately NO fallback to ``discord_user_id`` here — a
      Slack caller that forgot ``provider_user_id`` must fail rather than
      mint a Discord-scoped pseudonym from a stray field.

    For Slack the caller supplies a workspace-scoped compound id
    (``team_id:user_id``); the backend treats it as an opaque raw id and
    never stores the plaintext.

    Raises ``ValueError`` with a user-safe message on a bad shape; callers
    turn that into a 400.
    """
    # Shape is chosen by PRESENCE of the ``provider`` key, not its
    # truthiness — an explicit ``"provider": ""`` is a malformed
    # generalized call, NOT a license to fall back to the Discord fields
    # (which would silently mint a Discord-scoped pseudonym for it).
    if "provider" in data:
        raw_provider = (data.get("provider") or "").strip()
        if raw_provider not in _SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported provider: {raw_provider!r}")
        raw_user_id = (data.get("provider_user_id") or "").strip()
        if not raw_user_id:
            raise ValueError("provider_user_id is required")
        username = (data.get("provider_username") or "").strip()
        return raw_provider, raw_user_id, username

    # Legacy Discord shape.
    raw_user_id = (data.get("discord_user_id") or "").strip()
    if not raw_user_id:
        raise ValueError("discord_user_id is required")
    username = (data.get("discord_username") or "").strip()
    return "discord", raw_user_id, username


@internal.route("/api/internal/create_mcp_key", methods=["POST"])
def create_mcp_key():
    """Create or retrieve an MCP API key for a bot user.

    Provider-aware (see ``_resolve_provider_identity``): accepts the legacy
    Discord shape (``discord_user_id``/``discord_username``) AND the
    generalized ``{provider, provider_user_id}`` shape used by the Slack
    bot. Self-authenticates via ``X-Provisioning-Key`` against
    ``MCP_PROVISIONING_KEY`` — a key dedicated to this endpoint so that a
    compromise of a bot does not leak ``INTERNAL_KEY``. Atomic upsert on
    (mcp_provider, mcp_provider_user_id, mcp_purpose) prevents races on
    concurrent calls for the same user.
    """
    provisioning_key = request.headers.get("X-Provisioning-Key")
    if (
        not settings.MCP_PROVISIONING_KEY
        or not provisioning_key
        or provisioning_key != settings.MCP_PROVISIONING_KEY
    ):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        provider, raw_user_id, username = _resolve_provider_identity(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # The legacy Discord shape additionally required a username in the
    # payload; preserve that contract for Discord so the existing bot's
    # request validation behaviour is unchanged. Other providers don't
    # require it (it's never stored — see note below).
    if provider == "discord" and not username:
        return jsonify({"error": "discord_username is required"}), 400

    # ``username`` is intentionally NOT used for storage (it would
    # re-introduce the very PII we're pseudonymizing away). We still
    # accept it in the payload so the bot's request shape doesn't change
    # and so the `/mcp-key` flow can be extended later to send the user a
    # personalized confirmation message.
    pseudo_provider_user_id = pseudonymize_provider_user_id(
        raw_user_id, pepper=settings.USER_ID_PEPPER
    )
    pseudo_canonical_user_id = canonical_user_id(
        provider, raw_user_id, pepper=settings.USER_ID_PEPPER
    )

    if not settings.AZTEC_SOURCE_IDS:
        logger.error("AZTEC_SOURCE_IDS is not configured")
        return jsonify({"error": "Aztec sources not configured"}), 500

    raw_source_ids = [s.strip() for s in settings.AZTEC_SOURCE_IDS.split(",") if s.strip()]
    candidate_uuids = [sid for sid in raw_source_ids if looks_like_uuid(sid)]
    for sid in raw_source_ids:
        if sid not in candidate_uuids:
            logger.warning(f"Invalid UUID in AZTEC_SOURCE_IDS: {sid}")

    try:
        with db_session() as conn:
            if candidate_uuids:
                # AZTEC_SOURCE_IDS is the operator's declaration of
                # public corpora. Every UUID listed there must be marked
                # ``is_public=TRUE`` — refusing to provision against a
                # private source fails loudly at config time rather than
                # silently leaking access. See 0004_sources_is_public.
                existing = conn.execute(
                    text(
                        "SELECT id FROM sources "
                        "WHERE id = ANY(CAST(:ids AS uuid[])) "
                        "  AND is_public = TRUE"
                    ),
                    {"ids": candidate_uuids},
                )
                # Postgres returns rows in heap order (no ORDER BY), so we
                # can't rely on the SELECT to preserve AZTEC_SOURCE_IDS
                # order. Intersect with the canonical order from .env so
                # primary/extras land in the developer-question-weighted
                # sequence documented in CLAUDE.md.
                existing_set = {str(row[0]) for row in existing.fetchall()}
                valid_source_ids = [
                    sid for sid in candidate_uuids if sid in existing_set
                ]
            else:
                valid_source_ids = []

            missing = set(candidate_uuids) - set(valid_source_ids)
            for sid in missing:
                logger.warning(
                    f"Source not found or not is_public for UUID: {sid}"
                )

            if not valid_source_ids:
                logger.error("No valid Aztec sources found")
                return jsonify({"error": "No valid Aztec sources found"}), 500

            primary = valid_source_ids[0]
            extras = valid_source_ids[1:]

            agent = AgentsRepository(conn).upsert_mcp_key(
                mcp_provider=provider,
                mcp_provider_user_id=pseudo_provider_user_id,
                mcp_purpose="aztec_mcp",
                user_id=pseudo_canonical_user_id,
                # Constant — the username used to be embedded here
                # (``"Aztec MCP - alice"``) but that re-introduced the
                # PII we're pseudonymizing away. Operators can pivot on
                # ``surface``, ``id``, ``last_used_at``, or a prefix of
                # ``mcp_provider_user_id`` in psql instead.
                name="Aztec MCP",
                description="Aztec knowledge base access via MCP",
                key=str(uuid.uuid4()),
                source_id=primary,
                extra_source_ids=extras,
                surface="mcp",
            )
    except Exception:
        logger.exception("Failed to upsert MCP key")
        return jsonify({"error": "Internal server error"}), 500

    created_at = agent.get("created_at")
    updated_at = agent.get("updated_at")
    created = (
        created_at is not None
        and updated_at is not None
        and created_at == updated_at
    )

    return jsonify({"api_key": agent["key"], "created": created}), 200


@internal.route("/api/internal/forget_discord_user", methods=["POST"])
def forget_discord_user():
    """Erase all data tied to a bot user (GDPR Article 17).

    Despite the legacy route name, this endpoint is provider-aware: it
    accepts the original Discord shape (``discord_user_id``) AND the
    generalized shape (``provider`` + ``provider_user_id``) used by the
    Slack bot. Self-authenticates via ``X-Provisioning-Key`` against
    ``MCP_PROVISIONING_KEY`` (same trust model as create_mcp_key — both
    bots already hold this key). Deletes:

    - The MCP-provisioned agent for this user (matched on the
      ``(mcp_provider, mcp_provider_user_id)`` pair).
    - Every conversation / operational row owned by the user's
      pseudonymous ``user_id`` and its messages (cascade).

    Scope note: bot *chat* turns are written under the shared chat
    agent's owner (``user_id='local'``), NOT the per-user pseudonym (see
    ``stream_processor`` identity handling), so they are not per-user
    erasable here. This matches the existing Discord behaviour — what
    this endpoint removes is the user's MCP key plus any MCP-originated
    data keyed to their pseudonym, plus the bot's local cache (dropped
    bot-side by the slash-command handler).
    """
    provisioning_key = request.headers.get("X-Provisioning-Key")
    if (
        not settings.MCP_PROVISIONING_KEY
        or not provisioning_key
        or provisioning_key != settings.MCP_PROVISIONING_KEY
    ):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        provider, raw_user_id, _username = _resolve_provider_identity(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Compute the pseudonyms the row was written under. Callers still
    # pass the raw provider ID (for Slack, the ``team_id:user_id``
    # compound); only the storage representation is pseudonymized. If the
    # helper drifts between create and forget, /forget-me silently fails
    # to find rows — locked down by the parity contract test in
    # tests/api/test_internal_routes.py.
    pseudo_provider_user_id = pseudonymize_provider_user_id(
        raw_user_id, pepper=settings.USER_ID_PEPPER
    )
    pseudo_canonical_user_id = canonical_user_id(
        provider, raw_user_id, pepper=settings.USER_ID_PEPPER
    )
    # Order matters: every child table has FK user_id → users(user_id)
    # ON DELETE RESTRICT (see ``application/storage/db/models.py`` header
    # comment + migration 0015_user_id_fk), so the parent ``users`` row
    # must be deleted last. ``agents`` is special-cased by mcp_provider
    # since the canonical lookup is on the (provider, provider_user_id)
    # pair. ``conversation_messages``, ``shared_conversations``, and
    # ``pending_tool_state`` cascade from ``conversations`` so they're
    # not enumerated here. ``workflow_nodes``/``workflow_edges`` cascade
    # from ``workflows``; ``memories``/``todos``/``notes`` are
    # user-keyed AND cascade from ``user_tools`` — listed under their
    # user_id so the delete works whether or not the user has tools.
    delete_specs = (
        ("agents", "mcp_provider = :prov AND mcp_provider_user_id = :did",
            {"prov": provider, "did": pseudo_provider_user_id}),
        ("conversations", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("attachments", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("memories", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("todos", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("notes", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("connector_sessions", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("workflow_runs", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("workflows", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("user_tools", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("agent_folders", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("sources", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("prompts", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("user_logs", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("stack_logs", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("token_usage", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
        ("users", "user_id = :uid", {"uid": pseudo_canonical_user_id}),
    )

    deleted: dict[str, int] = {}
    try:
        with db_session() as conn:
            for table, where_clause, params in delete_specs:
                result = conn.execute(
                    text(f"DELETE FROM {table} WHERE {where_clause}"),
                    params,
                )
                deleted[table] = result.rowcount or 0
    except Exception:
        logger.exception("Failed to forget Discord user")
        return jsonify({"error": "Internal server error"}), 500

    return jsonify({"success": True, "deleted": deleted}), 200
