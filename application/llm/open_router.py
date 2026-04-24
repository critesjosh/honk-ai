from application.core.settings import settings
from application.llm.openai import OpenAILLM

OPEN_ROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# Model-id prefixes whose default reasoning behavior wastes a large
# fraction of the stream on chain-of-thought tokens (and in some cases
# 100% of it). Extend this tuple when you encounter another one.
_REASONING_DISABLED_MODEL_PREFIXES = (
    "x-ai/grok-4.1-fast",
    "z-ai/glm-4.6",
)


def _should_disable_reasoning(model_id: str) -> bool:
    if not isinstance(model_id, str):
        return False
    return any(
        model_id.startswith(prefix)
        for prefix in _REASONING_DISABLED_MODEL_PREFIXES
    )


def _inject_no_reasoning(model, kwargs):
    """Mutate kwargs in place to disable reasoning for flagged models.

    Per OpenRouter's reasoning spec, ``enabled``, ``effort``, and
    ``max_tokens`` are mutually exclusive — pick ONE. We use
    ``enabled: false`` to turn the feature off entirely so the model
    emits normal answer tokens instead of chain-of-thought.

    (``exclude: true`` is a separate knob that still *runs* reasoning
    and merely hides it; setting it here would be redundant and, worse,
    would conflict with ``enabled`` on some providers.)

    The OpenAI Python SDK forwards ``extra_body`` into the JSON request
    body verbatim, so OpenRouter sees ``reasoning`` as a top-level field.
    """
    if not _should_disable_reasoning(model):
        return
    eb = kwargs.pop("extra_body", None) or {}
    eb.setdefault("reasoning", {"enabled": False})
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
