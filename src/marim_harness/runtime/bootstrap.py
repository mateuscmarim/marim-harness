import logging
from pathlib import Path

from ..command_policy import CommandPolicy
from ..compaction import make_summarizer, make_titler
from ..config import (
    ModelSource,
    MultiModelSource,
    detect_active_providers,
    load_config,
)
from ..hooks import HookRunner, load_hooks_config
from ..mcp import build_mcp_servers, disabled_server_names, load_mcp_config
from ..notifications import Notifier
from ..session import SessionManager
from ..tools.provider import BuiltinToolProvider
from .deps import Deps, UIHooks, WorkspaceConfig
from .harness import Harness, HarnessConfig
from .permissions import Mode

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a coding agent operating inside a workspace directory. "
    "Use the provided tools to read, search, and edit files and run commands. "
    "Always read a file before editing it. Keep changes minimal and focused."
)


def build_harness(
    workspace: Path,
    *,
    mode: Mode | None = None,
    resume: bool = False,
) -> Harness:
    """Construct a ready-to-run Harness for ``workspace``. Shared by the TUI and
    the headless CLI so both wire up the model, session store, and aux agents
    identically. When ``resume`` is set, reattaches to the latest saved session
    and replays its history.

    ``mode`` is the initial approval mode. Pass it explicitly to force a mode
    (the headless ``--mode`` flag does this); leave it ``None`` to use the
    configured default (``MARIM_DEFAULT_MODE``, falling back to ``ask``) — the
    interactive TUI takes this path."""
    cfg = load_config()
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
    hooks_cfg = load_hooks_config(workspace, trust_project=cfg.trust_project_hooks)
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
        hooks=hook_runner,
        ui=UIHooks(detach_fanout=cfg.subagent.detach_fanout, notifier=notifier),
    )

    manager = SessionManager(workspace)
    latest = manager.latest() if resume else None
    store = manager.store(latest.id) if latest is not None else manager.create()

    # When not resuming, pick up the model from the most recent session so the
    # user doesn't have to re-select it after every restart.
    if not resume and store.model and store.model != model_id:
        model_id = store.model
        model = model_source.build(model_id)

    # MCP servers from the merged global + project config. Malformed specs are
    # dropped (build returns warnings); connections are opened later by the caller
    # (the TUI on mount, headless around its run).
    mcp_specs = load_mcp_config(workspace, trust_project=cfg.trust_project_hooks)
    mcp_servers, mcp_warnings = build_mcp_servers(mcp_specs)
    for warning in mcp_warnings:
        logger.warning("MCP config: %s", warning)
    mcp_disabled = disabled_server_names(mcp_specs)

    # LSP navigation tools are registered only when LSP is on AND tools are on;
    # diagnostics-on-edit is gated separately by lsp_enabled (the manager).
    register_lsp_tools = cfg.lsp_enabled and cfg.lsp_tools_enabled

    # The aux agents (summarizer/titler) must NOT share a claude-cli main model:
    # that instance carries the live session_id, so they would resume — and reply
    # into — the user's real Claude session (and drop their own instructions). Give
    # them a stateless, read-only ephemeral clone instead. Other providers reuse the
    # one model as before.
    from ..config.claude_cli_model import ClaudeCliModel

    aux_model = (
        model.ephemeral_clone(cwd=str(workspace))
        if isinstance(model, ClaudeCliModel)
        else model
    )

    harness = Harness(
        model=model,
        provider=BuiltinToolProvider(
            register_lsp_tools=register_lsp_tools,
            combined_job_tool=cfg.job_tool_combined,
        ),
        deps=deps,
        instructions=INSTRUCTIONS,
        config=HarnessConfig(
            lsp_enabled=cfg.lsp_enabled,
            model_label=model_source.label(model_id),
            store=store,
            manager=manager,
            max_context_tokens=cfg.max_context_tokens,
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
            mcp_servers=mcp_servers,
            mcp_disabled=mcp_disabled,
            notifications=cfg.notifications,
        ),
    )
    if resume:
        harness.resume()

    # The claude-cli provider needs late-bound hooks (live approval mode, the real
    # workspace/worktree cwd, the TUI tool-card side-channel). Bind them now; the
    # activity side-channel stays None until the TUI calls bind_ui.
    harness._wire_cli_model(harness.current_model)

    return harness
