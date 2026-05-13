import datetime
import json
import logging
import queue
import threading
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

from flask import jsonify, make_response, Response
from flask_restx import Namespace

from application.api.answer.routes.aztec_doc_slugs import apply_slug_override
from application.api.answer.services.continuation_service import ContinuationService
from application.api.answer.services.conversation_service import ConversationService
from application.core.model_utils import (
    get_api_key_for_provider,
    get_default_model_id,
    get_provider_from_model_id,
)

from application.core.settings import settings
from application.error import sanitize_api_error
from application.llm.llm_creator import LLMCreator
from application.storage.db.repositories.agents import AgentsRepository
from application.storage.db.repositories.token_usage import TokenUsageRepository
from application.storage.db.repositories.user_logs import UserLogsRepository
from application.storage.db.session import db_readonly, db_session
from application.utils import check_required_fields

logger = logging.getLogger(__name__)


answer_ns = Namespace("answer", description="Answer related operations", path="/")


_HEARTBEAT_INTERVAL_SECONDS = 15.0

# Cap sources emitted to the client. The React widget already shows 3 with
# a "+ N more" toggle, and long source lists push the answer off-screen.
_MAX_SOURCES_EMITTED = 10

# ---- Source URL mapping (Aztec fork) ------------------------------------
# Corpus paths stored in `metadata.source` are relative to the ingest zip.
# Map them to public URLs:
#   - Developer Docs (rendered) → docs.aztec.network
#   - Everything else (code, non-developer-docs markdown) → GitHub at v4.2.0
# Widget renders `source.source` as the <a href>, so we rewrite that field
# in-place before emitting.
_AZTEC_DOCS_BASE = "https://docs.aztec.network/developers/docs"
# Top-level developer docs (overview, ai_tooling, getting_started_*) live
# directly under /developers/ on the rendered site, NOT /developers/docs/.
_AZTEC_DEV_TOP_BASE = "https://docs.aztec.network/developers"
# Network docs (sequencer/prover/operator content) are rendered under
# /operate/ on the site even though the corpus prefix is `operators/`.
_AZTEC_OPERATE_BASE = "https://docs.aztec.network/operate"
# Unversioned site-root pages from aztec-packages' ``docs/docs/`` —
# e.g. ``networks.md`` rendered at /networks. Lives in the
# ``aztec_site_networks`` corpus (zip prefix ``aztec-site/``).
_AZTEC_SITE_BASE = "https://docs.aztec.network"
_AZTEC_GITHUB_BASE = (
    "https://github.com/AztecProtocol/aztec-packages/blob/v4.2.0"
)
# Rendered Noir language docs — point at the canonical site rather than
# the GitHub source. Stripping the markdown extension matches the
# Docusaurus URL scheme; trailing /index segments are also stripped.
_NOIR_DOCS_BASE = "https://noir-lang.org/docs"
# Noir is a separate repo; aztec-packages v4.2.0 pins it at this commit via
# the noir/noir-repo submodule. Used for noir-stdlib apiref source files
# (those are real .nr source code, not rendered docs). Update this commit
# when bumping Aztec versions.
_NOIR_GITHUB_BASE = (
    "https://github.com/noir-lang/noir/blob/842974fcf034b0a652631e69fc24f92f9ddd1d37"
)

# Corpus prefix → GitHub repo prefix. First match wins, so put the more
# specific prefixes before their catch-alls.
#
# Note on what is NOT here: `version-v4.2.0/` and `version-v4.2.0/operators/`
# used to fall through to GitHub at `docs/developer_versioned_docs/version-v4.2.0/`
# and `docs/network_versioned_docs/version-v4.2.0/operators/`, but those
# folders don't exist at the literal v4.2.0 tag (the tag has them under
# `version-v4.1.0-rc.2`, since the docs version snapshot is taken from a
# moving branch). All such markdown content is rendered on docs.aztec.network
# anyway, so we route it there directly via the dedicated rules in
# `_aztec_source_url` — never via GitHub.
_SOURCE_TO_REPO_PREFIX: List[Tuple[str, str]] = [
    ("end-to-end/",               "yarn-project/end-to-end/src/"),
    ("cli/",                      "yarn-project/cli/src/"),
    ("cli-wallet/",               "yarn-project/cli-wallet/src/"),
    ("aztec.js/",                 "yarn-project/aztec.js/src/"),
    ("aztec-nr/",                 "noir-projects/aztec-nr/"),
    ("noir-contracts/",           "noir-projects/noir-contracts/contracts/"),
    ("noir-protocol-circuits/",   "noir-projects/noir-protocol-circuits/"),
    ("l1-contracts/",             "l1-contracts/"),
    # Auto-generated TypeScript API reference. At the v4.2.0 tag these docs
    # live under docs/static/typescript-api/testnet/ (the folder was renamed
    # to mainnet/ on a later release).
    ("typescript-api/",           "docs/static/typescript-api/testnet/"),
]


def _strip_doc_ext(path: str) -> str:
    """Strip a markdown extension if present. Used for rendered-docs URLs."""
    for ext in (".mdx", ".md"):
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def _strip_index_suffix(path: str) -> str:
    """Docusaurus serves ``foo/index`` as ``foo`` on the rendered site —
    drop the trailing ``/index`` segment so the URL doesn't 404."""
    if path == "index":
        return ""
    if path.endswith("/index"):
        return path[: -len("/index")]
    return path


def _aztec_source_url(source_path: str) -> str:
    """Translate a corpus `metadata.source` path to a clickable public URL.

    Unknown patterns fall back to the original string; the widget will
    still render the title — just without a working href.

    Routing summary:
      * Rendered Aztec developer docs under `docs/`  → docs.aztec.network/developers/docs/<rest>
      * Top-level developer docs (overview, etc.)     → docs.aztec.network/developers/<rest>
      * Network / operator docs                       → docs.aztec.network/operate/operators/<rest>
      * Unversioned site-root pages (networks.md)    → docs.aztec.network/<rest>
      * Noir language docs + stdlib                   → github.com/noir-lang/noir at pinned commit
      * Aztec source code (TS / Sol / Noir)           → github.com/AztecProtocol/aztec-packages at v4.2.0
    """
    if not source_path or not isinstance(source_path, str):
        return source_path

    # Network / operator docs — rendered at /operate/operators/<rest> on
    # the site. The corpus path is `version-v4.2.0/operators/<rest>` per
    # the way the network docs are ingested.
    #
    # ``apply_slug_override`` swaps the final path segment when the
    # source file declares ``id:`` in its Docusaurus frontmatter (e.g.
    # ``registering-sequencer.md`` declares ``id: registering_sequencer``
    # so the site serves it at ``.../registering_sequencer``, not the
    # filename slug).
    if source_path.startswith("version-v4.2.0/operators/"):
        source_no_ext = _strip_doc_ext(source_path)
        rest = source_path[len("version-v4.2.0/"):]  # keep "operators/<rest>"
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        rest = apply_slug_override(rest, source_no_ext)
        return f"{_AZTEC_OPERATE_BASE}/{rest}".rstrip("/")

    # Rendered Aztec developer docs — files under `docs/` subfolder.
    if source_path.startswith("version-v4.2.0/docs/"):
        source_no_ext = _strip_doc_ext(source_path)
        rest = source_path[len("version-v4.2.0/docs/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        rest = apply_slug_override(rest, source_no_ext)
        return f"{_AZTEC_DOCS_BASE}/{rest}".rstrip("/")

    # Top-level developer docs (overview, ai_tooling, getting_started_*)
    # — these live directly under `version-v4.2.0/<file>.md` in the
    # corpus and are rendered at /developers/<filename> on the site, NOT
    # under /developers/docs/.
    if source_path.startswith("version-v4.2.0/"):
        source_no_ext = _strip_doc_ext(source_path)
        rest = source_path[len("version-v4.2.0/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        rest = apply_slug_override(rest, source_no_ext)
        return f"{_AZTEC_DEV_TOP_BASE}/{rest}".rstrip("/")

    # Unversioned aztec-packages ``docs/docs/`` pages — rendered at
    # the site root (e.g. ``aztec-site/networks.md`` →
    # ``docs.aztec.network/networks``). Same Docusaurus URL scheme as
    # the developer docs above.
    if source_path.startswith("aztec-site/"):
        rest = source_path[len("aztec-site/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        return f"{_AZTEC_SITE_BASE}/{rest}".rstrip("/")

    # Noir language docs are rendered at noir-lang.org/docs. Strip the
    # markdown extension and any `/index` suffix to match the
    # Docusaurus URL scheme (same convention as docs.aztec.network).
    if source_path.startswith("noir-docs/"):
        rest = source_path[len("noir-docs/"):]
        rest = _strip_index_suffix(_strip_doc_ext(rest))
        return f"{_NOIR_DOCS_BASE}/{rest}".rstrip("/")

    # Noir-stdlib stays on GitHub: those are real `.nr` source files
    # (apiref output of `noir_stdlib/src/`), not rendered docs pages.
    if source_path.startswith("noir-stdlib/"):
        rest = source_path[len("noir-stdlib/"):]
        for suffix in (".txt", ".md"):
            if rest.endswith(suffix):
                rest = rest[: -len(suffix)]
                break
        return f"{_NOIR_GITHUB_BASE}/noir_stdlib/src/{rest}"

    # Code / non-developer-docs → GitHub blob at v4.2.0
    for corpus_prefix, repo_prefix in _SOURCE_TO_REPO_PREFIX:
        if source_path.startswith(corpus_prefix):
            rest = source_path[len(corpus_prefix):]
            # Ingest pipelines append a parser-friendly extension to code
            # files so the DocsGPT parser allowlist accepts them:
            #   - body-bearing corpora (e.g. noir-contracts) append ``.txt``:
            #     ``Token.nr`` → ``Token.nr.txt``
            #   - apiref corpora (aztec-nr, noir-stdlib) append ``.md``:
            #     ``hash.nr`` → ``hash.nr.md`` (markdown rendered)
            # In both cases the canonical file the user wants linked is
            # the original ``.nr`` (or ``.ts`` / ``.sol``).
            for suffix in (".txt", ".md"):
                if rest.endswith(suffix):
                    rest = rest[: -len(suffix)]
                    break
            return f"{_AZTEC_GITHUB_BASE}/{repo_prefix}{rest}"

    return source_path


def _iter_with_heartbeat(
    source_iter: Iterable[Any],
    interval: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> Generator[Tuple[str, Any], None, None]:
    """Wrap a blocking iterator, inserting a heartbeat sentinel on silence.

    A single producer thread consumes ``source_iter`` and pushes items into
    a queue. The consumer pulls with a timeout; if the source has been
    silent for ``interval`` seconds it yields ``("heartbeat", None)``.
    Real items are yielded as ``("item", value)``; errors from the
    producer are re-raised on the consumer.

    The producer thread must not touch Flask request context.
    """
    q: "queue.Queue[Tuple[str, Any]]" = queue.Queue(maxsize=64)

    def _producer() -> None:
        try:
            for item in source_iter:
                q.put(("item", item))
        except BaseException as exc:  # noqa: BLE001 — re-raised on consumer
            q.put(("error", exc))
        else:
            q.put(("done", None))

    t = threading.Thread(target=_producer, daemon=True, name="sse-producer")
    t.start()

    while True:
        try:
            kind, payload = q.get(timeout=interval)
        except queue.Empty:
            yield ("heartbeat", None)
            continue
        if kind == "item":
            yield ("item", payload)
        elif kind == "done":
            return
        elif kind == "error":
            raise payload  # type: ignore[misc]


class BaseAnswerResource:
    """Shared base class for answer endpoints"""

    def __init__(self):
        self.default_model_id = get_default_model_id()
        self.conversation_service = ConversationService()

    def validate_request(
        self, data: Dict[str, Any], require_conversation_id: bool = False
    ) -> Optional[Response]:
        """Common request validation.

        Continuation requests (``tool_actions`` present) require
        ``conversation_id`` but not ``question``.
        """
        if data.get("tool_actions"):
            # Continuation mode — question is not required
            if missing := check_required_fields(data, ["conversation_id"]):
                return missing
            return None
        required_fields = ["question"]
        if require_conversation_id:
            required_fields.append("conversation_id")
        if missing_fields := check_required_fields(data, required_fields):
            return missing_fields
        return None

    @staticmethod
    def _prepare_tool_calls_for_logging(
        tool_calls: Optional[List[Dict[str, Any]]], max_chars: int = 10000
    ) -> List[Dict[str, Any]]:
        if not tool_calls:
            return []

        prepared = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                prepared.append({"result": str(tool_call)[:max_chars]})
                continue

            item = dict(tool_call)
            for key in ("result", "result_full"):
                value = item.get(key)
                if isinstance(value, str) and len(value) > max_chars:
                    item[key] = value[:max_chars]
            prepared.append(item)
        return prepared

    def check_usage(self, agent_config: Dict) -> Optional[Response]:
        """Check if there is a usage limit and if it is exceeded

        Args:
            agent_config: The config dict of agent instance

        Returns:
            None or Response if either of limits exceeded.

        """
        api_key = agent_config.get("user_api_key")
        if not api_key:
            return None
        with db_readonly() as conn:
            agent = AgentsRepository(conn).find_by_key(api_key)

        if not agent:
            return make_response(
                jsonify({"success": False, "message": "Invalid API key."}), 401
            )
        limited_token_mode_raw = agent.get("limited_token_mode", False)
        limited_request_mode_raw = agent.get("limited_request_mode", False)

        limited_token_mode = (
            limited_token_mode_raw
            if isinstance(limited_token_mode_raw, bool)
            else limited_token_mode_raw == "True"
        )
        limited_request_mode = (
            limited_request_mode_raw
            if isinstance(limited_request_mode_raw, bool)
            else limited_request_mode_raw == "True"
        )

        token_limit = int(
            agent.get("token_limit") or settings.DEFAULT_AGENT_LIMITS["token_limit"]
        )
        request_limit = int(
            agent.get("request_limit") or settings.DEFAULT_AGENT_LIMITS["request_limit"]
        )

        end_date = datetime.datetime.now(datetime.timezone.utc)
        start_date = end_date - datetime.timedelta(hours=24)

        if limited_token_mode or limited_request_mode:
            with db_readonly() as conn:
                token_repo = TokenUsageRepository(conn)
                if limited_token_mode:
                    daily_token_usage = token_repo.sum_tokens_in_range(
                        start=start_date, end=end_date, api_key=api_key,
                    )
                else:
                    daily_token_usage = 0
                if limited_request_mode:
                    daily_request_usage = token_repo.count_in_range(
                        start=start_date, end=end_date, api_key=api_key,
                    )
                else:
                    daily_request_usage = 0
        else:
            daily_token_usage = 0
            daily_request_usage = 0
        if not limited_token_mode and not limited_request_mode:
            return None
        token_exceeded = (
            limited_token_mode and token_limit > 0 and daily_token_usage >= token_limit
        )
        request_exceeded = (
            limited_request_mode
            and request_limit > 0
            and daily_request_usage >= request_limit
        )

        if token_exceeded or request_exceeded:
            return make_response(
                jsonify(
                    {
                        "success": False,
                        "message": "Exceeding usage limit, please try again later.",
                    }
                ),
                429,
            )
        return None

    def complete_stream(
        self,
        question: str,
        agent: Any,
        conversation_id: Optional[str],
        user_api_key: Optional[str],
        decoded_token: Dict[str, Any],
        isNoneDoc: bool = False,
        index: Optional[int] = None,
        should_save_conversation: bool = True,
        attachment_ids: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
        is_shared_usage: bool = False,
        shared_token: Optional[str] = None,
        model_id: Optional[str] = None,
        _continuation: Optional[Dict] = None,
    ) -> Generator[str, None, None]:
        """
        Generator function that streams the complete conversation response.

        Args:
            question: The user's question
            agent: The agent instance
            retriever: The retriever instance
            conversation_id: Existing conversation ID
            user_api_key: User's API key if any
            decoded_token: Decoded JWT token
            isNoneDoc: Flag for document-less responses
            index: Index of message to update
            should_save_conversation: Whether to persist the conversation
            attachment_ids: List of attachment IDs
            agent_id: ID of agent used
            is_shared_usage: Flag for shared agent usage
            shared_token: Token for shared agent
            model_id: Model ID used for the request
            retrieved_docs: Pre-fetched documents for sources (optional)

        Yields:
            Server-sent event strings
        """
        try:
            response_full, thought, source_log_docs, tool_calls = "", "", [], []
            is_structured = False
            schema_info = None
            structured_chunks = []
            query_metadata = {}
            paused = False

            if _continuation:
                gen_iter = agent.gen_continuation(
                    messages=_continuation["messages"],
                    tools_dict=_continuation["tools_dict"],
                    pending_tool_calls=_continuation["pending_tool_calls"],
                    tool_actions=_continuation["tool_actions"],
                )
            else:
                gen_iter = agent.gen(query=question)

            for kind, line in _iter_with_heartbeat(gen_iter):
                if kind == "heartbeat":
                    # SSE comment line — clients discard it. Keeps the
                    # connection active across long silent gaps (tool
                    # calls, retrieval) so Cloudflare/Caddy don't close
                    # the stream and the frontend stays responsive.
                    yield ": ping\n\n"
                    continue
                if "metadata" in line:
                    query_metadata.update(line["metadata"])
                elif "answer" in line:
                    response_full += str(line["answer"])
                    if line.get("structured"):
                        is_structured = True
                        schema_info = line.get("schema")
                        structured_chunks.append(line["answer"])
                    else:
                        data = json.dumps({"type": "answer", "answer": line["answer"]})
                        yield f"data: {data}\n\n"
                elif "sources" in line:
                    truncated_sources = []
                    seen_urls: set = set()
                    source_log_docs = line["sources"]
                    for source in line["sources"]:
                        if len(truncated_sources) >= _MAX_SOURCES_EMITTED:
                            break
                        truncated_source = source.copy()
                        if "text" in truncated_source:
                            truncated_source["text"] = (
                                truncated_source["text"][:100].strip() + "..."
                            )
                        raw_path = truncated_source.get("source")
                        if raw_path:
                            public_url = _aztec_source_url(raw_path)
                            if public_url in seen_urls:
                                continue
                            seen_urls.add(public_url)
                            truncated_source["source"] = public_url
                        truncated_sources.append(truncated_source)
                    if truncated_sources:
                        data = json.dumps(
                            {"type": "source", "source": truncated_sources}
                        )
                        yield f"data: {data}\n\n"
                elif "tool_calls" in line:
                    tool_calls = line["tool_calls"]
                    data = json.dumps({"type": "tool_calls", "tool_calls": tool_calls})
                    yield f"data: {data}\n\n"
                elif "thought" in line:
                    thought += line["thought"]
                    data = json.dumps({"type": "thought", "thought": line["thought"]})
                    yield f"data: {data}\n\n"
                elif "type" in line:
                    if line.get("type") == "tool_calls_pending":
                        # Save continuation state and end the stream
                        paused = True
                        data = json.dumps(line)
                        yield f"data: {data}\n\n"
                    elif line.get("type") == "error":
                        sanitized_error = {
                            "type": "error",
                            "error": sanitize_api_error(line.get("error", "An error occurred"))
                        }
                        data = json.dumps(sanitized_error)
                        yield f"data: {data}\n\n"
                    else:
                        data = json.dumps(line)
                        yield f"data: {data}\n\n"
            if is_structured and structured_chunks:
                structured_data = {
                    "type": "structured_answer",
                    "answer": response_full,
                    "structured": True,
                    "schema": schema_info,
                }
                data = json.dumps(structured_data)
                yield f"data: {data}\n\n"

            # ---- Paused: save continuation state and end stream early ----
            if paused:
                continuation = getattr(agent, "_pending_continuation", None)
                if continuation:
                    # Ensure we have a conversation_id — create a partial
                    # conversation if this is the first turn.
                    if not conversation_id and should_save_conversation:
                        try:
                            provider = (
                                get_provider_from_model_id(model_id)
                                if model_id
                                else settings.LLM_PROVIDER
                            )
                            sys_api_key = get_api_key_for_provider(
                                provider or settings.LLM_PROVIDER
                            )
                            llm = LLMCreator.create_llm(
                                provider or settings.LLM_PROVIDER,
                                api_key=sys_api_key,
                                user_api_key=user_api_key,
                                decoded_token=decoded_token,
                                model_id=model_id,
                                agent_id=agent_id,
                            )
                            conversation_id = (
                                self.conversation_service.save_conversation(
                                    None,
                                    question,
                                    response_full,
                                    thought,
                                    source_log_docs,
                                    tool_calls,
                                    llm,
                                    model_id or self.default_model_id,
                                    decoded_token,
                                    api_key=user_api_key,
                                    agent_id=agent_id,
                                    is_shared_usage=is_shared_usage,
                                    shared_token=shared_token,
                                )
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to create conversation for continuation: {e}",
                                exc_info=True,
                            )

                    if conversation_id:
                        try:
                            cont_service = ContinuationService()
                            cont_service.save_state(
                                conversation_id=str(conversation_id),
                                user=decoded_token.get("sub", "local"),
                                messages=continuation["messages"],
                                pending_tool_calls=continuation["pending_tool_calls"],
                                tools_dict=continuation["tools_dict"],
                                tool_schemas=getattr(agent, "tools", []),
                                agent_config={
                                    "model_id": model_id or self.default_model_id,
                                    "llm_name": getattr(agent, "llm_name", settings.LLM_PROVIDER),
                                    "api_key": getattr(agent, "api_key", None),
                                    "user_api_key": user_api_key,
                                    "agent_id": agent_id,
                                    "agent_type": agent.__class__.__name__,
                                    "prompt": getattr(agent, "prompt", ""),
                                    "json_schema": getattr(agent, "json_schema", None),
                                    "retriever_config": getattr(agent, "retriever_config", None),
                                },
                                client_tools=getattr(
                                    agent.tool_executor, "client_tools", None
                                ),
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to save continuation state: {str(e)}",
                                exc_info=True,
                            )

                id_data = {"type": "id", "id": str(conversation_id)}
                data = json.dumps(id_data)
                yield f"data: {data}\n\n"

                data = json.dumps({"type": "end"})
                yield f"data: {data}\n\n"
                return

            if isNoneDoc:
                for doc in source_log_docs:
                    doc["source"] = "None"
            provider = (
                get_provider_from_model_id(model_id)
                if model_id
                else settings.LLM_PROVIDER
            )
            system_api_key = get_api_key_for_provider(provider or settings.LLM_PROVIDER)

            llm = LLMCreator.create_llm(
                provider or settings.LLM_PROVIDER,
                api_key=system_api_key,
                user_api_key=user_api_key,
                decoded_token=decoded_token,
                model_id=model_id,
                agent_id=agent_id,
            )

            if should_save_conversation:
                conversation_id = self.conversation_service.save_conversation(
                    conversation_id,
                    question,
                    response_full,
                    thought,
                    source_log_docs,
                    tool_calls,
                    llm,
                    model_id or self.default_model_id,
                    decoded_token,
                    index=index,
                    api_key=user_api_key,
                    agent_id=agent_id,
                    is_shared_usage=is_shared_usage,
                    shared_token=shared_token,
                    attachment_ids=attachment_ids,
                    metadata=query_metadata if query_metadata else None,
                )
                # Persist compression metadata/summary if it exists and wasn't saved mid-execution
                compression_meta = getattr(agent, "compression_metadata", None)
                compression_saved = getattr(agent, "compression_saved", False)
                if conversation_id and compression_meta and not compression_saved:
                    try:
                        self.conversation_service.update_compression_metadata(
                            conversation_id, compression_meta
                        )
                        self.conversation_service.append_compression_message(
                            conversation_id, compression_meta
                        )
                        agent.compression_saved = True
                        logger.info(
                            f"Persisted compression metadata for conversation {conversation_id}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to persist compression metadata: {str(e)}",
                            exc_info=True,
                        )
            else:
                conversation_id = None
            id_data = {"type": "id", "id": str(conversation_id)}
            data = json.dumps(id_data)
            yield f"data: {data}\n\n"

            tool_calls_for_logging = self._prepare_tool_calls_for_logging(
                getattr(agent, "tool_calls", tool_calls) or tool_calls
            )

            log_data = {
                "action": "stream_answer",
                "level": "info",
                "user": decoded_token.get("sub"),
                "api_key": user_api_key,
                "agent_id": agent_id,
                "question": question,
                "response": response_full,
                "sources": source_log_docs,
                "tool_calls": tool_calls_for_logging,
                "attachments": attachment_ids,
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
            }
            if is_structured:
                log_data["structured_output"] = True
                if schema_info:
                    log_data["schema"] = schema_info
            # Clean up text fields to be no longer than 10000 characters

            for key, value in log_data.items():
                if isinstance(value, str) and len(value) > 10000:
                    log_data[key] = value[:10000]
            try:
                with db_session() as conn:
                    UserLogsRepository(conn).insert(
                        user_id=log_data.get("user"),
                        endpoint="stream_answer",
                        data=log_data,
                    )
            except Exception as log_err:
                logger.error(
                    f"Failed to persist stream_answer user log: {log_err}",
                    exc_info=True,
                )

            data = json.dumps({"type": "end"})
            yield f"data: {data}\n\n"
        except GeneratorExit:
            logger.info(f"Stream aborted by client for question: {question[:50]}... ")
            # Save partial response

            if should_save_conversation and response_full:
                try:
                    if isNoneDoc:
                        for doc in source_log_docs:
                            doc["source"] = "None"
                    llm = LLMCreator.create_llm(
                        settings.LLM_PROVIDER,
                        api_key=settings.API_KEY,
                        user_api_key=user_api_key,
                        decoded_token=decoded_token,
                        agent_id=agent_id,
                    )
                    self.conversation_service.save_conversation(
                        conversation_id,
                        question,
                        response_full,
                        thought,
                        source_log_docs,
                        tool_calls,
                        llm,
                        model_id or self.default_model_id,
                        decoded_token,
                        index=index,
                        api_key=user_api_key,
                        agent_id=agent_id,
                        is_shared_usage=is_shared_usage,
                        shared_token=shared_token,
                        attachment_ids=attachment_ids,
                        metadata=query_metadata if query_metadata else None,
                    )
                    compression_meta = getattr(agent, "compression_metadata", None)
                    compression_saved = getattr(agent, "compression_saved", False)
                    if conversation_id and compression_meta and not compression_saved:
                        try:
                            self.conversation_service.update_compression_metadata(
                                conversation_id, compression_meta
                            )
                            self.conversation_service.append_compression_message(
                                conversation_id, compression_meta
                            )
                            agent.compression_saved = True
                            logger.info(
                                f"Persisted compression metadata for conversation {conversation_id} (partial stream)"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to persist compression metadata (partial stream): {str(e)}",
                                exc_info=True,
                            )
                except Exception as e:
                    logger.error(
                        f"Error saving partial response: {str(e)}", exc_info=True
                    )
            raise
        except Exception as e:
            logger.error(f"Error in stream: {str(e)}", exc_info=True)
            data = json.dumps(
                {
                    "type": "error",
                    "error": "Please try again later. We apologize for any inconvenience.",
                }
            )
            yield f"data: {data}\n\n"
            return

    def process_response_stream(self, stream) -> Dict[str, Any]:
        """Process the stream response for non-streaming endpoint.

        Returns:
            Dict with keys: conversation_id, answer, sources, tool_calls,
            thought, error, and optional extra.
        """
        conversation_id = ""
        response_full = ""
        source_log_docs = []
        tool_calls = []
        thought = ""
        stream_ended = False
        is_structured = False
        schema_info = None
        pending_tool_calls = None

        for line in stream:
            try:
                event_data = line.replace("data: ", "").strip()
                event = json.loads(event_data)

                if event["type"] == "id":
                    conversation_id = event["id"]
                elif event["type"] == "answer":
                    response_full += event["answer"]
                elif event["type"] == "structured_answer":
                    response_full = event["answer"]
                    is_structured = True
                    schema_info = event.get("schema")
                elif event["type"] == "source":
                    source_log_docs = event["source"]
                elif event["type"] == "tool_calls":
                    tool_calls = event["tool_calls"]
                elif event["type"] == "tool_calls_pending":
                    pending_tool_calls = event.get("data", {}).get(
                        "pending_tool_calls", []
                    )
                elif event["type"] == "thought":
                    thought = event["thought"]
                elif event["type"] == "error":
                    logger.error(f"Error from stream: {event['error']}")
                    return {
                        "conversation_id": None,
                        "answer": None,
                        "sources": None,
                        "tool_calls": None,
                        "thought": None,
                        "error": event["error"],
                    }
                elif event["type"] == "end":
                    stream_ended = True
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Error parsing stream event: {e}, line: {line}")
                continue
        if not stream_ended:
            logger.error("Stream ended unexpectedly without an 'end' event.")
            return {
                "conversation_id": None,
                "answer": None,
                "sources": None,
                "tool_calls": None,
                "thought": None,
                "error": "Stream ended unexpectedly",
            }

        result: Dict[str, Any] = {
            "conversation_id": conversation_id,
            "answer": response_full,
            "sources": source_log_docs,
            "tool_calls": tool_calls,
            "thought": thought,
            "error": None,
        }

        if pending_tool_calls is not None:
            result["extra"] = {"pending_tool_calls": pending_tool_calls}

        if is_structured:
            result["extra"] = {"structured": True, "schema": schema_info}

        return result

    def error_stream_generate(self, err_response):
        data = json.dumps({"type": "error", "error": err_response})
        yield f"data: {data}\n\n"
