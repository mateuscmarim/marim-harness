import os
from dataclasses import dataclass, replace
from typing import Optional

from ..workspace.catalog import ModelEntry, fetch_openrouter_models

_DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4-6"
_DEFAULT_LOCAL_MODEL = "qwen2.5-coder"
_DEFAULT_GOOGLE_MODEL = "gemini-2.5-flash"


@dataclass
class ModelConfig:
    provider: str  # "openrouter" | "local" | "google"
    model: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    max_context_tokens: int = 100_000
    proactive_memory: bool = False


def load_config() -> ModelConfig:
    """Build a ModelConfig from environment variables.

    MARIM_PROVIDER (openrouter|local), MARIM_MODEL, MARIM_BASE_URL,
    OPENROUTER_API_KEY / MARIM_API_KEY.
    """
    provider = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    max_context_tokens = _int_env("MARIM_MAX_CONTEXT_TOKENS", 100_000)
    proactive_memory = _bool_env("MARIM_PROACTIVE_MEMORY", False)
    if provider == "local":
        return ModelConfig(
            provider="local",
            model=os.getenv("MARIM_MODEL", _DEFAULT_LOCAL_MODEL),
            base_url=os.getenv("MARIM_BASE_URL", "http://localhost:11434/v1"),
            api_key=os.getenv("MARIM_API_KEY", "local"),
            max_context_tokens=max_context_tokens,
            proactive_memory=proactive_memory,
        )
    if provider == "google":
        return ModelConfig(
            provider="google",
            model=os.getenv("MARIM_MODEL", _DEFAULT_GOOGLE_MODEL),
            base_url=None,
            api_key=(
                os.getenv("GOOGLE_API_KEY")
                or os.getenv("GEMINI_API_KEY")
                or os.getenv("MARIM_API_KEY")
            ),
            max_context_tokens=max_context_tokens,
            proactive_memory=proactive_memory,
        )
    return ModelConfig(
        provider="openrouter",
        model=os.getenv("MARIM_MODEL", _DEFAULT_OPENROUTER_MODEL),
        base_url=None,
        api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("MARIM_API_KEY"),
        max_context_tokens=max_context_tokens,
        proactive_memory=proactive_memory,
    )


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_TRUTHY = {"1", "true", "on", "yes"}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def build_model(cfg: ModelConfig):
    """Construct a Pydantic AI model from config. Imported lazily so tests that
    only check config parsing don't require provider packages."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if cfg.provider == "local":
        provider = OpenAIProvider(base_url=cfg.base_url, api_key=cfg.api_key)
        return OpenAIChatModel(cfg.model, provider=provider)

    if cfg.provider == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(cfg.model, provider=GoogleProvider(api_key=cfg.api_key))

    from pydantic_ai.providers.openrouter import OpenRouterProvider

    provider = OpenRouterProvider(api_key=cfg.api_key)
    return OpenAIChatModel(cfg.model, provider=provider)


class ModelSource:
    """Everything "where models come from" for one provider: build a model from
    an id, format its label, list the catalog, and report whether it's local.
    Bundling these lets the Harness and picker depend on one interface."""

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg

    @property
    def is_local(self) -> bool:
        return self.cfg.provider == "local"

    def label(self, model_id: str) -> str:
        return f"{self.cfg.provider}/{model_id}"

    def build(self, model_id: str):
        """Construct a Pydantic AI model for ``model_id`` on this provider."""
        return build_model(replace(self.cfg, model=model_id))

    async def list_models(self) -> list[ModelEntry]:
        """Available models for the picker. Only OpenRouter exposes a public
        catalog; all other providers return an empty list."""
        if self.cfg.provider != "openrouter":
            return []
        return await fetch_openrouter_models(self.cfg.api_key)
