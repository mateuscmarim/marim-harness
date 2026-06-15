import os
from dataclasses import dataclass
from typing import Optional

_DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4-6"
_DEFAULT_LOCAL_MODEL = "qwen2.5-coder"


@dataclass
class ModelConfig:
    provider: str  # "openrouter" | "local"
    model: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None


def load_config() -> ModelConfig:
    """Build a ModelConfig from environment variables.

    MARIM_PROVIDER (openrouter|local), MARIM_MODEL, MARIM_BASE_URL,
    OPENROUTER_API_KEY / MARIM_API_KEY.
    """
    provider = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    if provider == "local":
        return ModelConfig(
            provider="local",
            model=os.getenv("MARIM_MODEL", _DEFAULT_LOCAL_MODEL),
            base_url=os.getenv("MARIM_BASE_URL", "http://localhost:11434/v1"),
            api_key=os.getenv("MARIM_API_KEY", "local"),
        )
    return ModelConfig(
        provider="openrouter",
        model=os.getenv("MARIM_MODEL", _DEFAULT_OPENROUTER_MODEL),
        base_url=None,
        api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("MARIM_API_KEY"),
    )


def build_model(cfg: ModelConfig):
    """Construct a Pydantic AI model from config. Imported lazily so tests that
    only check config parsing don't require provider packages."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if cfg.provider == "local":
        provider = OpenAIProvider(base_url=cfg.base_url, api_key=cfg.api_key)
        return OpenAIChatModel(cfg.model, provider=provider)

    from pydantic_ai.providers.openrouter import OpenRouterProvider

    provider = OpenRouterProvider(api_key=cfg.api_key)
    return OpenAIChatModel(cfg.model, provider=provider)
