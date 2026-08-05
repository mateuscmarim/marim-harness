"""The settings screen's persistence layer: which widget id maps to which env
var, and the one funnel every auto-saving field writes through. Also holds the
Tools page's help-line copy and dependent-row enablement registries.

Kept apart from ``settings.py`` because it is the half of that screen with no
Textual in it — the registries are data, and ``EnvAutoSave`` only needs a
callback to report through. The screen supplies widget values and a status
line; nothing here queries the DOM.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from ...config import save_env_settings

MODES = ("ask", "auto", "plan")
TOOL_SEARCH_MODES = ("off", "auto", "on")

# Auto-save registries: widget id -> what to persist. The same ids are used in
# both the old single-Config layout and the topic-page layout, so these maps are
# the single source of truth for persistence and survive the page restructure.
ENV_CHECKBOXES: dict[str, str] = {
    "sw-lsp": "MARIM_LSP",
    "sw-lsp-tools": "MARIM_LSP_TOOLS",
    "sw-job": "MARIM_JOB_TOOL_COMBINED",
    "sw-mem": "MARIM_PROACTIVE_MEMORY",
    "sw-mask-obs": "MARIM_MASK_OBSERVATIONS",
    "sw-notifications": "MARIM_NOTIFICATIONS",
}
# widget id -> (env var, human label for the validation error message)
ENV_INT_INPUTS: dict[str, tuple[str, str]] = {
    "ctx-input": ("MARIM_CONTEXT_BUDGET", "Context budget"),
    "toolsearch-threshold": ("MARIM_TOOL_SEARCH_THRESHOLD", "Tool-search threshold"),
    "mask-keep-recent": ("MARIM_MASK_KEEP_RECENT", "Mask: keep recent returns"),
    "mask-min-chars": ("MARIM_MASK_MIN_CHARS", "Mask: min chars to elide"),
    "subagent-req-limit": ("MARIM_SUBAGENT_REQUEST_LIMIT", "Sub-agent request limit"),
    "wake-depth-cap": ("MARIM_WAKE_DEPTH_CAP", "Autonomous wake turns"),
    "advisor-max-tokens": ("MARIM_ADVISOR_MAX_TOKENS", "Advisor max tokens"),
    "advisor-max-uses": ("MARIM_ADVISOR_MAX_USES", "Advisor max uses/turn"),
}
# Integer inputs whose domain includes 0. The context budget's label promises
# "0 = unbudgeted" (window-only); the advisor per-turn cap's label promises
# "0 = unlimited" — both must accept it; every other integer field still
# requires a positive value.
ZERO_OK_INPUTS = frozenset({"ctx-input", "advisor-max-uses"})
# env var -> deprecated aliases removed in the same save. Saving the budget
# must retire MARIM_MAX_CONTEXT_TOKENS: leaving the old line behind would make
# the deprecation nag fire against a line the app wrote itself, and — worse —
# would let the stale alias linger where a user might expect it to still win.
DROP_ON_SAVE: dict[str, tuple[str, ...]] = {
    "MARIM_CONTEXT_BUDGET": ("MARIM_MAX_CONTEXT_TOKENS",),
}
# radio set id -> (env var, ordered choices)
ENV_RADIOS: dict[str, tuple[str, tuple[str, ...]]] = {
    "default-mode-set": ("MARIM_DEFAULT_MODE", MODES),
    "toolsearch-set": ("MARIM_TOOL_SEARCH", TOOL_SEARCH_MODES),
}
ENV_TEXT_INPUTS: dict[str, str] = {"notif-events-input": "MARIM_NOTIFICATION_EVENTS"}

# The three sub-agent model-tier rows: (tier key, env var, row label). Order
# matches the picker rows top-to-bottom. Each tier's env var holds a qualified
# ``provider:model_id`` or is unset (⇒ inherit the main model) — see
# ``SubagentTiers`` in config/model.py.
TIER_ROWS: tuple[tuple[str, str, str], ...] = (
    ("cheap", "MARIM_SUBAGENT_TIER_CHEAP", "Cheap tier"),
    ("med", "MARIM_SUBAGENT_TIER_MED", "Med tier"),
    ("high", "MARIM_SUBAGENT_TIER_HIGH", "High tier"),
)
TIER_ENV: dict[str, str] = {tier: env_key for tier, env_key, _ in TIER_ROWS}

# Help-line copy for the Tools settings page, keyed by widget id. Looked up via
# ``help_for`` against the focused widget's id-ancestor chain (leaf to root),
# so a control's own id can win over its containing RadioSet/section.
FIELD_HELP: dict[str, str] = {
    "sw-lsp": (
        "Language-server integration (diagnostics on edit). Applies next launch."
    ),
    "sw-lsp-tools": (
        "Six navigation tools (definitions, references, …). Requires LSP. "
        "Applies next launch."
    ),
    "toolsearch-set": (
        "Serve MCP/plugin tools via the search_tools tool instead of up-front "
        "schemas. 'auto' activates once the tool count passes the threshold. "
        "Applies next launch."
    ),
    "toolsearch-threshold": (
        "Tool count at which 'auto' tool search activates. Applies next launch."
    ),
    "sw-job": (
        "One combined job tool instead of separate list/output/wait/cancel "
        "tools. Applies next launch."
    ),
    "sw-workflows": (
        "Model-authored Python workflows in a sandbox (run_workflow). "
        "Applies live."
    ),
    "subagent-req-limit": (
        "Maximum model requests per sub-agent run. Applies next launch."
    ),
    "wake-depth-cap": (
        "Maximum autonomous turns after a finished job wakes the agent. "
        "Applies next launch."
    ),
    "sw-tiering": (
        "Route new spawns to cheap/med/high tier models. Off sends every spawn "
        "to the main model; tier picks stay saved. Applies live to new spawns."
    ),
    "tier-change-cheap": (
        "Model for cheap-tier spawns; unset inherits the main model. "
        "Saves to .env — applies to new sessions."
    ),
    "tier-change-med": (
        "Model for med-tier spawns; unset inherits the main model. "
        "Saves to .env — applies to new sessions."
    ),
    "tier-change-high": (
        "Model for high-tier spawns; unset inherits the main model. "
        "Saves to .env — applies to new sessions."
    ),
    "advisor-change": (
        "A model the agent can consult mid-task for strategic guidance. "
        "Saves the global default to .env (new sessions); /advisor overrides "
        "per session, live. Type 'off' to clear."
    ),
    "advisor-max-tokens": "Token cap on advisor replies. Applies next launch.",
    "advisor-max-uses": (
        "Advisor calls per turn; 0 = unlimited. Applies next launch."
    ),
    "thinking-change": (
        "Reasoning effort (off/minimal/low/medium/high/xhigh). Saves the "
        "global default to .env (new sessions); /think overrides per session, "
        "live. Unsupported models ignore it."
    ),
}

SECTION_HELP: dict[str, str] = {
    "tools": (
        "Env-backed settings — save automatically on change; apply next launch "
        "unless the field says live."
    ),
}

# checkbox widget id -> dependent row ids to enable/disable in lockstep.
CHECK_DEPENDENTS: dict[str, list[str]] = {
    "sw-lsp": ["row-lsp-tools"],
    "sw-tiering": ["row-tier-cheap", "row-tier-med", "row-tier-high"],
}

# value-widget key -> (dependent row ids, predicate over the current value)
# deciding whether those rows are enabled.
VALUE_DEPENDENTS: dict[str, tuple[list[str], Callable[[str], bool]]] = {
    "toolsearch": (["row-toolsearch-threshold"], lambda v: v != "off"),
    "advisor": (
        ["row-advisor-tokens", "row-advisor-uses"],
        lambda v: v != "off",
    ),
}


def help_for(ids: Iterable[str]) -> str | None:
    """Return FIELD_HELP for the first id in ``ids`` that has an entry."""
    for widget_id in ids:
        text = FIELD_HELP.get(widget_id)
        if text is not None:
            return text
    return None


def dependents_enabled(
    check_values: Mapping[str, bool],
    value_masters: Mapping[str, str],
) -> dict[str, bool]:
    """Map every dependent ``row-*`` id to whether its controls should be enabled."""
    enabled: dict[str, bool] = {}
    for master, rows in CHECK_DEPENDENTS.items():
        on = bool(check_values.get(master, False))
        for row in rows:
            enabled[row] = on
    for master, (rows, pred) in VALUE_DEPENDENTS.items():
        on = pred(value_masters.get(master, ""))
        for row in rows:
            enabled[row] = on
    return enabled


def env_flag(value: bool) -> str:
    return "1" if value else "0"


def parse_int_field(widget_id: str, raw: str) -> int | None:
    """Parse one integer field's raw text, or None when it is blank/invalid/below
    the field's floor. Fields in ``ZERO_OK_INPUTS`` accept 0 (a meaningful
    sentinel there); the rest require a positive integer. Pure — the caller turns
    None into the field-specific message from ``int_field_error``."""
    minimum = 0 if widget_id in ZERO_OK_INPUTS else 1
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= minimum else None


def int_field_error(widget_id: str) -> str:
    """The rejection message for a field ``parse_int_field`` returned None for."""
    label = ENV_INT_INPUTS[widget_id][1]
    kind = "non-negative" if widget_id in ZERO_OK_INPUTS else "positive"
    return f"{label} must be a {kind} integer."


class EnvAutoSave:
    """Writes settings to the global .env, reporting the outcome on the screen's
    status line. Every auto-saving widget goes through here, so a write failure
    is surfaced in exactly one place instead of six near-identical try/excepts —
    and callers that need to act *after* a successful save (the workflows and
    tiering toggles, which also flip a live harness seam) read the bool rather
    than repeating the error handling."""

    def __init__(self, status: Callable[[str], None]) -> None:
        self._status = status

    def save(
        self, values: Mapping[str, str], *, drop: Iterable[str] = ()
    ) -> bool:
        """Write ``values`` (and retire ``drop``), returning False and posting the
        failure if the write blew up. Reports nothing on success — the caller owns
        the confirmation wording, which differs per field."""
        try:
            save_env_settings(dict(values), drop=tuple(drop))
        except Exception as exc:  # surface any write failure on the status line
            self._status(f"Save failed: {exc}")
            return False
        return True

    def commit(self, env_key: str, value: str) -> None:
        """Persist a single env var (retiring any deprecated aliases in the same
        save) and confirm it on the status line. The plain auto-save path."""
        if self.save({env_key: value}, drop=DROP_ON_SAVE.get(env_key, ())):
            self._status(f"✓ saved {env_key} · applies next launch")

    def commit_int(self, widget_id: str, raw: str) -> None:
        """Validate and persist one integer Input. A blank/invalid/out-of-range
        value is rejected with a field-specific message and nothing is written."""
        value = parse_int_field(widget_id, raw)
        if value is None:
            self._status(int_field_error(widget_id))
            return
        self.commit(ENV_INT_INPUTS[widget_id][0], str(value))
