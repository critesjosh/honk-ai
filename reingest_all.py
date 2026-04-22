"""Re-ingest all sources with updated chunking logic.

Deletes existing FAISS indexes and re-processes each source's files
through the full chunking + embedding pipeline.
"""

import logging
import os
import sys
import tempfile

from bson.objectid import ObjectId

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Ensure the application package is importable
sys.path.insert(0, os.path.dirname(__file__))

from application.core.settings import settings
from application.parser.file.bulk import SimpleDirectoryReader
from application.parser.chunking import Chunker
from application.parser.schema.base import Document
from application.vectorstore.vector_creator import VectorCreator
from application.storage.storage_creator import StorageCreator
from application.worker import (
    embed_and_store_documents,
    count_tokens_docs,
    metadata_from_filename,
    SUPPORTED_SOURCE_EXTENSIONS,
    MAX_TOKENS,
    MIN_TOKENS,
)

class _DummyTask:
    """Stub that satisfies task_status.update_state() calls."""
    def update_state(self, **kwargs):
        pass

# Direct MongoDB connection
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/docsgpt")
client = MongoClient(MONGO_URI)
db = client.get_default_database()
sources_collection = db["sources"]


def reingest_source(source):
    source_id = str(source["_id"])
    name = source.get("name", source_id)
    file_path = source.get("file_path", "")
    logger.info(f"--- Re-ingesting: {name} ({source_id}) ---")

    storage = StorageCreator.get_storage()

    # Step 1: Delete old FAISS index
    index_dir = f"indexes/{source_id}"
    for fname in ["index.faiss", "index.pkl"]:
        fpath = f"{index_dir}/{fname}"
        try:
            if storage.file_exists(fpath):
                storage.delete_file(fpath)
                logger.info(f"  Deleted {fpath}")
        except Exception as e:
            logger.warning(f"  Could not delete {fpath}: {e}")

    # Step 2: Download source files to temp dir
    with tempfile.TemporaryDirectory() as temp_dir:
        if storage.is_directory(file_path):
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
                    logger.error(f"  Error downloading {storage_file_path}: {e}")
                    continue
        else:
            logger.warning(f"  file_path is not a directory: {file_path}, skipping")
            return

        # Step 3: Parse files
        reader = SimpleDirectoryReader(
            input_dir=temp_dir,
            recursive=True,
            required_exts=list(SUPPORTED_SOURCE_EXTENSIONS),
            exclude_hidden=True,
            file_metadata=metadata_from_filename,
        )
        raw_docs = reader.load_data()
        logger.info(f"  Parsed {len(raw_docs)} raw documents")

        # Step 4: Chunk with new settings
        chunker = Chunker(
            chunking_strategy="classic_chunk",
            max_tokens=MAX_TOKENS,
            min_tokens=MIN_TOKENS,
            duplicate_headers=True,
        )
        raw_docs = chunker.chunk(documents=raw_docs)
        logger.info(f"  Chunked into {len(raw_docs)} chunks")

        docs = [Document.to_langchain_format(raw_doc) for raw_doc in raw_docs]

        # Step 5: Embed and store
        vector_store_path = os.path.join(temp_dir, "vector_store")
        os.makedirs(vector_store_path, exist_ok=True)

        embed_and_store_documents(docs, vector_store_path, ObjectId(source_id), _DummyTask())

        tokens = count_tokens_docs(docs)

        # Step 6: Update token count in MongoDB
        sources_collection.update_one(
            {"_id": ObjectId(source_id)},
            {"$set": {"tokens": str(tokens)}},
        )
        logger.info(f"  Done: {len(docs)} chunks, {tokens} tokens")


def main():
    sources = list(sources_collection.find({"type": "local"}))
    logger.info(f"Found {len(sources)} local sources to re-ingest")
    for source in sources:
        try:
            reingest_source(source)
        except Exception as e:
            logger.error(f"Failed to re-ingest {source.get('name')}: {e}", exc_info=True)
    logger.info("All sources re-ingested.")


if __name__ == "__main__":
    main()
