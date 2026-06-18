import logging
from pathlib import Path

from .agent import Harness, HarnessConfig, make_summarizer, make_titler
from .command_policy import CommandPolicy
from .config import ModelSource, build_model, load_config
from .deps import Deps
from .hooks import HookRunner, load_hooks_config
from .mcp import build_mcp_servers, disabled_server_names, load_mcp_config
from .permissions import Mode
from .session import SessionManager
from .tools.provider import BuiltinToolProvider

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a coding agent operating inside a workspace directory. "
    "Use the provided tools to read, search, and edit files and run commands. "
    "Always read a file before editing it. Keep changes minimal and focused."
)


def build_harness(
    workspace: Path,
    *,
    mode: Mode,
    resume: bool = False,
) -> Harness:
    """Construct a ready-to-run Harness for ``workspace``. Shared by the TUI and
    the headless CLI so both wire up the model, session store, and aux agents
    identically. When ``resume`` is set, reattaches to the latest saved session
    and replays its history."""
    cfg = load_config()
    model_source = ModelSource(cfg)
    model = build_model(cfg)
    model_id = cfg.model
    command_policy = CommandPolicy(
        denylist=cfg.command_denylist, allowlist=cfg.command_allowlist
    )
    hooks_cfg = load_hooks_config(workspace, trust_project=cfg.trust_project_hooks)
    hook_runner = HookRunner(hooks_cfg) if hooks_cfg else None
    deps = Deps(
        workspace_root=workspace,
        mode=mode,
        command_policy=command_policy,
        hooks=hook_runner,
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
    mcp_specs = load_mcp_config(workspace)
    mcp_servers, mcp_warnings = build_mcp_servers(mcp_specs)
    for warning in mcp_warnings:
        logger.warning("MCP config: %s", warning)
    mcp_disabled = disabled_server_names(mcp_specs)

    # LSP navigation tools are registered only when LSP is on AND tools are on;
    # diagnostics-on-edit is gated separately by lsp_enabled (the manager).
    register_lsp_tools = cfg.lsp_enabled and cfg.lsp_tools_enabled

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
            summarizer=make_summarizer(model),
            titler=make_titler(model),
            model_source=model_source,
            model_id=model_id,
            proactive_memory=cfg.proactive_memory,
            autonomous_wake=cfg.autonomous_wake,
            wake_depth_cap=cfg.wake_depth_cap,
            mcp_servers=mcp_servers,
            mcp_disabled=mcp_disabled,
        ),
    )
    if resume:
        harness.resume()
    return harness
