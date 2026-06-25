# ask_user Chat Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render an `ask_user` tool call in the chat transcript as a clean Q→A summary (pairing each question with the chosen answer) across pending / answered / cancelled states, instead of the raw args-repr + JSON-blob it shows today.

**Architecture:** A new pure, side-effect-free formatter module (`ask_user_render.py`) parses the call's `args["questions"]` + result JSON into per-question render data and produces the collapsed-title tail and the expanded body as plain strings. `ToolCallWidget` (`tools.py`) gains two small `ask_user` branches — one in `_summary()` (title) and one in `_render_body()` (body) — that call the formatter and assemble the styled `Content`. This mirrors the existing `edit_file`/`write_file`/`update_tasks` special-case pattern: pure helper + thin render layer.

**Tech Stack:** Python ≥3.10, Textual (`Content`), Rich, pytest + Textual `Pilot`, `uv`.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Ruff line length **100**; lint set `E,F,I` (import sorting enforced).
- `requires-python = ">=3.10"` — no 3.11+-only syntax.
- CI order is **ruff → pyright → pytest**; match locally before claiming a task done. Coverage is on by default (`--cov-fail-under=90`); use `--no-cov` only for fast single-test loops.
- Pyright runs in **basic** mode over `src` only.
- The formatter module must stay **pure** — no Textual/Rich/app imports beyond reusing `_clip`/`humanize_tool` from `tool_summary.py`; it returns plain `str`. All glyph/`Content` styling stays in the widget. This keeps it unit-testable without an app.
- Pair questions to answers **by position, not by header** (the answer dict is built in question order; headers may be blank/fallback).
- Detect **answered vs note** by JSON-parse: result parses to a `dict` → answered; `status == "pending"` → pending; otherwise → cancelled. Never raise on malformed input.

---

## File Structure

**New:**
- `src/marim_harness/interfaces/tui/widgets/ask_user_render.py` — pure formatter: `AskUserQA`, `overall_state`, `parse_ask_user`, `ask_user_title_tail`, `ask_user_body`.
- `tests/test_ask_user_render.py` — formatter unit tests.

**Modified:**
- `src/marim_harness/interfaces/tui/widgets/tools.py` — `ask_user` branch in `_summary()` and `_render_body()`.
- `tests/test_widgets.py` — a widget render test for the three states.

---

## Task 1: Pure ask_user formatter

A standalone, side-effect-free module that turns `(args, result_text, status)` into the title tail and body strings. Unit-tested directly with no app.

**Files:**
- Create: `src/marim_harness/interfaces/tui/widgets/ask_user_render.py`
- Test: `tests/test_ask_user_render.py`

**Interfaces:**
- Consumes: `_clip`, `humanize_tool` from `.tool_summary` (existing).
- Produces:
  - `AskUserQA` dataclass: `question: str`, `answers: list[str]`, `typed: list[bool]`, `state: str`
  - `overall_state(result_text: str, status: str) -> str` → `"answered" | "pending" | "cancelled"`
  - `parse_ask_user(args: dict, result_text: str, status: str) -> list[AskUserQA]`
  - `ask_user_title_tail(qas: list[AskUserQA], state: str) -> str`
  - `ask_user_body(qas: list[AskUserQA]) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ask_user_render.py
import json

from marim_harness.interfaces.tui.widgets.ask_user_render import (
    ask_user_body,
    ask_user_title_tail,
    overall_state,
    parse_ask_user,
)


def _args(*questions):
    return {"questions": list(questions)}


def _q(question, options, header="", multi=False):
    return {
        "question": question,
        "header": header,
        "options": [{"label": o} for o in options],
        "multi": multi,
    }


def test_overall_state_pending_answered_cancelled():
    assert overall_state("", "pending") == "pending"
    assert overall_state(json.dumps({"h": "A"}), "done") == "answered"
    assert overall_state("User dismissed the prompt without answering.", "done") == "cancelled"
    assert overall_state("not json", "done") == "cancelled"


def test_single_select_answered_title_and_body():
    args = _args(_q("Which approach?", ["Option A", "Option B"], header="approach"))
    result = json.dumps({"approach": "Option B"})
    qas = parse_ask_user(args, result, "done")
    assert ask_user_title_tail(qas, "answered") == "Which approach? → Option B"
    assert ask_user_body(qas) == "Which approach?\n→ Option B"


def test_multi_select_with_typed_other_is_quoted():
    args = _args(_q("Which features?", ["Caching", "Retries"], header="features", multi=True))
    result = json.dumps({"features": ["Caching", "rate-limit by IP"]})
    qas = parse_ask_user(args, result, "done")
    # "rate-limit by IP" is not an offered label → quoted as the user's own words.
    assert ask_user_body(qas) == 'Which features?\n→ Caching, "rate-limit by IP"'


def test_multiple_questions_title_is_count():
    args = _args(
        _q("Q1?", ["A", "B"], header="q1"),
        _q("Q2?", ["C", "D"], header="q2"),
    )
    result = json.dumps({"q1": "A", "q2": "D"})
    qas = parse_ask_user(args, result, "done")
    assert ask_user_title_tail(qas, "answered") == "2 questions answered"
    assert ask_user_body(qas) == "Q1?\n→ A\n\nQ2?\n→ D"


def test_blank_header_pairs_by_position():
    # Both headers blank: pairing must use order, not the (colliding) header key.
    args = _args(_q("First?", ["A", "B"]), _q("Second?", ["C", "D"]))
    result = json.dumps({"First?": "B", "Second?": "C"})  # keys are fallback headers
    qas = parse_ask_user(args, result, "done")
    assert [qa.answers for qa in qas] == [["B"], ["C"]]


def test_pending_state_title_and_body():
    args = _args(_q("Which approach?", ["A", "B"], header="approach"))
    qas = parse_ask_user(args, "", "pending")
    assert ask_user_title_tail(qas, "pending") == "Which approach?  awaiting answer…"
    assert ask_user_body(qas) == "Which approach?\n→ (awaiting answer)"


def test_cancelled_state_title_and_body():
    args = _args(_q("Which approach?", ["A", "B"], header="approach"))
    result = "User dismissed the prompt without answering."
    qas = parse_ask_user(args, result, "done")
    assert ask_user_title_tail(qas, "cancelled") == "cancelled — no answer"
    assert ask_user_body(qas) == "Which approach?\n→ (cancelled)"


def test_malformed_inputs_do_not_raise():
    # Missing questions, non-dict items, non-list options — all degrade.
    assert parse_ask_user({}, "", "pending") == []
    qas = parse_ask_user({"questions": ["x", {"question": "Ok?", "options": "nope"}]},
                         json.dumps({"a": "v"}), "done")
    assert [qa.question for qa in qas] == ["Ok?"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_ask_user_render.py -q`
Expected: FAIL with `ModuleNotFoundError: ...ask_user_render`.

- [ ] **Step 3: Write the implementation**

```python
# src/marim_harness/interfaces/tui/widgets/ask_user_render.py
"""Pure formatter for rendering an ``ask_user`` tool call in the chat transcript.

Pairs each question with the answer the user chose and produces the collapsed
title tail and the expanded body as plain strings, across the three states the
interaction passes through (pending / answered / cancelled). No Textual/Rich/app
imports beyond ``_clip``/``humanize_tool`` — glyph + Content styling live in the
widget, so this stays unit-testable without an App. Pairs by POSITION (the answer
dict is built in question order; headers may be blank/fallback) and detects
answered-vs-note by JSON-parse (never raises on malformed input)."""

import json
from dataclasses import dataclass

from .tool_summary import _clip

_AWAITING = "awaiting answer…"
_CANCELLED_TAIL = "cancelled — no answer"
# Clip widths: the pending/single-answered lines carry a full question, so they get
# a roomier cap than the 100-char generic tool target.
_PENDING_Q_CAP = 60
_ANSWERED_LINE_CAP = 90


@dataclass
class AskUserQA:
    """One question paired with its answer(s). ``answers`` is empty while pending
    or cancelled; ``typed[i]`` is True when ``answers[i]`` was free-text the user
    typed rather than an offered option label. ``state`` mirrors the overall state."""

    question: str
    answers: list[str]
    typed: list[bool]
    state: str


def _answers_obj(result_text: str) -> dict | None:
    """The ``{header: answer}`` dict the tool returns on success, or None when the
    result is a non-JSON note (cancelled / no-UI / empty) or otherwise unparseable."""
    try:
        obj = json.loads(result_text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def overall_state(result_text: str, status: str) -> str:
    """``pending`` while the call is in flight; ``answered`` when the result parses
    to a JSON object; ``cancelled`` for any other (note-string) result."""
    if status == "pending":
        return "pending"
    return "answered" if _answers_obj(result_text) is not None else "cancelled"


def _question_dicts(args: dict) -> list[dict]:
    qs = args.get("questions")
    return [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []


def _option_labels(q: dict) -> set[str]:
    opts = q.get("options")
    out: set[str] = set()
    if isinstance(opts, list):
        for o in opts:
            if isinstance(o, dict) and isinstance(o.get("label"), str):
                out.add(o["label"])
    return out


def _normalize_answer(ans) -> list[str]:
    """A single-select answer is a string; multi-select is a list. Normalize to a
    list of strings."""
    if isinstance(ans, list):
        return [str(a) for a in ans]
    return [str(ans)]


def parse_ask_user(args: dict, result_text: str, status: str) -> list[AskUserQA]:
    """Pair each question (in order) with its answer. Answers come from the result
    JSON's values in insertion (question) order, so position pairing is robust to
    blank/duplicate headers. Degrades on malformed input — never raises."""
    state = overall_state(result_text, status)
    questions = _question_dicts(args)
    values: list = list(_answers_obj(result_text).values()) if state == "answered" else []
    out: list[AskUserQA] = []
    for i, q in enumerate(questions):
        qtext = str(q.get("question", "")).strip()
        if state == "answered" and i < len(values):
            answers = _normalize_answer(values[i])
            labels = _option_labels(q)
            typed = [a not in labels for a in answers]
        else:
            answers, typed = [], []
        out.append(AskUserQA(question=qtext, answers=answers, typed=typed, state=state))
    return out


def _fmt_answers(qa: AskUserQA) -> str:
    """The chosen answer(s) on one line — typed free-text wrapped in quotes so it
    reads as the user's own words, options bare; joined with ', '."""
    parts = [f'"{a}"' if t else a for a, t in zip(qa.answers, qa.typed)]
    return ", ".join(parts) or "(no answer)"


def ask_user_title_tail(qas: list[AskUserQA], state: str) -> str:
    """The collapsed-title text after ``{glyph} {label} · `` (the widget adds the
    glyph + 'Ask User'). One question shows ``Q → A``; many show a count; pending
    shows the question + 'awaiting answer…'; cancelled a fixed note."""
    if state == "pending":
        first = qas[0].question if qas else "question"
        return f"{_clip(first, _PENDING_Q_CAP)}  {_AWAITING}"
    if state == "cancelled":
        return _CANCELLED_TAIL
    if len(qas) == 1:
        return _clip(f"{qas[0].question} → {_fmt_answers(qas[0])}", _ANSWERED_LINE_CAP)
    return f"{len(qas)} questions answered"


def ask_user_body(qas: list[AskUserQA]) -> str:
    """The expanded body: one ``question`` / ``→ answer`` block per question,
    separated by a blank line. Pending/cancelled show a placeholder answer."""
    blocks = []
    for qa in qas:
        if qa.state == "answered":
            ans = _fmt_answers(qa)
        elif qa.state == "pending":
            ans = "(awaiting answer)"
        else:
            ans = "(cancelled)"
        blocks.append(f"{qa.question}\n→ {ans}")
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_ask_user_render.py -q`
Expected: PASS (8 passed). Then `uv run ruff check src tests` and `uv run pyright` clean.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/ask_user_render.py tests/test_ask_user_render.py
git commit -m "feat(tui): pure formatter for ask_user chat rendering"
```

---

## Task 2: Wire the formatter into ToolCallWidget

Add the two `ask_user` branches so the transcript widget uses the formatter for its title and body. Mirrors the existing `edit_file`/`write_file` special-casing.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/tools.py`
- Test: `tests/test_widgets.py`

**Interfaces:**
- Consumes: `overall_state`, `parse_ask_user`, `ask_user_title_tail`, `ask_user_body` (Task 1); `humanize_tool` (already imported in `tools.py`).
- Produces: no new public API — `ToolCallWidget` renders `ask_user` specially.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_widgets.py  (append) — render an ask_user call in the three states.
import json

import pytest

from marim_harness.interfaces.tui.widgets.tools import ToolCallWidget

_ASK_ARGS = {
    "questions": [
        {"question": "Which approach?", "header": "approach",
         "options": [{"label": "Option A"}, {"label": "Option B"}], "multi": False}
    ]
}


def test_ask_user_widget_answered_title_and_body():
    w = ToolCallWidget("ask_user", _ASK_ARGS)
    w.finish(json.dumps({"approach": "Option B"}), status="done")
    title = str(w._summary())
    assert "Ask User" in title and "Which approach? → Option B" in title
    assert "✓" in title
    assert w._render_body() == "Which approach?\n→ Option B"


def test_ask_user_widget_pending_title():
    w = ToolCallWidget("ask_user", _ASK_ARGS)  # status defaults to "pending"
    title = str(w._summary())
    assert "Which approach?  awaiting answer…" in title
    assert w._render_body() == "Which approach?\n→ (awaiting answer)"


def test_ask_user_widget_cancelled_title():
    w = ToolCallWidget("ask_user", _ASK_ARGS)
    w.finish("User dismissed the prompt without answering.", status="done")
    title = str(w._summary())
    assert "cancelled — no answer" in title
    assert "✕" in title
    assert w._render_body() == "Which approach?\n→ (cancelled)"
```

> Note: `_summary()` returns a `Content`; `str(...)` of it yields the plain text (the glyph + label + tail). `_render_body()` returns the plain body string. Neither requires mounting — `ToolCallWidget(...)` and `finish(...)` set `args`/`result_text`/`status` synchronously. If the repo's existing `tools.py` tests mount via `Pilot`, follow that pattern instead and assert the same substrings.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_widgets.py -k ask_user_widget -q`
Expected: FAIL — the generic path yields a raw repr / `{"approach": ...}` JSON, not `Which approach? → Option B`; pending title is bare `Ask User`.

- [ ] **Step 3: Edit `tools.py`**

Add the import near the top (with the other `.tool_summary` import on line 19):

```python
from .ask_user_render import (
    ask_user_body,
    ask_user_title_tail,
    overall_state,
    parse_ask_user,
)
```

In `_summary()`, add an `ask_user` branch as the first thing in the method (before the generic `summarize()` path):

```python
    def _summary(self) -> Content:
        if self.tool_name == "ask_user":
            return self._ask_user_summary()
        glyph, gstyle = self._glyph()
        s = summarize(self.tool_name, self.args)
        # ... (unchanged generic path below)
```

Add the helper method (place it right after `_summary`):

```python
    def _ask_user_summary(self) -> Content:
        """The ask_user title: a state-driven glyph + 'Ask User · {Q→A | count |
        awaiting | cancelled}'. Cancelled overrides the success glyph with ✕, since
        a dismissed prompt returns a (successful) note string, not an error."""
        state = overall_state(self.result_text, self.status)
        if state == "pending":
            glyph, gstyle = self._glyph()  # animated spinner
        elif state == "cancelled":
            glyph, gstyle = "✕", ""
        else:
            glyph, gstyle = "✓", ""
        qas = parse_ask_user(self.args, self.result_text, self.status)
        tail = ask_user_title_tail(qas, state)
        head = f"{humanize_tool('ask_user')} · {tail}"
        # head is our own composed text but bypass markup parsing for consistency
        # with the other Collapsible titles (untrusted question/answer text).
        return Content.assemble((f"{glyph} ", gstyle), head)
```

In `_render_body()`, add an `ask_user` branch right after the breadcrumb early-return:

```python
    def _render_body(self) -> RenderableType:
        from rich.console import Group

        # The breadcrumb is title-only — the checklist lives in the TaskPanel.
        if self._breadcrumb:
            return ""
        if self.tool_name == "ask_user":
            return ask_user_body(parse_ask_user(self.args, self.result_text, self.status))
        primary = self._primary_renderable()
        # ... (unchanged below)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_widgets.py -k ask_user_widget -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Full gate**

Run, in order:
```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: ruff clean, pyright clean, all tests pass (coverage ≥90%). The `ask_user` is rendered standalone (not folded into a tool group) by `_TopLevelSink.intercept_tool` — unchanged; confirm no existing tool/stream test regressed.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/tools.py tests/test_widgets.py
git commit -m "feat(tui): render ask_user as a Q→A summary in the transcript"
```

---

## Self-Review

- **Spec coverage:** collapsed title (all 4 states) → Task 1 `ask_user_title_tail` + Task 2 `_ask_user_summary` ✓; expanded Q→A body → Task 1 `ask_user_body` + Task 2 `_render_body` branch ✓; pair-by-position → `parse_ask_user` (test `test_blank_header_pairs_by_position`) ✓; typed/"Other" quoted → `_fmt_answers` (test `test_multi_select_with_typed_other_is_quoted`) ✓; multi-select joined → `_fmt_answers` ✓; answered-vs-note via JSON-parse → `overall_state` ✓; degrade-not-crash → `test_malformed_inputs_do_not_raise` ✓; pure formatter / styling in widget → module returns `str`, glyph+Content in `_ask_user_summary` ✓; testing (pure unit + widget) → Tasks 1 & 2 ✓.
- **Placeholder scan:** none — every step has full code.
- **Type consistency:** `parse_ask_user(args, result_text, status) -> list[AskUserQA]`, `ask_user_title_tail(qas, state)`, `ask_user_body(qas)`, `overall_state(result_text, status)` are used with identical signatures in Task 2.
- **Open choice resolved:** formatter lives in a new `ask_user_render.py` (keeps `tool_summary.py` focused and the formatter independently testable) — the spec left this to the implementer; this plan picks the new file.
- **Body styling:** the body is plain text (the host `Static` is `markup=False`, matching neighbouring tool bodies); the `→` prefix carries the visual distinction, so no Rich styling is needed in the body. Consistent with the spec's "match neighbouring tool bodies."
