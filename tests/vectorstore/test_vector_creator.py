from unittest.mock import patch

import pytest

from application.vectorstore.vector_creator import VectorCreator


@pytest.mark.unit
class TestVectorCreator:
    def test_registered_vectorstores(self):
        # Aztec fork is pgvector-only — the upstream FAISS / Mongo /
        # Qdrant / Milvus / Elasticsearch backends were removed.
        assert set(VectorCreator.vectorstores) == {"pgvector"}

    def test_create_vectorstore_invalid_type(self):
        with pytest.raises(ValueError, match="No vectorstore class found for type"):
            VectorCreator.create_vectorstore("nonexistent")

    def test_create_vectorstore_case_insensitive(self):
        with patch.object(
            VectorCreator.vectorstores["pgvector"], "__init__", return_value=None
        ) as mock_init:
            VectorCreator.create_vectorstore(
                "PGVECTOR", source_id="test", embeddings_key="key"
            )
            mock_init.assert_called_once_with(source_id="test", embeddings_key="key")

    def test_create_vectorstore_passes_args(self):
        with patch.object(
            VectorCreator.vectorstores["pgvector"], "__init__", return_value=None
        ) as mock_init:
            VectorCreator.create_vectorstore(
                "pgvector", source_id="src1", embeddings_key="ek"
            )
            mock_init.assert_called_once_with(source_id="src1", embeddings_key="ek")
