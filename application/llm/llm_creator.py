"""LLM provider registry.

Aztec fork ships with a deliberately tiny provider list. Production
runs ``LLM_PROVIDER=openrouter`` (Grok / GLM via OpenRouter); the
``openai`` family stays because the OpenRouter provider class extends
``OpenAILLM`` for the OpenAI-compatible API surface, and ``OPENAI_API_KEY``
is also used directly for embeddings (text-embedding-3-large).

If you need another provider, re-add the import + registry entry here
and the matching dependency in ``application/requirements.txt``. The
upstream provider files (anthropic, google_ai, groq, llama_cpp,
novita, premai, sagemaker, docsgpt) were removed when the admin SPA
came out — their registry entries forced eager imports of heavy SDKs
even when ``LLM_PROVIDER=openrouter``.
"""

import logging

from application.llm.openai import AzureOpenAILLM, OpenAILLM
from application.llm.open_router import OpenRouterLLM

logger = logging.getLogger(__name__)


class LLMCreator:
    llms = {
        "openai": OpenAILLM,
        "azure_openai": AzureOpenAILLM,
        "openrouter": OpenRouterLLM,
    }

    @classmethod
    def create_llm(
        cls,
        type,
        api_key,
        user_api_key,
        decoded_token,
        model_id=None,
        agent_id=None,
        backup_models=None,
        *args,
        **kwargs,
    ):
        from application.core.model_utils import get_base_url_for_model

        llm_class = cls.llms.get(type.lower())
        if not llm_class:
            raise ValueError(f"No LLM class found for type {type}")

        base_url = None
        if model_id:
            base_url = get_base_url_for_model(model_id)

        return llm_class(
            api_key,
            user_api_key,
            decoded_token=decoded_token,
            model_id=model_id,
            agent_id=agent_id,
            base_url=base_url,
            backup_models=backup_models,
            *args,
            **kwargs,
        )
