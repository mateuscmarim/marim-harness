"""Tool-call log widgets: a single call (``ToolCallWidget``) and a folded run of
consecutive calls (``ToolGroupWidget``). ``ToolCallWidget`` special-cases the
file tools — an inline diff for ``edit_file``, highlighted content for
``write_file``/``read_file``."""

import re
import time
from pathlib import Path

from rich.console import RenderableType
from rich.text import Text
from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Collapsible, Static

from .ask_user_render import (
    ask_user_body,
    ask_user_title_tail,
    overall_state,
    parse_ask_user,
)
from .diff import _DIFF_CAP, _reverse_edits, render_edit_diff, render_file_diff
from .format import _SPINNER, _SPINNER_TICK_INTERVAL, format_duration
from .highlight import _LEXERS, _highlight_lines, strip_line_numbers
from .tool_summary import humanize_tool, summarize

# The bash tool prefixes its result with "exit N" (inline) or carries it as the
# first preview line of an offloaded result — a non-zero N is a failed command.
_EXIT_RE = re.compile(r"(?m)^exit (-?\d+)")
# Foreground for failed bash output (the shared error red, see themes.py).
_FAIL_FG = "#d9544f"


class ToolCallWidget(Collapsible):
    """A single tool call: the (clickable) title shows a summary line; expanding
    reveals the arguments and the result."""

    def __init__(
        self, tool_name: str, args: dict, *, workspace_root: Path | None = None
    ) -> None:
        self.tool_name = tool_name
        self.args = args
        # update_tasks renders as a flat one-line breadcrumb, not a card: the live
        # TaskPanel already shows the current checklist (always-visible, up to date),
        # so an expandable body here would just duplicate it. The header digest
        # (``2/5 done · ▸ <current>``) is the temporal marker the panel can't give —
        # "the plan moved here". No arrow, no body, dim line. Doing it in the widget
        # (rather than a renderer hook) keeps the live and restored paths identical,
        # since both construct a ToolCallWidget.
        self._breadcrumb = tool_name == "update_tasks"
        # Where edit_file paths resolve, injected by the renderer (which holds the
        # app) so this leaf widget never reaches back through app.harness.deps.
        # None ⇒ the diff falls back to the simple old/new-string view.
        self._workspace_root = workspace_root
        self.status = "pending"
        self._spin = 0
        self.result_text = ""
        # Show edit diffs uncapped (Ctrl+O / "reveal all" flips this on).
        self.reveal = False
        # Post-edit file text + reconstructed pre-edit text, loaded at finish() so
        # edit_file renders a real before/after diff (gutter line numbers, context,
        # bands). None until then ⇒ the simple old/new-string diff is the fallback.
        self._old_text: str | None = None
        self._new_text: str | None = None
        # Memoized (added, removed) line counts for the title's "+N -M" (see
        # _diff_stat). None forces a first compute; the bool tracks whether the
        # cached counts reflect the loaded before/after file diff vs the simple
        # edit-string diff, so the cache recomputes once when the file text loads.
        self._diff_counts: tuple[int, int] | None = None
        self._diff_counts_loaded = False
        # Syntax highlighting (Pygments tokenization) for a write_file/read_file body
        # is CPU-heavy and synchronous. Doing it inline — at construction for
        # write_file, at result time for read_file — meant a fan-out of N file tools
        # tokenized serially on the single event loop and froze the UI. So the first
        # body render is plain (highlight=False) and the highlighted one is computed
        # off-thread and swapped in (see _schedule_highlight / _highlight_async).
        # ``_ready`` latches once that swap lands; ``_scheduled`` guards against arming
        # the worker twice (on_mount + finish).
        self._highlight_ready = False
        self._highlight_scheduled = False
        # markup=False: tool args/results are arbitrary text (commands, file
        # content, output) that may contain Rich markup syntax like `[/]`.
        self._body = Static(self._render_body(highlight=False), id="tool-body", markup=False)
        # edit_file auto-expands so its diff shows inline; everything else stays
        # collapsed and click-to-expand.
        # title is a Content (not str) on purpose — see _summary; Textual renders
        # it at runtime, but its stub types title as str.
        # Blank the collapse arrows for the breadcrumb so it reads as a plain line
        # rather than a fold the user is invited to open onto nothing (the default
        # ▶/▼ are restored for every other tool).
        super().__init__(
            self._body,
            title=self._summary(),  # pyright: ignore[reportArgumentType]
            collapsed=tool_name != "edit_file",
            collapsed_symbol="" if self._breadcrumb else "▶",
            expanded_symbol="" if self._breadcrumb else "▼",
            classes="tool-breadcrumb" if self._breadcrumb else None,
        )
        # The breadcrumb is a plain status line, not a fold: drop the title's
        # focusability so it can't take Tab focus or show the focus accent. The
        # toggle itself is swallowed in _on_collapsible_title_toggle, so a click
        # or Enter can't open it onto its (empty) body either.
        if self._breadcrumb:
            self._title.can_focus = False

    def _on_collapsible_title_toggle(self, event) -> None:
        """Handle the title's Toggle: swallow it for the breadcrumb (a status line,
        not a fold — a click/Enter must not expand it onto its empty body), and for
        every other tool flip the fold exactly once.

        We drive the toggle ourselves and call ``event.prevent_default()`` rather
        than delegating to ``super()``. This method shadows
        ``Collapsible._on_collapsible_title_toggle`` *by name*, and Textual's
        dispatcher walks the whole MRO invoking every class that defines a handler
        of that name — so a lone Toggle reaches BOTH our override and the base
        handler. The base handler is ``collapsed = not collapsed``; left to run it
        would toggle a second time and cancel ours (the fold never moves; the
        breadcrumb opens anyway). ``prevent_default`` sets the message's
        no-default-action flag, which stops the MRO walk before the base class's
        same-named handler. ``event.stop`` additionally keeps the Toggle from
        bubbling to an enclosing ToolGroupWidget."""
        event.stop()
        event.prevent_default()
        if self._breadcrumb:
            return
        self.collapsed = not self.collapsed

    def _glyph(self) -> tuple[str, str]:
        """The status glyph and its style: an animated spinner while pending (so
        ``·`` is freed up to mean 'separator' only), then ✓/✕/✗."""
        if self.status == "failed":
            return "✗", _FAIL_FG
        if self.status == "denied":
            return "✕", ""
        if self.status == "done":
            return "✓", ""
        return _SPINNER[self._spin], ""

    def _summary(self) -> Content:
        if self.tool_name == "ask_user":
            return self._ask_user_summary()
        glyph, gstyle = self._glyph()
        s = summarize(self.tool_name, self.args)
        target = s.target
        # edit_file appends a +N -M line stat to its path (the diff is the body).
        if self.tool_name == "edit_file":
            added, removed = self._diff_stat()
            target = f"{target} +{added} -{removed}" if target else f"+{added} -{removed}"
        head = f"{s.label} · {target}" if target else s.label
        # Glyph carries the status colour; the (untrusted) head is a literal span so
        # markup in a path/command is never parsed; badges trail dim. The breadcrumb
        # mutes its whole head so it recedes next to real tool actions.
        head_span = (head, "dim") if self._breadcrumb else head
        parts: list = [(f"{glyph} ", gstyle), head_span]
        for b in s.badges:
            parts.extend(("   ", (b, "dim")))
        return Content.assemble(*parts)

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

    def on_mount(self) -> None:
        # Animate the working glyph while pending; the timer is stopped at finish so
        # a finished session isn't left with hundreds of 10Hz no-op ticks.
        self._spinner_timer = self.set_interval(_SPINNER_TICK_INTERVAL, self._tick)
        # write_file's content is known now, so highlight it off-thread post-mount
        # rather than synchronously at construction. (read_file has no result yet —
        # finish() arms it.)
        self._schedule_highlight()

    def _tick(self) -> None:
        if self.status != "pending":
            return
        self._spin = (self._spin + 1) % len(_SPINNER)
        self.title = self._summary()

    def _bash_failed(self) -> bool:
        """True when this is a bash call whose result reports a non-zero exit."""
        if self.tool_name != "bash" or not self.result_text:
            return False
        m = _EXIT_RE.search(self.result_text)
        return bool(m) and m.group(1) != "0"

    def _default_collapsed(self) -> bool:
        """Whether this tool collapses by default: edit_file and failed tools stay
        open (the diff / the error is the point), everything else collapses."""
        if self.status == "failed":
            return False
        return self.tool_name != "edit_file"

    def _highlight(self, code: str, path: str) -> RenderableType:
        """Syntax-highlight ``code`` by the file's extension into a single Text with
        the syntax-baked background stripped, so it inherits the widget background
        instead of rendering a stray dark box. Plain ``code`` on a lexer miss."""
        lexer = _LEXERS.get(Path(path).suffix.lower())
        if not lexer:
            return code
        out = Text()
        for i, line in enumerate(_highlight_lines(code, lexer)):
            if i:
                out.append("\n")
            out.append_text(line)
        return out

    def _result_renderable(self, *, highlight: bool = True) -> RenderableType:
        """The result body, syntax-highlighted when it is file source. ``highlight``
        is False for the first (plain) render before the off-thread highlight lands."""
        if self.tool_name == "read_file" and self.result_text:
            stripped = strip_line_numbers(self.result_text)
            if not highlight:
                return stripped
            return self._highlight(stripped, str(self.args.get("path", "")))
        return self.result_text

    def _primary_renderable(self, *, highlight: bool = True) -> "RenderableType | None":
        """The per-tool body that replaces the raw arg repr: a diff for edit_file,
        highlighted content for write_file. None for tools rendered generically.
        ``highlight`` is False for the first (plain) render before the off-thread
        highlight lands; it gates only write_file content (edit_file's diff has its
        own deferral via _load_diff_async)."""
        if self.tool_name == "edit_file":
            cap = None if self.reveal else _DIFF_CAP
            diff, _, _ = self._edit_diff(cap=cap)
            return diff
        if self.tool_name == "write_file":
            content = str(self.args.get("content", ""))
            if not highlight:
                return content
            return self._highlight(content, str(self.args.get("path", "")))
        return None

    def _diff_stat(self) -> tuple[int, int]:
        """The (added, removed) line counts for the title's "+N -M", memoized.

        The counts are a pure function of immutable inputs — the edit strings while
        pending, plus the loaded before/after file text once available — so they're
        computed once per state and reused. _summary() rebuilds the whole title on
        every 10Hz spinner tick while the call is pending; without this each tick
        would re-run _edit_diff(cap=None), which builds (and throws away) the entire
        diff renderable just to recover two numbers that never change. The cache key
        is whether the file text is loaded, so the counts recompute exactly once when
        finish() swaps the simple edit-string diff for the richer file diff (whose
        difflib counts can differ from the raw edit-line counts)."""
        loaded = self._old_text is not None and self._new_text is not None
        if self._diff_counts is None or self._diff_counts_loaded != loaded:
            _, added, removed = self._edit_diff(cap=None)
            self._diff_counts = (added, removed)
            self._diff_counts_loaded = loaded
        return self._diff_counts

    def _edit_diff(self, *, cap):
        """The edit_file diff renderable + (added, removed) counts: a real
        before/after file diff once the file text is loaded, else the simple
        old/new-string diff."""
        if self._old_text is not None and self._new_text is not None:
            lexer = _LEXERS.get(Path(str(self.args.get("path", ""))).suffix.lower())
            return render_file_diff(
                self._old_text, self._new_text, cap=cap, lexer=lexer
            )
        return render_edit_diff(self.args.get("edits", []), cap=cap)

    def _reversed_file_text(self) -> "tuple[str, str] | None":
        """The blocking half of the diff load: read the just-edited file and
        reconstruct its pre-edit text by reverse-applying the edits. Returns
        ``(old_text, new_text)``, or None on any failure (no workspace root,
        unreadable file, ambiguous reversal). Pure I/O + CPU with no widget mutation,
        so it's safe to run in a worker thread (see ``_load_diff_async``)."""
        root = self._workspace_root
        if root is None:
            return None
        from ....workspace.fs import resolve_in_workspace

        try:
            path = resolve_in_workspace(root, str(self.args.get("path", "")))
            new_text = path.read_text(encoding="utf-8")
        except Exception:
            return None
        old_text = _reverse_edits(new_text, self.args.get("edits", []))
        if old_text is None:
            return None
        return old_text, new_text

    def _load_diff(self) -> None:
        """Synchronous diff load — used only on the unmounted path (a direct
        unit-test construction with no event loop to host a worker). The mounted
        path goes through ``_load_diff_async`` so a large file's read can't stall the
        UI. Best-effort: a failure leaves ``_old_text``/``_new_text`` None and the
        simple diff in place."""
        loaded = self._reversed_file_text()
        if loaded is not None:
            self._old_text, self._new_text = loaded

    async def _load_diff_async(self) -> None:
        """Load the real before/after diff off the UI thread, then swap it in. The
        read + reversal (unbounded by file size) runs in a worker thread so a large
        edited file can't block the event loop; the simple old/new-string diff is
        already on screen and is replaced in place once the file text is available.
        Best-effort — a failed/ambiguous load leaves the simple diff untouched."""
        import asyncio

        loaded = await asyncio.to_thread(self._reversed_file_text)
        if loaded is None:
            return
        self._old_text, self._new_text = loaded
        # _summary/_render_body now take the richer file-diff branch; rebuilding the
        # title also refreshes the cached +N -M counts (the load flips _diff_stat's key).
        self.title = self._summary()
        self._refresh_body()

    def _highlights_a_body(self) -> bool:
        """Whether this tool renders a syntax-highlighted file body worth deferring:
        write_file content (known at construction) or a read_file result (known at
        finish). edit_file highlights its diff through its own _load_diff_async, so
        it's intentionally excluded here."""
        if self.tool_name == "write_file":
            return bool(self.args.get("content"))
        if self.tool_name == "read_file":
            return bool(self.result_text)
        return False

    def _schedule_highlight(self) -> None:
        """Arm the off-thread highlight once the body that needs it exists. The plain
        body is already on screen; this swaps in the highlighted one when ready (see
        _highlight_async). Mounted → a worker; unmounted (a direct unit-test
        construction with no loop to host one) → render synchronously, mirroring
        _load_diff's fallback. Guarded so on_mount + finish can both call it but the
        worker arms at most once."""
        if self._highlight_ready or self._highlight_scheduled or not self._highlights_a_body():
            return
        self._highlight_scheduled = True
        if self.is_mounted:
            self.run_worker(self._highlight_async(), name="highlight")
        else:
            self._highlight_ready = True
            self._body.update(self._render_body())

    async def _highlight_async(self) -> None:
        """Build the highlighted body off the UI thread, then swap it in. Pygments
        tokenization (unbounded by file size) runs in a worker thread so a fan-out of
        file tools can't tokenize serially on the event loop; the plain body rendered
        first stays up until this lands. Best-effort — a failure leaves the plain body
        in place."""
        import asyncio

        try:
            body = await asyncio.to_thread(self._render_body, highlight=True)
        except Exception:
            return
        self._highlight_ready = True
        self._body.update(body)

    def _refresh_body(self) -> None:
        """Re-render the body for display, highlighting only once the off-thread pass
        has landed (else plain, with a swap pending). Keeps every display-path render
        — finish, reveal, diff-load — from re-tokenizing on the loop."""
        self._body.update(self._render_body(highlight=self._highlight_ready))

    def _render_body(self, *, highlight: bool = True) -> RenderableType:
        from rich.console import Group

        # The breadcrumb is title-only — the checklist lives in the TaskPanel.
        if self._breadcrumb:
            return ""
        if self.tool_name == "ask_user":
            return ask_user_body(parse_ask_user(self.args, self.result_text, self.status))
        primary = self._primary_renderable(highlight=highlight)
        if primary is not None:
            if not self.result_text:
                return primary
            # Text() keeps the result literal inside the Group (markup=False).
            return Group(primary, "", Text(self.result_text))

        arg_lines = "\n".join(f"{k}: {v!r}" for k, v in self.args.items())
        if not self.result_text:
            return arg_lines or "(no arguments)"
        result = self._result_renderable(highlight=highlight)
        # A failed command's output is shown red so the error stands out.
        if self.status == "failed" and isinstance(result, str):
            red = Text(result, style=_FAIL_FG)
            return Group(Text(arg_lines), "", red) if arg_lines else red
        if isinstance(result, str):
            return f"{arg_lines}\n\n{result}" if arg_lines else result
        # Text() keeps arg_lines literal: a bare str inside a Group would be
        # markup-parsed by Rich even though the host Static has markup=False.
        return Group(Text(arg_lines), "", result)

    def set_reveal(self, value: bool) -> None:
        """Reveal (uncap) the edit diff and expand, or restore the default
        capped/collapsed state. Driven by the app's Ctrl+O reveal-all toggle."""
        self.reveal = value
        self.collapsed = False if value else self._default_collapsed()
        self._refresh_body()

    def finish(self, result_text: str, status: str = "done") -> None:
        self.status = status
        self.result_text = result_text
        if self.tool_name == "edit_file" and status == "done":
            if self.is_mounted:
                # Read + reverse off the UI thread; the simple diff rendered below
                # shows immediately and the worker swaps in the richer file diff.
                self.run_worker(self._load_diff_async(), name="load-diff")
            else:
                # Unmounted (a direct unit-test construction): no event loop to host
                # a worker, so load inline before the body renders below.
                self._load_diff()
        # A bash command that exited non-zero is a failure: flag it (red ✗ in the
        # title, red output) and keep it expanded so the error is visible.
        if status == "done" and self._bash_failed():
            self.status = "failed"
            self.collapsed = False
        self.title = self._summary()
        self._refresh_body()
        # read_file's result is known now — highlight it off-thread (no-op for tools
        # without a highlighted body, or if already armed at mount).
        self._schedule_highlight()
        timer = getattr(self, "_spinner_timer", None)
        if timer is not None:
            timer.stop()


class ToolGroupWidget(Collapsible):
    """A run of consecutive tool calls folded into one collapsed row. Its children
    are the individual ToolCallWidgets; the title summarizes the batch (total count
    plus a per-tool breakdown), so a burst of reads is one line, not N.

    A group is only created once a run has two-or-more calls — a lone call stays a
    bare ToolCallWidget, since wrapping one tool adds a redundant header and an
    extra click for no grouping benefit. Starts expanded while the run is live and
    folds to a one-line summary once every child finishes."""

    def __init__(self) -> None:
        # Insertion-ordered count per tool name, for the title breakdown.
        self._counts: dict[str, int] = {}
        self._finished = 0
        self._any_failed = False
        self._t0 = time.monotonic()
        self._t_end: float | None = None
        self.body = Vertical(classes="tool-group-body")
        # Open while the run is in flight (live rows visible); folds on finish.
        super().__init__(
            self.body, title=self._summary(), collapsed=False  # pyright: ignore[reportArgumentType]
        )

    def _summary(self) -> Content:
        total = sum(self._counts.values())
        label = "1 tool" if total == 1 else f"{total} tools"
        # "Read ×3 · Grep" — humanized, multiplier only when it repeats.
        parts = [
            f"{humanize_tool(n)} ×{c}" if c > 1 else humanize_tool(n)
            for n, c in self._counts.items()
        ]
        breakdown = " · ".join(parts)
        text = f"≡ {label} · {breakdown}" if breakdown else f"≡ {label}"
        if self._t_end is not None:
            # precise=True keeps a decimal under a minute so a fast batch reads
            # "0.3s" rather than rounding to a "0s" that looks like a bug.
            text = f"{text} · {format_duration(self._t_end - self._t0, precise=True)}"
        # Tool names are our own literals, but bypass markup parsing anyway for
        # consistency with the other Collapsible titles in this module.
        return Content(text)

    def note_child_finished(self, failed: bool = False) -> None:
        """A child call reached a terminal status. Once every child is done, freeze
        the duration into the header and fold the group — unless a child failed, in
        which case stay open so the error stays visible."""
        self._finished += 1
        self._any_failed = self._any_failed or failed
        if self._finished >= sum(self._counts.values()):
            self._t_end = time.monotonic()
            self.title = self._summary()
            self.collapsed = not self._any_failed

    async def add_tool(self, widget: ToolCallWidget) -> None:
        """Mount a tool call into the group and refresh the summary line. The
        widget may already be mounted elsewhere (a lone call promoted into a new
        group); the caller detaches it first."""
        self._counts[widget.tool_name] = self._counts.get(widget.tool_name, 0) + 1
        self.title = self._summary()
        await self.body.mount(widget)
