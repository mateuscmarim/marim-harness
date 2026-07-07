# src/marim_harness/interfaces/tui/plan_card.py
"""The inline Plan card behind ``present_plan``: shows the plan's summary and
numbered steps, then the execution choices, and resolves with the chosen
label. Mounted above the status bar like the ask/approval panels (not a modal),
so the transcript stays scrollable while the user decides. The full plan also
persists in the PlanScreen overlay (Ctrl+P); this card is the deliberate
'here's my plan — how should I run it?' moment in the transcript."""

from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ...ask_user import Choice
from .interaction_panel import InteractionPanel

# Returned on Escape / dismissal — must match the "keep planning" label in
# tools/planning_tools._PLAN_CHOICES so present_plan maps it to "stay in plan mode".
_DISMISS_LABEL = "Keep planning"


def _steps_markup(steps: list[str]) -> str:
    return "\n".join(f"[$accent]{i}.[/] {s}" for i, s in enumerate(steps, 1))


class PlanCard(InteractionPanel):
    """Resolves with the chosen execution-choice label (str), or the
    "Keep planning" label if dismissed with Escape."""

    DEFAULT_CSS = """
    #plan-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #plan-summary { margin-bottom: 1; }
    #plan-steps { margin-bottom: 1; }
    #plan-choices { height: auto; max-height: 8; }
    """

    BINDINGS = [("escape", "dismiss_card", "Keep planning")]

    def __init__(self, summary: str, steps: list[str], choices: list[Choice]) -> None:
        super().__init__()
        self._summary = summary
        self._steps = steps
        self._choices = choices

    def compose(self) -> ComposeResult:
        yield Static("Plan", id="plan-title")
        yield Static(self._summary, id="plan-summary")
        yield Static(Content.from_markup(_steps_markup(self._steps)), id="plan-steps")
        options = OptionList(id="plan-choices")
        yield options

    def on_mount(self) -> None:
        options = self.query_one("#plan-choices", OptionList)
        for i, choice in enumerate(self._choices):
            suffix = f"\n  [dim]{choice.description}[/]" if choice.description else ""
            prompt = Content.from_markup(f"{choice.label}{suffix}")
            options.add_option(Option(prompt, id=str(i)))
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # A stale event can land after the future already resolved (mirrors the
        # guard in AskUserPanel) — the panel is going away, so ignore it.
        if self.result.done() or event.option.id is None:
            return
        self.resolve(self._choices[int(event.option.id)].label)

    def action_dismiss_card(self) -> None:
        self.resolve(_DISMISS_LABEL)
