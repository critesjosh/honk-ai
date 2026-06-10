import logging
import os
from abc import ABC, abstractmethod

import requests
from langchain_openai import OpenAIEmbeddings

from application.core.settings import settings


class RemoteEmbeddings:
    """
    Wrapper for remote embeddings API (OpenAI-compatible).
    Used when EMBEDDINGS_BASE_URL is configured.
    Sends requests to {base_url}/v1/embeddings in OpenAI format.
    """

    def __init__(self, api_url: str, model_name: str, api_key: str = None):
        self.api_url = api_url.rstrip("/")
        self.model_name = model_name
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        # Unknown until the first embedding call returns; auto-detected in
        # embed_query / embed_documents. Hardcoding a value here would
        # misreport the dimension for any non-768 model (e.g. OpenAI
        # text-embedding-3-large at 3072) to callers that introspect it.
        self.dimension = None

    def _embed(self, inputs):
        """Send embedding request to remote API in OpenAI-compatible format."""
        payload = {"input": inputs}
        if self.model_name:
            payload["model"] = self.model_name

        url = f"{self.api_url}/v1/embeddings"
        response = requests.post(url, headers=self.headers, json=payload, timeout=180)
        response.raise_for_status()
        result = response.json()

        # Handle OpenAI-compatible response format
        if isinstance(result, dict):
            if "error" in result:
                raise ValueError(f"Remote embeddings API error: {result['error']}")
            if "data" in result:
                # Sort by index to ensure correct order
                data = sorted(result["data"], key=lambda x: x.get("index", 0))
                return [item["embedding"] for item in data]
            raise ValueError(
                f"Unexpected response format from remote embeddings API: {result}"
            )
        else:
            raise ValueError(
                f"Unexpected response format from remote embeddings API: {result}"
            )

    def embed_query(self, query: str):
        """Embed a single query string."""
        embeddings_list = self._embed(query)
        if (
            isinstance(embeddings_list, list)
            and len(embeddings_list) == 1
            and isinstance(embeddings_list[0], list)
        ):
            if self.dimension is None:
                self.dimension = len(embeddings_list[0])
            return embeddings_list[0]
        raise ValueError(
            f"Unexpected result structure after embedding query: {embeddings_list}"
        )

    def embed_documents(self, documents: list):
        """Embed a list of documents."""
        if not documents:
            return []
        embeddings_list = self._embed(documents)
        if self.dimension is None and embeddings_list:
            self.dimension = len(embeddings_list[0])
        return embeddings_list

    def __call__(self, text):
        if isinstance(text, str):
            return self.embed_query(text)
        elif isinstance(text, list):
            return self.embed_documents(text)
        else:
            raise ValueError("Input must be a string or a list of strings")


class EmbeddingsSingleton:
    _instances = {}

    @staticmethod
    def get_instance(embeddings_name, *args, **kwargs):
        if embeddings_name not in EmbeddingsSingleton._instances:
            EmbeddingsSingleton._instances[embeddings_name] = (
                EmbeddingsSingleton._create_instance(embeddings_name, *args, **kwargs)
            )
        return EmbeddingsSingleton._instances[embeddings_name]

    @staticmethod
    def _create_instance(embeddings_name, *args, **kwargs):
        if embeddings_name == "openai_text-embedding-ada-002":
            return OpenAIEmbeddings(*args, **kwargs)
        raise ValueError(
            f"No embeddings backend for {embeddings_name!r}: local "
            "(SentenceTransformer) embeddings were removed from this fork. "
            "Set EMBEDDINGS_BASE_URL to use a remote OpenAI-compatible "
            "embeddings API."
        )


class BaseVectorStore(ABC):
    def __init__(self):
        pass

    @abstractmethod
    def search(self, *args, **kwargs):
        """Search for similar documents/chunks in the vectorstore"""
        pass

    @abstractmethod
    def add_texts(self, texts, metadatas=None, *args, **kwargs):
        """Add texts with their embeddings to the vectorstore"""
        pass

    def delete_index(self, *args, **kwargs):
        """Delete the entire index/collection"""
        pass

    def save_local(self, *args, **kwargs):
        """Save vectorstore to local storage"""
        pass

    def get_chunks(self, *args, **kwargs):
        """Get all chunks from the vectorstore"""
        pass

    def add_chunk(self, text, metadata=None, *args, **kwargs):
        """Add a single chunk to the vectorstore"""
        pass

    def delete_chunk(self, chunk_id, *args, **kwargs):
        """Delete a specific chunk from the vectorstore"""
        pass

    def is_azure_configured(self):
        return (
            settings.OPENAI_API_BASE
            and settings.OPENAI_API_VERSION
            and settings.AZURE_DEPLOYMENT_NAME
        )

    def _get_embeddings(self, embeddings_name, embeddings_key=None):
        # Check for remote embeddings first
        if settings.EMBEDDINGS_BASE_URL:
            logging.info(
                f"Using remote embeddings API at: {settings.EMBEDDINGS_BASE_URL}"
            )
            cache_key = f"remote_{settings.EMBEDDINGS_BASE_URL}_{embeddings_name}"
            if cache_key not in EmbeddingsSingleton._instances:
                EmbeddingsSingleton._instances[cache_key] = RemoteEmbeddings(
                    api_url=settings.EMBEDDINGS_BASE_URL,
                    model_name=embeddings_name,
                    api_key=embeddings_key,
                )
            return EmbeddingsSingleton._instances[cache_key]

        if embeddings_name == "openai_text-embedding-ada-002":
            if self.is_azure_configured():
                os.environ["OPENAI_API_TYPE"] = "azure"
                return EmbeddingsSingleton.get_instance(
                    embeddings_name, model=settings.AZURE_EMBEDDINGS_DEPLOYMENT_NAME
                )
            return EmbeddingsSingleton.get_instance(
                embeddings_name, openai_api_key=embeddings_key
            )
        raise ValueError(
            f"No embeddings backend for {embeddings_name!r}: local "
            "(SentenceTransformer) embeddings were removed from this fork. "
            "Set EMBEDDINGS_BASE_URL to use a remote OpenAI-compatible "
            "embeddings API."
        )
