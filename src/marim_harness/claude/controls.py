"""The control requests that carry marim's mode, model and thinking switches
to a running Claude Code process — the pure half.

Before this module marim's ``/mode``, ``/model`` and ``/think`` reached Claude
Code only by proxy: plan mode was enforced by denying every mutating
``can_use_tool`` (still the hard guarantee — see ``approvals.py``), a model
switch respawned the process with a new ``--model``, and the thinking level
was a documented no-op. Claude Code's stream-json protocol has a control
request for each of them (``set_permission_mode``, ``set_model``,
``set_max_thinking_tokens``, ``apply_flag_settings``); this module maps
marim's vocabulary onto their wire values and remembers what the process
last acknowledged so the adapter sends only what changed. ``process.py``
holds the effectful wrappers, ``config/claude_cli_model.py`` decides when
to sync.

Everything here is side-effect-free and unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..runtime.permissions import Mode

# marim mode → Claude Code permission mode. ``plan`` is Claude's own plan
# mode (its system prompt tells the model to research and present a plan,
# and the CLI refuses writes on its side too); marim's broker keeps denying
# every mutating tool on top, so the read-only guarantee never depends on
# the CLI honouring the switch. ``auto`` and ``ask`` both map to ``default``:
# under it the CLI asks marim (over ``can_use_tool``) before every gated
# tool, and marim's broker answers per its own mode — auto accepts, ask
# prompts. Never ``bypassPermissions`` (the broker would stop being asked)
# and never ``acceptEdits``/``dontAsk`` (they pre-decide what the broker
# should decide).
_PERMISSION_MODES: dict[Mode, str] = {
    Mode.plan: "plan",
    Mode.auto: "default",
    Mode.ask: "default",
}


def claude_permission_mode(mode: Mode) -> str:
    """The ``set_permission_mode`` value for a marim ``Mode``."""
    return _PERMISSION_MODES[mode]


@dataclass(frozen=True)
class ThinkingControls:
    """One marim thinking level as Claude Code sees it.

    Claude Code has two thinking levers and which one a model honours depends
    on the model: token-budget models (Sonnet 4.5, Haiku 4.5, ...) read
    ``max_thinking_tokens`` and ignore effort; adaptive-thinking models (the
    Claude 5 family, Opus 4.6+, Sonnet 4.6) read the ``effortLevel`` setting
    and ignore the budget. The adapter cannot know which kind the session's
    model is (the CLI decides, and ``/model`` may switch families
    mid-session), so a level always sends BOTH — each model applies the one
    it understands and the CLI silently drops the other.

    ``budget``: ``set_max_thinking_tokens`` — a positive budget enables
    extended thinking with that ceiling (the CLI clamps it to ≥ 1024 and
    below the model's max output), ``0`` disables it.
    ``effort``: ``apply_flag_settings {settings: {effortLevel}}`` — one of the
    CLI's effort names. An adaptive-thinking model cannot have thinking
    switched off through the CLI (verified live: a budget of 0 leaves
    ``get_settings`` unchanged), so ``off`` carries the lowest effort — the
    nearest thing to "no reasoning" such a model offers — rather than
    resetting effort to the session default, which on Opus is ``high``.
    """

    budget: int
    effort: str | None


# The level → controls table (covers every ``thinking.THINKING_LEVELS`` entry;
# tests pin the two together). Budgets are round extended-thinking ceilings
# (the CLI's floor is 1024) that grow with the level; effort names are the
# CLI's own vocabulary, which happens to spell the four upper marim levels
# identically. ``minimal`` has no effort name of its own, so it takes the
# lowest one — on an adaptive model it is "as little thinking as possible",
# the closest the CLI offers.
_THINKING_CONTROLS: dict[str, ThinkingControls] = {
    "off": ThinkingControls(budget=0, effort="low"),
    "minimal": ThinkingControls(budget=1024, effort="low"),
    "low": ThinkingControls(budget=4096, effort="low"),
    "medium": ThinkingControls(budget=16384, effort="medium"),
    "high": ThinkingControls(budget=32768, effort="high"),
    "xhigh": ThinkingControls(budget=65536, effort="xhigh"),
}


def thinking_controls(level: str | None) -> ThinkingControls | None:
    """The controls for a marim thinking level; ``None`` for an unset level
    (leave whatever the CLI's own settings say — marim has no opinion) or an
    unknown one (a persisted typo degrades to "unset", never to a wrong
    budget — the same graceful-degrade contract as ``resolve_thinking``)."""
    return _THINKING_CONTROLS.get(level) if level else None


@dataclass
class ControlState:
    """What the process last acknowledged, so a sync sends only the deltas.

    ``mode`` starts as ``None`` (never ``Mode``): a fresh process runs in the
    CLI's default permission mode whatever marim's mode is, so the first
    sync after every spawn must send it. ``model`` is seeded from the launch
    ``--model`` (``None`` = the CLI's default), which IS what the process
    runs, so an unchanged model costs nothing. ``thinking`` starts as
    ``None`` = the CLI's own default; an unset marim level matches that and
    sends nothing, a set one differs and is sent once.
    """

    mode: Mode | None = None
    model: str | None = None
    thinking: ThinkingControls | None = None
