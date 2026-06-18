# `ask_user` Structured User-Prompt Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the main agent pause mid-turn and ask the user a structured question (single- or multi-select, with an always-available free-text field), mirroring Claude Code's `AskUserQuestion`.

**Architecture:** A new `ask_user` tool calls an optional async callback on `Deps` (exactly like `request_approval`); the TUI wires that callback to `push_screen_wait(AskUserModal(...))`. Pure data/normalization lives in a new root module `ask_user.py`; the modal lives in `interfaces/tui/ask_user.py`. Headless leaves the callback `None`, so the tool returns a graceful note.

**Tech Stack:** Python 3, pydantic-ai (tool + `RunContext[Deps]`), Textual (`ModalScreen`, `OptionList`, `SelectionList`, `Input`), pytest + `anyio`.

## Global Constraints

- TUI-only feature; the headless path is untouched except for the tool's graceful "not available" return.
- The `ask_user` tool is registered on the **main agent only**, never on sub-agents (not added to `_SUBAGENT_FNS`).
- The tool's result to the agent is **always a JSON object keyed by each question's `header`**: single-select → a string; multi-select → a list of strings.
- Headless / no-callback return string (verbatim): `Can't ask the user — no interactive UI here. Proceed with your best judgment.`
- Cancelled (Esc / callback returns falsy) return string (verbatim): `User dismissed the prompt without answering.`
- Empty-after-normalization return string (verbatim): `ask_user needs at least one question, each with at least one option.`
- Follow existing patterns: models like `tasks.py` (`@dataclass` + coercion); tool like `update_tasks`/`spawn_agent`; callback added to `Deps` like `request_approval`; modal like `model_picker.py`.
- Commit each task separately. Stage only named files — never `git add -A` (the tree carries unrelated untracked files: `.marim/`, `.coverage`, `coverage.xml`, and unrelated docs).
- Commit message trailers (every commit):
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x
  ```
- Gates after each task: `uv run ruff check src tests && uv run pyright src && uv run pytest`.

---

### Task 1: Data model + normalization (`ask_user.py`)

A pure module: the `Choice`/`Question` dataclasses, a `coerce_questions` normalizer, and an `answers_to_json` serializer. No I/O, no UI. Both the tool (Task 2) and the modal (Task 3) import from here.

**Files:**
- Create: `src/marim_harness/ask_user.py`
- Test: `tests/test_ask_user.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces:
  - `@dataclass Choice(label: str, description: Optional[str] = None)`
  - `@dataclass Question(question: str, header: str = "", options: list[Choice] = [], multi: bool = False)`
  - `coerce_questions(questions: list[Question]) -> list[Question]` — drops blank-label choices, drops option-less / blank-question questions, fills a blank header from the question text, caps the list at 4.
  - `answers_to_json(answers: dict) -> str` — compact JSON of the `{header: answer}` mapping.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ask_user.py
from marim_harness.ask_user import (
    Choice,
    Question,
    answers_to_json,
    coerce_questions,
)


def test_blank_label_choices_dropped():
    q = Question("Pick", "Pick", [Choice("ok"), Choice("  "), Choice("")])
    [out] = coerce_questions([q])
    assert [c.label for c in out.options] == ["ok"]


def test_question_with_no_usable_options_dropped():
    good = Question("Pick", "Pick", [Choice("ok")])
    empty = Question("Empty", "Empty", [Choice("  ")])
    assert [q.header for q in coerce_questions([empty, good])] == ["Pick"]


def test_blank_question_text_dropped():
    assert coerce_questions([Question("   ", "H", [Choice("ok")])]) == []


def test_missing_header_falls_back_to_question_text():
    long_q = "Which database engine should we use for this service?"
    [out] = coerce_questions([Question(long_q, "", [Choice("Postgres")])])
    assert out.header
    assert long_q.startswith(out.header)
    assert len(out.header) <= 40


def test_questions_capped_at_four():
    qs = [Question(f"q{i}", f"h{i}", [Choice("x")]) for i in range(7)]
    assert len(coerce_questions(qs)) == 4


def test_answers_to_json_preserves_single_and_multi_shapes():
    out = answers_to_json({"DB": "Postgres", "Features": ["auth", "cache"]})
    import json
    assert json.loads(out) == {"DB": "Postgres", "Features": ["auth", "cache"]}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ask_user.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.ask_user'`.

- [ ] **Step 3: Implement `src/marim_harness/ask_user.py`**

```python
"""Structured user prompts: the data model and serialization behind the
``ask_user`` tool. A prompt is a list of :class:`Question`s, each offering
:class:`Choice`s plus an always-available free-text field; the user's answers
come back as a ``{header: answer}`` mapping that this module renders to JSON for
the agent. Pure data + normalization — no I/O, no UI. The TUI modal and the tool
both import these types.
"""

import json
from dataclasses import dataclass, field
from typing import Optional

# Most questions the modal will present in one prompt — a bad call can't open an
# unbounded modal, and it mirrors AskUserQuestion's 1–4 range.
_MAX_QUESTIONS = 4
# How much of the question text to use as a fallback header (the result's key)
# when the model omits one.
_HEADER_FALLBACK_CHARS = 40


@dataclass
class Choice:
    """One selectable option. ``description`` is optional secondary text shown
    dim beneath the label in the picker."""

    label: str
    description: Optional[str] = None


@dataclass
class Question:
    """One question in a prompt. ``header`` is the short key the answer is
    returned under (falls back to the question text if blank); ``multi`` makes it
    multi-select."""

    question: str
    header: str = ""
    options: list[Choice] = field(default_factory=list)
    multi: bool = False


def _clean_choice(choice: Choice) -> Optional[Choice]:
    """Drop a choice with a blank label; trim a blank description to None."""
    label = (choice.label or "").strip()
    if not label:
        return None
    desc = (choice.description or "").strip() or None
    return Choice(label=label, description=desc)


def _fallback_header(question: str) -> str:
    """A stable key from the question text when no header was given."""
    return " ".join(question.split())[:_HEADER_FALLBACK_CHARS]


def _clean_question(q: Question) -> Optional[Question]:
    """Normalize one question, or None to drop it. Drops blank-label choices and
    then the whole question if it has no text or no surviving options; fills a
    blank header from the question text."""
    question = (q.question or "").strip()
    if not question:
        return None
    options = [c for c in (_clean_choice(o) for o in q.options) if c is not None]
    if not options:
        return None
    header = (q.header or "").strip() or _fallback_header(question)
    return Question(question=question, header=header, options=options, multi=bool(q.multi))


def coerce_questions(questions: list[Question]) -> list[Question]:
    """Normalize a validated question list defensively: drop malformed entries,
    fill blank headers, and cap the count. pydantic-ai has already validated the
    shapes; this guards the values."""
    cleaned = [q for q in (_clean_question(q) for q in questions) if q is not None]
    return cleaned[:_MAX_QUESTIONS]


def answers_to_json(answers: dict) -> str:
    """Render the ``{header: answer}`` mapping the modal returns to a compact
    JSON object string for the agent — single-select answers are strings,
    multi-select answers are lists."""
    return json.dumps(answers, ensure_ascii=False)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ask_user.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Run the gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: ruff/pyright clean; full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/ask_user.py tests/test_ask_user.py
git commit -m "feat(ask-user): add Question/Choice model + normalization

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 2: `ask_user` tool + `Deps` callback + registration

Add the `AskUserFn` callback type and `ask_user` field to `Deps`, implement the `ask_user` tool, and register it on the main agent only.

**Files:**
- Modify: `src/marim_harness/deps.py` (add `AskUserFn`, add `ask_user` field)
- Modify: `src/marim_harness/tools/provider.py` (add `ask_user` tool + register it)
- Test: `tests/test_ask_user_tool.py`

**Interfaces:**
- Consumes: `Question`, `coerce_questions`, `answers_to_json` from Task 1.
- Produces:
  - `Deps.ask_user: Optional[AskUserFn]` where `AskUserFn = Callable[[list[Question]], Awaitable[Optional[dict]]]` (returns `{header: answer}` or `None` if cancelled).
  - tool `async def ask_user(ctx: RunContext[Deps], questions: list[Question]) -> str` registered on the main agent (Task 4 wires `deps.ask_user`).

- [ ] **Step 1: Add the `AskUserFn` type and `ask_user` field to `Deps`**

In `src/marim_harness/deps.py`, add the import near the other concrete imports (after `from .jobs import JobRegistry`):

```python
from .ask_user import Question
```

Add the type alias next to the other callback aliases (after the `BackgroundAgentRunner` block):

```python
# (questions) -> {header: answer}, where answer is a str (single-select) or a
# list[str] (multi-select); None when the user cancelled. Wired by the TUI; None
# when there's no interactive UI (headless), so the tool degrades gracefully.
AskUserFn = Callable[[list[Question]], Awaitable[Optional[dict]]]
```

Add the field to the `Deps` dataclass, directly beneath `request_approval`:

```python
    # Lets the ask_user tool put a structured question to the user mid-turn. None
    # when headless (the tool then returns a graceful note).
    ask_user: Optional[AskUserFn] = None
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_ask_user_tool.py
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.deps import Deps
from marim_harness.tools.provider import BuiltinToolProvider

_QUESTIONS = [
    {
        "question": "Which database?",
        "header": "DB",
        "options": [{"label": "Postgres"}, {"label": "SQLite"}],
    }
]


def _agent() -> Agent:
    agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[])), deps_type=Deps)
    BuiltinToolProvider().register(agent)
    return agent


def _call_tool(tool_name: str, args: dict):
    """A FunctionModel that calls ``tool_name`` once, then echoes its return."""
    state: dict = {}
    captured: dict = {}

    def model(messages, info):
        if not state:
            state["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart":
                    captured["ret"] = str(p.content)
        return ModelResponse(parts=[TextPart(content=captured.get("ret", ""))])

    return FunctionModel(model), captured


def test_ask_user_headless_returns_note(tmp_path):
    deps = Deps(workspace_root=tmp_path)  # ask_user is None
    agent = _agent()
    model, captured = _call_tool("ask_user", {"questions": _QUESTIONS})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert "no interactive UI" in captured["ret"]


def test_ask_user_cancelled_returns_note(tmp_path):
    async def cancel(questions):
        return None

    deps = Deps(workspace_root=tmp_path)
    deps.ask_user = cancel
    agent = _agent()
    model, captured = _call_tool("ask_user", {"questions": _QUESTIONS})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert "dismissed" in captured["ret"]


def test_ask_user_returns_header_keyed_json(tmp_path):
    import json

    async def answer(questions):
        return {"DB": "Postgres"}

    deps = Deps(workspace_root=tmp_path)
    deps.ask_user = answer
    agent = _agent()
    model, captured = _call_tool("ask_user", {"questions": _QUESTIONS})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert json.loads(captured["ret"]) == {"DB": "Postgres"}


def test_ask_user_empty_questions_returns_error(tmp_path):
    async def answer(questions):
        raise AssertionError("callback must not run for empty input")

    deps = Deps(workspace_root=tmp_path)
    deps.ask_user = answer
    agent = _agent()
    # a question whose only option has a blank label normalizes away to nothing
    model, captured = _call_tool(
        "ask_user",
        {"questions": [{"question": "x", "header": "x", "options": [{"label": " "}]}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert "at least one question" in captured["ret"]


def test_ask_user_registered_on_main_not_subagent(tmp_path):
    from marim_harness.tools.provider import _SUBAGENT_FNS

    deps = Deps(workspace_root=tmp_path)
    agent = _agent()
    names = {t.name for t in agent._function_toolset.tools.values()}
    assert "ask_user" in names
    assert "ask_user" not in _SUBAGENT_FNS
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ask_user_tool.py -q`
Expected: FAIL — `ask_user` tool not defined / not registered.

- [ ] **Step 4: Implement the tool in `src/marim_harness/tools/provider.py`**

Add to the imports at the top (extend the existing `from ..tasks import ...` area):

```python
from ..ask_user import Question, answers_to_json, coerce_questions
```

Add the return-string constants near the top of the module (after the imports, beside other module constants):

```python
_ASK_USER_EMPTY = "ask_user needs at least one question, each with at least one option."
_ASK_USER_NO_UI = (
    "Can't ask the user — no interactive UI here. Proceed with your best judgment."
)
_ASK_USER_CANCELLED = "User dismissed the prompt without answering."
```

Add the tool function (place it next to `update_tasks`):

```python
async def ask_user(ctx: RunContext[Deps], questions: list[Question]) -> str:
    """Ask the user to choose between concrete options, pausing your turn until
    they answer. Use this only when the user's decision changes what you do next
    and you can't settle it yourself or from the code — not for things you can
    verify or reasonably assume.

    Pass 1–4 questions. Each is {question, header, options, multi}: `header` is a
    short label the answer is returned under; `options` is a list of {label,
    description} choices (description optional); set `multi` true to let the user
    pick several. A free-text field is offered on every question automatically —
    don't add an "other" option yourself.

    Returns a JSON object keyed by each question's `header`: a single-select
    answer is the chosen label (or the user's typed free text); a multi-select
    answer is a list of chosen labels. If there's no interactive UI, or the user
    dismisses the prompt, you get a short note instead — proceed with your best
    judgment."""
    coerced = coerce_questions(questions)
    if not coerced:
        return _ASK_USER_EMPTY
    if ctx.deps.ask_user is None:
        return _ASK_USER_NO_UI
    answers = await ctx.deps.ask_user(coerced)
    if not answers:
        return _ASK_USER_CANCELLED
    return answers_to_json(answers)
```

Register it on the main agent in `BuiltinToolProvider.register`, directly after `agent.tool(update_tasks)`:

```python
        agent.tool(ask_user)
```

Do **not** add `ask_user` to `_SUBAGENT_FNS` or `register_subagent`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ask_user_tool.py -q`
Expected: PASS (5 passed).

> If `agent._function_toolset.tools` or `_SUBAGENT_FNS` differs from the live structure, discover the real accessor (e.g. `grep -n "_SUBAGENT_FNS\|_function_toolset\|def tools" src/marim_harness/tools/provider.py`) and adjust the registration assertion in the test to match — the production registration line (`agent.tool(ask_user)`) stays as written.

- [ ] **Step 6: Run the gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: ruff/pyright clean; full suite green.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/deps.py src/marim_harness/tools/provider.py tests/test_ask_user_tool.py
git commit -m "feat(ask-user): add ask_user tool + Deps callback (main agent only)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 3: `AskUserModal` (Textual picker)

The keyboard-driven modal. Steps through the questions one at a time, dismisses with the `{header: answer}` mapping (or `None` if cancelled).

**Files:**
- Create: `src/marim_harness/interfaces/tui/ask_user.py`
- Test: `tests/test_ask_user_modal.py`

**Interfaces:**
- Consumes: `Question`, `Choice` from `marim_harness.ask_user` (Task 1).
- Produces: `AskUserModal(questions: list[Question])` — a `ModalScreen[Optional[dict]]` that dismisses with `{header: str | list[str]}` or `None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ask_user_modal.py
import pytest
from textual.app import App

from marim_harness.ask_user import Choice, Question
from marim_harness.interfaces.tui.ask_user import AskUserModal


class _Harness(App):
    def __init__(self, questions):
        super().__init__()
        self._questions = questions
        self.result = "unset"

    def on_mount(self) -> None:
        self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await self.push_screen_wait(AskUserModal(self._questions))


@pytest.mark.anyio
async def test_single_select_returns_highlighted_label():
    qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # highlighted is index 0
        await pilot.pause()
    assert app.result == {"Pick": "Alpha"}


@pytest.mark.anyio
async def test_single_select_second_option():
    qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == {"Pick": "Beta"}


@pytest.mark.anyio
async def test_multi_select_confirm_returns_list():
    qs = [Question("Pick many", "Feat", [Choice("a"), Choice("b")], multi=True)]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space")  # toggle highlighted (index 0)
        await pilot.click("#ask-confirm")
        await pilot.pause()
    assert app.result == {"Feat": ["a"]}


@pytest.mark.anyio
async def test_free_text_answer():
    qs = [Question("Pick one", "Pick", [Choice("Alpha")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#ask-other").focus()
        await pilot.pause()
        app.query_one("#ask-other").value = "custom thing"
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == {"Pick": "custom thing"}


@pytest.mark.anyio
async def test_escape_cancels():
    qs = [Question("Pick one", "Pick", [Choice("Alpha")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@pytest.mark.anyio
async def test_multi_question_steps_and_collects():
    qs = [
        Question("First?", "One", [Choice("a1"), Choice("a2")]),
        Question("Second?", "Two", [Choice("b1"), Choice("b2")]),
    ]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # Q1 -> a1
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")  # Q2 -> b2
        await pilot.pause()
    assert app.result == {"One": "a1", "Two": "b2"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ask_user_modal.py -q`
Expected: FAIL — `ModuleNotFoundError` for `interfaces.tui.ask_user` / `AskUserModal`.

- [ ] **Step 3: Implement `src/marim_harness/interfaces/tui/ask_user.py`**

```python
"""The modal behind the ``ask_user`` tool: steps the user through a prompt's
questions one at a time and dismisses with a ``{header: answer}`` mapping (or
None if cancelled). Single-select uses an OptionList; multi-select a
SelectionList with a Confirm button; a free-text Input is always visible so
"Other" is offered on every question."""

from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ...ask_user import Choice, Question


def _option_prompt(choice: Choice) -> Text:
    """An option's rendered prompt: the label, with any description dim beneath."""
    text = Text(choice.label)
    if choice.description:
        text.append(f"\n  {choice.description}", style="dim")
    return text


class AskUserModal(ModalScreen[Optional[dict]]):
    """Dismisses with ``{header: str | list[str]}`` for every question, or None
    if the user pressed Escape."""

    CSS = """
    AskUserModal {
        align: center middle;
    }
    #ask-box {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
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
        with Vertical(id="ask-box"):
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
            sel.focus()
        else:
            options = OptionList(id="ask-options")
            await body.mount(options)
            for i, opt in enumerate(q.options):
                options.add_option(Option(_option_prompt(opt), id=str(i)))
            options.highlighted = 0
            options.focus()

    def _record(self, answer) -> None:
        """Store the current question's answer, then advance or dismiss."""
        q = self._questions[self._index]
        self._answers[q.header] = answer
        self._index += 1
        if self._index >= len(self._questions):
            self.dismiss(self._answers)
        else:
            self.run_worker(self._show_question())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        q = self._questions[self._index]
        if q.multi or event.option.id is None:
            return
        self._record(q.options[int(event.option.id)].label)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        q = self._questions[self._index]
        if q.multi:
            self._confirm_multi()
            return
        text = event.value.strip()
        if text:
            self._record(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ask-confirm":
            self._confirm_multi()

    def _confirm_multi(self) -> None:
        """Collect the checked labels plus any free-text, then advance."""
        q = self._questions[self._index]
        sel = self.query_one("#ask-select", SelectionList)
        labels = [q.options[i].label for i in sel.selected]
        other = self.query_one("#ask-other", Input).value.strip()
        if other:
            labels.append(other)
        self._record(labels)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ask_user_modal.py -q`
Expected: PASS (6 passed).

> Textual widget message names and `SelectionList.selected` are assumed from the existing `model_picker.py` usage. If a handler doesn't fire or an attribute differs in the installed Textual version, discover the real API (`uv run python -c "import textual; print(textual.__version__)"`, then check the `SelectionList`/`OptionList` message classes) and adjust the handler names/attribute access to match — keep the behavior (single-select records on selection, multi-select records on Confirm, Esc cancels) identical.

- [ ] **Step 5: Run the gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: ruff/pyright clean; full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/ask_user.py tests/test_ask_user_modal.py
git commit -m "feat(ask-user): add AskUserModal picker (single/multi/free-text)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 4: Wire the callback into the TUI app

Connect the tool's `Deps.ask_user` callback to the modal, in the same place `request_approval` is wired.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (import modal; set `deps.ask_user`; add `_ask_user`)
- Test: `tests/test_app.py` (append)

**Interfaces:**
- Consumes: `AskUserModal` (Task 3); `Deps.ask_user` (Task 2).
- Produces: `HarnessApp._ask_user(questions) -> Optional[dict]`, assigned to `self.harness.deps.ask_user` in `__init__`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_app.py

@pytest.mark.anyio
async def test_ask_user_callback_is_wired(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.harness.deps.ask_user == app._ask_user


@pytest.mark.anyio
async def test_ask_user_callback_shows_modal_and_returns_answer(tmp_path: Path):
    from marim_harness.ask_user import Choice, Question

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
        worker = app.run_worker(app._ask_user(qs))
        await pilot.pause()
        await pilot.press("enter")  # selects highlighted "Alpha"
        await pilot.pause()
        assert worker.result == {"Pick": "Alpha"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_app.py -q -k ask_user`
Expected: FAIL — `_ask_user` / `deps.ask_user` not set.

- [ ] **Step 3: Wire it in `src/marim_harness/interfaces/tui/app.py`**

Add the import beside the other modal imports (after `from .approval import ApprovalModal`):

```python
from .ask_user import AskUserModal
```

In `HarnessApp.__init__`, directly after `self.harness.deps.request_approval = self._request_approval`:

```python
        self.harness.deps.ask_user = self._ask_user
```

Add the method beside `_request_approval`:

```python
    async def _ask_user(self, questions):
        """Put a structured question to the user and return their {header:
        answer} mapping, or None if they dismissed it. Runs inside the turn
        worker, so push_screen_wait is valid (same as _request_approval)."""
        return await self.push_screen_wait(AskUserModal(questions))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_app.py -q -k ask_user`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: ruff/pyright clean; full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py tests/test_app.py
git commit -m "feat(ask-user): wire ask_user callback to the TUI modal

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

## Self-Review

**Spec coverage:**
- Tool `ask_user` with `Question`/`Choice` models, 1–4 questions, auto free-text → Tasks 1 + 2. ✅
- Always-JSON-object-keyed-by-header return; single→string, multi→list → Task 1 (`answers_to_json`) + Task 2, asserted in `test_ask_user_tool.py`. ✅
- `Deps.ask_user` callback like `request_approval`; headless `None` → graceful note → Task 2. ✅
- Cancelled → dismissed note; empty → error string → Task 2. ✅
- Modal: stepped questions, single-select OptionList, multi-select SelectionList + Confirm, always-visible free-text, descriptions dim, Esc cancels → Task 3. ✅
- Wiring at the `request_approval` site; runs in turn worker → Task 4. ✅
- Main agent only, not sub-agents → Task 2 (registration + `test_ask_user_registered_on_main_not_subagent`). ✅
- Testing: normalization unit tests, tool unit tests, modal pilot tests, wiring tests → Tasks 1–4. ✅

**Placeholder scan:** No TBD/TODO; every code step carries complete code; the two `>` notes are verification fallbacks (discover-the-real-API), not deferred work.

**Type consistency:** `Question(question, header, options, multi)` and `Choice(label, description)` are used identically across Tasks 1–4. `coerce_questions(list[Question]) -> list[Question]`, `answers_to_json(dict) -> str`, `AskUserFn = Callable[[list[Question]], Awaitable[Optional[dict]]]`, and `AskUserModal(list[Question]) -> ModalScreen[Optional[dict]]` match between producer and consumer tasks. Modal widget ids (`#ask-other`, `#ask-confirm`, `#ask-select`, `#ask-options`) match between the implementation and its tests.

## Execution Handoff

Plan complete. Recommended: subagent-driven execution (fresh subagent per task, review between tasks), consistent with the prior feature.
