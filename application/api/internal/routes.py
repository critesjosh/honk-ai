import os
import datetime
import json
import uuid
from flask import Blueprint, request, send_from_directory, jsonify
from werkzeug.utils import secure_filename
from bson.objectid import ObjectId
from bson.dbref import DBRef
from pymongo import ReturnDocument
import logging
from application.core.mongo_db import MongoDB
from application.core.settings import settings
from application.storage.storage_creator import StorageCreator


logger = logging.getLogger(__name__)
mongo = MongoDB.get_client()
db = mongo[settings.MONGO_DB_NAME]
conversations_collection = db["conversations"]
sources_collection = db["sources"]
agents_collection = db["agents"]

current_dir = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


internal = Blueprint("internal", __name__)


SELF_AUTHENTICATED_ROUTES = {"/api/internal/create_mcp_key"}


@internal.before_request
def verify_internal_key():
    """Verify INTERNAL_KEY for all internal endpoint requests.

    Deny by default: if INTERNAL_KEY is not configured, reject all requests.
    Routes in SELF_AUTHENTICATED_ROUTES handle their own authentication.
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
    id = request.form["id"]
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

    storage = StorageCreator.get_storage()
    index_base_path = f"indexes/{id}"
    
    if settings.VECTOR_STORE == "faiss":
        if "file_faiss" not in request.files:
            logger.error("No file_faiss part")
            return {"status": "no file"}
        file_faiss = request.files["file_faiss"]
        if file_faiss.filename == "":
            return {"status": "no file name"}
        if "file_pkl" not in request.files:
            logger.error("No file_pkl part")
            return {"status": "no file"}
        file_pkl = request.files["file_pkl"]
        if file_pkl.filename == "":
            return {"status": "no file name"}

        # Save index files to storage
        faiss_storage_path = f"{index_base_path}/index.faiss"
        pkl_storage_path = f"{index_base_path}/index.pkl"
        storage.save_file(file_faiss, faiss_storage_path)
        storage.save_file(file_pkl, pkl_storage_path)


    existing_entry = sources_collection.find_one({"_id": ObjectId(id)})
    if existing_entry:
        update_fields = {
            "user": user,
            "name": job_name,
            "language": job_name,
            "date": datetime.datetime.now(),
            "model": settings.EMBEDDINGS_NAME,
            "type": type,
            "tokens": tokens,
            "retriever": retriever,
            "remote_data": remote_data,
            "sync_frequency": sync_frequency,
            "file_path": file_path,
            "directory_structure": directory_structure,
        }
        if file_name_map is not None:
            update_fields["file_name_map"] = file_name_map
        sources_collection.update_one(
            {"_id": ObjectId(id)},
            {"$set": update_fields},
        )
    else:
        insert_doc = {
            "_id": ObjectId(id),
            "user": user,
            "name": job_name,
            "language": job_name,
            "date": datetime.datetime.now(),
            "model": settings.EMBEDDINGS_NAME,
            "type": type,
            "tokens": tokens,
            "retriever": retriever,
            "remote_data": remote_data,
            "sync_frequency": sync_frequency,
            "file_path": file_path,
            "directory_structure": directory_structure,
        }
        if file_name_map is not None:
            insert_doc["file_name_map"] = file_name_map
        sources_collection.insert_one(insert_doc)
    return {"status": "ok"}


@internal.route("/api/internal/create_mcp_key", methods=["POST"])
def create_mcp_key():
    """Create or retrieve a personal MCP API key for a Discord user.

    Uses a dedicated provisioning key (not INTERNAL_KEY) to limit blast radius.
    Atomic upsert prevents race conditions on concurrent requests.
    """
    provisioning_key = request.headers.get("X-Provisioning-Key")
    if (
        not settings.MCP_PROVISIONING_KEY
        or not provisioning_key
        or provisioning_key != settings.MCP_PROVISIONING_KEY
    ):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    discord_user_id = data.get("discord_user_id", "").strip()
    discord_username = data.get("discord_username", "").strip()

    if not discord_user_id:
        return jsonify({"error": "discord_user_id is required"}), 400
    if not discord_username:
        return jsonify({"error": "discord_username is required"}), 400

    # Sanitize display name (mutable, user-controlled)
    sanitized_username = discord_username[:50]

    # Validate and load Aztec source IDs
    if not settings.AZTEC_SOURCE_IDS:
        logger.error("AZTEC_SOURCE_IDS is not configured")
        return jsonify({"error": "Aztec sources not configured"}), 500

    source_id_strings = [
        s.strip() for s in settings.AZTEC_SOURCE_IDS.split(",") if s.strip()
    ]
    if not source_id_strings:
        logger.error("AZTEC_SOURCE_IDS is empty")
        return jsonify({"error": "Aztec sources not configured"}), 500

    # Validate each source exists and build DBRefs
    aztec_source_dbrefs = []
    for sid in source_id_strings:
        try:
            oid = ObjectId(sid)
        except Exception:
            logger.warning(f"Invalid ObjectId in AZTEC_SOURCE_IDS: {sid}")
            continue
        if sources_collection.find_one({"_id": oid}):
            aztec_source_dbrefs.append(DBRef("sources", oid))
        else:
            logger.warning(f"Source not found for ObjectId: {sid}")

    if not aztec_source_dbrefs:
        logger.error("No valid Aztec sources found")
        return jsonify({"error": "No valid Aztec sources found"}), 500

    # Ensure unique index exists (idempotent)
    agents_collection.create_index(
        [("provider", 1), ("provider_user_id", 1), ("purpose", 1)],
        unique=True,
        sparse=True,
    )

    now = datetime.datetime.now(datetime.timezone.utc)

    # Atomic upsert: find existing or insert new
    result = agents_collection.find_one_and_update(
        {
            "provider": "discord",
            "provider_user_id": discord_user_id,
            "purpose": "aztec_mcp",
        },
        {
            "$setOnInsert": {
                "user": f"discord:{discord_user_id}",
                "name": f"Aztec MCP - {sanitized_username}",
                "description": "Aztec knowledge base access via MCP",
                "agent_type": "classic",
                "status": "published",
                "key": str(uuid.uuid4()),
                "sources": aztec_source_dbrefs,
                "chunks": "2",
                "retriever": "classic",
                "prompt_id": "default",
                "tools": [],
                "limited_request_mode": True,
                "request_limit": 1000,
                "limited_token_mode": True,
                "token_limit": 500000,
                "provider": "discord",
                "provider_user_id": discord_user_id,
                "purpose": "aztec_mcp",
                "createdAt": now,
            },
            "$set": {
                "updatedAt": now,
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    created = result.get("createdAt") == now and result.get("updatedAt") == now

    return jsonify({
        "api_key": result["key"],
        "created": created,
    }), 200
