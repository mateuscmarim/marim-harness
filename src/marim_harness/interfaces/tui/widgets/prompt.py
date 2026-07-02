"""The multi-line prompt input box: submit/newline keys, shell-style history
recall, auto-grow, image-attachment handling (paste + ``[Image #N]`` markers),
and Claude-Code-style paste collapsing (``[Pasted text #N …]`` markers)."""

import re
from pathlib import Path

from textual import events
from textual.message import Message
from textual.widgets import TextArea

_IMAGE_MARKER = re.compile(r"\[Image #(\d+)\]")
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
        from ...history import PromptHistory

        # NB: TextArea.history is its own undo stack — keep prompt history apart.
        self.prompt_history = history if history is not None else PromptHistory()
        # Navigation cursor into history.entries; None means "editing the live
        # draft". ``_draft`` stashes that draft while scrolling back.
        self._hist_idx: int | None = None
        self._draft = ""
        super().__init__(soft_wrap=True, show_line_numbers=False)
        self.attachments: list[tuple[Path, str]] = []
        # Full texts of collapsed pastes, in insertion order; the box shows a
        # numbered [Pasted text #N …] marker per entry (mirrors attachments).
        self.pastes: list[str] = []
        self._slash_active: bool = False
        # Set for the next text change when it comes from a history recall (_show),
        # so on_text_area_changed skips slash-menu activation for it — see _show.
        self._suppress_slash: bool = False

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._slash_active:
            self._slash_active = False
            self.post_message(self.SlashDismissed())
            event.prevent_default()
            event.stop()
            return
        if self._slash_active:
            # Drive the (unfocusable) slash menu from the prompt: Up/Down move its
            # highlight, Tab completes the highlighted command into the box. Each
            # helper returns False when the menu is hidden/empty, so the key falls
            # through to the prompt's own handling (history recall, normal Tab).
            if event.key == "down" and self._menu_navigate(1):
                event.prevent_default()
                event.stop()
                return
            if event.key == "up" and self._menu_navigate(-1):
                event.prevent_default()
                event.stop()
                return
            if event.key == "tab" and self._menu_accept():
                event.prevent_default()
                event.stop()
                return
        if event.key in ("alt+enter", "ctrl+g"):
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Steer(self._expand_pastes(self.text), atts))
            self.attachments = []
            self.pastes = []
            self._reset_nav()
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Submitted(self._expand_pastes(self.text), atts))
            self.attachments = []
            self.pastes = []
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

    def _menu_navigate(self, delta: int) -> bool:
        """Ask the app to move the open slash menu's highlight. Reached through the
        app (like ctrl+x) rather than the widget directly, so the prompt still works
        in bare-widget tests where no app method exists."""
        nav = getattr(self.app, "autocomplete_navigate", None)
        return bool(nav and nav(delta))

    def _menu_accept(self) -> bool:
        """Ask the app to complete the highlighted slash command. Returns True when
        the menu was open and a command was accepted."""
        accept = getattr(self.app, "autocomplete_accept", None)
        return bool(accept and accept())

    def _on_paste_image(self) -> bool:
        from .... import images

        got = images.read_clipboard_image()
        if got is None:
            return False
        data, media_type = got
        return self._cache_and_insert(data, media_type)

    def load_attachments(self, attachments: list[tuple[bytes, str]]) -> None:
        """Restore image attachments from their raw bytes (e.g. when a queued
        message is popped back into the box for editing). The text already carries
        the matching ``[Image #N]`` markers, so this only re-caches the bytes and
        repopulates ``self.attachments`` in order — it inserts no markers."""
        from .... import images

        self.attachments = []
        for data, media_type in attachments:
            cached = images.store_image(self._session_id(), data, media_type)
            self.attachments.append((cached.path, media_type))

    def _cache_and_insert(self, data: bytes, media_type: str) -> bool:
        from .... import images

        cached = images.store_image(self._session_id(), data, media_type)
        self.attachments.append((cached.path, media_type))
        self.insert(f"[Image #{len(self.attachments)}]")
        return True

    def _maybe_collapse_paste(self, text: str) -> bool:
        """Stash a large paste and replace the current selection with its
        compact marker instead of the text. Returns True when it consumed
        the paste; small pastes fall through to Textual's default
        selection-aware paste handling."""
        lines = text.count("\n") + 1
        if lines <= _PASTE_MAX_LINES and len(text) <= _PASTE_MAX_CHARS:
            return False
        self.pastes.append(text)
        # Replace like a real paste would: a selection is consumed by the
        # marker, not skipped over (TextArea.insert ignores the selection).
        self.replace(_paste_marker(len(self.pastes), text), *self.selection)
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
        if path is not None:
            media_type = images.media_type_for_path(path)
            if media_type is None:
                return  # image-looking path, unknown type: normal text insert
            event.prevent_default()
            event.stop()
            self._cache_and_insert(path.read_bytes(), media_type)
            return
        # This widget is always the final handler for a paste: stop it from
        # bubbling to ancestors regardless of outcome. prevent_default() is
        # conditional though — it only suppresses TextArea's own selection-
        # aware paste handling when we've replaced the selection ourselves
        # with a collapse marker. A small paste leaves prevent_default()
        # uncalled so TextArea's default paste (selection-aware) still runs.
        event.stop()
        if self._maybe_collapse_paste(event.text):
            event.prevent_default()

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

    def _at_first_line(self) -> bool:
        return self.cursor_location[0] == 0

    def _at_last_line(self) -> bool:
        return self.cursor_location[0] == self.document.line_count - 1

    def _reset_nav(self) -> None:
        self._hist_idx = None
        self._draft = ""

    def _show(self, text: str) -> None:
        """Replace the box with ``text`` and drop the cursor at the end.

        Recalling a history entry that is a slash command must NOT pop the
        autocomplete menu: it would capture Up/Down (the slash menu owns those keys
        while open) and trap the user on that entry, unable to keep scrolling
        history. Flag the resulting change so on_text_area_changed skips slash
        activation — the menu still appears the moment the user actually edits the
        recalled text. Only arm the flag when the text really changes, since an
        unchanged assignment fires no Changed event to consume it."""
        if text != self.text:
            self._suppress_slash = True
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
        """Rows the box should occupy: one per *visual* line, clamped to the
        [min, max] window. The box soft-wraps, so a single long logical line
        can occupy several rows — ``document.line_count`` would under-count
        those and the box wouldn't grow while text visibly wrapped."""
        lines = self.wrapped_document.height
        return max(self._MIN_LINES, min(lines, self._MAX_LINES))

    def _resize(self) -> None:
        # +2 for the box border's top and bottom rows (see styles.tcss), so the
        # visible text area, not the outer box, tracks the [min, max] window.
        self.styles.height = self._target_height() + 2

    def on_resize(self) -> None:
        # A width change re-wraps the text, changing the visual line count —
        # re-fit. (Our own height writes land here too, but re-setting an
        # unchanged height is a no-op, so this can't loop.)
        self._resize()

    def on_text_area_changed(self, event: "TextArea.Changed") -> None:
        self._resize()
        # A history recall set this text (see _show): don't open the slash menu for
        # it, so Up/Down keep scrolling history instead of being captured by the
        # menu. The menu still opens once the user edits the recalled text.
        if self._suppress_slash:
            self._suppress_slash = False
            return
        # Slash-command autocomplete: track when the first line starts with /.
        first_line = self.text.split("\n", 1)[0]
        if first_line.startswith("/"):
            self._slash_active = True
            self.post_message(self.SlashChanged(self.text))
        elif self._slash_active:
            self._slash_active = False
            self.post_message(self.SlashDismissed())
