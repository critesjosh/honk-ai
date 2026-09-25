import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ModelProvider(str, Enum):
    """Providers the registry may expose.

    Deliberately mirrors ``LLMCreator.llms`` (application/llm/llm_creator.py).
    Registering a model whose provider has no LLM class turns into a
    request-time 500 the first time config drifts, so the enum is kept in
    lock-step with the providers this fork can actually build.
    """

    OPENAI = "openai"
    OPENROUTER = "openrouter"
    AZURE_OPENAI = "azure_openai"


@dataclass
class ModelCapabilities:
    supports_tools: bool = False
    supports_structured_output: bool = False
    supports_streaming: bool = True
    supported_attachment_types: List[str] = field(default_factory=list)
    context_window: int = 128000
    input_cost_per_token: Optional[float] = None
    output_cost_per_token: Optional[float] = None


@dataclass
class AvailableModel:
    id: str
    provider: ModelProvider
    display_name: str
    description: str = ""
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    enabled: bool = True
    base_url: Optional[str] = None

    def to_dict(self) -> Dict:
        result = {
            "id": self.id,
            "provider": self.provider.value,
            "display_name": self.display_name,
            "description": self.description,
            "supported_attachment_types": self.capabilities.supported_attachment_types,
            "supports_tools": self.capabilities.supports_tools,
            "supports_structured_output": self.capabilities.supports_structured_output,
            "supports_streaming": self.capabilities.supports_streaming,
            "context_window": self.capabilities.context_window,
            "enabled": self.enabled,
        }
        if self.base_url:
            result["base_url"] = self.base_url
        return result


class ModelRegistry:
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not ModelRegistry._initialized:
            self.models: Dict[str, AvailableModel] = {}
            self.default_model_id: Optional[str] = None
            self._load_models()
            ModelRegistry._initialized = True

    @classmethod
    def get_instance(cls) -> "ModelRegistry":
        return cls()

    def _load_models(self):
        from application.core.settings import settings

        self.models.clear()

        # Only providers LLMCreator can build (openai / azure_openai /
        # openrouter) are ever registered, so every model the default
        # selection below can land on is creatable at request time.
        if (
            settings.OPENAI_API_KEY
            or (settings.LLM_PROVIDER == "openai" and settings.API_KEY)
            or settings.OPENAI_BASE_URL
        ):
            self._add_openai_models(settings)
        if settings.OPENAI_API_BASE or (
            settings.LLM_PROVIDER == "azure_openai" and settings.API_KEY
        ):
            self._add_azure_openai_models(settings)
        if settings.OPEN_ROUTER_API_KEY or (
            settings.LLM_PROVIDER == "openrouter" and settings.API_KEY
        ):
            self._add_openrouter_models(settings)
        # Default model selection
        if settings.LLM_NAME:
            # Parse LLM_NAME (may be comma-separated)
            model_names = self._parse_model_names(settings.LLM_NAME)
            # First model in the list becomes default
            for model_name in model_names:
                if model_name in self.models:
                    self.default_model_id = model_name
                    break
            # Backward compat: try exact match if no parsed model found
            if not self.default_model_id and settings.LLM_NAME in self.models:
                self.default_model_id = settings.LLM_NAME

        if not self.default_model_id:
            if settings.LLM_PROVIDER and settings.API_KEY:
                for model_id, model in self.models.items():
                    if model.provider.value == settings.LLM_PROVIDER:
                        self.default_model_id = model_id
                        break

        if not self.default_model_id and self.models:
            self.default_model_id = next(iter(self.models.keys()))
        logger.info(
            f"ModelRegistry loaded {len(self.models)} models, default: {self.default_model_id}"
        )

    def _add_openai_models(self, settings):
        from application.core.model_configs import (
            OPENAI_MODELS,
            create_custom_openai_model,
        )

        # Check if using local OpenAI-compatible endpoint (Ollama, LM Studio, etc.)
        using_local_endpoint = bool(
            settings.OPENAI_BASE_URL and settings.OPENAI_BASE_URL.strip()
        )

        if using_local_endpoint:
            # When OPENAI_BASE_URL is set, ONLY register custom models from LLM_NAME
            # Do NOT add standard OpenAI models (gpt-5.1, etc.)
            if settings.LLM_NAME:
                model_names = self._parse_model_names(settings.LLM_NAME)
                for model_name in model_names:
                    custom_model = create_custom_openai_model(
                        model_name, settings.OPENAI_BASE_URL
                    )
                    self.models[model_name] = custom_model
                    logger.info(
                        f"Registered custom OpenAI model: {model_name} at {settings.OPENAI_BASE_URL}"
                    )
        else:
            # Standard OpenAI API usage - add standard models if API key is valid
            if settings.OPENAI_API_KEY:
                for model in OPENAI_MODELS:
                    self.models[model.id] = model

    def _add_azure_openai_models(self, settings):
        from application.core.model_configs import AZURE_OPENAI_MODELS

        if settings.LLM_PROVIDER == "azure_openai" and settings.LLM_NAME:
            for model in AZURE_OPENAI_MODELS:
                if model.id == settings.LLM_NAME:
                    self.models[model.id] = model
                    return
        for model in AZURE_OPENAI_MODELS:
            self.models[model.id] = model

    def _add_openrouter_models(self, settings):
        from application.core.model_configs import OPENROUTER_MODELS

        if settings.OPEN_ROUTER_API_KEY:
            for model in OPENROUTER_MODELS:
                self.models[model.id] = model
            return
        if settings.LLM_PROVIDER == "openrouter" and settings.LLM_NAME:
            for model in OPENROUTER_MODELS:
                if model.id == settings.LLM_NAME:
                    self.models[model.id] = model
                    return
        for model in OPENROUTER_MODELS:
            self.models[model.id] = model

    def _parse_model_names(self, llm_name: str) -> List[str]:
        """
        Parse LLM_NAME which may contain comma-separated model names.
        E.g., 'deepseek-r1:1.5b,gemma:2b' -> ['deepseek-r1:1.5b', 'gemma:2b']
        """
        if not llm_name:
            return []
        return [name.strip() for name in llm_name.split(",") if name.strip()]

    def get_model(self, model_id: str) -> Optional[AvailableModel]:
        return self.models.get(model_id)

    def get_all_models(self) -> List[AvailableModel]:
        return list(self.models.values())

    def get_enabled_models(self) -> List[AvailableModel]:
        return [m for m in self.models.values() if m.enabled]

    def model_exists(self, model_id: str) -> bool:
        return model_id in self.models
