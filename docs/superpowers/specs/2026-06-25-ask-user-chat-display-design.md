# ask_user Chat Display — Design

**Date:** 2026-06-25
**Status:** Approved (brainstorm), pending implementation plan
**Area:** `src/marim_harness/interfaces/tui/widgets/` (tool-call rendering)

## Problem

When the agent calls the `ask_user` tool, the call is rendered in the chat
transcript by the generic `ToolCallWidget` path — there is no special-case for it
(unlike `edit_file`, `write_file`, `read_file`, `update_tasks`). The result is poor:

- **Collapsed:** the title is just `✓ Ask user`. `summarize()` finds no path/command
  target for `ask_user`, so the one-line view carries nothing useful.
- **Expanded:** `_render_body()` falls to the generic branch, dumping a raw Python
  repr of the arguments — `questions: [Question(question='…', header='…',
  options=[Choice(label='…', description='…'), …], multi=False), …]` — followed by
  the answer as a compact JSON blob `{"header": "answer", …}`.

So a user-facing Q&A interaction reads, in the transcript, as a raw data structure.

## Goal

Render an `ask_user` call as a clean **Q→A summary** that pairs each question with
the answer the user chose, in all three states the interaction passes through
(pending / answered / cancelled). Match the existing "special-case in
`ToolCallWidget` backed by a pure, tested formatter" pattern.

## Non-goals

- No change to the interactive `AskUserModal` (that is a separate surface).
- No change to the `ask_user` tool's data model, JSON result, or the agent-facing
  contract (`answers_to_json`).
- Not showing every offered option per question — only the chosen answer (keeps the
  transcript compact; the modal already showed the full option set live).

## Rendering spec

### Collapsed title (`_summary()`)

| State | Line |
|-------|------|
| Answered, single question | `✓ Ask user · {question} → {answer}` |
| Answered, multiple questions | `✓ Ask user · {N} questions answered` |
| Pending (modal open) | `{spinner} Ask user · {question}  awaiting answer…` |
| Cancelled (Esc) | `✕ Ask user · cancelled — no answer` |

- The glyph + status colour come from the widget's existing `_glyph()` (spinner
  while pending, `✓` done, `✕` denied/cancelled).
- The question and answer are clipped to fit one row using the module's existing
  clip helper (same treatment as a long path/command). For the pending and
  single-answered lines the *question* is the salient bit and is what gets clipped.
- Multiple-question pending shows the first question (or `{N} questions` if that
  reads better at clip width — implementer picks the one that fits the helper).

### Expanded body — one block per question

```
{question 1}
→ {answer 1}

{question 2}  (multi)
→ {answer 2a}, {answer 2b}, "{typed answer}"
```

- One block per question, blank line between blocks.
- `→ ` prefixes the answer line; the answer text is styled to stand out (e.g. the
  accent/normal foreground) while the question is the default/muted — concrete
  style chosen to match neighbouring tool bodies.
- **Multi-select** answers join on one line separated by `, ` (or ` · `).
- A **typed / "Other"** answer (free-text that does not match any offered option
  label) is wrapped in quotes so it reads as the user's own words, not a preset.
  Detection: the answer string is not equal to any `Choice.label` of that question.
- **Pending** body: the questions with `→ (awaiting answer)` under each (or under the
  current one), so an expanded in-flight call is legible.
- **Cancelled** body: the questions with `→ (cancelled)` — the questions are still
  shown (they were asked) but no answer was given.

### Pairing questions to answers — by POSITION, not header

The answer mapping is `{header: answer}` keyed by the *coerced* header (which falls
back to a truncated question text when the model left `header` blank), while the raw
call args may carry a blank header. Pairing by header would mismatch blank-header
questions. The answers dict is built in question order (`_record` appends in order),
so its values are in question order. **Pair `questions[i]` with the i-th answer
value.** This is robust to blank/duplicate/fallback headers.

## Architecture

### New: a pure formatter

A side-effect-free helper that turns the call's args + result into a render model.
Lives in `tool_summary.py` (it already holds the pure `summarize`/`humanize_tool`
helpers) or a small sibling `ask_user_render.py` if `tool_summary.py` is getting
large — the implementer picks per file size, following the codebase's
small-focused-files preference.

Shape (names indicative, finalized in the plan):

```python
@dataclass
class AskUserQA:
    question: str
    answers: list[str]          # 0 = pending/cancelled, 1 = single, N = multi
    typed: list[bool]           # parallel: True where the answer was free-text
    state: str                  # "answered" | "pending" | "cancelled"

def parse_ask_user(args: dict, result_text: str, status: str) -> list[AskUserQA]:
    """Pair each question (in order) with its answer from the result JSON.
    status drives pending vs answered; an empty/cancel result → cancelled."""

def ask_user_title(qas: list[AskUserQA], status: str) -> str:   # collapsed line tail
def ask_user_body(qas: list[AskUserQA]) -> Renderable/str       # expanded body
```

- `parse_ask_user` tolerates a missing/blank/non-JSON result (pending → `[]` answers;
  cancelled → state cancelled). It never raises on malformed input — a render helper
  must degrade, not crash the log.
- Determining **answered vs non-answered** (resolved): the `ask_user` tool returns
  `answers_to_json(...)` — a JSON object — on success, and one of three plain-string
  notes otherwise (`provider.py`): `_ASK_USER_CANCELLED` ("User dismissed the prompt
  without answering."), `_ASK_USER_NO_UI` (no interactive UI), `_ASK_USER_EMPTY`
  (invalid questions). So `parse_ask_user` detects state by **trying to parse the
  result as a JSON object**: parses to a `dict` → answered; status `pending` →
  pending; otherwise → non-answered, carrying the note text. This is robust and does
  not hardcode any sentinel constant — all three notes render via the cancelled-style
  line (the note string itself can be shown after `Ask user · `).

### Thin widget wiring (`ToolCallWidget`)

- `_summary()`: add an `ask_user` branch that builds the title tail from
  `ask_user_title(...)` instead of `summarize().target`.
- `_render_body()` / `_primary_renderable()`: add an `ask_user` branch returning
  `ask_user_body(...)` instead of the generic `arg_lines + result`.
- No change to mounting: `ask_user` already mounts standalone (not folded into a
  tool group) via `_TopLevelSink.intercept_tool` — keep that.

Keeps the leaf widget thin; all parsing/formatting is in the pure helper.

## Testing

- **Pure formatter (direct unit tests):** `parse_ask_user` + `ask_user_title` +
  `ask_user_body` over: single-select answered; multi-select answered; typed/"Other"
  answer (quoted); multi-question (count line); blank-header question (position
  pairing still correct); pending (no result); cancelled (cancel sentinel);
  malformed/non-JSON result (degrades, no raise).
- **Widget (Pilot):** a `ToolCallWidget("ask_user", args)` shows the Q→A title and,
  when finished with a JSON result, the Q→A body; a pending one shows `awaiting
  answer…`; a cancelled one shows the cancelled line. Reuse the existing
  `tests/test_ask_user_modal.py` / tool-widget test patterns.
- Restored/replay path: a resumed session reconstructs the same `ToolCallWidget` from
  history, so the rendering is identical live and on replay — add/confirm a replay
  assertion if the existing tool tests cover that path.

## Affected files (indicative)

- `interfaces/tui/widgets/tool_summary.py` (or new `ask_user_render.py`) — the pure
  formatter.
- `interfaces/tui/widgets/tools.py` — two small `ask_user` branches in `_summary` and
  `_render_body`.
- `tests/` — formatter unit tests + a widget render test.

## Open items

None blocking. The state-detection question (cancel vs answered) is resolved above
via JSON-parse. The only implementer choices left are cosmetic and called out inline:
formatter file location (`tool_summary.py` vs a new `ask_user_render.py`), the exact
styles/separators for the answer line, and whether the multi-question pending line
shows the first question or a count at clip width.
