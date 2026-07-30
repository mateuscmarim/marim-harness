"""The settings screen's topic pages, one builder per rail section.

These are the widget trees only — every builder is a plain generator over
``ComposeResult`` with no screen state behind it, so what a section *contains*
can be read without wading through the screen's event handling, and vice versa.
Widget ids are the contract between the two halves: the screen's handlers and
``settings_env``'s registries both address fields by id, so an id renamed here
must be renamed there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.content import Content
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Static

from ...config import ModelConfig, global_config_path
from .settings_env import MODES, TIER_ROWS, TOOL_SEARCH_MODES
from .themes import MARIM_THEMES, THEME_NAMES
from .widgets.box_checkbox import BoxCheckbox

if TYPE_CHECKING:
    # Both deliberately TYPE_CHECKING-only: importing McpManager for real drags
    # pydantic_ai onto this module's import path (see mcp/__init__.py's __getattr__).
    from ...mcp import McpManager
    from ...runtime.harness import Harness

# Each theme's accent hex, for the colored dot in the Theme section + the rail badge.
ACCENTS = {t.name: str(t.primary) for t in MARIM_THEMES}


def short_theme(name: str) -> str:
    """``marim-teal`` -> ``teal`` for the compact rail badge."""
    return name.split("-")[-1]


def tier_value_text(env_cfg: ModelConfig, tier: str) -> str:
    return env_cfg.subagent.tiers.model_for(tier) or "inherit main"


def advisor_value_text(env_cfg: ModelConfig) -> str:
    return env_cfg.advisor_model or "off"


def thinking_value_text(env_cfg: ModelConfig) -> str:
    return env_cfg.thinking_level or "off"


def mcp_status_word(mcp: McpManager, name: str) -> str:
    if name in mcp.disabled:
        return "disabled"
    if name in set(mcp.mcp_status.connected):
        return "connected"
    if name in dict(mcp.mcp_status.failed):
        return "failed"
    return "—"


def session_widgets(
    harness: Harness, env_cfg: ModelConfig, *, autonomous_wake: bool
) -> ComposeResult:
    yield Static(
        "Mode, model & autonomous wake apply live; default mode applies next launch.",
        classes="muted",
    )
    yield Label("Mode (this session)")
    with RadioSet(id="mode-set"):
        for name in MODES:
            yield RadioButton(
                name,
                value=(name == harness.deps.workspace.mode.value),
                id=f"mode-{name}",
            )
    with Horizontal(classes="srow"):
        yield Static(f"Model: {harness.model_label}", id="model-label")
        # compact: without it Button's default tall borders survive the
        # `.srow Button { border: none }` override and paint a ▔ strip at
        # height 1 instead of the label.
        yield Button("change", id="model-change", variant="primary", compact=True)
    yield BoxCheckbox(
        "Autonomous wake (react to finished jobs)",
        value=autonomous_wake,
        id="sw-autonomous-wake",
    )
    yield Label("Default mode (new sessions)")
    with RadioSet(id="default-mode-set"):
        for name in MODES:
            yield RadioButton(
                name,
                value=(name == env_cfg.default_mode),
                id=f"defmode-{name}",
            )


def theme_widgets() -> ComposeResult:
    yield Static("Accent palette for the harness UI.", classes="muted")
    for i, name in enumerate(THEME_NAMES):
        with Horizontal(id=f"theme-{i}", classes="theme-row"):
            yield Static(
                Content.assemble(("● ", ACCENTS[name]), name),
                classes="theme-name",
            )
            yield Static("", id=f"theme-active-{i}", classes="theme-active")


def mcp_widgets(mcp: McpManager, names: list[str]) -> ComposeResult:
    if not names:
        yield Static("No MCP servers configured.", classes="muted")
        return
    with Horizontal(classes="mcp-head"):
        yield Static("SERVER", classes="mcp-name")
        yield Static("STATUS", classes="mcp-status")
        yield Static("ON", classes="mcp-on")
    for i, name in enumerate(names):
        with Horizontal(classes="mcp-row"):
            yield Static(name, classes="mcp-name")
            yield Static(
                mcp_status_word(mcp, name),
                id=f"mcp-state-{i}",
                classes="mcp-status",
            )
            yield BoxCheckbox(
                value=(name not in mcp.disabled),
                id=f"mcp-toggle-{i}",
                classes="mcp-on",
            )


def context_widgets(env_cfg: ModelConfig) -> ComposeResult:
    yield Static("Saved to .env — applies on next launch.", classes="muted")
    with Horizontal(classes="frow"):
        yield Label("Context budget (tokens, 0 = unbudgeted)")
        yield Input(
            value=str(env_cfg.max_context_tokens),
            id="ctx-input",
            type="integer",
        )
    yield BoxCheckbox(
        "Mask stale observations at compaction",
        value=env_cfg.mask_observations,
        id="sw-mask-obs",
    )
    with Horizontal(classes="frow"):
        yield Label("Mask: keep recent returns")
        yield Input(
            value=str(env_cfg.mask_keep_recent),
            id="mask-keep-recent",
            type="integer",
        )
    with Horizontal(classes="frow"):
        yield Label("Mask: min chars to elide")
        yield Input(
            value=str(env_cfg.mask_min_chars),
            id="mask-min-chars",
            type="integer",
        )
    yield BoxCheckbox("Proactive memory", value=env_cfg.proactive_memory, id="sw-mem")


def tools_widgets(env_cfg: ModelConfig) -> ComposeResult:
    """The Tools page: the plain env toggles, then three model-choosing blocks
    (sub-agent tiers, advisor, thinking) that each pair a picker row with the
    prose explaining what saving it does and does not apply to."""
    yield Static(
        "Saved to .env — applies on next launch (dynamic workflows applies live).",
        classes="muted",
    )
    yield BoxCheckbox("LSP", value=env_cfg.lsp_enabled, id="sw-lsp")
    yield BoxCheckbox(
        "LSP navigation tools",
        value=env_cfg.lsp_tools_enabled,
        id="sw-lsp-tools",
    )
    yield BoxCheckbox("Job tool combined", value=env_cfg.job_tool_combined, id="sw-job")
    # Not in ENV_CHECKBOXES: unlike its neighbours this toggle also applies
    # live (the run_workflow tool checks services.run_workflow per call, so
    # flipping the seam needs no relaunch) — it takes a dedicated handler
    # instead of the plain env commit.
    yield BoxCheckbox(
        "Dynamic workflows (run_workflow)",
        value=env_cfg.workflows_enabled,
        id="sw-workflows",
    )
    yield Label("Tool search (MCP/plugin tools)")
    with RadioSet(id="toolsearch-set"):
        for name in TOOL_SEARCH_MODES:
            yield RadioButton(
                name,
                value=(name == env_cfg.tool_search),
                id=f"toolsearch-{name}",
            )
    with Horizontal(classes="frow"):
        yield Label("Tool-search threshold")
        yield Input(
            value=str(env_cfg.tool_search_threshold),
            id="toolsearch-threshold",
            type="integer",
        )
    with Horizontal(classes="frow"):
        yield Label("Sub-agent request limit")
        yield Input(
            value=str(env_cfg.subagent.request_limit),
            id="subagent-req-limit",
            type="integer",
        )
    with Horizontal(classes="frow"):
        yield Label("Autonomous wake turns")
        yield Input(
            value=str(env_cfg.subagent.wake_depth_cap),
            id="wake-depth-cap",
            type="integer",
        )
    yield from _tier_widgets(env_cfg)
    yield from _advisor_widgets(env_cfg)
    yield from _thinking_widgets(env_cfg)


def _tier_widgets(env_cfg: ModelConfig) -> ComposeResult:
    yield BoxCheckbox(
        "Model tiering",
        value=env_cfg.subagent.tiers.enabled,
        id="sw-tiering",
    )
    yield Static(
        "Master switch — off routes every new spawn to the main model while "
        "keeping the tier models below saved (toggle back on to restore "
        "routing, no re-entry). Applies to new spawns live; sub-agents "
        "already in flight keep their model. Per-tier changes below save to "
        ".env and apply to new sessions (next harness rebuild/relaunch).",
        classes="muted",
    )
    for tier, _env_key, label in TIER_ROWS:
        with Horizontal(classes="srow"):
            yield Static(label, classes="tier-row-label")
            yield Static(
                tier_value_text(env_cfg, tier),
                id=f"tier-value-{tier}",
                classes="tier-row-value",
            )
            yield Button(
                "change", id=f"tier-change-{tier}", variant="primary", compact=True
            )


def _advisor_widgets(env_cfg: ModelConfig) -> ComposeResult:
    yield Static(
        "Advisor — a model the agent can consult mid-task for strategic "
        "guidance (the advisor tool). This row saves the global default "
        "to .env (new sessions); /advisor overrides it per session, live. "
        "Type 'off' in the picker to clear it. Uses/turn: 0 = unlimited.",
        classes="muted",
    )
    with Horizontal(classes="srow"):
        yield Static("Advisor", classes="tier-row-label")
        yield Static(
            advisor_value_text(env_cfg), id="advisor-value", classes="tier-row-value"
        )
        yield Button("change", id="advisor-change", variant="primary", compact=True)
    with Horizontal(classes="frow"):
        yield Label("Advisor max tokens")
        yield Input(
            value=str(env_cfg.advisor_max_tokens),
            id="advisor-max-tokens",
            type="integer",
        )
    with Horizontal(classes="frow"):
        yield Label("Advisor max uses/turn")
        yield Input(
            value=str(env_cfg.advisor_max_uses or 0),
            id="advisor-max-uses",
            type="integer",
        )


def _thinking_widgets(env_cfg: ModelConfig) -> ComposeResult:
    yield Static(
        "Thinking — reasoning effort applied to the model "
        "(off/minimal/low/medium/high/xhigh). This row saves the global "
        "default to .env (new sessions); /think overrides it per session, "
        "live. Not every provider supports it; unsupported models ignore it.",
        classes="muted",
    )
    with Horizontal(classes="srow"):
        yield Static("Thinking", classes="tier-row-label")
        yield Static(
            thinking_value_text(env_cfg), id="thinking-value", classes="tier-row-value"
        )
        yield Button("change", id="thinking-change", variant="primary", compact=True)


def notifications_widgets(env_cfg: ModelConfig) -> ComposeResult:
    yield Static("Saved to .env — applies on next launch.", classes="muted")
    yield BoxCheckbox(
        "Desktop notifications",
        value=env_cfg.notifications.enabled,
        id="sw-notifications",
    )
    with Horizontal(classes="frow"):
        yield Label("Notification events")
        yield Input(
            value=", ".join(sorted(env_cfg.notifications.events)),
            id="notif-events-input",
        )


def advanced_widgets(harness: Harness, env_cfg: ModelConfig) -> ComposeResult:
    deny = ", ".join(env_cfg.command_denylist) or "(none)"
    allow = ", ".join(env_cfg.command_allowlist) or "(none)"
    yield Static("Read-only — managed in config or project settings.", classes="muted")
    yield Static(f"Command denylist: {deny}", classes="muted")
    yield Static(f"Command allowlist: {allow}", classes="muted")
    # Live per-project decision (deps.trust), not the env/config knob:
    # MARIM_TRUST_PROJECT_HOOKS is only one input to resolution (config >
    # env > store > default — see marim_harness.trust) and can disagree
    # with what's actually in effect this session, e.g. a decision
    # recorded via /trust or the first-open TrustPanel. `source` names
    # which layer won, the same wording /trust reports.
    trust = harness.deps.trust
    yield Static(
        f"Project trust: {'on' if trust.project else 'off'} (source: {trust.source})",
        id="trust-status",
        classes="muted",
    )
    yield Static(f"Config file: {global_config_path()}", classes="muted")
