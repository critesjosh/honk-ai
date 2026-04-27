"""Run an agent end-to-end given a config dict and an input string.

The webhook worker uses this to execute an agent on incoming payloads.
Kept in its own module so the agent-execution surface (retriever
config, model selection, prompt rendering, agent gen) is separable
from the ingest/sync surface.
"""

from __future__ import annotations

import logging

from application.agents.agent_creator import AgentCreator
from application.api.answer.services.stream_processor import get_prompt
from application.core.settings import settings
from application.retriever.retriever_creator import RetrieverCreator
from application.storage.db.repositories.sources import SourcesRepository
from application.storage.db.session import db_readonly


def run_agent_logic(agent_config, input_data):
    try:
        from application.core.model_utils import (
            get_api_key_for_provider,
            get_default_model_id,
            get_provider_from_model_id,
            validate_model_id,
        )
        from application.utils import calculate_doc_token_budget

        retriever = agent_config.get("retriever", "classic")
        # agent_config is a PG row dict: ``source_id`` is a UUID, and the
        # retriever/chunks live on the source row. Resolve source row for
        # its retriever/chunks if the agent points at one.
        source_id = agent_config.get("source_id") or agent_config.get("source")
        source_active = {}
        if source_id:
            with db_readonly() as conn:
                src_row = SourcesRepository(conn).get(
                    str(source_id),
                    agent_config.get("user_id") or agent_config.get("user"),
                )
            if src_row:
                source_active = str(src_row["id"])
                retriever = src_row.get("retriever", retriever)
        source = {"active_docs": source_active}
        chunks = int(agent_config.get("chunks", 2) or 2)
        prompt_id = agent_config.get("prompt_id", "default")
        user_api_key = agent_config["key"]
        agent_id = (
            str(agent_config.get("id"))
            if agent_config.get("id")
            else (str(agent_config.get("_id")) if agent_config.get("_id") else None)
        )
        agent_type = agent_config.get("agent_type", "classic")
        owner = agent_config.get("user_id") or agent_config.get("user")
        decoded_token = {"sub": owner}
        json_schema = agent_config.get("json_schema")
        prompt = get_prompt(prompt_id)

        # Determine model_id: check agent's default_model_id, fallback to system default
        agent_default_model = agent_config.get("default_model_id", "")
        if agent_default_model and validate_model_id(agent_default_model):
            model_id = agent_default_model
        else:
            model_id = get_default_model_id()

        # Get provider and API key for the selected model
        provider = (
            get_provider_from_model_id(model_id)
            if model_id else settings.LLM_PROVIDER
        )
        system_api_key = get_api_key_for_provider(provider or settings.LLM_PROVIDER)

        # Calculate proper doc_token_limit based on model's context window
        doc_token_limit = calculate_doc_token_budget(model_id=model_id)

        retriever = RetrieverCreator.create_retriever(
            retriever,
            source=source,
            chat_history=[],
            prompt=prompt,
            chunks=chunks,
            doc_token_limit=doc_token_limit,
            model_id=model_id,
            user_api_key=user_api_key,
            agent_id=agent_id,
            decoded_token=decoded_token,
        )

        # Pre-fetch documents using the retriever
        retrieved_docs = []
        try:
            docs = retriever.search(input_data)
            if docs:
                retrieved_docs = docs
        except Exception as e:
            logging.warning(f"Failed to retrieve documents: {e}")

        agent = AgentCreator.create_agent(
            agent_type,
            endpoint="webhook",
            llm_name=provider or settings.LLM_PROVIDER,
            model_id=model_id,
            api_key=system_api_key,
            agent_id=agent_id,
            user_api_key=user_api_key,
            prompt=prompt,
            chat_history=[],
            retrieved_docs=retrieved_docs,
            decoded_token=decoded_token,
            attachments=[],
            json_schema=json_schema,
        )
        answer = agent.gen(query=input_data)
        response_full = ""
        thought = ""
        source_log_docs = []
        tool_calls = []

        for line in answer:
            if "answer" in line:
                response_full += str(line["answer"])
            elif "sources" in line:
                source_log_docs.extend(line["sources"])
            elif "tool_calls" in line:
                tool_calls.extend(line["tool_calls"])
            elif "thought" in line:
                thought += line["thought"]
        result = {
            "answer": response_full,
            "sources": source_log_docs,
            "tool_calls": tool_calls,
            "thought": thought,
        }
        logging.info(f"Agent response: {result}")
        return result
    except Exception as e:
        logging.error(f"Error in run_agent_logic: {e}", exc_info=True)
        raise
