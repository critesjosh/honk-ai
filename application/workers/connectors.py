"""Remote connectors and their sync drivers.

- ``remote_worker``: pulls from a remote ``loader`` (web crawler, S3,
  Reddit, GitHub, …), embeds, uploads.
- ``sync`` / ``sync_worker``: scheduled re-runs of ``remote_worker``
  for sources flagged with a sync_frequency.
- ``ingest_connector``: OAuth-style connectors (Google Drive, Dropbox,
  …) — slightly different download path, same backend upload.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import tempfile
import uuid
from collections import Counter
from typing import Any, Dict

from application.parser.chunking import Chunker
from application.parser.connectors.connector_creator import ConnectorCreator
from application.parser.embedding_pipeline import embed_and_store_documents
from application.parser.file.bulk import SimpleDirectoryReader
from application.parser.file.constants import SUPPORTED_SOURCE_EXTENSIONS
from application.parser.remote.remote_creator import RemoteCreator
from application.parser.schema.base import Document
from application.storage.db.repositories.sources import SourcesRepository
from application.storage.db.session import db_readonly, db_session
from application.utils import count_tokens_docs, num_tokens_from_string, safe_filename
from application.workers._helpers import (
    MAX_TOKENS,
    MIN_TOKENS,
    metadata_from_filename,
    upload_index,
)


def remote_worker(
    self,
    source_data,
    name_job,
    user,
    loader,
    directory="temp",
    retriever="classic",
    sync_frequency="never",
    operation_mode="upload",
    doc_id=None,
):
    safe_user = safe_filename(user)
    full_path = os.path.join(directory, safe_user, uuid.uuid4().hex)
    os.makedirs(full_path, exist_ok=True)
    self.update_state(state="PROGRESS", meta={"current": 1})
    try:
        logging.info("Initializing remote loader with type: %s", loader)
        remote_loader = RemoteCreator.create_loader(loader)
        raw_docs = remote_loader.load_data(source_data)

        chunker = Chunker(
            chunking_strategy="classic_chunk",
            max_tokens=MAX_TOKENS,
            min_tokens=MIN_TOKENS,
            duplicate_headers=True,
        )
        docs = chunker.chunk(documents=raw_docs)
        docs = [Document.to_langchain_format(raw_doc) for raw_doc in raw_docs]
        tokens = count_tokens_docs(docs)
        logging.info("Total tokens calculated: %d", tokens)

        # Build directory structure from loaded documents.
        # Format matches local file uploads: nested structure with type,
        # size_bytes, token_count.
        directory_structure: Dict[str, Any] = {}
        for doc in raw_docs:
            # For crawlers: file_path is a virtual path like "guides/setup.md"
            # For other remotes: use key or title as fallback
            file_path = ""
            if doc.extra_info:
                file_path = (
                    doc.extra_info.get("file_path", "")
                    or doc.extra_info.get("key", "")
                    or doc.extra_info.get("title", "")
                )
            if not file_path:
                file_path = doc.doc_id or ""

            if file_path:
                token_count = num_tokens_from_string(doc.text) if doc.text else 0
                size_bytes = len(doc.text.encode("utf-8")) if doc.text else 0

                file_name = (
                    file_path.split("/")[-1] if "/" in file_path else file_path
                )
                ext = os.path.splitext(file_name)[1].lower()
                mime_types = {
                    ".txt": "text/plain",
                    ".md": "text/markdown",
                    ".pdf": "application/pdf",
                    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ".doc": "application/msword",
                    ".html": "text/html",
                    ".json": "application/json",
                    ".csv": "text/csv",
                    ".xml": "application/xml",
                    ".py": "text/x-python",
                    ".js": "text/javascript",
                    ".ts": "text/typescript",
                    ".jsx": "text/jsx",
                    ".tsx": "text/tsx",
                }
                file_type = mime_types.get(ext, "application/octet-stream")

                # Build nested directory structure from path.
                # e.g., "guides/setup.md" -> {"guides": {"setup.md": {...}}}
                path_parts = file_path.split("/")
                current_level = directory_structure
                for i, part in enumerate(path_parts):
                    if i == len(path_parts) - 1:
                        current_level[part] = {
                            "type": file_type,
                            "size_bytes": size_bytes,
                            "token_count": token_count,
                        }
                    else:
                        if part not in current_level:
                            current_level[part] = {}
                        current_level = current_level[part]

        logging.info(
            f"Built directory structure with {len(directory_structure)} files: "
            f"{list(directory_structure.keys())}"
        )

        if operation_mode == "upload":
            id = uuid.uuid4()
            embed_and_store_documents(docs, full_path, id, self)
        elif operation_mode == "sync":
            if not doc_id:
                logging.error("Invalid doc_id provided for sync operation: %s", doc_id)
                raise ValueError("doc_id must be provided for sync operation.")
            id = str(doc_id)
            embed_and_store_documents(docs, full_path, id, self)
        self.update_state(state="PROGRESS", meta={"current": 100})

        # Serialize remote_data as JSON if it's a dict (for S3, Reddit, etc.)
        remote_data_serialized = (
            json.dumps(source_data) if isinstance(source_data, dict) else source_data
        )
        file_data = {
            "name": name_job,
            "user": user,
            "tokens": tokens,
            "retriever": retriever,
            "id": str(id),
            "type": loader,
            "remote_data": remote_data_serialized,
            "sync_frequency": sync_frequency,
            "directory_structure": json.dumps(directory_structure),
        }

        if operation_mode == "sync":
            last_sync_now = datetime.datetime.now()
            file_data["last_sync"] = last_sync_now

            try:
                with db_session() as conn:
                    repo = SourcesRepository(conn)
                    src = repo.get_any(str(id), user)
                    if src is not None:
                        repo.update(str(src["id"]), user, {"date": last_sync_now})
            except Exception as upd_err:
                logging.warning(
                    f"Failed to update last_sync for source {id}: {upd_err}"
                )
        upload_index(full_path, file_data)
    except Exception as e:
        logging.error("Error in remote_worker task: %s", str(e), exc_info=True)
        raise
    finally:
        if os.path.exists(full_path):
            shutil.rmtree(full_path)
    logging.info("remote_worker task completed successfully")
    return {
        "id": str(id),
        "urls": source_data,
        "name_job": name_job,
        "user": user,
        "limited": False,
    }


def sync(
    self,
    source_data,
    name_job,
    user,
    loader,
    sync_frequency,
    retriever,
    doc_id=None,
    directory="temp",
):
    try:
        remote_worker(
            self,
            source_data,
            name_job,
            user,
            loader,
            directory,
            retriever,
            sync_frequency,
            "sync",
            doc_id,
        )
    except Exception as e:
        logging.error(f"Error during sync: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
    return {"status": "success"}


def sync_worker(self, frequency):
    from sqlalchemy import text as sql_text

    sync_counts: Counter = Counter()
    with db_readonly() as conn:
        result = conn.execute(
            sql_text(
                "SELECT id, name, user_id, type, remote_data, retriever "
                "FROM sources WHERE sync_frequency = :freq"
            ),
            {"freq": frequency},
        )
        rows = result.fetchall()

    for row in rows:
        doc = dict(row._mapping)
        name = doc.get("name")
        user = doc.get("user_id")
        source_type = doc.get("type")
        source_data = doc.get("remote_data")
        retriever = doc.get("retriever")
        doc_id = str(doc.get("id"))
        resp = sync(
            self, source_data, name, user, source_type, frequency, retriever, doc_id,
        )
        sync_counts["total_sync_count"] += 1
        sync_counts[
            "sync_success" if resp["status"] == "success" else "sync_failure"
        ] += 1
    return {
        key: sync_counts[key]
        for key in ["total_sync_count", "sync_success", "sync_failure"]
    }


def ingest_connector(
    self,
    job_name: str,
    user: str,
    source_type: str,
    session_token=None,
    file_ids=None,
    folder_ids=None,
    recursive=True,
    retriever: str = "classic",
    operation_mode: str = "upload",
    doc_id=None,
    sync_frequency: str = "never",
) -> Dict[str, Any]:
    """Ingestion for internal knowledge bases (GoogleDrive, etc.)."""
    logging.info(
        f"Starting remote ingestion from {source_type} for user: {user}, "
        f"job: {job_name}"
    )
    self.update_state(state="PROGRESS", meta={"current": 1})

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            # Step 1: Initialize the appropriate loader
            self.update_state(
                state="PROGRESS",
                meta={"current": 10, "status": "Initializing connector"},
            )

            if not session_token:
                raise ValueError(f"{source_type} connector requires session_token")

            if not ConnectorCreator.is_supported(source_type):
                raise ValueError(
                    f"Unsupported connector type: {source_type}. "
                    f"Supported types: "
                    f"{ConnectorCreator.get_supported_connectors()}"
                )

            remote_loader = ConnectorCreator.create_connector(
                source_type, session_token,
            )

            # Create a clean config for storage
            api_source_config = {
                "file_ids": file_ids or [],
                "folder_ids": folder_ids or [],
                "recursive": recursive,
            }

            # Step 2: Download files to temp directory
            self.update_state(
                state="PROGRESS",
                meta={"current": 20, "status": "Downloading files"},
            )
            download_info = remote_loader.download_to_directory(
                temp_dir, api_source_config,
            )

            if download_info.get("empty_result", False) or not download_info.get(
                "files_downloaded", 0
            ):
                logging.warning(f"No files were downloaded from {source_type}")
                return {
                    "name": job_name,
                    "user": user,
                    "tokens": 0,
                    "type": source_type,
                    "source_config": api_source_config,
                    "directory_structure": "{}",
                }

            # Step 3: Use SimpleDirectoryReader to process downloaded files
            self.update_state(
                state="PROGRESS",
                meta={"current": 40, "status": "Processing files"},
            )
            reader = SimpleDirectoryReader(
                input_dir=temp_dir,
                recursive=True,
                required_exts=list(SUPPORTED_SOURCE_EXTENSIONS),
                exclude_hidden=True,
                file_metadata=metadata_from_filename,
            )
            raw_docs = reader.load_data()
            directory_structure = getattr(reader, "directory_structure", {})

            # Step 4: Process documents (chunking, embedding, etc.)
            self.update_state(
                state="PROGRESS",
                meta={"current": 60, "status": "Processing documents"},
            )

            chunker = Chunker(
                chunking_strategy="classic_chunk",
                max_tokens=MAX_TOKENS,
                min_tokens=MIN_TOKENS,
                duplicate_headers=True,
            )
            raw_docs = chunker.chunk(documents=raw_docs)

            # Preserve source information in document metadata
            for doc in raw_docs:
                if hasattr(doc, "extra_info") and doc.extra_info:
                    source = doc.extra_info.get("source")
                    if source and os.path.isabs(source):
                        doc.extra_info["source"] = os.path.relpath(
                            source, start=temp_dir,
                        )

            docs = [Document.to_langchain_format(raw_doc) for raw_doc in raw_docs]

            if operation_mode == "upload":
                id = uuid.uuid4()
            elif operation_mode == "sync":
                if not doc_id:
                    logging.error(
                        "Invalid doc_id provided for sync operation: %s", doc_id,
                    )
                    raise ValueError("doc_id must be provided for sync operation.")
                id = str(doc_id)
            else:
                raise ValueError(f"Invalid operation_mode: {operation_mode}")

            vector_store_path = os.path.join(temp_dir, "vector_store")
            os.makedirs(vector_store_path, exist_ok=True)

            self.update_state(
                state="PROGRESS",
                meta={"current": 80, "status": "Storing documents"},
            )
            embed_and_store_documents(docs, vector_store_path, id, self)

            tokens = count_tokens_docs(docs)

            file_data = {
                "user": user,
                "name": job_name,
                "tokens": tokens,
                "retriever": retriever,
                "id": str(id),
                "type": "connector:file",
                "remote_data": json.dumps(
                    {"provider": source_type, **api_source_config},
                ),
                "directory_structure": json.dumps(directory_structure),
                "sync_frequency": sync_frequency,
            }

            if operation_mode == "sync":
                file_data["last_sync"] = datetime.datetime.now()
            else:
                file_data["last_sync"] = datetime.datetime.now()

            if operation_mode == "sync":
                try:
                    with db_session() as conn:
                        repo = SourcesRepository(conn)
                        src = repo.get_any(str(id), user)
                        if src is not None:
                            repo.update(
                                str(src["id"]), user,
                                {"date": file_data["last_sync"]},
                            )
                except Exception as upd_err:
                    logging.warning(
                        f"Failed to update last_sync for source {id}: {upd_err}"
                    )

            upload_index(vector_store_path, file_data)

            self.update_state(
                state="PROGRESS",
                meta={"current": 100, "status": "Complete"},
            )

            logging.info(f"Remote ingestion completed: {job_name}")

            return {
                "user": user,
                "name": job_name,
                "tokens": tokens,
                "type": source_type,
                "id": str(id),
                "status": "complete",
            }

        except Exception as e:
            logging.error(f"Error during remote ingestion: {e}", exc_info=True)
            raise
