from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..command_policy import CommandPolicy
from ..compaction import make_summarizer, make_titler
from ..config import (
    ModelSource,
    MultiModelSource,
    detect_active_providers,
    load_config,
)
from ..config.context_limits import build_context_limits
from ..hooks import HookRunner, load_hooks_config
from ..lsp.bundled import bundled_lsp_providers
from ..lsp.provider import LspRegistry
from ..mcp import build_mcp_servers, disabled_server_names, load_mcp_config
from ..notifications import Notifier
from ..plugins.discovery import plugin_lsp_providers
from ..session import SessionManager
from ..session.ctrl import aux_model_for
from ..trust import resolve_project_trust
from ..trust_surface import scan_project_surface
from .deps import Deps, TrustState, UIHooks, WorkspaceConfig
from .harness import Harness
from .permissions import Mode

if TYPE_CHECKING:
    from ..stats.ledger import StatsLedger

logger = logging.getLogger(__name__)


def _build_stats_ledger(workspace: Path, *, enabled: bool) -> StatsLedger | None:
    """The stats ledger, or None when MARIM_STATS is off. Extracted out of
    build_harness to keep it under the cyclomatic-complexity ceiling."""
    if not enabled:
        return None
    from ..stats.ledger import (
        StatsLedger,
        default_sessions_base,
        default_stats_base,
        workspace_slug,
    )

    sessions_base = default_sessions_base()
    return StatsLedger(default_stats_base(sessions_base), workspace_slug(workspace))


def build_lsp_registry(workspace: Path, *, trust_project: bool) -> LspRegistry:
    """Assemble the session LSP registry: bundled providers first (lowest
    precedence), then trusted third-party plugin providers, so a project/global
    plugin can override a bundled language by declaring the same extension."""
    providers = list(bundled_lsp_providers())
    providers += plugin_lsp_providers(workspace, trust_project=trust_project)
    return LspRegistry(providers)


def build_harness(
    workspace: Path,
    *,
    mode: Mode | None = None,
    resume: bool = False,
    session_id: str | None = None,
) -> Harness:
    """Construct a ready-to-run Harness for ``workspace``. Shared by the TUI and
    the headless CLI so both wire up the model, session store, and aux agents
    identically. When ``resume`` is set, reattaches to the latest saved session
    and replays its history.

    ``mode`` is the initial approval mode. Pass it explicitly to force a mode
    (the headless ``--mode`` flag does this); leave it ``None`` to use the
    configured default (``MARIM_DEFAULT_MODE``, falling back to ``ask``) — the
    interactive TUI takes this path.

    ``session_id`` opens exactly that session (used by the server, which picks
    sessions explicitly rather than "latest"); it replays any saved history,
    and is mutually exclusive with ``resume``."""
    cfg = load_config()
    # Resolve project trust once, store-aware: an explicit env decision wins,
    # otherwise the per-project trust store is consulted (honored only while
    # its fingerprint still matches the current gated surface). Every loader
    # below and the live TrustState on Deps consume this single `trusted`
    # value, so the CLI's gate and the TUI's first-open prompt can never
    # disagree about what "trusted" means for this run.
    surface = scan_project_surface(workspace)
    resolution = resolve_project_trust(
        workspace, explicit=cfg.trust_project_hooks,
        fingerprint=surface.fingerprint, surface_empty=surface.empty,
    )
    trusted = resolution.trusted
    if mode is None:
        mode = Mode(cfg.default_mode)
    configs, default_provider = detect_active_providers()
    model_source = MultiModelSource(
        {p: ModelSource(c) for p, c in configs.items()}, default_provider
    )
    # A None model (claude-cli's "let the CLI choose" default) must round-trip to a
    # bare empty id, not the literal string "None" — otherwise we'd spawn
    # `claude --model None`. `parse_qualified` turns "claude-cli:" into bare "".
    model_id = f"{default_provider}:{configs[default_provider].model or ''}"
    model = model_source.build(model_id)
    command_policy = CommandPolicy(
        denylist=cfg.command_denylist, allowlist=cfg.command_allowlist
    )
    hooks_cfg = load_hooks_config(workspace, trust_project=trusted)
    hook_runner = HookRunner(hooks_cfg) if hooks_cfg else None
    notifier = Notifier(cfg.notifications)
    deps = Deps(
        workspace=WorkspaceConfig(
            root=workspace,
            mode=mode,
            command_policy=command_policy,
            tool_search=cfg.tool_search,
            tool_search_threshold=cfg.tool_search_threshold,
        ),
        trust=TrustState(
            project=trusted, source=resolution.source, fingerprint=surface.fingerprint
        ),
        hooks=hook_runner,
        ui=UIHooks(detach_fanout=cfg.subagent.detach_fanout, notifier=notifier),
    )

    if resume and session_id is not None:
        raise ValueError("pass resume or session_id, not both")

    manager = SessionManager(workspace)
    if session_id is not None:
        store = manager.store(session_id)
    else:
        latest = manager.latest() if resume else None
        store = manager.store(latest.id) if latest is not None else manager.create()

    stats_ledger = _build_stats_ledger(workspace, enabled=cfg.stats_enabled)

    # When starting fresh (not resuming, no explicit session), pick up the model
    # from the most recent session so the user doesn't have to re-select it after
    # every restart. Resumed/explicit sessions get theirs via harness.resume().
    if not resume and session_id is None and store.model and store.model != model_id:
        model_id = store.model
        model = model_source.build(model_id)

    # MCP servers from the merged global + project config. Malformed specs are
    # dropped (build returns warnings); connections are opened later by the caller
    # (the TUI on mount, headless around its run).
    mcp_specs = load_mcp_config(workspace, trust_project=trusted)
    mcp_servers, mcp_warnings = build_mcp_servers(mcp_specs)
    for warning in mcp_warnings:
        logger.warning("MCP config: %s", warning)
    mcp_disabled = disabled_server_names(mcp_specs)

    # LSP navigation tools are registered only when LSP is on AND tools are on
    # AND some language present in the workspace has a startable server;
    # diagnostics-on-edit is gated separately by lsp_enabled (the manager).
    # Without the coverage gate, a workspace with no server (e.g. a Python
    # repo on a machine without jedi-language-server) carries six tools that
    # can only ever return their install hint — schema tokens on every
    # request, a wasted round trip per attempt. Deciding at build time keeps
    # the toolset stable for prompt caching; the cost is that when NO language
    # was covered at startup, a server installed mid-session needs a restart
    # to surface the tools (under partial coverage the tools stay registered
    # and LspManager still probes availability per call).
    lsp_reg = build_lsp_registry(workspace, trust_project=trusted)
    register_lsp_tools = cfg.lsp_enabled and cfg.lsp_tools_enabled
    if register_lsp_tools:
        found = lsp_reg.workspace_languages(workspace)
        if not any(lsp_reg.availability(lang).available for lang in found):
            register_lsp_tools = False
            logger.info(
                "LSP tools disabled: no language server available for "
                "workspace languages %s",
                sorted(found) if found else "(none detected)",
            )

    # The aux agents (summarizer/titler) must NOT share a claude-cli main model:
    # that instance carries the live session_id, so they would resume — and reply
    # into — the user's real Claude session (and drop their own instructions). Give
    # them a stateless, read-only ephemeral clone instead. Other providers reuse the
    # one model as before. aux_model_for is the SAME helper update_model uses on a
    # runtime /model switch, so the clone can't be dropped on one path but not the
    # other.
    aux_model = aux_model_for(model, cwd=str(workspace))

    from .builder import HarnessBuilder

    builder = (
        HarnessBuilder(workspace=workspace, model=model)
        .with_defaults()                      # full CLI toolset
        .with_deps(deps)                       # CLI-built Deps: notifier, tool-search knobs
        .with_jobs(combined=cfg.job_tool_combined)
        # with_defaults() turned LSP fully on; re-derive it from the CLI's
        # two-switch config (manager vs. navigation tools) rather than reach
        # into builder privates — with_lsp(enabled=False) still folds tools
        # off too, matching register_lsp_tools's own "both must be true" rule.
        .with_lsp(enabled=cfg.lsp_enabled, tools=register_lsp_tools, registry=lsp_reg)
        .with_config_overrides(
            # The builder derives forge_enabled from an explicit backend (None
            # here), which would turn CLI forge OFF. Pin the config-driven value
            # so tea auto-detection keeps working — this override must stay.
            forge_enabled=cfg.forge_enabled,
            scratchpad_enabled=cfg.scratchpad_enabled,
            workflows_enabled=cfg.workflows_enabled,
            workflow_timeout_secs=cfg.workflow_timeout_secs,
            model_label=model_source.label(model_id),
            store=store,
            manager=manager,
            stats_ledger=stats_ledger,
            max_context_tokens=cfg.max_context_tokens,
            # Window discovery covers EVERY active provider (the same set the
            # MultiModelSource above routes across): /model can switch to a
            # qualified `local:...`/`google:...` id, and the new provider's
            # window must still be discoverable after the invalidate.
            context_limits=build_context_limits(
                configs,
                window_override=cfg.context_window,
                budget=cfg.max_context_tokens or None,
                budget_overrides_raw=cfg.context_budgets,
            ),
            mask_observations=cfg.mask_observations,
            mask_keep_recent=cfg.mask_keep_recent,
            mask_min_chars=cfg.mask_min_chars,
            # The aux agents (summarizer/titler) must NOT share a claude-cli main
            # model: that instance carries the live session_id, so they would
            # resume — and reply into — the user's real Claude session (and drop
            # their own instructions). aux_model above is the stateless, ephemeral
            # clone for that case; this override replaces the builder's own
            # summarizer/titler (built from the plain `model`) with aux_model's.
            summarizer=make_summarizer(aux_model),
            titler=make_titler(aux_model),
            model_source=model_source,
            model_id=model_id,
            proactive_memory=cfg.proactive_memory,
            autonomous_wake=cfg.subagent.autonomous_wake,
            wake_depth_cap=cfg.subagent.wake_depth_cap,
            subagent_concurrency=cfg.subagent.concurrency,
            subagent_transcript_cap=cfg.subagent.transcript_cap,
            subagent_request_limit=cfg.subagent.request_limit,
            subagent_tiers=cfg.subagent.tiers,
            advisor_model=cfg.advisor_model,
            advisor_max_tokens=cfg.advisor_max_tokens,
            advisor_max_uses=cfg.advisor_max_uses,
            thinking_level=cfg.thinking_level,
            mcp_servers=mcp_servers,
            mcp_disabled=mcp_disabled,
            # Same trust decision load_mcp_config was just called with above
            # (see HarnessConfig.mcp_trust_project's docstring for why this
            # must not be re-derived independently downstream).
            mcp_trust_project=trusted,
            notifications=cfg.notifications,
        )
    )
    harness = builder.build()
    harness.project_surface = surface
    if resolution.prompt_needed:
        harness.trust_prompt = surface
    if resume or session_id is not None:
        harness.resume()

    # The claude-cli provider needs late-bound hooks (live approval mode, the real
    # workspace/worktree cwd, the TUI tool-card side-channel). Bind them now; the
    # activity side-channel stays None until the TUI calls bind_ui.
    harness.wire_cli_model(harness.current_model)

    return harness
