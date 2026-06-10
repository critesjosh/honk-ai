import logging
import os

from application.core.settings import settings
from application.llm.llm_creator import LLMCreator
from application.retriever.apiref_resolver import resolve_canonical_apiref
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
        api_key=None,
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
        # No LLM rephrase at construction time: every call site either
        # passes the question via ``search(query)`` (which rephrases with
        # chat history there) or seeds ``source["question"]`` without any
        # chat history (``scripts/eval/eval_retrieval.py`` does this and
        # calls ``_get_data()`` directly), so rephrasing here was a no-op.
        self.question = self.original_question
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

        # Deliberately not logged: the rephrased query is an LLM restatement
        # of the user's question, and this repo hashes user questions before
        # logging.
        try:
            rephrased_query = self.llm.gen(model=self.model_id, messages=messages)
            return rephrased_query if rephrased_query else self.original_question
        except Exception as e:
            logging.error(f"Error rephrasing query: {e}", exc_info=True)
            return self.original_question

    # Aztec-fork: parser-friendliness suffixes appended at ingest time
    # to satisfy the DocsGPT extension allowlist. We strip these from
    # the filename shown to the LLM so faithfulness rules ("only cite
    # filenames that appear as chunk headers") don't push the model to
    # echo "hash.nr.md" or "Token.nr.txt" into its prose.
    #
    # A name like "hash.nr.md" → "hash.nr" (the canonical code path).
    # Plain ".md" / ".txt" filenames (e.g. concept docs ending in
    # exactly ".md") are left alone — only the doubled
    # ``.<code>.<parser>`` shape is treated as the hack.
    _AZTEC_PARSER_SUFFIXES = (".md", ".txt")
    _AZTEC_CODE_EXTENSIONS = (".nr", ".ts", ".sol")

    @classmethod
    def _strip_extension_hack(cls, name: str) -> str:
        if not isinstance(name, str):
            return name
        for psuf in cls._AZTEC_PARSER_SUFFIXES:
            if name.endswith(psuf):
                base = name[: -len(psuf)]
                if any(base.endswith(cext) for cext in cls._AZTEC_CODE_EXTENSIONS):
                    return base
        return name

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
        title = self._strip_extension_hack(title)

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
        # Strip the parser-friendliness suffix from the LLM-visible
        # filename. The original `metadata.source` (with suffix) is
        # preserved in the SSE source frame; only the chunk header
        # the LLM grounds against gets the canonical extension.
        filename = self._strip_extension_hack(filename)

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
                # Propagate ``chunk_type`` (set by the ingest chunker in
                # ``application/parser/file/bulk.py`` for ``*.nr.md`` /
                # ``*.ts.md`` / ``*.sol.md`` files) so the chunk-header
                # builder in ``stream_processor.pre_fetch_docs`` can tag
                # apiref chunks visibly to the LLM. The prompt then asks
                # the LLM to cite those chunks when the answer mentions
                # an identifier they define.
                doc_dict = {
                    "title": title,
                    "text": page_content,
                    "source": source_path,
                    "filename": filename,
                }
                chunk_type = _md.get("chunk_type") if isinstance(_md, dict) else None
                if chunk_type:
                    doc_dict["chunk_type"] = chunk_type
                all_docs.append(doc_dict)
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

        # Scoped apiref resolver — see apiref_resolver.py for the design.
        # Returns the apiref chunk that DEFINES an identifier in the
        # question (scoped to inferred source family), or None. We pin
        # the resolved chunk at position 0 if we don't already have it,
        # so the SSE source frame (which mirrors retrieval order) cites
        # the canonical reference first for identifier-shaped queries.
        # For concept queries and TS questions without matching apiref,
        # resolver returns None and behavior is unchanged from the
        # single-pass baseline.
        apiref_pinned = False
        try:
            pin = resolve_canonical_apiref(
                docsearch,
                self.question,
                query_vector,
                self.vectorstores,
            )
        except Exception:
            logging.warning(
                "apiref_resolver raised; falling back to standard retrieval",
                exc_info=True,
            )
            pin = None
        if pin is not None:
            pinned_doc, pinned_distance = pin
            pinned_key = (
                pinned_doc.metadata.get("source"),
                pinned_doc.metadata.get("filename"),
                (pinned_doc.page_content or "")[:200],
            )
            # If the pinned chunk is already the global #1, leave the
            # list alone (idempotent). Otherwise prepend it. ``pairs`` is
            # (Document, distance); dedup happens inside the packer.
            existing_top = pairs[0] if pairs else None
            if existing_top is not None:
                top_key = (
                    existing_top[0].metadata.get("source") if hasattr(existing_top[0], "metadata") else None,
                    existing_top[0].metadata.get("filename") if hasattr(existing_top[0], "metadata") else None,
                    (getattr(existing_top[0], "page_content", "") or "")[:200],
                )
                if top_key == pinned_key:
                    apiref_pinned = False  # already first; no-op
                else:
                    pairs = [(pinned_doc, pinned_distance)] + list(pairs)
                    apiref_pinned = True
            else:
                pairs = [(pinned_doc, pinned_distance)]
                apiref_pinned = True

        all_docs, cumulative_tokens = self._pack_into_budget(
            pairs, token_budget
        )
        logging.info(
            f"ClassicRAG._get_data: Retrieval complete - retrieved "
            f"{len(all_docs)} documents (global rerank over "
            f"{len(self.vectorstores)} sources, candidate_k={candidate_k}, "
            f"apiref_pinned={apiref_pinned}, "
            f"cumulative_tokens={cumulative_tokens}/{token_budget})"
        )
        return all_docs

    def search(self, query: str = ""):
        """Search for documents using optional query override"""
        if query:
            self.original_question = query
            self.question = self._rephrase_query()
        return self._get_data()
