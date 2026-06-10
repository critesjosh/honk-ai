from application.core.settings import settings
from application.llm.openai import OpenAILLM

OPEN_ROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# Model-id prefixes whose default reasoning behavior wastes a large
# fraction of the stream on chain-of-thought tokens (and in some cases
# 100% of it). Extend this tuple when you encounter another one.
_REASONING_DISABLED_MODEL_PREFIXES = (
    "x-ai/grok-4.1-fast",
    "qwen/qwen3.6-flash",
)


def _should_disable_reasoning(model_id: str) -> bool:
    if not isinstance(model_id, str):
        return False
    return any(
        model_id.startswith(prefix)
        for prefix in _REASONING_DISABLED_MODEL_PREFIXES
    )


def _reasoning_enabled_agent_ids() -> set:
    """Agent UUIDs opted IN to reasoning (overrides the model-prefix disable).

    Experimental, env-driven via ``REASONING_ENABLED_AGENT_IDS`` (CSV). Lets
    us turn reasoning on for ONE surface (e.g. the Slack chat agent) without
    affecting the other agents that share the same model. Read live from
    settings so a value change only needs a process restart, not a code edit.
    """
    raw = settings.REASONING_ENABLED_AGENT_IDS or ""
    return {a.strip() for a in raw.split(",") if a.strip()}


def _apply_reasoning_policy(agent_id, model, kwargs, tools=None):
    """Mutate ``kwargs`` in place to set OpenRouter's ``reasoning`` field.

    Precedence:
      1. Agent opted in via ``REASONING_ENABLED_AGENT_IDS`` AND the call
         offers tools → ENABLE reasoning with a ``max_tokens`` budget
         (``settings.REASONING_MAX_TOKENS``). This wins even for models in
         ``_REASONING_DISABLED_MODEL_PREFIXES``.
      2. Else, model in ``_REASONING_DISABLED_MODEL_PREFIXES`` → disable
         reasoning (``enabled: false``) so the model spends the stream on
         answer tokens, not chain-of-thought.
      3. Else, leave the request untouched (provider default).

    The tools gate exists because the opt-in's purpose is better tool-call
    ROUTING; whether the model will actually call a tool is unknowable
    before generation, so "tools offered" is the closest request-time
    proxy. No-tool utility calls made under the same agent_id (query
    rephrase in ``ClassicRAG``, history compression) fall through to the
    model-prefix disable instead of burning the reasoning budget.

    Per OpenRouter's reasoning spec, ``enabled`` / ``effort`` / ``max_tokens``
    are mutually exclusive — passing ``max_tokens`` alone implies enabled, and
    ``exclude: true`` is a separate knob (still runs reasoning, merely hides
    it) we intentionally do NOT set. The OpenAI Python SDK forwards
    ``extra_body`` into the JSON request body verbatim, so OpenRouter sees
    ``reasoning`` as a top-level field.
    """
    if tools and agent_id and agent_id in _reasoning_enabled_agent_ids():
        eb = kwargs.pop("extra_body", None) or {}
        # Authoritative: this policy is the single source of the reasoning
        # field, so overwrite rather than setdefault.
        eb["reasoning"] = {"max_tokens": settings.REASONING_MAX_TOKENS}
        kwargs["extra_body"] = eb
        return
    if not _should_disable_reasoning(model):
        return
    eb = kwargs.pop("extra_body", None) or {}
    eb["reasoning"] = {"enabled": False}
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

    # ``tools`` is named explicitly (mirroring OpenAILLM's signatures) so the
    # policy can gate on it — the usage decorators pass it positionally.

    def _raw_gen(self, baseself, model, messages, stream=False, tools=None, *args, **kwargs):
        _apply_reasoning_policy(getattr(self, "agent_id", None), model, kwargs, tools=tools)
        return super()._raw_gen(baseself, model, messages, stream, tools, *args, **kwargs)

    def _raw_gen_stream(self, baseself, model, messages, stream=True, tools=None, *args, **kwargs):
        _apply_reasoning_policy(getattr(self, "agent_id", None), model, kwargs, tools=tools)
        return super()._raw_gen_stream(baseself, model, messages, stream, tools, *args, **kwargs)
