"""The multi-line prompt input box: submit/newline keys, shell-style history
recall, auto-grow, and image-attachment handling (paste + ``[Image #N]`` markers)."""

import re
from pathlib import Path

from textual import events
from textual.message import Message
from textual.widgets import TextArea

_IMAGE_MARKER = re.compile(r"\[Image #(\d+)\]")


class PromptInput(TextArea):
    """The multi-line message box. Enter submits; Shift+Enter and Ctrl+J insert a
    newline. The box auto-grows with its content up to ``_MAX_LINES``, then
    scrolls internally.

    Up/Down recall previously submitted prompts shell-style — but only at the
    text boundaries (Up on the first line, Down on the last), so inside a
    multi-line draft the arrows still move the cursor normally."""

    _MIN_LINES = 1
    _MAX_LINES = 6

    class Submitted(Message):
        """Posted when the user presses Enter; carries the box's full text and
        any attached images as (bytes, media_type) tuples."""

        def __init__(self, value: str,
                     attachments: list[tuple[bytes, str]] | None = None) -> None:
            self.value = value
            self.attachments = attachments or []
            super().__init__()

    class Steer(Message):
        """Posted when the user presses Alt+Enter; carries the box's full text
        and any attached images, to inject into the running turn."""

        def __init__(self, value: str,
                     attachments: list[tuple[bytes, str]] | None = None) -> None:
            self.value = value
            self.attachments = attachments or []
            super().__init__()

    class SlashChanged(Message):
        """Posted when the first line starts with ``/``."""
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class SlashDismissed(Message):
        """Posted when text stops starting with ``/``."""
        def __init__(self) -> None:
            super().__init__()

    def __init__(self, history=None) -> None:
        from ....history import PromptHistory

        # NB: TextArea.history is its own undo stack — keep prompt history apart.
        self.prompt_history = history if history is not None else PromptHistory()
        # Navigation cursor into history.entries; None means "editing the live
        # draft". ``_draft`` stashes that draft while scrolling back.
        self._hist_idx: int | None = None
        self._draft = ""
        super().__init__(soft_wrap=True, show_line_numbers=False)
        self.attachments: list[tuple[Path, str]] = []
        self._slash_active: bool = False

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._slash_active:
            self._slash_active = False
            self.post_message(self.SlashDismissed())
            event.prevent_default()
            event.stop()
            return
        if event.key in ("alt+enter", "ctrl+g"):
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Steer(self.text, atts))
            self.attachments = []
            self._reset_nav()
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Submitted(self.text, atts))
            self.attachments = []
            self._reset_nav()
            return
        if event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "ctrl+x":
            # Open the sub-agent viewer. Intercepted here because TextArea binds
            # ctrl+x to "cut", which would otherwise swallow it before the app's
            # binding runs. Guarded so the widget still works in bare-app tests.
            event.prevent_default()
            event.stop()
            toggle = getattr(self.app, "action_toggle_subagents", None)
            if toggle is not None:
                toggle()
            return
        if event.key == "up" and self._at_first_line() and self._recall_prev():
            event.prevent_default()
            event.stop()
            return
        if event.key == "down" and self._at_last_line() and self._recall_next():
            event.prevent_default()
            event.stop()
            return
        if event.key == "ctrl+v" and self._on_paste_image():
            event.prevent_default()
            event.stop()
            return
        if event.key in ("backspace", "delete") and self._delete_markers(event.key):
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)

    def _on_paste_image(self) -> bool:
        from .... import images

        got = images.read_clipboard_image()
        if got is None:
            return False
        data, media_type = got
        return self._cache_and_insert(data, media_type)

    def _cache_and_insert(self, data: bytes, media_type: str) -> bool:
        from .... import images

        cached = images.store_image(self._session_id(), data, media_type)
        self.attachments.append((cached.path, media_type))
        self.insert(f"[Image #{len(self.attachments)}]")
        return True

    def _session_id(self) -> str:
        # Resolve lazily from the running app's harness; fall back to a constant
        # bucket if unavailable (e.g. isolated widget tests). Persistence (the
        # externalize task) re-stores under the real session id regardless, so a
        # fallback bucket here only affects the transient paste-time cache path.
        try:
            return self.app.harness.session.store.session_id  # type: ignore[attr-defined]
        except Exception:
            return "default"

    def on_paste(self, event: events.Paste) -> None:
        from .... import images

        path = images.detect_image_path(event.text)
        if path is None:
            return  # let TextArea insert the pasted text normally
        media_type = images.media_type_for_path(path)
        if media_type is None:
            return
        event.prevent_default()
        event.stop()
        self._cache_and_insert(path.read_bytes(), media_type)

    def _offset(self, loc: tuple[int, int]) -> int:
        """Absolute character offset of a (row, col) cursor location."""
        row, col = loc
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + col

    def _location(self, offset: int) -> tuple[int, int]:
        """The (row, col) location of an absolute character offset in self.text."""
        head = self.text[:offset]
        return (head.count("\n"), offset - (head.rfind("\n") + 1))

    def _delete_markers(self, key: str) -> bool:
        """Keep ``[Image #N]`` markers atomic: if a backspace/delete touches any
        part of a marker (including its brackets), remove the whole marker and
        drop the matching attachment instead of breaking the text. Surviving
        markers renumber so they stay ``#1..#M`` aligned with ``attachments``.
        Returns True when it consumed the edit, False to fall through to the
        normal TextArea editing."""
        text = self.text
        spans = [(m.start(), m.end(), int(m.group(1)))
                 for m in _IMAGE_MARKER.finditer(text)]
        if not spans:
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
        hit = [s for s in spans if s[0] < hi and s[1] > lo]
        if not hit:
            return False
        lo = min(lo, min(s[0] for s in hit))
        hi = max(hi, max(s[1] for s in hit))
        removed = {s[2] for s in hit}
        for n in sorted(removed, reverse=True):
            if 1 <= n <= len(self.attachments):
                del self.attachments[n - 1]

        def _renumber(m: "re.Match[str]") -> str:
            n = int(m.group(1))
            return f"[Image #{n - sum(r < n for r in removed)}]"

        new_prefix = _IMAGE_MARKER.sub(_renumber, text[:lo])
        new_text = new_prefix + _IMAGE_MARKER.sub(_renumber, text[hi:])
        self.text = new_text
        self.move_cursor(self._location(len(new_prefix)))
        return True

    def _at_first_line(self) -> bool:
        return self.cursor_location[0] == 0

    def _at_last_line(self) -> bool:
        return self.cursor_location[0] == self.document.line_count - 1

    def _reset_nav(self) -> None:
        self._hist_idx = None
        self._draft = ""

    def _show(self, text: str) -> None:
        """Replace the box with ``text`` and drop the cursor at the end."""
        self.text = text
        self.move_cursor(self.document.end)

    def _recall_prev(self) -> bool:
        """Move one step back into history. Returns whether it consumed the key."""
        entries = self.prompt_history.entries
        if not entries:
            return False
        if self._hist_idx is None:
            self._draft = self.text  # remember what we were typing
            self._hist_idx = len(entries) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        # else: already at the oldest — stay put, but still consume the key.
        self._show(entries[self._hist_idx])
        return True

    def _recall_next(self) -> bool:
        """Move one step forward; past the newest entry restores the draft."""
        if self._hist_idx is None:
            return False  # not navigating — let Down move the cursor
        entries = self.prompt_history.entries
        if self._hist_idx < len(entries) - 1:
            self._hist_idx += 1
            self._show(entries[self._hist_idx])
        else:
            self._hist_idx = None
            self._show(self._draft)
        return True

    def _target_height(self) -> int:
        """Rows the box should occupy: one per logical line, clamped to the
        [min, max] window."""
        lines = self.document.line_count
        return max(self._MIN_LINES, min(lines, self._MAX_LINES))

    def _resize(self) -> None:
        # +2 for the box border's top and bottom rows (see styles.tcss), so the
        # visible text area, not the outer box, tracks the [min, max] window.
        self.styles.height = self._target_height() + 2

    def on_text_area_changed(self, event: "TextArea.Changed") -> None:
        self._resize()
        # Slash-command autocomplete: track when the first line starts with /.
        first_line = self.text.split("\n", 1)[0]
        if first_line.startswith("/"):
            self._slash_active = True
            self.post_message(self.SlashChanged(self.text))
        elif self._slash_active:
            self._slash_active = False
            self.post_message(self.SlashDismissed())
