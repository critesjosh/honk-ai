import os
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

current_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from application.core.db_uri import (  # noqa: E402
    normalize_pgvector_connection_string,
    normalize_postgres_uri,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    AUTH_TYPE: Optional[str] = None  # simple_jwt, session_jwt, or None
    # Default flipped from upstream's "docsgpt" to "openrouter" — the
    # docsgpt provider class was removed alongside the unused-component
    # sweep. Aztec prod sets ``LLM_PROVIDER=openrouter`` in ``.env``;
    # this default keeps boot working when env is unset (e.g. unit tests).
    LLM_PROVIDER: str = "openrouter"
    LLM_NAME: Optional[str] = None  # if LLM_PROVIDER is openai, LLM_NAME can be gpt-4 or gpt-3.5-turbo
    EMBEDDINGS_NAME: str = "huggingface_sentence-transformers/all-mpnet-base-v2"
    EMBEDDINGS_BASE_URL: Optional[str] = None  # Remote embeddings API URL (OpenAI-compatible)
    EMBEDDINGS_KEY: Optional[str] = None  # api key for embeddings (if using openai, just copy API_KEY)
    # Output dimensionality of the embedding model. Only consulted by the
    # pgvector backend when creating the documents table — it must match the
    # configured EMBEDDINGS_NAME or inserts will fail.
    #   mpnet-base-v2:            768 (default)
    #   text-embedding-3-small:  1536
    #   text-embedding-3-large:  3072
    EMBEDDINGS_DIMENSION: int = 768

    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    # Only consulted when VECTOR_STORE=mongodb or when running scripts/db/backfill.py; user data lives in Postgres.
    MONGO_URI: Optional[str] = None
    # User-data Postgres DB.
    POSTGRES_URI: Optional[str] = None
    # On app startup, apply pending Alembic migrations. Default ON for dev; disable in prod if you manage schema out-of-band.
    AUTO_MIGRATE: bool = True
    # On app startup, create the target Postgres database if it's missing (requires CREATEDB privilege). Dev-friendly default.
    AUTO_CREATE_DB: bool = True
    DEFAULT_LLM_TOKEN_LIMIT: int = 128000  # Fallback when model not found in registry
    RESERVED_TOKENS: dict = {
        "system_prompt": 500,
        "current_query": 500,
        "safety_buffer": 1000,
    }
    DEFAULT_AGENT_LIMITS: dict = {
        "token_limit": 50000,
        "request_limit": 500,
    }
    UPLOAD_FOLDER: str = "inputs"
    PARSE_PDF_AS_IMAGE: bool = False
    PARSE_IMAGE_REMOTE: bool = False
    VECTOR_STORE: str = "pgvector"  # pgvector is the only backend in this fork (see vector_creator.py)
    AGENT_NAME: str = "classic"
    FALLBACK_LLM_PROVIDER: Optional[str] = None  # provider for fallback llm
    FALLBACK_LLM_NAME: Optional[str] = None  # model name for fallback llm
    FALLBACK_LLM_API_KEY: Optional[str] = None  # api key for fallback llm

    # Experimental per-agent reasoning override. OpenRouter reasoning is
    # DISABLED by default for some models (see llm/open_router.py); for the
    # agent UUIDs listed here it is instead ENABLED with a token budget —
    # but only on calls that offer tools (the gate exists for tool-call
    # routing; no-tool utility calls keep the model default/disable).
    # CSV of agent UUIDs; empty = no override (every agent keeps the default).
    REASONING_ENABLED_AGENT_IDS: Optional[str] = None
    REASONING_MAX_TOKENS: int = 16000  # reasoning-token budget when enabled

    # OAuth redirect base for MCP server connections (mcp_tool.py).
    CONNECTOR_REDIRECT_BASE_URI: Optional[str] = (
        "http://127.0.0.1:7091/api/connectors/callback"
    )

    # LLM Cache
    CACHE_REDIS_URL: str = "redis://localhost:6379/2"

    API_URL: str = "http://localhost:7091"  # backend url for celery worker
    MCP_OAUTH_REDIRECT_URI: Optional[str] = None  # public callback URL for MCP OAuth
    INTERNAL_KEY: Optional[str] = None  # internal api key for worker-to-backend auth
    MCP_PROVISIONING_KEY: Optional[str] = None  # dedicated key for MCP key provisioning endpoint
    # HMAC pepper for pseudonymizing stored user identifiers (e.g.
    # Discord user IDs). Required at startup — fail-closed: an unset
    # or insufficient-entropy pepper would silently turn pseudonymization
    # into the identity function. ``openssl rand -hex 32`` is the canonical
    # recipe; the validator rejects non-hex inputs and inputs that decode
    # to fewer than 16 bytes.
    USER_ID_PEPPER: str = ""
    AZTEC_SOURCE_IDS: Optional[str] = None  # comma-separated Postgres source UUIDs for Aztec MCP agents
    # Version tag of the Aztec corpus currently indexed in the vector
    # store (e.g. "v4.3.0"). Surfaced via GET /api/version so MCP
    # clients can detect a version mismatch between their local
    # aztec-packages clone and the corpus the backend is answering
    # from. Empty/unset → endpoint returns the literal "unknown" and
    # the MCP client treats this as "skip the version gate" (it logs
    # debug only) so callers don't get permanently locked out before
    # the operator sets this.
    AZTEC_CORPUS_VERSION: Optional[str] = None
    # Literal-identifier guardrail (application/api/answer/routes/base.py):
    # verify long 0x hex literals (contract addresses, tx/block hashes, public
    # keys) in grounded answers against what the model was given.
    #   off     — disabled (legacy, byte-identical behaviour)
    #   audit   — detect + log/record metadata only; never alters bytes (default)
    #   enforce — also hold back partial literals mid-stream and correct
    #             single-nibble corruptions / scrub unverifiable literals
    # Default ``audit`` so the false-positive rate can be sized in prod before
    # flipping to ``enforce``. Unknown values fall back to ``audit``.
    LITERAL_GUARD_MODE: str = "audit"
    CORS_ALLOWED_ORIGINS: Optional[str] = None  # comma-separated origin URLs; empty = same-origin only; "*" = any (insecure)

    # Cap on tokens of retrieved documents injected into the LLM prompt.
    # Upstream uses the model's full context window; for 200k-context models
    # this stuffs huge contexts and makes generation slow. 6k keeps answers
    # grounded while letting Claude Sonnet respond in 10-15s instead of 60s.
    # Set to 0 to disable the cap.
    RAG_MAX_DOC_TOKENS: int = 6000

    API_KEY: Optional[str] = None  # LLM api key (used by LLM_PROVIDER)

    # Provider-specific API keys (for multi-model support)
    OPENAI_API_KEY: Optional[str] = None
    OPEN_ROUTER_API_KEY: Optional[str] = None

    OPENAI_API_BASE: Optional[str] = None  # azure openai api base url
    OPENAI_API_VERSION: Optional[str] = None  # azure openai api version
    AZURE_DEPLOYMENT_NAME: Optional[str] = None  # azure deployment name for answering
    AZURE_EMBEDDINGS_DEPLOYMENT_NAME: Optional[str] = None  # azure deployment name for embeddings
    OPENAI_BASE_URL: Optional[str] = None  # openai base url for open ai compatable models

    # PGVector vectorstore config. Write the URI in whichever form you
    # prefer — ``postgres://``, ``postgresql://``, or even the SQLAlchemy
    # dialect form (``postgresql+psycopg://``) are all accepted and
    # normalized internally for ``psycopg.connect()``.
    PGVECTOR_CONNECTION_STRING: Optional[str] = None

    FLASK_DEBUG_MODE: bool = False
    STORAGE_TYPE: str = "local"  # local or s3

    # Anonymous startup version check for security issues.
    VERSION_CHECK: bool = True

    JWT_SECRET_KEY: str = ""

    # Encryption settings
    ENCRYPTION_SECRET_KEY: str = "default-docsgpt-encryption-key"

    # Tool pre-fetch settings
    ENABLE_TOOL_PREFETCH: bool = True

    # Conversation Compression Settings
    ENABLE_CONVERSATION_COMPRESSION: bool = True
    COMPRESSION_THRESHOLD_PERCENTAGE: float = 0.8  # Trigger at 80% of context
    COMPRESSION_MODEL_OVERRIDE: Optional[str] = None  # Use different model for compression
    COMPRESSION_PROMPT_VERSION: str = "v1.0"  # Track prompt iterations
    COMPRESSION_MAX_HISTORY_POINTS: int = 3  # Keep only last N compression points to prevent DB bloat

    @field_validator("REASONING_MAX_TOKENS", mode="after")
    @classmethod
    def _reasoning_budget_floor(cls, v: int) -> int:
        # A 0 / negative budget would reach OpenRouter as an invalid
        # ``reasoning.max_tokens``; clamp to a safe floor of 1.
        return max(1, int(v))

    @field_validator("POSTGRES_URI", mode="before")
    @classmethod
    def _normalize_postgres_uri_validator(cls, v):
        return normalize_postgres_uri(v)

    @field_validator("PGVECTOR_CONNECTION_STRING", mode="before")
    @classmethod
    def _normalize_pgvector_connection_string_validator(cls, v):
        return normalize_pgvector_connection_string(v)

    @field_validator("USER_ID_PEPPER", mode="after")
    @classmethod
    def _validate_user_id_pepper(cls, v: str) -> str:
        """Reject empty / non-hex / low-entropy peppers at boot.

        Decoded byte length matters, not string length. ``"x" * 32``
        passes a naive length check but isn't valid hex; ``"abcd"`` is
        valid hex but only 2 bytes of entropy. Both should fail loudly
        before the app starts serving traffic.
        """
        if not v:
            raise ValueError(
                "USER_ID_PEPPER must be set. Generate with `openssl rand -hex 32`."
            )
        try:
            decoded = bytes.fromhex(v)
        except ValueError as exc:
            raise ValueError(
                "USER_ID_PEPPER must be hex-encoded (run `openssl rand -hex 32`)."
            ) from exc
        if len(decoded) < 16:
            raise ValueError(
                f"USER_ID_PEPPER must decode to >=16 bytes; got {len(decoded)}. "
                "Use `openssl rand -hex 32` for 32 bytes (recommended)."
            )
        return v

    @field_validator(
        "API_KEY",
        "OPENAI_API_KEY",
        "EMBEDDINGS_KEY",
        "FALLBACK_LLM_API_KEY",
        "INTERNAL_KEY",
        "MCP_PROVISIONING_KEY",
        mode="before",
    )
    @classmethod
    def normalize_api_key(cls, v: Optional[str]) -> Optional[str]:
        """
        Normalize API keys: convert 'None', 'none', empty strings,
        and whitespace-only strings to actual None.
        Handles Pydantic loading 'None' from .env as string "None".
        """
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        stripped = v.strip()
        if stripped == "" or stripped.lower() == "none":
            return None
        return stripped


# Project root is one level above application/
path = Path(__file__).parent.parent.parent.absolute()
# Tests set DOCSGPT_SETTINGS_SKIP_ENV_FILE=1 (tests/conftest.py) so the
# repo-root operator .env can't leak into test runs at import time.
settings = Settings(
    _env_file=None if os.environ.get("DOCSGPT_SETTINGS_SKIP_ENV_FILE") else path.joinpath(".env"),
    _env_file_encoding="utf-8",
)
