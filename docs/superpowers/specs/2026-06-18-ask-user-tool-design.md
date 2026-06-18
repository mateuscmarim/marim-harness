# `ask_user` — Structured User Prompts Mid-Turn — Design

**Date:** 2026-06-18
**Status:** Approved design (pre-plan)

## Goal

Give the marim agent the ability to pause mid-turn and ask the user a
structured question with selectable options — single- or multi-select, with an
always-available free-text "Other" path — the same way Claude Code's
`AskUserQuestion` tool works. The user answers via a keyboard-driven picker in
the TUI; the agent receives the answer(s) and continues the turn.

## Motivation

Today the agent can only ask the user something by emitting plain assistant text
and waiting for the next typed reply. That gives no structured choices, no
multi-select, and no per-option descriptions. A structured picker lets the agent
offer concrete options (e.g. "which approach?", "which files to include?") and
get back a clean, machine-readable answer without the user free-typing.

## Scope

**In scope:** interactive TUI only. A new agent-facing `ask_user` tool, a
`Deps`-level async callback, and a new Textual modal that presents the
questions.

**Out of scope:**
- Headless / print mode (`-p`) gets a graceful "not available" return, no UI.
- Sub-agents do not get this tool (they run autonomously with no user attached).
- No persistence of prompts/answers — they live only in the turn's tool result.

## Architecture

Mirror the existing `request_approval` path exactly. That path is the proven
pattern for "a tool needs the user to decide something mid-turn":

- A tool calls an optional async callback stored on `Deps`.
- The TUI wires that callback to a method that does `push_screen_wait(Modal)`.
- When the callback is `None` (headless), the tool returns a graceful note and
  the agent proceeds on its own judgment.

This keeps the agent core UI-agnostic and isolates all widget code in one new
file.

### Rejected alternatives

- **Generalize `request_approval` into one generic UI-request channel.**
  Rejected: tangles the security-sensitive yes/no approval gate with open-ended
  prompting, complicating a path that is currently simple.
- **Status quo (plain assistant text + next typed reply).** Rejected as the
  deliverable: no structured picker, no multi-select, no per-option
  descriptions. It is the baseline this feature replaces for structured asks.

## Components

### 1. Tool: `ask_user` (in `src/marim_harness/tools/provider.py`)

Structured like the existing `update_tasks` tool, which already accepts a
`list[Task]` model parameter.

```python
ask_user(ctx: RunContext[Deps], questions: list[Question]) -> str
```

Parameter models (defined alongside the tool, Pydantic/dataclass style like
`Task`):

```python
class Choice:
    label: str
    description: str | None = None

class Question:
    question: str
    header: str = ""
    options: list[Choice] = []
    multi: bool = False
```

Rules:
- 1–4 questions per call. Each question needs at least one listed `Choice`.
- The "Other" (free-text) field is offered automatically on every question —
  the agent never specifies it.
- `multi=False` (default) → single-select; `multi=True` → multi-select.

**Schema + normalization.** The param is typed `list[Question]`, so pydantic-ai
builds a clean schema the model follows, validates the call, and retries a
malformed one (the agent loop already handles tool-arg retries). On top of that,
a `coerce_questions` pass normalizes the validated list defensively:
- A missing/empty `header` falls back to a truncation of `question`, so the
  result is always keyed by something stable.
- Choices with a blank `label` are dropped; questions left with no usable
  options (or a blank `question`) are dropped.
- The list is capped at 4 questions so a bad call can't open an unbounded modal.

**Return value:** always a compact JSON object string keyed by `header`:
- single-select question → the chosen option's `label`, or the typed "Other"
  text if the user chose "Other".
- multi-select question → a JSON list of chosen `label`s (plus the typed "Other"
  text if "Other" was selected).

Example return:
`{"DB": "Postgres", "Features": ["auth", "cache"]}`

The tool's docstring (its model-facing description) explains: use this to ask
the user to choose between concrete options when their decision changes what you
do next; do not use it for things you can decide yourself or verify in the code;
each question gets an automatic "Other" free-text path; the result is a JSON
object keyed by each question's `header`.

### 2. Callback on `Deps` (in `src/marim_harness/deps.py`)

```python
# (questions) -> {header: answer} where answer is str | list[str];
# None means the user cancelled. Wired by the TUI; None when headless.
AskUserFn = Callable[[list[Question]], Awaitable[Optional[dict]]]
```

Added as `ask_user: Optional[AskUserFn] = None` on `Deps`, next to
`request_approval`.

### 3. Modal: `AskUserModal` (new file `src/marim_harness/interfaces/tui/ask_user.py`)

A `ModalScreen` that **steps through the questions one at a time** — matching the
reference picker (one question with its option list visible; footer
`↑/↓ navigate · Enter select · Esc cancel`; a `Question 2/3` indicator shown
only when there is more than one question).

A free-text field (`Input`, labelled "or type your own") is always visible
beneath the options, so "Other" is offered on every question without a reveal
step.

- **Single-select** → `OptionList` of the choices. Arrows / number keys move the
  highlight; Enter (or selecting an entry) records the choice and advances.
  Typing in the free-text field and pressing Enter records that text instead and
  advances.
- **Multi-select** → Textual `SelectionList` (checkboxes) plus a `Confirm`
  button. Space toggles options; pressing `Confirm` records all checked labels
  (plus the free-text field's contents, if any) and advances.
- Each `Choice.description` renders as dim secondary text beneath its label.
- **Esc** cancels the entire prompt → the modal dismisses with a cancel
  sentinel (callback returns `None`).
- After the last question is confirmed, the modal dismisses with the full
  `{header: answer}` mapping.

### 4. Wiring (in `src/marim_harness/interfaces/tui/app.py`)

- In the same place `request_approval` is wired (`app.py:197`), set
  `self.harness.deps.ask_user = self._ask_user`.
- Add `async def _ask_user(self, questions) -> Optional[dict]:` that does
  `return await self.push_screen_wait(AskUserModal(questions))`. It runs inside
  the turn worker (same as `_request_approval`), so `push_screen_wait` is valid.

### 5. Registration (in `src/marim_harness/tools/provider.py`)

- Register `ask_user` on the main agent in `register()` (alongside
  `update_tasks` / `spawn_agent`).
- **Do not** add it to `_SUBAGENT_FNS` / `register_subagent` — sub-agents never
  get it.

## Data Flow

1. Agent calls `ask_user(questions=[...])` during a turn.
2. Tool validates/coerces the questions. If `ctx.deps.ask_user is None` →
   return the headless note (see Error Handling). Otherwise `await
   ctx.deps.ask_user(questions)`.
3. TUI shows `AskUserModal`, user navigates and answers (or presses Esc).
4. Modal dismisses with `{header: answer}` (or `None` if cancelled).
5. Tool serializes the mapping to JSON and returns it as the tool result; the
   agent reads it and continues the turn.

## Error Handling

- **No interactive UI (headless / callback is `None`)** → tool returns:
  `"Can't ask the user — no interactive UI here. Proceed with your best
  judgment."`
- **User cancelled (Esc, callback returns `None`)** → tool returns:
  `"User dismissed the prompt without answering."`
- **Empty / malformed `questions`** (e.g. zero questions, or a question with no
  options after coercion) → tool returns a short error string naming the problem
  rather than raising, so a bad call costs one tool round-trip, not the turn.

## Testing

- **Normalization unit tests** (`tests/test_ask_user.py`):
  - blank-label `Choice` dropped; option-less or blank-question `Question` dropped.
  - missing `header` → falls back to truncated question as the key.
  - more than 4 questions → capped at 4.
  - `answers_to_json`: single value stays a string, list value stays a list,
    object keyed by header.
- **Tool unit tests** (`tests/test_ask_user_tool.py`, FunctionModel pattern):
  - `ask_user is None` → returns the headless note.
  - callback returns `None`/empty → returns the cancelled note.
  - callback returns answers → tool returns the header-keyed JSON.
  - empty `questions` (after normalization) → error string, no raise.
  - `ask_user` is registered on the main agent and NOT on sub-agents.
- **Modal tests** via Textual `run_test()` pilot (mirroring `test_approval.py`):
  - single-select: highlight + Enter returns the chosen label.
  - multi-select: Space toggles options, `Confirm` returns the list.
  - free-text: typing in the field + Enter returns the typed text.
  - Esc returns the cancel sentinel (`None`).
  - multi-question: answering Q1 advances to Q2; final dismiss carries both
    answers keyed by header.

## Global Constraints

- TUI-only feature; the headless path is untouched except for the graceful
  "not available" return.
- Tool is registered on the main agent only, never on sub-agents.
- Result returned to the agent is always a JSON object keyed by each question's
  `header`.
- Follow existing patterns: tool defined like `update_tasks`/`spawn_agent`;
  callback added to `Deps` like `request_approval`; modal built like
  `ModelPickerModal` (OptionList / SelectionList, `push_screen_wait`).
