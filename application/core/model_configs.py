"""
Model configurations for all supported LLM providers.
"""

from application.core.model_settings import (
    AvailableModel,
    ModelCapabilities,
    ModelProvider,
)

# Base image attachment types supported by most vision-capable LLMs
IMAGE_ATTACHMENTS = [
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
]

# PDF excluded: most OpenAI-compatible endpoints don't support native PDF uploads.
# When excluded, PDFs are synthetically processed by converting pages to images.
OPENAI_ATTACHMENTS = IMAGE_ATTACHMENTS

OPENROUTER_ATTACHMENTS = IMAGE_ATTACHMENTS


OPENAI_MODELS = [
    AvailableModel(
        id="gpt-5.1",
        provider=ModelProvider.OPENAI,
        display_name="GPT-5.1",
        description="Flagship model with enhanced reasoning, coding, and agentic capabilities",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            supported_attachment_types=OPENAI_ATTACHMENTS,
            context_window=200000,
        ),
    ),
    AvailableModel(
        id="gpt-5-mini",
        provider=ModelProvider.OPENAI,
        display_name="GPT-5 Mini",
        description="Faster, cost-effective variant of GPT-5.1",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            supported_attachment_types=OPENAI_ATTACHMENTS,
            context_window=200000,
        ),
    )
]


OPENROUTER_MODELS = [
    AvailableModel(
        id="qwen/qwen3.6-flash",
        provider=ModelProvider.OPENROUTER,
        display_name="Qwen 3.6 Flash",
        description="Default Aztec model (LLM_NAME). Used by the Aztec 4.3.0 (Discord) and Ask Aztec — public web (/ask) agents. Alibaba Qwen 3.6 Flash — 1M context, fast, ~$0.25/$1.50 per 1M.",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=1000000,
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="qwen/qwen3.5-397b-a17b",
        provider=ModelProvider.OPENROUTER,
        display_name="Qwen 3.5 397B",
        description="Large MoE model with strong coding and reasoning capabilities",
        capabilities=ModelCapabilities(
            supports_tools=True,
            context_window=131072,
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="anthropic/claude-sonnet-4.6",
        provider=ModelProvider.OPENROUTER,
        display_name="Claude Sonnet 4.6",
        description="Balanced performance and capability from Anthropic",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=200000,
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="anthropic/claude-opus-4.6",
        provider=ModelProvider.OPENROUTER,
        display_name="Claude Opus 4.6",
        description="Most capable Claude model from Anthropic",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=200000,
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="moonshotai/kimi-k2.5",
        provider=ModelProvider.OPENROUTER,
        display_name="Kimi K2.5",
        description="MoE model with function calling, structured output, and reasoning",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=262144,
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="openai/gpt-5.3-codex",
        provider=ModelProvider.OPENROUTER,
        display_name="GPT-5.3 Codex",
        description="OpenAI code-optimized model with strong agentic capabilities",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=200000,
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="qwen/qwen3-coder:free",
        provider=ModelProvider.OPENROUTER,
        display_name="Qwen 3 Coder",
        description="Latest Qwen model with high-speed inference",
        capabilities=ModelCapabilities(
            supports_tools=True,
            context_window=128000,
            supported_attachment_types=OPENROUTER_ATTACHMENTS
        ),
    ),
    AvailableModel(
        id="google/gemma-3-27b-it:free",
        provider=ModelProvider.OPENROUTER,
        display_name="Gemma 3 27B",
        description="Latest Gemma model with high-speed inference",
        capabilities=ModelCapabilities(
            supports_tools=True,
            context_window=128000,
            supported_attachment_types=OPENROUTER_ATTACHMENTS
        ),
    ),
    AvailableModel(
        id="google/gemini-3-flash-preview",
        provider=ModelProvider.OPENROUTER,
        display_name="Gemini 3 Flash Preview",
        description="Fast, cost-efficient Gemini 3 model with 1M token context, via OpenRouter.",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=int(1e6),
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="qwen/qwen3.5-flash-02-23",
        provider=ModelProvider.OPENROUTER,
        display_name="Qwen 3.5 Flash",
        description="Qwen 3.5 Flash — fast, cost-efficient, strong at instruction-following. Via OpenRouter.",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=131072,
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="x-ai/grok-4.1-fast",
        provider=ModelProvider.OPENROUTER,
        display_name="Grok 4.1 Fast",
        description="xAI Grok 4.1 Fast — reasoning disabled for low-latency streaming. Via OpenRouter.",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=int(2e6),
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
    AvailableModel(
        id="z-ai/glm-4.6",
        provider=ModelProvider.OPENROUTER,
        display_name="GLM 4.6",
        description="Z.ai GLM-4.6 via OpenRouter. Registered to keep the per-agent backup_models fallback chain resolvable when agents list it as a backup.",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            context_window=200000,
            supported_attachment_types=OPENROUTER_ATTACHMENTS,
        ),
    ),
]


AZURE_OPENAI_MODELS = [
    AvailableModel(
        id="azure-gpt-4",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Azure OpenAI GPT-4",
        description="Azure-hosted GPT model",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            supported_attachment_types=OPENAI_ATTACHMENTS,
            context_window=8192,
        ),
    ),
]


def create_custom_openai_model(model_name: str, base_url: str) -> AvailableModel:
    """Create a custom OpenAI-compatible model (e.g., LM Studio, Ollama)."""
    return AvailableModel(
        id=model_name,
        provider=ModelProvider.OPENAI,
        display_name=model_name,
        description=f"Custom OpenAI-compatible model at {base_url}",
        base_url=base_url,
        capabilities=ModelCapabilities(
            supports_tools=True,
            supported_attachment_types=OPENAI_ATTACHMENTS,
        ),
    )
