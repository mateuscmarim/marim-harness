# Paste Collapsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Large pastes into the prompt box collapse to a compact `[Pasted text #N +13 lines]` marker and expand back to the full text at submit, mirroring the existing `[Image #N]` mechanism.

**Architecture:** All changes live in `src/marim_harness/interfaces/tui/widgets/prompt.py` (`PromptInput`). A `pastes: list[str]` side-stash parallels `attachments`; `on_paste` intercepts large pastes and inserts a numbered marker; Submit/Steer expand markers before posting (so the model, queue, steering, and prompt history — recorded from the submitted value at app.py:886 — all see real text); `_delete_markers` generalizes to keep both marker kinds atomic under backspace/delete.

**Tech Stack:** Python ≥3.10, Textual 8.2.7, pytest + anyio + Textual Pilot, uv.

**Spec:** `docs/superpowers/specs/2026-07-02-paste-collapsing-design.md`

## Global Constraints

- Use `uv` for everything: `uv run pytest …`, `uv run ruff …`, `uv run pyright`. Never bare python/pytest/pip.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`.
- `requires-python >=3.10` — no 3.11+-only syntax.
- Verification order: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Collapse thresholds, verbatim from the spec: **more than 3 lines OR more than 600 characters**.
- Marker formats, verbatim: multi-line `[Pasted text #N +13 lines]` (13 = the paste's total line count); long single line `[Pasted text #N +2971 chars]`.
- Marker regex, verbatim: `\[Pasted text #(\d+) (\+\d+ (?:lines|chars))\]`.
- Image markers (`[Image #N]`) and paste markers number independently.
- Preserve existing "why" comments in prompt.py when editing nearby code.

---

### Task 1: Collapse on paste, expand at submit

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/prompt.py`
- Test: `tests/test_paste_collapse.py` (new file — the paste feature gets its own focused test module, like `tests/test_image_paste.py`)

**Interfaces:**
- Consumes: existing `PromptInput` internals — `self.insert(text)`, `events.Paste`, the Submit/Steer branches in `_on_key`, `images.detect_image_path` / `images.media_type_for_path` (unchanged).
- Produces (Task 2 relies on these exact names): module constant `_PASTE_MARKER` (compiled regex with group 1 = number, group 2 = the `+N lines|chars` tail), instance attribute `self.pastes: list[str]`, helpers `_maybe_collapse_paste(text: str) -> bool` and `_expand_pastes(text: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paste_collapse.py`:

```python
"""Large pastes collapse to [Pasted text #N …] markers and expand at submit.
See docs/superpowers/specs/2026-07-02-paste-collapsing-design.md."""

import pytest
from textual import events
from textual.app import App, ComposeResult

from marim_harness.interfaces.tui.widgets.prompt import PromptInput


class _PromptHost(App):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []
        self.steered: list[str] = []

    def compose(self) -> ComposeResult:
        yield PromptInput()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self.submitted.append(event.value)

    def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
        self.steered.append(event.value)


async def _paste(pilot, pi: PromptInput, text: str) -> None:
    pi.post_message(events.Paste(text))
    await pilot.pause()


@pytest.mark.anyio
async def test_multiline_paste_collapses_to_marker():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        blob = "\n".join(f"line {i}" for i in range(13))
        await _paste(pilot, pi, blob)
        assert pi.text == "[Pasted text #1 +13 lines]"
        assert pi.pastes == [blob]


@pytest.mark.anyio
async def test_long_single_line_paste_collapses_with_char_count():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        blob = "x" * 601
        await _paste(pilot, pi, blob)
        assert pi.text == "[Pasted text #1 +601 chars]"
        assert pi.pastes == [blob]


@pytest.mark.anyio
async def test_small_pastes_insert_normally():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await _paste(pilot, pi, "a\nb\nc")     # 3 lines: at the threshold, not over
        await _paste(pilot, pi, "y" * 600)      # 600 chars: at the threshold, not over
        assert pi.text == "a\nb\nc" + "y" * 600
        assert pi.pastes == []


@pytest.mark.anyio
async def test_submit_expands_markers_in_order():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        first = "\n".join(f"a{i}" for i in range(5))
        second = "\n".join(f"b{i}" for i in range(4))
        await _paste(pilot, pi, first)
        pi.insert(" between ")
        await _paste(pilot, pi, second)
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == [f"{first} between {second}"]
        assert pi.pastes == []  # stash cleared with the draft


@pytest.mark.anyio
async def test_steer_expands_markers_too():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        blob = "\n".join(f"s{i}" for i in range(6))
        await _paste(pilot, pi, blob)
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert app.steered == [blob]
        assert pi.pastes == []


@pytest.mark.anyio
async def test_unmatched_marker_submits_as_literal_text():
    """A hand-typed marker with no stash entry passes through unchanged."""
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        pi.insert("[Pasted text #7 +9 lines]")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["[Pasted text #7 +9 lines]"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_paste_collapse.py -v`
Expected: FAIL — `AttributeError: 'PromptInput' object has no attribute 'pastes'` (first two tests) and assertion failures on the raw pasted text (the paste currently inserts verbatim).

- [ ] **Step 3: Implement collapse + expand in prompt.py**

3a. Module level, directly under the existing `_IMAGE_MARKER` (prompt.py:11):

```python
_PASTE_MARKER = re.compile(r"\[Pasted text #(\d+) (\+\d+ (?:lines|chars))\]")
# Collapse thresholds (spec: more than 3 lines OR more than 600 chars).
_PASTE_MAX_LINES = 3
_PASTE_MAX_CHARS = 600


def _paste_marker(n: int, text: str) -> str:
    """The compact marker for stash entry ``n``: a line count for multi-line
    pastes, a character count for long one-liners."""
    lines = text.count("\n") + 1
    if lines > 1:
        return f"[Pasted text #{n} +{lines} lines]"
    return f"[Pasted text #{n} +{len(text)} chars]"
```

3b. In `__init__`, next to `self.attachments` (prompt.py:67):

```python
        # Full texts of collapsed pastes, in insertion order; the box shows a
        # numbered [Pasted text #N …] marker per entry (mirrors attachments).
        self.pastes: list[str] = []
```

3c. Update the module docstring (line 1-2) to mention pasted-text markers:

```python
"""The multi-line prompt input box: submit/newline keys, shell-style history
recall, auto-grow, image-attachment handling (paste + ``[Image #N]`` markers),
and Claude-Code-style paste collapsing (``[Pasted text #N …]`` markers)."""
```

3d. Rewrite `on_paste` (prompt.py:198-209). The current early-return structure must be inverted so text pastes reach the collapse check; note the image-path-with-unknown-media-type case still falls through to a normal insert:

```python
    def on_paste(self, event: events.Paste) -> None:
        from .... import images

        path = images.detect_image_path(event.text)
        if path is not None:
            media_type = images.media_type_for_path(path)
            if media_type is None:
                return  # image-looking path, unknown type: normal text insert
            event.prevent_default()
            event.stop()
            self._cache_and_insert(path.read_bytes(), media_type)
            return
        if self._maybe_collapse_paste(event.text):
            event.prevent_default()
            event.stop()
```

3e. Add the two helpers next to `_cache_and_insert`:

```python
    def _maybe_collapse_paste(self, text: str) -> bool:
        """Stash a large paste and insert its compact marker instead of the
        text. Returns True when it consumed the paste; small pastes fall
        through to the normal TextArea insert."""
        lines = text.count("\n") + 1
        if lines <= _PASTE_MAX_LINES and len(text) <= _PASTE_MAX_CHARS:
            return False
        self.pastes.append(text)
        self.insert(_paste_marker(len(self.pastes), text))
        return True

    def _expand_pastes(self, text: str) -> str:
        """Replace each [Pasted text #N …] marker with its stashed content.
        A marker with no matching stash entry (hand-typed, or mangled past
        recognition and retyped) is left as literal text."""

        def _sub(m: "re.Match[str]") -> str:
            n = int(m.group(1))
            if 1 <= n <= len(self.pastes):
                return self.pastes[n - 1]
            return m.group(0)

        return _PASTE_MARKER.sub(_sub, text)
```

3f. In `_on_key`, expand at both post sites and clear the stash. The steer branch (prompt.py:97-104) becomes:

```python
        if event.key in ("alt+enter", "ctrl+g"):
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Steer(self._expand_pastes(self.text), atts))
            self.attachments = []
            self.pastes = []
            self._reset_nav()
            return
```

and the enter branch (prompt.py:105-112):

```python
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Submitted(self._expand_pastes(self.text), atts))
            self.attachments = []
            self.pastes = []
            self._reset_nav()
            return
```

(Prompt history is recorded by the app from the submitted value — app.py:886 — so it stores the expanded text with no further change.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_paste_collapse.py -v`
Expected: 6 passed.

Also run the neighbors that share `PromptInput`:
`uv run pytest --no-cov tests/test_widgets.py tests/test_image_paste.py -v`
Expected: all passed (image-path pastes still work — the restructured `on_paste` is behavior-preserving for them).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/widgets/prompt.py tests/test_paste_collapse.py
git commit -m "feat(tui): large pastes collapse to [Pasted text #N] markers, expand at submit"
```

---

### Task 2: Atomic deletion for paste markers

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/prompt.py` (`_delete_markers`, prompt.py:222-265 pre-Task-1 numbering)
- Test: `tests/test_paste_collapse.py` (append)

**Interfaces:**
- Consumes from Task 1: `_PASTE_MARKER` (group 1 = number, group 2 = `+N lines|chars` tail), `self.pastes`.
- Produces: nothing new — `_delete_markers(key) -> bool` keeps its signature; it now treats both marker kinds atomically and renumbers each kind independently.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_paste_collapse.py`:

```python
@pytest.mark.anyio
async def test_backspace_removes_whole_marker_and_stash_entry():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        blob = "\n".join(f"line {i}" for i in range(13))
        await _paste(pilot, pi, blob)
        # Cursor sits right after the marker; one backspace kills all of it.
        await pilot.press("backspace")
        await pilot.pause()
        assert pi.text == ""
        assert pi.pastes == []


@pytest.mark.anyio
async def test_deleting_first_of_two_markers_renumbers_survivor():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        first = "\n".join(f"a{i}" for i in range(5))
        second = "x" * 700
        await _paste(pilot, pi, first)
        await _paste(pilot, pi, second)
        assert pi.text == "[Pasted text #1 +5 lines][Pasted text #2 +700 chars]"
        # Put the cursor inside the FIRST marker and delete it.
        pi.move_cursor((0, 5))
        await pilot.press("backspace")
        await pilot.pause()
        # Survivor renumbers to #1 and keeps its own +chars tail.
        assert pi.text == "[Pasted text #1 +700 chars]"
        assert pi.pastes == [second]
        # And it still expands to the right content.
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == [second]


@pytest.mark.anyio
async def test_image_and_paste_markers_number_independently(tmp_path):
    """Pasting an image path makes [Image #1]; a text paste makes
    [Pasted text #1 …] — deleting the paste marker must not disturb the
    image attachment. Fake image bytes follow test_image_paste.py's pattern
    (there is no fixture file; a path ending .png with any bytes suffices)."""
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNGbytes")
        await _paste(pilot, pi, str(img))          # -> [Image #1]
        blob = "\n".join(f"l{i}" for i in range(9))
        await _paste(pilot, pi, blob)              # -> [Pasted text #1 +9 lines]
        assert pi.text == "[Image #1][Pasted text #1 +9 lines]"
        # Deleting the paste marker must not touch the image attachment.
        await pilot.press("backspace")
        await pilot.pause()
        assert pi.text == "[Image #1]"
        assert pi.pastes == []
        assert len(pi.attachments) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_paste_collapse.py -v -k "backspace or renumber or independently"`
Expected: FAIL — backspace deletes one character of the marker instead of the whole thing (`_delete_markers` only knows `[Image #N]`).

- [ ] **Step 3: Generalize `_delete_markers`**

Replace the whole method with (the shape — offset window, hit test, span
widening, renumber-and-splice — is unchanged from the image-only version; it
now runs over both marker kinds and renumbers each independently):

```python
    def _delete_markers(self, key: str) -> bool:
        """Keep ``[Image #N]`` and ``[Pasted text #N …]`` markers atomic: if a
        backspace/delete touches any part of a marker (including its
        brackets), remove the whole marker and drop the matching
        attachment/stash entry instead of breaking the text. Surviving
        markers renumber so each kind stays ``#1..#M`` aligned with its list
        (the two kinds number independently). Returns True when it consumed
        the edit, False to fall through to the normal TextArea editing."""
        text = self.text
        image_spans = [(m.start(), m.end(), int(m.group(1)))
                       for m in _IMAGE_MARKER.finditer(text)]
        paste_spans = [(m.start(), m.end(), int(m.group(1)))
                       for m in _PASTE_MARKER.finditer(text)]
        if not image_spans and not paste_spans:
            return False
        lo = self._offset(self.selection.start)
        hi = self._offset(self.selection.end)
        if lo > hi:
            lo, hi = hi, lo
        if lo == hi:  # no selection — a single-character edit
            if key == "backspace":
                if lo == 0:
                    return False
                lo -= 1
            else:  # delete
                if hi >= len(text):
                    return False
                hi += 1
        image_hit = [s for s in image_spans if s[0] < hi and s[1] > lo]
        paste_hit = [s for s in paste_spans if s[0] < hi and s[1] > lo]
        if not image_hit and not paste_hit:
            return False
        every_hit = image_hit + paste_hit
        lo = min(lo, min(s[0] for s in every_hit))
        hi = max(hi, max(s[1] for s in every_hit))
        removed_images = {s[2] for s in image_hit}
        removed_pastes = {s[2] for s in paste_hit}
        for n in sorted(removed_images, reverse=True):
            if 1 <= n <= len(self.attachments):
                del self.attachments[n - 1]
        for n in sorted(removed_pastes, reverse=True):
            if 1 <= n <= len(self.pastes):
                del self.pastes[n - 1]

        def _renumber_image(m: "re.Match[str]") -> str:
            n = int(m.group(1))
            return f"[Image #{n - sum(r < n for r in removed_images)}]"

        def _renumber_paste(m: "re.Match[str]") -> str:
            n = int(m.group(1))
            return f"[Pasted text #{n - sum(r < n for r in removed_pastes)} {m.group(2)}]"

        def _renumber(segment: str) -> str:
            segment = _IMAGE_MARKER.sub(_renumber_image, segment)
            return _PASTE_MARKER.sub(_renumber_paste, segment)

        new_prefix = _renumber(text[:lo])
        new_text = new_prefix + _renumber(text[hi:])
        self.text = new_text
        self.move_cursor(self._location(len(new_prefix)))
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_paste_collapse.py -v`
Expected: 9 passed.

Regression check on the image-marker behavior the method previously owned:
`uv run pytest --no-cov tests/test_image_paste.py tests/test_widgets.py -v`
Expected: all passed.

- [ ] **Step 5: Full gate (CI order), commit**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
git add src/marim_harness/interfaces/tui/widgets/prompt.py tests/test_paste_collapse.py
git commit -m "feat(tui): atomic deletion and renumbering for paste markers"
```

---

## Verification against spec

| Spec section | Covered by |
|---|---|
| Trigger (>3 lines or >600 chars, after image detection) | Task 1 (`_maybe_collapse_paste`, `on_paste` restructure) |
| Marker + stash (`pastes`, 1-based, lines/chars variants) | Task 1 (`_paste_marker`, regex) |
| Expansion at submit (Submitted + Steer, stash cleared, history/queue see real text) | Task 1 (`_expand_pastes`, `_on_key` branches) |
| Atomic deletion, independent renumbering per kind | Task 2 |
| Edge: mangled/unmatched marker → literal text | Task 1 (`_expand_pastes` fallback + test) |
| History recall shows expanded text | Follows from submit-time expansion: app.py:886 records the already-expanded submitted value; asserted indirectly by `test_submit_expands_markers_in_order` |
| Testing list in spec | Tasks 1–2 test steps (thresholds, order, steer, delete/renumber, coexistence) |
