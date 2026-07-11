"""The settings screen's Providers section: stacked cards for the four built-in
providers (openrouter / google / local / claude-cli), a default-provider radio,
live apply, implicit verification, and key removal.

Credentials save to the GLOBAL .env only (a project .env may not set these keys
at all — see _PROJECT_ENV_BLOCKLIST in config/env.py), and ``save_env_settings``
mirrors them into ``os.environ``, so an in-place
``MultiModelSource.refresh_from_env()`` right after a save makes the provider
active for the model picker without a restart. Key inputs are password fields
that start EMPTY — the placeholder proves the configured state without ever
painting the secret, and an empty commit is a no-op so focus/blur can never
clobber a stored key."""

from __future__ import annotations

import os
from dataclasses import dataclass

_KNOWN = ("openrouter", "google", "local", "claude-cli")


@dataclass(frozen=True)
class ProviderSpec:
    """Which env keys one provider reads/writes, driving its settings card."""

    name: str
    write_key: str | None  # env var an API-key commit writes (None: no key field)
    key_fallbacks: tuple[str, ...]  # alt env names probed for the placeholder hint
    read_keys: tuple[str, ...]  # any of these set ⇒ configured
    drop_keys: tuple[str, ...]  # removed together by the remove button
    base_url_key: str | None = None  # local only


PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "openrouter",
        write_key="OPENROUTER_API_KEY",
        key_fallbacks=(),
        read_keys=("OPENROUTER_API_KEY",),
        drop_keys=("OPENROUTER_API_KEY",),
    ),
    # google is configured by EITHER env name, but a save always writes
    # GOOGLE_API_KEY and a remove must drop BOTH (either one alone would
    # keep the provider configured).
    ProviderSpec(
        "google",
        write_key="GOOGLE_API_KEY",
        key_fallbacks=("GEMINI_API_KEY",),
        read_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        drop_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    ),
    # local is marked configured by its base URL (matching _provider_has_creds);
    # removal clears URL + key together — a leftover key alone is meaningless.
    ProviderSpec(
        "local",
        write_key="MARIM_API_KEY",
        key_fallbacks=(),
        read_keys=("MARIM_BASE_URL",),
        drop_keys=("MARIM_BASE_URL", "MARIM_API_KEY"),
        base_url_key="MARIM_BASE_URL",
    ),
    # claude-cli stores nothing: the CLI owns auth; status is binary detection.
    ProviderSpec(
        "claude-cli", write_key=None, key_fallbacks=(), read_keys=(), drop_keys=()
    ),
)
_SPECS = {s.name: s for s in PROVIDER_SPECS}

_DEFAULT_LOCAL_URL = "http://localhost:11434/v1"


def key_hint(value: str | None) -> str:
    """Placeholder for a password input: proves whether a key is stored — and
    shows its last 4 chars when the key is long enough that this reveals
    nothing useful — without ever painting the secret itself."""
    if not value:
        return "not set"
    if len(value) >= 8:
        return f"configured · …{value[-4:]} — type to replace"
    return "configured — type to replace"


def short_error(exc: Exception) -> str:
    """First line of an exception, truncated to fit the one-line card badge."""
    text = (str(exc) or type(exc).__name__).splitlines()[0]
    return text if len(text) <= 48 else text[:47] + "…"


def spec_configured(spec: ProviderSpec) -> bool:
    """Env-based configured check (any read key set). claude-cli has no read
    keys — the pane special-cases it via CLI-binary detection instead."""
    return any(os.getenv(k) for k in spec.read_keys)


def current_default_provider() -> str:
    """MARIM_PROVIDER from the env, normalized like load_config: lowercased,
    unknown values falling back to openrouter (the historical default)."""
    default = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    return default if default in _KNOWN else "openrouter"
