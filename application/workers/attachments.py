"""Attachment processing — single file, no vectorisation.

Reads a file out of storage, extracts text via the same parser used
for ingest, stores the result on the ``attachments`` table for later
inline-attachment use.
"""

from __future__ import annotations

import logging
import mimetypes

from application.core.settings import settings
from application.parser.file.bulk import (
    SimpleDirectoryReader,
    get_default_file_extractor,
)
from application.storage.db.repositories.attachments import AttachmentsRepository
from application.storage.db.session import db_session
from application.storage.storage_creator import StorageCreator
from application.utils import num_tokens_from_string
from application.workers._helpers import metadata_from_filename


def attachment_worker(self, file_info, user):
    """Process and store a single attachment without vectorization."""

    filename = file_info["filename"]
    attachment_id = file_info["attachment_id"]
    relative_path = file_info["path"]
    metadata = file_info.get("metadata", {})

    try:
        self.update_state(state="PROGRESS", meta={"current": 10})
        storage = StorageCreator.get_storage()

        self.update_state(
            state="PROGRESS",
            meta={"current": 30, "status": "Processing content"},
        )

        file_extractor = get_default_file_extractor(
            ocr_enabled=settings.DOCLING_OCR_ATTACHMENTS_ENABLED,
        )
        attachment_document = storage.process_file(
            relative_path,
            lambda local_path, **kwargs: SimpleDirectoryReader(
                input_files=[local_path],
                exclude_hidden=True,
                errors="ignore",
                file_extractor=file_extractor,
                file_metadata=metadata_from_filename,
            )
            .load_data()[0],
        )
        content = attachment_document.text
        parser_metadata = {
            key: value
            for key, value in (attachment_document.extra_info or {}).items()
            if key.startswith("transcript_")
        }
        if parser_metadata:
            metadata = {**metadata, **parser_metadata}

        token_count = num_tokens_from_string(content)
        if token_count > 100000:
            content = content[:250000]
            token_count = num_tokens_from_string(content)

        self.update_state(
            state="PROGRESS",
            meta={"current": 80, "status": "Storing in database"},
        )

        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # The upload route produces a UUID-shaped ``attachment_id`` (stored
        # in the storage path) but the PG ``attachments.id`` is generated
        # by the DB. Keep ``attachment_id`` as the caller-visible handle
        # used for the storage path, and stash it in ``legacy_mongo_id``
        # so the attachment row is resolvable via that handle too.
        with db_session() as conn:
            AttachmentsRepository(conn).create(
                user, filename, relative_path,
                mime_type=mime_type,
                content=content,
                token_count=token_count,
                metadata=metadata,
                legacy_mongo_id=str(attachment_id),
            )

        logging.info(
            f"Stored attachment with ID: {attachment_id}", extra={"user": user},
        )

        self.update_state(
            state="PROGRESS",
            meta={"current": 100, "status": "Complete"},
        )

        return {
            "filename": filename,
            "path": relative_path,
            "token_count": token_count,
            "attachment_id": attachment_id,
            "mime_type": mime_type,
            "metadata": metadata,
        }
    except Exception as e:
        logging.error(
            f"Error processing file {filename}: {e}",
            extra={"user": user},
            exc_info=True,
        )
        raise
