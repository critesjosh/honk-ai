"""Incremental re-ingestion of an existing source.

Diffs the current storage tree against the source's last-recorded
directory structure, deletes vector chunks for removed files, embeds
chunks for added files, and updates the source row's directory
structure + token counts.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile

from application.core.settings import settings
from application.parser.chunking import Chunker
from application.parser.file.bulk import SimpleDirectoryReader
from application.parser.file.constants import SUPPORTED_SOURCE_EXTENSIONS
from application.storage.db.repositories.sources import SourcesRepository
from application.storage.db.session import db_readonly, db_session
from application.storage.storage_creator import StorageCreator
from application.workers._helpers import (
    MAX_TOKENS,
    MIN_TOKENS,
    _apply_display_names_to_structure,
    _get_display_name,
    _normalize_file_name_map,
    metadata_from_filename,
)


def reingest_source_worker(self, source_id, user):
    """Re-ingestion worker that handles incremental updates.

    Adds chunks from newly added files, removes chunks from deleted
    files. Returns ``status: "no_changes"`` when nothing differs.
    """
    try:
        from application.vectorstore.vector_creator import VectorCreator

        self.update_state(
            state="PROGRESS",
            meta={"current": 10, "status": "Initializing re-ingestion scan"},
        )

        with db_readonly() as conn:
            source = SourcesRepository(conn).get_any(source_id, user)
        if not source:
            raise ValueError(f"Source {source_id} not found or access denied")
        source_id = str(source["id"])

        storage = StorageCreator.get_storage()
        source_file_path = source.get("file_path", "")
        file_name_map = _normalize_file_name_map(source.get("file_name_map"))

        self.update_state(
            state="PROGRESS",
            meta={"current": 20, "status": "Scanning current files"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            # Download all files from storage to temp directory, preserving
            # directory structure
            if storage.is_directory(source_file_path):
                files_list = storage.list_files(source_file_path)

                for storage_file_path in files_list:
                    if storage.is_directory(storage_file_path):
                        continue

                    rel_path = os.path.relpath(storage_file_path, source_file_path)
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

            reader = SimpleDirectoryReader(
                input_dir=temp_dir,
                recursive=True,
                required_exts=list(SUPPORTED_SOURCE_EXTENSIONS),
                exclude_hidden=True,
                file_metadata=metadata_from_filename,
            )
            reader.load_data()
            directory_structure = reader.directory_structure
            logging.info(
                f"Directory structure built with token counts: {directory_structure}"
            )

            try:
                old_directory_structure = source.get("directory_structure") or {}
                if isinstance(old_directory_structure, str):
                    try:
                        old_directory_structure = json.loads(old_directory_structure)
                    except Exception:
                        old_directory_structure = {}

                def _flatten_directory_structure(struct, prefix=""):
                    files = set()
                    if isinstance(struct, dict):
                        for name, meta in struct.items():
                            current_path = (
                                os.path.join(prefix, name) if prefix else name
                            )
                            if isinstance(meta, dict) and (
                                "type" in meta and "size_bytes" in meta
                            ):
                                files.add(current_path)
                            elif isinstance(meta, dict):
                                files |= _flatten_directory_structure(
                                    meta, current_path
                                )
                    return files

                old_files = _flatten_directory_structure(old_directory_structure)
                new_files = _flatten_directory_structure(directory_structure)

                added_files = sorted(new_files - old_files)
                removed_files = sorted(old_files - new_files)

                if added_files:
                    logging.info(f"Files added since last ingest: {added_files}")
                else:
                    logging.info("No files added since last ingest.")

                if removed_files:
                    logging.info(f"Files removed since last ingest: {removed_files}")
                else:
                    logging.info("No files removed since last ingest.")

            except Exception as e:
                logging.error(
                    f"Error comparing directory structures: {e}", exc_info=True
                )
                added_files = []
                removed_files = []
            try:
                if not added_files and not removed_files:
                    logging.info("No changes detected.")
                    return {
                        "source_id": source_id,
                        "user": user,
                        "status": "no_changes",
                        "added_files": [],
                        "removed_files": [],
                    }

                vector_store = VectorCreator.create_vectorstore(
                    settings.VECTOR_STORE,
                    source_id,
                    settings.EMBEDDINGS_KEY,
                )

                self.update_state(
                    state="PROGRESS",
                    meta={"current": 40, "status": "Processing file changes"},
                )

                # 1) Delete chunks from removed files
                deleted = 0
                if removed_files:
                    try:
                        for ch in vector_store.get_chunks() or []:
                            metadata = (
                                ch.get("metadata", {})
                                if isinstance(ch, dict)
                                else getattr(ch, "metadata", {})
                            )
                            raw_source = metadata.get("source")

                            source_file = str(raw_source) if raw_source else ""

                            if source_file in removed_files:
                                cid = ch.get("doc_id")
                                if cid:
                                    try:
                                        vector_store.delete_chunk(cid)
                                        deleted += 1
                                    except Exception as de:
                                        logging.error(
                                            f"Failed deleting chunk {cid}: {de}"
                                        )
                        logging.info(
                            f"Deleted {deleted} chunks from {len(removed_files)} "
                            "removed files"
                        )
                    except Exception as e:
                        logging.error(
                            f"Error during deletion of removed file chunks: {e}",
                            exc_info=True,
                        )

                # 2) Add chunks from new files
                added = 0
                if added_files:
                    try:
                        # Build list of local files for added files only
                        added_local_files = []
                        for rel_path in added_files:
                            local_path = os.path.join(temp_dir, rel_path)
                            if os.path.isfile(local_path):
                                added_local_files.append(local_path)

                        if added_local_files:
                            reader_new = SimpleDirectoryReader(
                                input_files=added_local_files,
                                exclude_hidden=True,
                                errors="ignore",
                                file_metadata=metadata_from_filename,
                            )
                            raw_docs_new = reader_new.load_data()
                            chunker_new = Chunker(
                                chunking_strategy="classic_chunk",
                                max_tokens=MAX_TOKENS,
                                min_tokens=MIN_TOKENS,
                                duplicate_headers=True,
                            )
                            chunked_new = chunker_new.chunk(documents=raw_docs_new)

                            for (
                                file_path,
                                token_count,
                            ) in reader_new.file_token_counts.items():
                                try:
                                    rel_path = os.path.relpath(
                                        file_path, start=temp_dir
                                    )
                                    path_parts = rel_path.split(os.sep)
                                    current_dir = directory_structure

                                    for part in path_parts[:-1]:
                                        if part in current_dir and isinstance(
                                            current_dir[part], dict
                                        ):
                                            current_dir = current_dir[part]
                                        else:
                                            break

                                    filename = path_parts[-1]
                                    if filename in current_dir and isinstance(
                                        current_dir[filename], dict
                                    ):
                                        current_dir[filename][
                                            "token_count"
                                        ] = token_count
                                        logging.info(
                                            f"Updated token count for "
                                            f"{rel_path}: {token_count}"
                                        )
                                except Exception as e:
                                    logging.warning(
                                        f"Could not update token count for "
                                        f"{file_path}: {e}"
                                    )

                            for d in chunked_new:
                                meta = dict(d.extra_info or {})
                                try:
                                    raw_src = meta.get("source")
                                    if isinstance(raw_src, str) and os.path.isabs(
                                        raw_src
                                    ):
                                        meta["source"] = os.path.relpath(
                                            raw_src, start=temp_dir
                                        )
                                except Exception:
                                    pass
                                display_name = _get_display_name(
                                    file_name_map, meta.get("source")
                                )
                                if display_name:
                                    display_name = str(display_name)
                                    meta["filename"] = display_name
                                    meta["file_name"] = display_name
                                    meta["title"] = display_name

                                vector_store.add_chunk(d.text, metadata=meta)
                                added += 1
                            logging.info(
                                f"Added {added} chunks from {len(added_files)} "
                                "new files"
                            )
                    except Exception as e:
                        logging.error(
                            f"Error during ingestion of new files: {e}", exc_info=True
                        )

                # 3) Update source directory structure timestamp
                try:
                    total_tokens = sum(reader.file_token_counts.values())
                    directory_structure = _apply_display_names_to_structure(
                        directory_structure, file_name_map
                    )

                    now = datetime.datetime.now()
                    with db_session() as conn:
                        SourcesRepository(conn).update(
                            source_id, user,
                            {
                                "directory_structure": directory_structure,
                                "date": now,
                                "tokens": total_tokens,
                            },
                        )
                except Exception as e:
                    logging.error(
                        f"Error updating directory_structure in DB: {e}",
                        exc_info=True,
                    )

                self.update_state(
                    state="PROGRESS",
                    meta={"current": 100, "status": "Re-ingestion completed"},
                )

                return {
                    "source_id": source_id,
                    "user": user,
                    "status": "completed",
                    "added_files": added_files,
                    "removed_files": removed_files,
                    "chunks_added": added,
                    "chunks_deleted": deleted,
                }
            except Exception as e:
                logging.error(
                    f"Error while processing file changes: {e}", exc_info=True
                )
                raise

    except Exception as e:
        logging.error(f"Error in reingest_source_worker: {e}", exc_info=True)
        raise
