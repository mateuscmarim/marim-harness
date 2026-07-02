"""The inline panel behind the ``ask_user`` tool: steps the user through a
prompt's questions one at a time and resolves with a ``{header: answer}``
mapping (or None if cancelled). Single-select uses an OptionList; multi-select
a SelectionList with a Confirm button; a free-text Input is always visible so
"Other" is offered on every question. Mounted above the status bar (not a
modal) so the transcript stays scrollable while the question is pending."""


from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Input, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ...ask_user import Choice, Question
from .interaction_panel import InteractionPanel


def _option_prompt(choice: Choice) -> Text:
    """An option's rendered prompt: the label, with any description dim beneath."""
    text = Text(choice.label)
    if choice.description:
        text.append(f"\n  {choice.description}", style="dim")
    return text


class AskUserPanel(InteractionPanel):
    """Resolves with ``{header: str | list[str]}`` for every question, or None
    if the user pressed Escape."""

    DEFAULT_CSS = """
    #ask-progress {
        color: $text-muted;
    }
    #ask-question {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #ask-body {
        height: auto;
        max-height: 18;
    }
    #ask-other-label {
        color: $text-muted;
        margin-top: 1;
    }
    #ask-confirm {
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, questions: list[Question]) -> None:
        super().__init__()
        self._questions = questions
        self._index = 0
        self._answers: dict = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="ask-progress")
        yield Static("", id="ask-question")
        yield Vertical(id="ask-body")
        yield Static("Or type your own answer:", id="ask-other-label")
        yield Input(placeholder="type a custom answer…", id="ask-other")
        yield Button("Confirm selection", id="ask-confirm", variant="primary")

    def on_mount(self) -> None:
        self.run_worker(self._show_question())

    async def _show_question(self) -> None:
        """Render the current question: progress line, prompt, the option widget
        (OptionList for single-select, SelectionList for multi), and toggle the
        Confirm button (multi-select only)."""
        q = self._questions[self._index]
        total = len(self._questions)
        progress = f"Question {self._index + 1}/{total}" if total > 1 else ""
        self.query_one("#ask-progress", Static).update(progress)
        self.query_one("#ask-question", Static).update(q.question)

        body = self.query_one("#ask-body", Vertical)
        await body.remove_children()
        other = self.query_one("#ask-other", Input)
        other.value = ""
        confirm = self.query_one("#ask-confirm", Button)
        confirm.display = q.multi

        if q.multi:
            sel: SelectionList[int] = SelectionList(id="ask-select")
            await body.mount(sel)
            for i, opt in enumerate(q.options):
                sel.add_option((_option_prompt(opt), i))
            sel.highlighted = 0
            sel.focus()
        else:
            options = OptionList(id="ask-options")
            await body.mount(options)
            for i, opt in enumerate(q.options):
                options.add_option(Option(_option_prompt(opt), id=str(i)))
            options.highlighted = 0
            options.focus()

    def _record(self, answer: str | list[str]) -> None:
        """Store the current question's answer, then advance or resolve."""
        q = self._questions[self._index]
        self._answers[q.header] = answer
        self._index += 1
        if self._index >= len(self._questions):
            self.resolve(self._answers)
        else:
            self.run_worker(self._show_question())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # A second event can land after the final _record already resolved
        # ``result`` (e.g. one queued right before the panel is torn down by
        # run_panel's finally) — _index is already past the end of
        # _questions at that point, so indexing it would IndexError. The
        # panel is going away regardless, so just ignore the stale event.
        if self.result.done():
            return
        q = self._questions[self._index]
        if q.multi or event.option.id is None:
            return
        self._record(q.options[int(event.option.id)].label)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # See on_option_list_option_selected: guard against a stale event
        # landing after the future is already resolved.
        if self.result.done():
            return
        q = self._questions[self._index]
        if q.multi:
            self._confirm_multi()
            return
        text = event.value.strip()
        if text:
            self._record(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # See on_option_list_option_selected: guard against a stale event
        # landing after the future is already resolved (this is the only
        # path into _confirm_multi besides on_input_submitted, both guarded).
        if self.result.done():
            return
        if event.button.id == "ask-confirm":
            self._confirm_multi()

    def _confirm_multi(self) -> None:
        """Collect the checked labels plus any free-text, then advance.

        If nothing is checked and no free-text is present the submission is
        ignored: the user must select at least one option, type free-text, or
        press Escape to cancel.
        """
        q = self._questions[self._index]
        sel = self.query_one("#ask-select", SelectionList)
        labels = [q.options[i].label for i in sel.selected]
        other = self.query_one("#ask-other", Input).value.strip()
        if not labels and not other:
            return
        if other:
            labels.append(other)
        self._record(labels)

    def action_cancel(self) -> None:
        self.resolve(None)
