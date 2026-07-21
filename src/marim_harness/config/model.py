import asyncio
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any

from ..command_policy import split_patterns
from ..notifications import NotificationConfig, parse_events
from ..workspace.catalog import (
    ModelEntry,
    fetch_google_models,
    fetch_local_models,
    fetch_openrouter_models,
)

logger = logging.getLogger(__name__)

_DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4-6"
_DEFAULT_LOCAL_MODEL = "qwen2.5-coder"
_DEFAULT_GOOGLE_MODEL = "gemini-2.5-flash"
# None ⇒ let the claude CLI use its own configured default model.
_DEFAULT_CLAUDE_CLI_MODEL: str | None = None

# Every provider load_config knows how to wire. An unknown value falls through to
# the OpenRouter branch (the historical default), but we warn first so a typo
# like MARIM_PROVIDER=azure doesn't masquerade as a confusing "missing API key".
KNOWN_PROVIDERS = frozenset({"openrouter", "local", "google", "claude-cli"})


def parse_qualified(
    qualified: str, active: set[str] | frozenset[str], default: str
) -> tuple[str, str]:
    """Split a ``provider:model_id`` into ``(provider, bare_id)``.

    If the segment before the first ':' is an active provider, route there with
    the remainder as the bare id. Otherwise the whole string is a bare id on the
    ``default`` provider — which makes bare ids (old sessions, MARIM_MODEL) and
    unknown prefixes (e.g. an OpenRouter ``vendor/model`` id) Just Work."""
    head, sep, rest = qualified.partition(":")
    if sep and head in active:
        return head, rest
    return default, qualified


# How many spawns may run their model loop at once when nothing configures it —
# shared by the env path (MARIM_SUBAGENT_CONCURRENCY unset) and the embedding
# path (HarnessConfig) so the two defaults cannot drift. Wide enough for a
# typical review/research fan-out to run fully parallel, small enough that a
# runaway fan-out (a live workflow once queued one spawn per CHARACTER of a
# mis-stringified args value) is contained by queueing, not by luck. None (via
# the explicit 0 env sentinel, or passed directly) remains "unbounded".
DEFAULT_SUBAGENT_CONCURRENCY = 8


def _parse_concurrency(raw: str | None, default: int) -> int | None:
    """Resolve the sub-agent concurrency cap from a raw env value. Unset/blank
    ⇒ the default cap; a parseable non-positive int (0, -1) ⇒ None, the explicit
    "unbounded" opt-out; unparseable garbage ⇒ the safe default cap (never
    silently unbounded). Pure; unit-tested directly."""
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else None


@dataclass(frozen=True)
class SubagentTiers:
    """The user-curated model per sub-agent tier. Each value is a qualified
    ``provider:model_id`` (or None ⇒ inherit the main model). The set of
    non-None ids is the allowlist a raw ``model=`` slug override is bounded to
    once any tier is configured.

    ``enabled`` is the master switch: when False, every tier reports None (so all
    spawns inherit the main model) and the allowlist is empty — WITHOUT clearing
    the curated slugs, so the user can toggle routing off and back on without
    re-entering their models. Both readers below funnel through it, so the whole
    resolver (config → runner) sees a disabled tier set as "no tiers configured"."""

    cheap: str | None = None
    med: str | None = None
    high: str | None = None
    enabled: bool = True

    def model_for(self, tier: str) -> str | None:
        """The configured model id for ``tier``, or None (unset/disabled ⇒ inherit
        main)."""
        if not self.enabled:
            return None
        return {"cheap": self.cheap, "med": self.med, "high": self.high}.get(tier)

    def allowlist(self) -> frozenset[str]:
        """The non-None tier model ids — the permitted set for a slug override.
        Empty while disabled, which reverts a slug override to legacy passthrough
        (disabling tiering is not a slug lockout)."""
        if not self.enabled:
            return frozenset()
        return frozenset(m for m in (self.cheap, self.med, self.high) if m)


@dataclass
class SubagentConfig:
    """Fan-out and concurrency knobs for spawned sub-agents."""

    concurrency: int | None = DEFAULT_SUBAGENT_CONCURRENCY
    transcript_cap: int = 2000
    detach_fanout: bool = True
    autonomous_wake: bool = True
    wake_depth_cap: int = 8
    # Backstop on a single sub-agent run: the most model requests it may make
    # before pydantic-ai aborts it. Bounds a runaway sub-agent (stuck calling
    # tools and never concluding) rather than blocking the spawning turn forever.
    request_limit: int = 50
    # The user-curated model per sub-agent tier (cheap/med/high). Empty tiers
    # inherit the main model, so an unconfigured install behaves like today.
    tiers: "SubagentTiers" = field(default_factory=SubagentTiers)


@dataclass
class ModelConfig:
    provider: str  # "openrouter" | "local" | "google" | "claude-cli"
    model: str | None  # None ⇒ claude-cli uses its own configured default
    base_url: str | None = None
    api_key: str | None = None
    # The GLOBAL context budget in tokens — an economic ceiling, not the
    # model's window. Compaction/masking trigger at min(budget, 0.8 × the
    # discovered window); see config/context_limits.py. Kept under its
    # historical name because persisted settings and the TUI field bind to
    # it. 0 ⇒ unbudgeted (window-only).
    max_context_tokens: int = 100_000
    # Manual context-window override for servers discovery can't read
    # (a non-LM-Studio local server, an offline box). None ⇒ discover.
    context_window: int | None = None
    # Per-model budget overrides: comma-separated pattern=tokens pairs,
    # fnmatch on the model id (e.g. "anthropic/claude-opus*=60000"); "=0"
    # means unbudgeted for that model. Raw string; parsed by ContextLimits.
    context_budgets: str = ""
    # When true, compaction also elides older tool-observation payloads in the
    # retained tail to save tokens (see compaction.mask_stale_observations).
    # Cache-safe because it only runs when compaction already rewrites the tail.
    mask_observations: bool = True
    # How many of the most-recent tool returns masking leaves intact (the agent is
    # most likely still acting on them), and the minimum rendered length below
    # which a return isn't worth masking. Both consumed only when masking runs.
    mask_keep_recent: int = 4
    mask_min_chars: int = 200
    proactive_memory: bool = False
    # Default approval mode for a fresh interactive (TUI) session: "ask" | "auto"
    # | "plan". A durable, explicit preference — distinct from silently carrying
    # over whatever mode the last session happened to end in. "ask" stays the safe
    # default; opting into "auto" is a conscious choice. (The headless one-shot has
    # its own --mode flag and does not consult this.)
    default_mode: str = "ask"
    # Tool search: defer the MCP/plugin tool surface behind Pydantic AI's native
    # tool search. "off" = load all MCP tools every request (today's behavior);
    # "on" = always defer; "auto" = defer only when the live MCP tool count exceeds
    # tool_search_threshold. Builtins are never deferred.
    tool_search: str = "auto"
    tool_search_threshold: int = 15
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
    # Forge (Gitea/GitHub) tools master switch. False ⇒ forge_toolsets returns []
    # and no forge tools are attached, regardless of backend availability.
    forge_enabled: bool = True
    # Session scratchpad master switch. False ⇒ no scratchpad dir is
    # advertised, writable, or approval-exempt (services.get_scratchpad
    # stays None).
    scratchpad_enabled: bool = True
    # Dynamic workflows (the run_workflow tool) master switch. False ⇒ the
    # engine is never built, regardless of whether pydantic-monty is
    # installed (see HarnessConfig.workflows_enabled).
    workflows_enabled: bool = True
    # Ceiling (seconds) on the wall-clock budget a single run_workflow call
    # may request via its timeout_secs parameter. MARIM_WORKFLOW_TIMEOUT.
    workflow_timeout_secs: float = 1800.0
    # Prototype: collapse the four job tools (jobs/job_output/wait_for_job/
    # cancel_job) into one job(action, …) tool. Off ⇒ the four separate tools.
    job_tool_combined: bool = False
    # Advisor: a model the main agent can consult mid-task via the advisor
    # tool. ``advisor_model`` is a qualified ``provider:model_id`` (or a bare
    # slug for the default provider); None = no advisor. ``advisor_max_tokens``
    # caps each consultation's output; ``advisor_max_uses`` caps calls per turn
    # (None = unlimited).
    advisor_model: str | None = None
    advisor_max_tokens: int = 2048
    advisor_max_uses: int | None = None
    # Shell-command allow/deny patterns (regex), enforced in the bash tool in
    # every mode. Empty lists -> no restriction.
    command_denylist: list[str] = field(default_factory=list)
    command_allowlist: list[str] = field(default_factory=list)
    # Fan-out and concurrency knobs grouped together.
    subagent: SubagentConfig = field(default_factory=SubagentConfig)
    # Desktop notification settings.
    notifications: NotificationConfig = field(
        default_factory=lambda: NotificationConfig(enabled=True)
    )


# Once-only guard for the deprecation warning below: load_config() runs many
# times per process (e.g. every Settings-screen open), and the nag is only
# useful the first time.
_budget_deprecation_warned = False


def _context_budget_env() -> int:
    """MARIM_CONTEXT_BUDGET, falling back to the deprecated
    MARIM_MAX_CONTEXT_TOKENS (same meaning, old name) with a one-time
    warning, else the historical 100k default. Parsed directly rather than
    via _int_env because 0 is a meaningful value here — "unbudgeted"
    (window-only) — not an invalid one to be replaced by the default."""
    global _budget_deprecation_warned

    def _parse(raw: str) -> int | None:
        try:
            value = int(raw)
        except ValueError:
            return None
        # A negative budget is garbage and must fail CLOSED (the default cap),
        # not open (0 = unbudgeted ⇒ MORE spend): only an explicit 0 uncaps.
        return value if value >= 0 else None

    raw = os.getenv("MARIM_CONTEXT_BUDGET")
    if raw is not None:
        value = _parse(raw)
        if value is not None:
            return value
    elif os.getenv("MARIM_MAX_CONTEXT_TOKENS") is not None:
        if not _budget_deprecation_warned:
            _budget_deprecation_warned = True
            logger.warning(
                "MARIM_MAX_CONTEXT_TOKENS is deprecated; rename it to "
                "MARIM_CONTEXT_BUDGET (same meaning: the global context budget)."
            )
        value = _parse(os.environ["MARIM_MAX_CONTEXT_TOKENS"])
        if value is not None:
            return value
    return 100_000


def _common_kwargs() -> dict[str, Any]:
    """Provider-independent knobs shared by every ModelConfig. Grouped sub-agent
    and notification knobs are built into their own config objects (see
    SubagentConfig/NotificationConfig) so every provider branch shares them."""
    # Unset ⇒ the shared default cap; an explicit non-positive value (0, -1) is
    # the "no cap" sentinel → None (unbounded). Can't route through _int_env: it
    # clamps non-positive back to the default, which would swallow the sentinel
    # now that the default is a positive cap rather than 0. Unparseable garbage
    # falls back to the safe cap, not to unbounded.
    _concurrency = _parse_concurrency(
        os.getenv("MARIM_SUBAGENT_CONCURRENCY"), DEFAULT_SUBAGENT_CONCURRENCY
    )
    subagent = SubagentConfig(
        concurrency=_concurrency,
        transcript_cap=_int_env("MARIM_SUBAGENT_TRANSCRIPT_CAP", 2000),
        detach_fanout=_bool_env("MARIM_DETACH_FANOUT", True),
        autonomous_wake=_bool_env("MARIM_AUTONOMOUS_WAKE", True),
        wake_depth_cap=_int_env("MARIM_WAKE_DEPTH_CAP", 8),
        request_limit=_int_env("MARIM_SUBAGENT_REQUEST_LIMIT", 50),
        tiers=SubagentTiers(
            cheap=(os.getenv("MARIM_SUBAGENT_TIER_CHEAP") or None),
            med=(os.getenv("MARIM_SUBAGENT_TIER_MED") or None),
            high=(os.getenv("MARIM_SUBAGENT_TIER_HIGH") or None),
            # Master switch: off ⇒ bypass routing (spawns inherit main) while the
            # slugs above stay parsed, so the toggle round-trips losslessly.
            enabled=_bool_env("MARIM_SUBAGENT_TIERING", True),
        ),
    )
    notifications = NotificationConfig(
        enabled=_bool_env("MARIM_NOTIFICATIONS", True),
        events=parse_events(os.getenv("MARIM_NOTIFICATION_EVENTS", "")),
    )
    return dict(
        max_context_tokens=_context_budget_env(),
        context_window=(_int_env("MARIM_CONTEXT_WINDOW", 0) or None),
        context_budgets=os.getenv("MARIM_CONTEXT_BUDGETS", ""),
        mask_observations=_bool_env("MARIM_MASK_OBSERVATIONS", True),
        mask_keep_recent=_int_env("MARIM_MASK_KEEP_RECENT", 4),
        mask_min_chars=_int_env("MARIM_MASK_MIN_CHARS", 200),
        proactive_memory=_bool_env("MARIM_PROACTIVE_MEMORY", False),
        default_mode=_mode_env("MARIM_DEFAULT_MODE", "ask"),
        tool_search=_enum_env("MARIM_TOOL_SEARCH", "auto", _VALID_TOOL_SEARCH),
        tool_search_threshold=_int_env("MARIM_TOOL_SEARCH_THRESHOLD", 15),
        trust_project_hooks=_bool_env("MARIM_TRUST_PROJECT_HOOKS", False),
        lsp_enabled=_bool_env("MARIM_LSP", True),
        lsp_tools_enabled=_bool_env("MARIM_LSP_TOOLS", True),
        forge_enabled=_bool_env("MARIM_FORGE", True),
        scratchpad_enabled=_bool_env("MARIM_SCRATCHPAD", True),
        workflows_enabled=_bool_env("MARIM_WORKFLOWS", True),
        workflow_timeout_secs=float(_int_env("MARIM_WORKFLOW_TIMEOUT", 1800)),
        job_tool_combined=_bool_env("MARIM_JOB_TOOL_COMBINED", False),
        advisor_model=(os.getenv("MARIM_ADVISOR_MODEL") or None),
        advisor_max_tokens=_int_env("MARIM_ADVISOR_MAX_TOKENS", 2048),
        # Unset or 0 = unlimited — the same "0 falls through to None" pattern
        # context_window uses (see _int_env: non-positive returns the default).
        advisor_max_uses=(_int_env("MARIM_ADVISOR_MAX_USES", 0) or None),
        command_denylist=split_patterns(os.getenv("MARIM_COMMAND_DENYLIST", "")),
        command_allowlist=split_patterns(os.getenv("MARIM_COMMAND_ALLOWLIST", "")),
        subagent=subagent,
        notifications=notifications,
    )


def _provider_config(provider: str, common: dict[str, Any]) -> ModelConfig:
    """Build the per-provider ModelConfig (model id, base_url, api_key) sharing
    ``common``. Unknown provider falls back to openrouter (historical default)."""
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
            api_key=(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
                     or os.getenv("MARIM_API_KEY")),
            **common,
        )
    if provider == "claude-cli":
        return ModelConfig(
            provider="claude-cli",
            model=os.getenv("MARIM_MODEL", _DEFAULT_CLAUDE_CLI_MODEL),
            base_url=None,
            api_key=None,  # the CLI owns auth (the Claude subscription)
            **common,
        )
    return ModelConfig(
        provider="openrouter",
        model=os.getenv("MARIM_MODEL", _DEFAULT_OPENROUTER_MODEL),
        base_url=None,
        api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("MARIM_API_KEY"),
        **common,
    )


def _claude_cli_available() -> bool:
    """True when a ``claude`` binary can be resolved (the only 'cred' this provider
    needs; a not-logged-in CLI fails clearly at first use)."""
    from ..subagents.cli_backend import resolve_cli_binary

    return resolve_cli_binary() is not None


def _provider_has_creds(provider: str) -> bool:
    if provider == "openrouter":
        return bool(os.getenv("OPENROUTER_API_KEY"))
    if provider == "google":
        return bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
    if provider == "local":
        return bool(os.getenv("MARIM_BASE_URL"))
    if provider == "claude-cli":
        return _claude_cli_available()
    return False


def detect_active_providers() -> tuple[dict[str, ModelConfig], str]:
    """Every provider whose creds are present, keyed by name, plus the default
    provider (MARIM_PROVIDER). The default is always included so startup has a
    home even if its creds are absent."""
    default = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    if default not in KNOWN_PROVIDERS:
        default = "openrouter"
    common = _common_kwargs()
    active = {p for p in KNOWN_PROVIDERS if _provider_has_creds(p)}
    active.add(default)
    return {p: _provider_config(p, common) for p in active}, default


def load_config() -> ModelConfig:
    """Build the default-provider ModelConfig from environment variables.

    MARIM_PROVIDER selects the provider; MARIM_MODEL, MARIM_BASE_URL,
    OPENROUTER_API_KEY / GOOGLE_API_KEY / GEMINI_API_KEY / MARIM_API_KEY supply
    the model id and credentials. Command allow/deny lists come from
    MARIM_COMMAND_DENYLIST / MARIM_COMMAND_ALLOWLIST."""
    provider = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    if provider not in KNOWN_PROVIDERS:
        logger.warning(
            "Unknown MARIM_PROVIDER=%r; falling back to 'openrouter' "
            "(known providers: %s).",
            provider, ", ".join(sorted(KNOWN_PROVIDERS)),
        )
        provider = "openrouter"
    return _provider_config(provider, _common_kwargs())


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        logger.debug("Ignoring invalid %s=%r (not an integer); using %d.", name, raw, default)
        return default
    if parsed <= 0:
        logger.debug("Ignoring invalid %s=%r (must be positive); using %d.", name, raw, default)
        return default
    return parsed


_TRUTHY = {"1", "true", "on", "yes"}

_VALID_MODES = frozenset({"ask", "auto", "plan"})
_VALID_TOOL_SEARCH = frozenset({"off", "auto", "on"})


def _enum_env(name: str, default: str, valid: frozenset[str]) -> str:
    """Read a string env var validated against ``valid`` (case-insensitive). An
    unknown value falls back to ``default`` (warned, not raised)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in valid:
        logger.warning(
            "Ignoring invalid %s=%r (expected one of %s); using %r.",
            name, raw, ", ".join(sorted(valid)), default,
        )
        return default
    return value


def _mode_env(name: str, default: str) -> str:
    """Approval-mode env var, validated against ask/auto/plan."""
    return _enum_env(name, default, _VALID_MODES)


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
        assert cfg.model is not None  # local always has a model id
        provider = OpenAIProvider(base_url=cfg.base_url, api_key=cfg.api_key)
        return OpenAIChatModel(cfg.model, provider=provider)

    if cfg.provider == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        assert cfg.model is not None  # google always has a model id
        # GoogleProvider's typed overloads (pydantic-ai 2.x) demand a str
        # api_key, but its runtime implementation accepts None and falls back
        # to GOOGLE_API_KEY/GEMINI_API_KEY — the same envs _google_config
        # already read — raising a clear UserError when auth is truly absent.
        # Passing None through preserves that behavior.
        provider = GoogleProvider(api_key=cfg.api_key)  # pyright: ignore[reportArgumentType]
        return GoogleModel(cfg.model, provider=provider)

    if cfg.provider == "claude-cli":
        from .claude_cli_model import ClaudeCliModel

        return ClaudeCliModel(cfg.model)

    from .openrouter_cost import build_openrouter_model

    assert cfg.model is not None  # openrouter always has a model id
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

    async def list_models(self, *, strict: bool = False) -> list[ModelEntry]:
        """Available models for the picker. Returns [] on unsupported providers.

        ``strict=True`` propagates to the fetchers so a real failure (bad key,
        dead server) raises instead of degrading to ``[]`` — used by provider
        verification, which needs to tell "connected, 0 models" apart from
        "failed to connect"."""
        if self.cfg.provider == "openrouter":
            return await fetch_openrouter_models(self.cfg.api_key, strict=strict)
        if self.cfg.provider == "google":
            return await fetch_google_models(self.cfg.api_key, strict=strict)
        if self.cfg.provider == "local":
            return await fetch_local_models(self.cfg.base_url, self.cfg.api_key, strict=strict)
        if self.cfg.provider == "claude-cli":
            return [
                ModelEntry(id="sonnet", name="sonnet", provider="claude-cli"),
                ModelEntry(id="opus", name="opus", provider="claude-cli"),
                ModelEntry(id="haiku", name="haiku", provider="claude-cli"),
            ]
        return []


class MultiModelSource:
    """A ModelSource over several providers at once. Implements the same
    interface the Harness/picker use (``list_models``/``build``/``label``/
    ``is_local``); models are addressed by a colon-qualified ``provider:model_id``.
    A bare or unknown-prefix id resolves on ``default``."""

    def __init__(self, sources: dict[str, ModelSource], default: str) -> None:
        self.sources = sources
        self.default = default

    @classmethod
    def from_env(cls) -> "MultiModelSource":
        configs, default = detect_active_providers()
        return cls({p: ModelSource(c) for p, c in configs.items()}, default)

    def refresh_from_env(self) -> None:
        """Re-detect providers from the current environment, IN PLACE.

        ``build_collaborators`` captures this object in closures at Harness
        construction (``lambda mid, _src=cfg.model_source: _src.build(mid)``),
        so mutating — never replacing — ``sources``/``default`` is what makes
        a settings-screen credential change visible to the model picker,
        ``set_model``, and sub-agent model building without any rewiring.
        ``save_env_settings`` mirrors saves into ``os.environ`` first, so
        ``detect_active_providers`` here sees the new credentials."""
        configs, default = detect_active_providers()
        self.sources.clear()
        self.sources.update({p: ModelSource(c) for p, c in configs.items()})
        self.default = default

    @property
    def is_local(self) -> bool:
        # The picker reads is_local only to decide whether to keep free-text entry
        # available after a catalog loads. The composite always wants free-text on
        # so a user can type a qualified `provider:model_id` even when catalogs are
        # populated — so report True. (This flag does not assert "local provider"
        # for the composite; nothing else consumes it on this type.)
        return True

    def _route(self, qualified: str) -> tuple[ModelSource, str]:
        provider, bare = parse_qualified(qualified, set(self.sources), self.default)
        return self.sources.get(provider, self.sources[self.default]), bare

    def label(self, model_id: str) -> str:
        provider, bare = parse_qualified(model_id, set(self.sources), self.default)
        return f"{provider}:{bare}"

    def build(self, model_id: str):
        source, bare = self._route(model_id)
        return source.build(bare)

    async def list_models(self) -> list[ModelEntry]:
        async def _one(provider: str, source: ModelSource) -> list[ModelEntry]:
            try:
                entries = await source.list_models()
            except Exception as exc:  # noqa: BLE001 - one provider's failure must not sink the rest
                logger.warning("model catalog for %s failed: %s", provider, exc)
                return []
            return [replace(e, provider=provider) for e in entries]

        results = await asyncio.gather(
            *[_one(p, s) for p, s in self.sources.items()]
        )
        return [e for group in results for e in group]
