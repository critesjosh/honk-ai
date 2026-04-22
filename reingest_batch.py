"""Batched re-ingest for remaining sources — 50x faster than sequential."""

import logging
import os
import sys
import tempfile

from bson.objectid import ObjectId

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(__file__))

from application.core.settings import settings
from application.parser.file.bulk import SimpleDirectoryReader
from application.parser.chunking import Chunker
from application.parser.schema.base import Document
from application.parser.embedding_pipeline import sanitize_content
from application.vectorstore.vector_creator import VectorCreator
from application.storage.storage_creator import StorageCreator
from application.worker import (
    count_tokens_docs,
    metadata_from_filename,
    SUPPORTED_SOURCE_EXTENSIONS,
    MAX_TOKENS,
    MIN_TOKENS,
)

from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/docsgpt")
client = MongoClient(MONGO_URI)
db = client.get_default_database()
sources_collection = db["sources"]

BATCH_SIZE = 50  # docs per OpenAI embeddings API call

# Only re-ingest these 5 remaining sources (the first 6 completed successfully)
REMAINING_IDS = [
    "69daee9985d3a941c455c426",  # E2E Tests (502 chunks)
    "69daee99cc51599b3d55c426",  # L1 Contracts (681 chunks)
    "69daee985f9305204555c426",  # Operator Docs (589 chunks)
    "69daee99f08e3aae2e55c426",  # Protocol Circuits (952 chunks)
    "69daee998a454ddc2055c426",  # Developer Docs (1281 chunks)
]


def reingest_source_batched(source):
    source_id = str(source["_id"])
    name = source.get("name", source_id)
    file_path = source.get("file_path", "")
    logger.info(f"--- Re-ingesting: {name} ({source_id}) ---")

    storage = StorageCreator.get_storage()

    # Delete old FAISS index
    for fname in ["index.faiss", "index.pkl"]:
        fpath = f"indexes/{source_id}/{fname}"
        try:
            if storage.file_exists(fpath):
                storage.delete_file(fpath)
        except Exception:
            pass

    with tempfile.TemporaryDirectory() as temp_dir:
        # Download source files
        if storage.is_directory(file_path):
            for sfp in storage.list_files(file_path):
                if storage.is_directory(sfp):
                    continue
                rel = os.path.relpath(sfp, file_path)
                local = os.path.join(temp_dir, rel)
                os.makedirs(os.path.dirname(local), exist_ok=True)
                try:
                    with open(local, "wb") as f:
                        f.write(storage.get_file(sfp).read())
                except Exception as e:
                    logger.error(f"  Download error {sfp}: {e}")
        else:
            logger.warning(f"  Not a directory: {file_path}")
            return

        # Parse
        reader = SimpleDirectoryReader(
            input_dir=temp_dir, recursive=True,
            required_exts=list(SUPPORTED_SOURCE_EXTENSIONS),
            exclude_hidden=True, file_metadata=metadata_from_filename,
        )
        raw_docs = reader.load_data()
        logger.info(f"  Parsed {len(raw_docs)} raw documents")

        # Chunk
        chunker = Chunker(
            chunking_strategy="classic_chunk",
            max_tokens=MAX_TOKENS, min_tokens=MIN_TOKENS,
            duplicate_headers=True,
        )
        raw_docs = chunker.chunk(documents=raw_docs)
        logger.info(f"  Chunked into {len(raw_docs)} chunks")

        docs = [Document.to_langchain_format(d) for d in raw_docs]

        # Sanitize and tag metadata
        for doc in docs:
            doc.page_content = sanitize_content(doc.page_content)
            doc.metadata["source_id"] = str(source_id)

        # Create initial store with first doc
        docs_init = [docs.pop(0)]
        store = VectorCreator.create_vectorstore(
            settings.VECTOR_STORE,
            docs_init=docs_init,
            source_id=source_id,
            embeddings_key=os.getenv("EMBEDDINGS_KEY"),
        )

        # Batch-add remaining docs
        total = len(docs)
        for i in range(0, total, BATCH_SIZE):
            batch = docs[i:i + BATCH_SIZE]
            texts = [d.page_content for d in batch]
            metas = [d.metadata for d in batch]
            store.add_texts(texts, metadatas=metas)
            logger.info(f"  Embedded {min(i + BATCH_SIZE, total)}/{total}")

        # Save
        vector_store_path = os.path.join(temp_dir, "vector_store")
        os.makedirs(vector_store_path, exist_ok=True)
        store.save_local(vector_store_path)
        logger.info("  Vector store saved")

        # Move to indexes dir
        idx_dir = f"indexes/{source_id}"
        os.makedirs(idx_dir, exist_ok=True)
        for fname in ["index.faiss", "index.pkl"]:
            src = os.path.join(vector_store_path, fname)
            dst = os.path.join(idx_dir, fname)
            if os.path.exists(src):
                import shutil
                shutil.copy2(src, dst)

        tokens = count_tokens_docs([docs_init[0]] + docs)
        sources_collection.update_one(
            {"_id": ObjectId(source_id)},
            {"$set": {"tokens": str(tokens)}},
        )
        logger.info(f"  Done: {total + 1} chunks, {tokens} tokens")


def main():
    for sid in REMAINING_IDS:
        source = sources_collection.find_one({"_id": ObjectId(sid)})
        if not source:
            logger.error(f"Source {sid} not found, skipping")
            continue
        try:
            reingest_source_batched(source)
        except Exception as e:
            logger.error(f"Failed {source.get('name')}: {e}", exc_info=True)
    logger.info("All remaining sources re-ingested.")


if __name__ == "__main__":
    main()
