"""Local-file ingestion worker.

Pulls a tree (file or directory) from storage, optionally extracts
zips, runs the parser + chunker + embedder, and uploads the resulting
index back to the backend via ``/api/upload_index``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid

from application.celery_init import celery
from application.parser.chunking import Chunker
from application.parser.embedding_pipeline import embed_and_store_documents
from application.parser.file.bulk import SimpleDirectoryReader
from application.parser.schema.base import Document
from application.storage.storage_creator import StorageCreator
from application.utils import count_tokens_docs
from application.workers._helpers import (
    MAX_TOKENS,
    MIN_TOKENS,
    RECURSION_DEPTH,
    _apply_display_names_to_structure,
    _get_display_name,
    _normalize_file_name_map,
    metadata_from_filename,
    upload_index,
)
from application.workers.zip_safety import extract_zip_recursive


@celery.task(bind=True)
def ingest(
    self, directory, formats, job_name, user, file_path, filename, file_name_map=None
):
    """Celery task wrapper for ``ingest_worker``.

    Lives here (not in a separate ``api/user/tasks.py`` like upstream)
    so the corpus-ingest pipeline (``scripts/ingest/upload.py`` →
    ``/api/upload`` → this task) survives the upstream-admin-API removal.
    """
    return ingest_worker(
        self,
        directory,
        formats,
        job_name,
        file_path,
        filename,
        user,
        file_name_map=file_name_map,
    )


def ingest_worker(
    self,
    directory,
    formats,
    job_name,
    file_path,
    filename,
    user,
    retriever="classic",
    file_name_map=None,
):
    """Ingest and process documents.

    Args:
        self: Reference to the instance of the task.
        directory: Specifies the directory for ingesting ('inputs' or 'temp').
        formats: List of file extensions to consider for ingestion.
        job_name: Name of the job for this ingestion task (original, unsanitized).
        file_path: Complete file path to use consistently throughout the pipeline.
        filename: Original unsanitized filename provided by the user.
        user: Identifier for the user initiating the ingestion (original, unsanitized).
        retriever: Type of retriever to use for processing the documents.
        file_name_map: Optional mapping of safe relative paths to original filenames.

    Returns:
        Information about the completed ingestion task.
    """
    input_files = None
    recursive = True
    limit = None
    exclude = True
    sample = False

    storage = StorageCreator.get_storage()

    logging.info(f"Ingest path: {file_path}", extra={"user": user, "job": job_name})

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            os.makedirs(temp_dir, exist_ok=True)

            if storage.is_directory(file_path):
                logging.info(f"Processing directory: {file_path}")
                files_list = storage.list_files(file_path)

                for storage_file_path in files_list:
                    if storage.is_directory(storage_file_path):
                        continue

                    rel_path = os.path.relpath(storage_file_path, file_path)
                    local_file_path = os.path.join(temp_dir, rel_path)

                    os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

                    try:
                        file_data = storage.get_file(storage_file_path)
                        with open(local_file_path, "wb") as f:
                            f.write(file_data.read())
                    except Exception as e:
                        logging.error(
                            f"Error downloading file {storage_file_path}: {e}"
                        )
                        continue
            else:
                temp_filename = os.path.basename(file_path)
                temp_file_path = os.path.join(temp_dir, temp_filename)

                file_data = storage.get_file(file_path)
                with open(temp_file_path, "wb") as f:
                    f.write(file_data.read())

                if temp_filename.endswith(".zip"):
                    logging.info(f"Extracting zip file: {temp_filename}")
                    extract_zip_recursive(
                        temp_file_path,
                        temp_dir,
                        current_depth=0,
                        max_depth=RECURSION_DEPTH,
                    )

            self.update_state(state="PROGRESS", meta={"current": 1})
            if sample:
                logging.info(f"Sample mode enabled. Using {limit} documents.")
            reader = SimpleDirectoryReader(
                input_dir=temp_dir,
                input_files=input_files,
                recursive=recursive,
                required_exts=formats,
                exclude_hidden=exclude,
                file_metadata=metadata_from_filename,
            )
            raw_docs = reader.load_data()

            directory_structure = getattr(reader, "directory_structure", {})
            logging.info(f"Directory structure from reader: {directory_structure}")
            file_name_map = _normalize_file_name_map(file_name_map)
            if file_name_map:
                for doc in raw_docs:
                    extra_info = getattr(doc, "extra_info", None)
                    if not isinstance(extra_info, dict):
                        continue
                    rel_path = extra_info.get("source") or extra_info.get("file_path")
                    display_name = _get_display_name(file_name_map, rel_path)
                    if display_name:
                        display_name = str(display_name)
                        extra_info["filename"] = display_name
                        extra_info["file_name"] = display_name
                        extra_info["title"] = display_name
                directory_structure = _apply_display_names_to_structure(
                    directory_structure, file_name_map
                )

            chunker = Chunker(
                chunking_strategy="classic_chunk",
                max_tokens=MAX_TOKENS,
                min_tokens=MIN_TOKENS,
                duplicate_headers=True,
            )
            raw_docs = chunker.chunk(documents=raw_docs)

            docs = [Document.to_langchain_format(raw_doc) for raw_doc in raw_docs]

            id = uuid.uuid4()

            vector_store_path = os.path.join(temp_dir, "vector_store")
            os.makedirs(vector_store_path, exist_ok=True)

            embed_and_store_documents(docs, vector_store_path, id, self)

            tokens = count_tokens_docs(docs)

            self.update_state(state="PROGRESS", meta={"current": 100})

            if sample:
                for i in range(min(5, len(raw_docs))):
                    logging.info(f"Sample document {i}: {raw_docs[i]}")
            file_data = {
                "name": job_name,
                "file": filename,
                "user": user,
                "tokens": tokens,
                "retriever": retriever,
                "id": str(id),
                "type": "local",
                "file_path": file_path,
                "directory_structure": json.dumps(directory_structure),
            }
            if file_name_map:
                file_data["file_name_map"] = json.dumps(file_name_map)

            upload_index(vector_store_path, file_data)
        except Exception as e:
            logging.error(f"Error in ingest_worker: {e}", exc_info=True)
            raise
    return {
        "directory": directory,
        "formats": formats,
        "name_job": job_name,
        "filename": filename,
        "user": user,
        # Surface the created source UUID so callers polling /api/task_status
        # can capture it without a separate sources-listing endpoint (the admin
        # SPA's GET /api/sources was removed). scripts/ingest/upload.py reads
        # this from the task result. ``id`` is the source PK created above.
        "source_id": str(id),
        "limited": False,
    }
