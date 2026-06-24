import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any

from ..command_policy import split_patterns
from ..notifications import DEFAULT_EVENTS, parse_events
from ..workspace.catalog import ModelEntry, fetch_google_models, fetch_openrouter_models

logger = logging.getLogger(__name__)

_DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4-6"
_DEFAULT_LOCAL_MODEL = "qwen2.5-coder"
_DEFAULT_GOOGLE_MODEL = "gemini-2.5-flash"

# Every provider load_config knows how to wire. An unknown value falls through to
# the OpenRouter branch (the historical default), but we warn first so a typo
# like MARIM_PROVIDER=azure doesn't masquerade as a confusing "missing API key".
_KNOWN_PROVIDERS = frozenset({"openrouter", "local", "google"})


@dataclass
class ModelConfig:
    provider: str  # "openrouter" | "local" | "google"
    model: str
    base_url: str | None = None
    api_key: str | None = None
    max_context_tokens: int = 100_000
    proactive_memory: bool = False
    # When true, project-local .marim/hooks.json hooks are honored; otherwise
    # only the global hooks config runs (supply-chain guard for cloned repos).
    trust_project_hooks: bool = False
    # LSP master switch. False ⇒ no language-server pool is built, so the six
    # navigation tools are not registered and diagnostics-on-edit is a no-op.
    lsp_enabled: bool = True
    # LSP navigation-tools switch. False (while lsp_enabled is True) ⇒ the six
    # tools are not registered, but the manager still runs so diagnostics-on-edit
    # keeps grounding the agent after writes.
    lsp_tools_enabled: bool = True
    # Prototype: collapse the four job tools (jobs/job_output/wait_for_job/
    # cancel_job) into one job(action, …) tool. Off ⇒ the four separate tools.
    job_tool_combined: bool = False
    # Autonomous wake-on-completion (interactive TUI only): when a background job
    # finishes while the turn worker is idle, fire a digest-only turn so the agent
    # reacts without waiting for the user. Off ⇒ today's passive behavior.
    autonomous_wake: bool = True
    # Cap on consecutive autonomous turns before one is forced to wait for the
    # user — a loop guard for wake→spawn→wake chains.
    wake_depth_cap: int = 3
    # Cap on how many spawned sub-agents run their model loop at once. None ⇒
    # unbounded; set MARIM_SUBAGENT_CONCURRENCY to bound a fan-out that trips a
    # shared provider route's upstream rate limit.
    subagent_concurrency: int | None = None
    # Detached fan-out (interactive only): when on, spawn_agent runs detached as a
    # background job so a fan-out doesn't freeze the session; autonomous wake
    # synthesizes the reports. Default on; MARIM_DETACH_FANOUT=0 forces inline.
    detach_fanout: bool = True
    # Shell-command allow/deny patterns (regex), enforced in the bash tool in
    # every mode. Empty lists -> no restriction.
    command_denylist: list[str] = field(default_factory=list)
    command_allowlist: list[str] = field(default_factory=list)
    # Desktop notifications: on by default. Fires native OS notifications for the
    # events listed in ``notification_events``; set MARIM_NOTIFICATIONS=0 to mute.
    notifications_enabled: bool = True
    notification_events: set[str] = field(default_factory=lambda: set(DEFAULT_EVENTS))


def load_config() -> ModelConfig:
    """Build a ModelConfig from environment variables.

    MARIM_PROVIDER (openrouter|local), MARIM_MODEL, MARIM_BASE_URL,
    OPENROUTER_API_KEY / MARIM_API_KEY. MARIM_COMMAND_DENYLIST /
    MARIM_COMMAND_ALLOWLIST hold comma- or newline-separated command patterns.
    """
    provider = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    if provider not in _KNOWN_PROVIDERS:
        logger.warning(
            "Unknown MARIM_PROVIDER=%r; falling back to 'openrouter' "
            "(known providers: %s).",
            provider,
            ", ".join(sorted(_KNOWN_PROVIDERS)),
        )
    max_context_tokens = _int_env("MARIM_MAX_CONTEXT_TOKENS", 100_000)
    proactive_memory = _bool_env("MARIM_PROACTIVE_MEMORY", False)
    trust_project_hooks = _bool_env("MARIM_TRUST_PROJECT_HOOKS", False)
    lsp_enabled = _bool_env("MARIM_LSP", True)
    lsp_tools_enabled = _bool_env("MARIM_LSP_TOOLS", True)
    job_tool_combined = _bool_env("MARIM_JOB_TOOL_COMBINED", False)
    autonomous_wake = _bool_env("MARIM_AUTONOMOUS_WAKE", True)
    wake_depth_cap = _int_env("MARIM_WAKE_DEPTH_CAP", 3)
    # 0 (and any non-positive value) is the "no cap" sentinel, mapped to None so
    # the runner stays unbounded — matching the historical default.
    subagent_concurrency = _int_env("MARIM_SUBAGENT_CONCURRENCY", 0) or None
    if subagent_concurrency is not None and subagent_concurrency < 0:
        subagent_concurrency = None
    detach_fanout = _bool_env("MARIM_DETACH_FANOUT", True)
    command_denylist = split_patterns(os.getenv("MARIM_COMMAND_DENYLIST", ""))
    command_allowlist = split_patterns(os.getenv("MARIM_COMMAND_ALLOWLIST", ""))
    notifications_enabled = _bool_env("MARIM_NOTIFICATIONS", True)
    notification_events = parse_events(os.getenv("MARIM_NOTIFICATION_EVENTS", ""))
    # Provider-independent knobs, shared verbatim by every branch below. Keeping
    # them in one dict means a new ModelConfig field is added here once, not in
    # three parallel constructor calls that silently drift.
    common: dict[str, Any] = dict(
        max_context_tokens=max_context_tokens,
        proactive_memory=proactive_memory,
        trust_project_hooks=trust_project_hooks,
        lsp_enabled=lsp_enabled,
        lsp_tools_enabled=lsp_tools_enabled,
        job_tool_combined=job_tool_combined,
        autonomous_wake=autonomous_wake,
        wake_depth_cap=wake_depth_cap,
        subagent_concurrency=subagent_concurrency,
        detach_fanout=detach_fanout,
        command_denylist=command_denylist,
        command_allowlist=command_allowlist,
        notifications_enabled=notifications_enabled,
        notification_events=notification_events,
    )
    if provider == "local":
        return ModelConfig(
            provider="local",
            model=os.getenv("MARIM_MODEL", _DEFAULT_LOCAL_MODEL),
            base_url=os.getenv("MARIM_BASE_URL", "http://localhost:11434/v1"),
            api_key=os.getenv("MARIM_API_KEY", "local"),
            **common,
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
            **common,
        )
    return ModelConfig(
        provider="openrouter",
        model=os.getenv("MARIM_MODEL", _DEFAULT_OPENROUTER_MODEL),
        base_url=None,
        api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("MARIM_API_KEY"),
        **common,
    )


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.debug("Ignoring invalid %s=%r (not an integer); using %d.", name, raw, default)
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

    from .openrouter_cost import build_openrouter_model

    return build_openrouter_model(cfg.model, cfg.api_key)


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
        """Available models for the picker. Returns [] on unsupported providers."""
        if self.cfg.provider == "openrouter":
            return await fetch_openrouter_models(self.cfg.api_key)
        if self.cfg.provider == "google":
            return await fetch_google_models(self.cfg.api_key)
        return []
