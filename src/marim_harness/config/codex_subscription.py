"""Native subscription configuration; upstream owns transport and credential refresh."""

import os
from pathlib import Path

LOGIN_HELP = (
    "Codex subscription authentication failed. Run `codex login`, then restart Marim. "
    "Credentials refreshed in memory are not saved to the CLI auth file."
)


def credentials_present() -> bool:
    """Presence only: neither a connection test nor an assertion of model access."""
    home = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex")
    return (home / "auth.json").is_file()


def subscription_provider():
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.providers.openai_codex import OpenAICodexProvider

    try:
        # The public default loader is read-only. Never supply an API key,
        # endpoint override, credential store, or persistence callback here.
        return OpenAICodexProvider()
    except (UserError, OSError, ValueError):
        raise UserError(LOGIN_HELP) from None


def subscription_model(model_id: str | None, provider=None):
    from pydantic_ai.models.openai_codex import OpenAICodexModel

    if not model_id or not model_id.strip():
        raise ValueError(
            "openai-codex requires MARIM_CODEX_SUBSCRIPTION_MODEL, MARIM_MODEL when it is the "
            "default provider, or a qualified openai-codex:<model> selection."
        )
    return OpenAICodexModel(model_id, provider=provider or subscription_provider())
