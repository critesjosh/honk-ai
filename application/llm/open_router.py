from application.core.settings import settings
from application.llm.openai import OpenAILLM

OPEN_ROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# Model-id prefixes whose default reasoning behavior makes them unusable
# for low-latency streaming — they emit 100% chain-of-thought tokens and
# zero answer content. Extend this tuple when you encounter another one.
_REASONING_DISABLED_MODEL_PREFIXES = (
    "x-ai/grok-4.1-fast",
)


def _should_disable_reasoning(model_id: str) -> bool:
    if not isinstance(model_id, str):
        return False
    return any(
        model_id.startswith(prefix)
        for prefix in _REASONING_DISABLED_MODEL_PREFIXES
    )


def _inject_no_reasoning(model, kwargs):
    """Mutate kwargs in place to suppress reasoning for flagged models.

    ``reasoning.enabled: false`` disables the feature entirely so the model
    produces answer tokens. ``reasoning.exclude: true`` belt-and-braces
    hides any reasoning that still runs. Sent together because provider
    compatibility at OpenRouter varies. The OpenAI Python SDK forwards
    ``extra_body`` into the JSON request body verbatim, so OpenRouter sees
    these as top-level request fields.
    """
    if not _should_disable_reasoning(model):
        return
    eb = kwargs.pop("extra_body", None) or {}
    eb.setdefault("reasoning", {"enabled": False, "exclude": True})
    kwargs["extra_body"] = eb


class OpenRouterLLM(OpenAILLM):
    """OpenRouter speaks the OpenAI chat.completions API with extensions.

    We reuse OpenAILLM's transport and override only the bits that differ:
    the base URL and any OpenRouter-specific request-body fields (like
    ``reasoning``).
    """

    def __init__(self, api_key=None, user_api_key=None, base_url=None, *args, **kwargs):
        super().__init__(
            api_key=api_key or settings.OPEN_ROUTER_API_KEY or settings.API_KEY,
            user_api_key=user_api_key,
            base_url=base_url or OPEN_ROUTER_BASE_URL,
            *args,
            **kwargs,
        )

    def _raw_gen(self, baseself, model, messages, stream=False, *args, **kwargs):
        _inject_no_reasoning(model, kwargs)
        return super()._raw_gen(baseself, model, messages, stream, *args, **kwargs)

    def _raw_gen_stream(self, baseself, model, messages, stream=True, *args, **kwargs):
        _inject_no_reasoning(model, kwargs)
        return super()._raw_gen_stream(baseself, model, messages, stream, *args, **kwargs)
