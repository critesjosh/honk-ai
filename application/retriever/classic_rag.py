import logging
import os

from application.core.settings import settings
from application.llm.llm_creator import LLMCreator
from application.retriever.base import BaseRetriever
from application.utils import num_tokens_from_string
from application.vectorstore.vector_creator import VectorCreator


class ClassicRAG(BaseRetriever):
    def __init__(
        self,
        source,
        chat_history=None,
        prompt="",
        chunks=2,
        doc_token_limit=50000,
        model_id="docsgpt-local",
        user_api_key=None,
        agent_id=None,
        llm_name=settings.LLM_PROVIDER,
        api_key=settings.API_KEY,
        decoded_token=None,
    ):
        self.original_question = source.get("question", "")
        self.chat_history = chat_history if chat_history is not None else []
        self.prompt = prompt
        if isinstance(chunks, str):
            try:
                self.chunks = int(chunks)
            except ValueError:
                logging.warning(
                    f"Invalid chunks value '{chunks}', using default value 2"
                )
                self.chunks = 2
        else:
            self.chunks = chunks
        user_id = decoded_token.get("sub") if decoded_token else "default"
        logging.info(
            f"ClassicRAG initialized with chunks={self.chunks}, user_id={user_id}, "
            f"sources={'active_docs' in source and source['active_docs'] is not None}"
        )
        self.model_id = model_id
        self.doc_token_limit = doc_token_limit
        self.user_api_key = user_api_key
        self.agent_id = agent_id
        self.llm_name = llm_name
        self.api_key = api_key
        self.llm = LLMCreator.create_llm(
            self.llm_name,
            api_key=self.api_key,
            user_api_key=self.user_api_key,
            decoded_token=decoded_token,
            agent_id=self.agent_id,
        )

        if "active_docs" in source and source["active_docs"] is not None:
            if isinstance(source["active_docs"], list):
                self.vectorstores = source["active_docs"]
            else:
                self.vectorstores = [source["active_docs"]]
        else:
            self.vectorstores = []
        self.question = self._rephrase_query()
        self.decoded_token = decoded_token
        self._validate_vectorstore_config()

    def _validate_vectorstore_config(self):
        """Validate vectorstore IDs and remove any empty/invalid entries"""
        if not self.vectorstores:
            logging.warning("No vectorstores configured for retrieval")
            return
        invalid_ids = [
            vs_id for vs_id in self.vectorstores if not vs_id or not vs_id.strip()
        ]
        if invalid_ids:
            logging.warning(f"Found invalid vectorstore IDs: {invalid_ids}")
            self.vectorstores = [
                vs_id for vs_id in self.vectorstores if vs_id and vs_id.strip()
            ]

    def _rephrase_query(self):
        """Rephrase user query with chat history context for better retrieval"""
        if (
            not self.original_question
            or not self.chat_history
            or self.chat_history == []
            or self.chunks == 0
            or not self.vectorstores
        ):
            return self.original_question
        prompt = (
            "Given the following conversation history:\n"
            f"{self.chat_history}\n\n"
            "Rephrase the following user question to be a standalone search query "
            "that captures all relevant context from the conversation:\n"
        )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": self.original_question},
        ]

        try:
            rephrased_query = self.llm.gen(model=self.model_id, messages=messages)
            print(f"Rephrased query: {rephrased_query}")
            return rephrased_query if rephrased_query else self.original_question
        except Exception as e:
            logging.error(f"Error rephrasing query: {e}", exc_info=True)
            return self.original_question

    def _extract_doc_fields(self, doc):
        """Normalise page_content + metadata + derived fields from a Document
        or dict-shaped result into the fields the retrieval output uses."""
        if hasattr(doc, "page_content") and hasattr(doc, "metadata"):
            page_content = doc.page_content
            metadata = doc.metadata or {}
        else:
            page_content = doc.get("text", doc.get("page_content", ""))
            metadata = doc.get("metadata", {}) or {}

        title = metadata.get("title", metadata.get("post_title", page_content))
        if not isinstance(title, str):
            title = str(title)
        title = title.split("/")[-1]

        filename = (
            metadata.get("filename")
            or metadata.get("file_name")
            or metadata.get("source")
        )
        if isinstance(filename, str):
            filename = os.path.basename(filename) or filename
        else:
            filename = title
        if not filename:
            filename = title

        source_path = (
            metadata.get("source")
            or metadata.get("_source_id")
            or "unknown"
        )
        return page_content, metadata, title, filename, source_path

    def _pack_into_budget(self, ranked_pairs, token_budget):
        """Greedy-pack docs (ordered by ascending cosine distance) into the
        token budget, deduplicating near-identical chunks so a single
        document with several adjacent chunks does not starve other sources.
        """
        seen_keys = set()
        all_docs = []
        cumulative_tokens = 0

        for doc, _distance in ranked_pairs:
            if cumulative_tokens >= token_budget:
                break

            page_content, _md, title, filename, source_path = (
                self._extract_doc_fields(doc)
            )

            # Dedup: near-identical chunks from the same source often
            # surface together when pgvector walks a document linearly.
            # Key includes filename so that two different files in the
            # same corpus don't collapse just because source_path fell
            # back to the corpus-level _source_id (when metadata.source
            # is missing).
            dedup_key = (source_path, filename, (page_content or "")[:200])
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            doc_text_with_header = f"{filename}\n{page_content}"
            doc_tokens = num_tokens_from_string(doc_text_with_header)
            if cumulative_tokens + doc_tokens < token_budget:
                all_docs.append(
                    {
                        "title": title,
                        "text": page_content,
                        "source": source_path,
                        "filename": filename,
                    }
                )
                cumulative_tokens += doc_tokens

        return all_docs, cumulative_tokens

    def _get_data(self):
        """Retrieve chunks for the question across ``self.vectorstores``.

        Strategy: embed the question once, then run a SINGLE global vector
        search across all configured sources sorted by cosine distance, and
        greedy-pack the top results into the token budget. This replaces
        an older per-source FIFO loop that caused the first few sources in
        ``AZTEC_SOURCE_IDS`` to greedily consume the budget while later
        sources contributed zero documents regardless of relevance.
        """
        if self.chunks == 0 or not self.vectorstores:
            logging.info(
                f"ClassicRAG._get_data: Skipping retrieval - chunks={self.chunks}, "
                f"vectorstores_count={len(self.vectorstores) if self.vectorstores else 0}"
            )
            return []

        token_budget = max(int(self.doc_token_limit * 0.9), 100)

        # Global candidate pool size. Needs to be large enough that the
        # packer can fill `token_budget` even after dedup drops some top
        # hits. The ingest filter drops chunks <50 tokens, so a lower bound
        # on chunks-that-fit is ``token_budget / 50``; over-fetch by 2x for
        # headroom. Also scale with source count and the requested
        # ``chunks`` hint so small/tightly-scoped queries aren't hurt.
        candidate_k = max(
            120,
            len(self.vectorstores) * 10,
            self.chunks * 20,
            (token_budget // 50) * 2,
        )

        # One vectorstore instance drives the whole search. We pass all
        # source_ids to search_by_vector_with_score, so the instance's own
        # _source_id is ignored — we just need it for its embedding client
        # and the DB connection.
        try:
            docsearch = VectorCreator.create_vectorstore(
                settings.VECTOR_STORE,
                self.vectorstores[0],
                settings.EMBEDDINGS_KEY,
            )
        except Exception as e:
            logging.error(
                f"Error creating vectorstore: {e}", exc_info=True
            )
            return []

        if not hasattr(docsearch, "search_by_vector_with_score"):
            # Raise loudly rather than silently answer without context —
            # silent empty retrieval is exactly the kind of operational
            # bug we don't want masked. This surfaces as a 5xx to the
            # caller and a stack trace in logs, which is what we want
            # for a misconfigured backend.
            raise RuntimeError(
                f"Vector backend {type(docsearch).__name__} does not "
                "implement search_by_vector_with_score. Implement the "
                "method on this backend or switch to pgvector."
            )

        try:
            query_vector = docsearch._embedding.embed_query(self.question)
        except Exception as e:
            logging.error(f"Error embedding question: {e}", exc_info=True)
            return []

        pairs = docsearch.search_by_vector_with_score(
            query_vector,
            k=candidate_k,
            source_ids=list(self.vectorstores),
        )
        all_docs, cumulative_tokens = self._pack_into_budget(
            pairs, token_budget
        )
        logging.info(
            f"ClassicRAG._get_data: Retrieval complete - retrieved "
            f"{len(all_docs)} documents (global rerank over "
            f"{len(self.vectorstores)} sources, candidate_k={candidate_k}, "
            f"cumulative_tokens={cumulative_tokens}/{token_budget})"
        )
        return all_docs

    def search(self, query: str = ""):
        """Search for documents using optional query override"""
        if query:
            self.original_question = query
            self.question = self._rephrase_query()
        return self._get_data()
