"""Vector store registry.

Aztec fork is pgvector-only — embeddings live in the same Postgres
instance as user data. The upstream FAISS / Elasticsearch / Milvus /
MongoDB / Qdrant backends were removed when the admin SPA came out;
keeping their imports forced large client libraries (qdrant-client,
elasticsearch, pymilvus, faiss-cpu) into the backend image even
though ``VECTOR_STORE=pgvector`` was set.

If you ever need to switch backends, restore the matching module +
registry entry here and the dependency in
``application/requirements.txt``.
"""

from application.vectorstore.pgvector import PGVectorStore


class VectorCreator:
    vectorstores = {
        "pgvector": PGVectorStore,
    }

    @classmethod
    def create_vectorstore(cls, type, *args, **kwargs):
        vectorstore_class = cls.vectorstores.get(type.lower())
        if not vectorstore_class:
            raise ValueError(
                f"No vectorstore class found for type {type}. "
                "Aztec fork is pgvector-only — set VECTOR_STORE=pgvector."
            )
        return vectorstore_class(*args, **kwargs)
