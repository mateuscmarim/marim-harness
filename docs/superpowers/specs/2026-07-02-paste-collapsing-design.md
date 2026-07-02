# Paste collapsing in the prompt box

**Date:** 2026-07-02
**Status:** Approved

## Problem

Pasting a large block of text into the prompt floods the box: the auto-grow
clamps at 6 rows, the draft becomes hard to edit around, and the transcript
above loses space. Claude Code's CLI solves this by collapsing large pastes
into a compact `[Pasted text #1 +13 lines]` marker; marim should behave the
same.

## Decision

Mirror the existing `[Image #N]` attachment mechanism in
`interfaces/tui/widgets/prompt.py`: large pastes are stashed in a side list
and represented in the box by a numbered marker; markers expand back to the
full text at submit time.

Alternatives rejected:

- **Visual folding in the TextArea** — Textual has no fold support; a custom
  document layer is disproportionate effort.
- **Long pastes as file attachments** — changes the message shape and the
  runtime contract; unnecessary for keeping the box tidy.

## Design

### Trigger

In `PromptInput.on_paste`, after the existing image-path detection falls
through, the paste collapses when the text has **more than 3 lines** or
**more than 600 characters**. Anything smaller inserts normally.

### Marker and stash

- `self.pastes: list[str]` holds the full pasted texts, in insertion order.
- The inserted marker is 1-based, aligned with the list:
  - multi-line: `[Pasted text #N +13 lines]` (13 = the paste's line count)
  - long single line: `[Pasted text #N +2971 chars]`
- Marker regex: `\[Pasted text #(\d+) \+\d+ (?:lines|chars)\]`.

### Expansion at submit

`Submitted` and `Steer` both post the *expanded* text: each marker is
replaced by its stashed content before the message is posted, and
`self.pastes` is cleared alongside `self.attachments`. Consequences:

- The model, the queue, and steering all see the real text — no downstream
  changes anywhere.
- Prompt history stores the expanded text, so recalling a submitted prompt
  restores the full content (not a dangling marker).

### Atomic deletion

`_delete_markers` generalizes to treat both `[Image #N]` and
`[Pasted text #N …]` markers atomically: a backspace/delete touching any part
of a marker removes the whole marker, drops the matching stash/attachment
entry, and renumbers the surviving markers of that kind. The two marker kinds
number independently (`[Image #1]` and `[Pasted text #1 …]` can coexist).

### Edge cases

- A hand-mangled marker no longer matches the regex and submits as literal
  text — harmless.
- A marker whose number has no stash entry (shouldn't happen; defensive)
  expands to itself unchanged.
- Pasting while text is selected behaves like any TextArea insert.
- History recall and the slash menu are untouched.

## Out of scope

- "Paste again to expand" / in-place preview of collapsed content.
- Persisting pastes across app restarts (the stash lives with the draft).

## Testing

- Collapses: 4+ lines; 601+ chars on one line. Does not collapse: 3 short
  lines; 600 chars.
- Submit expands multiple markers in order; `pastes` clears after submit.
- Steer expands the same way.
- History recall after submit shows expanded text.
- Atomic delete removes marker + stash entry and renumbers; image and paste
  markers coexist and renumber independently.
