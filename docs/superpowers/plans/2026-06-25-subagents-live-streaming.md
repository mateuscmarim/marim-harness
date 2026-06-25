# Live-streaming background sub-agents (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A detached/background sub-agent streams its transcript, tools, tokens, and cost live into the sub-agents screen and its inline card — exactly like a foreground spawn — instead of showing the "no live transcript" placeholder until it finishes.

**Architecture:** The streaming path already exists end-to-end and already runs for background jobs; its output is dropped only because `stream_id` is nulled for background runs (`subagents.py`). Thread the spawn's `tool_call_id` through `run_background → _execute_spawn → handler` so events forward to the existing `on_subagent_event` TUI consumer, and on the TUI side mark the card as a background run (a quiet `bg` marker) and let it stream live instead of marking it detached + showing the placeholder. No process-boundary work: a background sub-agent is an in-process `asyncio` task.

**Tech Stack:** Python ≥3.10, Pydantic AI, Textual, pytest (`anyio`), `uv`.

## Global Constraints

- The **model-facing** background-job contract is unchanged: the finished-job digest, `job_output` / `wait_for_job` / `cancel`, the wake scheduler, output spill files, and `jobs.py` semantics are not touched. Streaming is a pure UI overlay.
- A background sub-agent is an **in-process `asyncio` task** — no cross-process mechanism is needed or added.
- **Bash** background jobs (OS processes with an output buffer) are out of scope; they are not sub-agents and do not appear in the sub-agents screen.
- Streaming must tolerate a missing card: `on_subagent_event` already no-ops when `tool_widgets[stream_id]` is absent (e.g. after `/clear`), and must continue to.
- An empty `stream_id` (`""`) means "no UI stream" (headless, or a spawn with no tool-call id); it must never forward to the UI.
- Tooling: `uv run` for everything; ruff line length 100 (lint set `E,F,I`); `uv run pyright` clean (basic mode, src only); coverage stays ≥90 (`--cov-fail-under=90` is on by default); async tests use `@pytest.mark.anyio`. Run `uv run ruff check src tests && uv run pyright && uv run pytest` before claiming a task done (the CI order).

---

### Task 1: Thread `stream_id` through background spawns (engine)

Wire a background spawn's `tool_call_id` all the way to the event handler so its run events forward to the UI, and make an empty `stream_id` mean "don't forward".

**Files:**
- Modify: `src/marim_harness/deps.py:41-44` (the `BackgroundAgentRunner` type alias + comment)
- Modify: `src/marim_harness/subagents.py:198-220` (`handler` forward gate), `:411-421` (`_execute_spawn` handler/notice wiring), `:499-517` (`run_background` signature)
- Modify: `src/marim_harness/tools/provider.py:412-417` (the `spawn_agent` background/auto-detach branch)
- Test: `tests/test_agent_subagents.py`

**Interfaces:**
- Produces:
  - `SubagentRunner.run_background(type: str, task: str, mcp_names=None, max_output_chars=None, model=None, isolation=None, stream_id: str = "") -> str` — when `stream_id` is non-empty **and** a UI listener (`deps.on_subagent_event`) exists, the run's events forward to `on_subagent_event(stream_id, event, usage)`, exactly like a foreground spawn. An empty `stream_id` forwards nothing.
  - `BackgroundAgentRunner = Callable[[str, str, list[str] | None, int | None, str | None, str | None, str], Awaitable[str]]` — the trailing `str` is the spawn's `stream_id` (its `tool_call_id`; `""` when none).
- Consumes: existing `SubagentRunner.handler(stream_id)`, `deps.on_subagent_event`, `RunContext.tool_call_id`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent_subagents.py` (it already imports `Path`, `pytest`, `Deps`, `Mode`, `_make_harness`, `FunctionModel`; add `ModelResponse, TextPart` from `pydantic_ai` if not already imported at the top — check the existing imports and reuse them):

```python
@pytest.mark.anyio
async def test_run_background_streams_events_to_listener(tmp_path: Path):
    """A background spawn with a stream_id + UI listener forwards its run events,
    exactly like a foreground spawn — the Phase 2 live-streaming wiring."""
    recorded: list[str] = []

    async def cb(stream_id, event, usage):
        recorded.append(stream_id)

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="BG ANSWER")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, on_subagent_event=cb)
    h = _make_harness(FunctionModel(fn), deps)
    out = await h.subagents.run_background("explore", "scan", stream_id="call_99")
    assert out == "BG ANSWER"
    assert recorded and all(sid == "call_99" for sid in recorded)


@pytest.mark.anyio
async def test_run_background_without_stream_id_does_not_forward(tmp_path: Path):
    """An empty stream_id (headless / no tool-call id) forwards nothing, even when
    a listener is wired."""
    recorded: list[str] = []

    async def cb(stream_id, event, usage):
        recorded.append(stream_id)

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="X")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, on_subagent_event=cb)
    h = _make_harness(FunctionModel(fn), deps)
    await h.subagents.run_background("explore", "scan")  # no stream_id
    assert recorded == []
```

If `ModelResponse` / `TextPart` are not already imported at the top of the file, add them — the file already uses `from pydantic_ai import ...` style or `from pydantic_ai.messages import ...`; match whatever the existing `fn`-based tests in this file use (they reference `ModelResponse(parts=[TextPart(content=...)])`, so the imports are already present).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_agent_subagents.py::test_run_background_streams_events_to_listener tests/test_agent_subagents.py::test_run_background_without_stream_id_does_not_forward -v`
Expected: FAIL — `test_run_background_streams_events_to_listener` fails because `run_background` has no `stream_id` parameter (TypeError: unexpected keyword argument 'stream_id'), or, once the param exists but isn't wired, because `recorded` stays empty.

- [ ] **Step 3: Make the forward gate treat `stream_id` truthily**

In `src/marim_harness/subagents.py`, in `handler` (around lines 198-201 and 219-220), change both `stream_id is not None` checks to a truthiness check so `""` means "no forward":

```python
        cb = self.deps.on_subagent_event
        hooks_on = self.deps.hooks is not None
        forward = cb is not None and bool(stream_id)
        if not hooks_on and not forward:
            return None
```

and:

```python
                # Forward the whole usage (not just a token total) so the UI can
                # render the cache split and cost, not only the running count.
                if cb is not None and stream_id:
                    await cb(stream_id, event, getattr(ctx, "usage", None))
```

- [ ] **Step 4: Stop suppressing the stream for background runs in `_execute_spawn`**

In `src/marim_harness/subagents.py`, in `_execute_spawn` (around lines 409-421), replace the two `None if background else stream_id` expressions with `stream_id` (the forward gate now handles `""`, and a background run gets the same retry-notice routing as foreground):

```python
        # Foreground passes its tool_call_id; a background spawn now passes its own
        # stream_id too (Phase 2), so it streams to the UI exactly like foreground.
        # An empty stream_id (headless / no id) forwards nothing — handler() gates
        # on truthiness.
        first_event_at: list[float] = []
        probe = (lambda: first_event_at.append(time.perf_counter())) if debug else None
        handler = self.handler(stream_id, on_first_event=probe)
        try:
            # Bound concurrent model runs (the part that hits the provider) so a
            # wide fan-out queues instead of slamming a rate-limited route at once.
            async with self._slot():
                result = await self._run_to_completion(
                    sub, task, run_deps, granted, handler, stream_id,
                )
```

- [ ] **Step 5: Add the `stream_id` parameter to `run_background`**

In `src/marim_harness/subagents.py`, update `run_background` (around lines 499-517). Add a trailing `stream_id: str = ""` parameter and pass it through; extend the docstring to note the Phase 2 behavior:

```python
    async def run_background(
        self, type: str, task: str, mcp_names: list[str] | None = None,
        max_output_chars: int | None = None, model: str | None = None,
        isolation: str | None = None, stream_id: str = "",
    ) -> str:
        """Run a sub-agent as a detached background job: same isolation, mode-based
        reach, and MCP grant as a foreground spawn. When ``stream_id`` is set (the
        launching spawn's tool_call_id) and a UI listener exists, the run streams
        its events to that card live — identical to a foreground spawn (Phase 2);
        with no ``stream_id`` (headless) it streams nothing and the job's result is
        its final report, surfaced when the agent pulls it. Any unknown-server note
        rides along on that report. ``max_output_chars`` applies only as a soft
        instruction here (the report is pulled later via the jobs API, which has no
        spill hook), so a background report is not hard-capped, with the over-budget
        remainder spilled to a workspace file the same way a foreground one is.
        ``model`` optionally overrides the model this spawn runs on.
        ``isolation="worktree"`` runs it in its own git worktree, committing its
        changes to a branch named in the report."""
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=True, stream_id=stream_id,
        )
```

- [ ] **Step 6: Update the `BackgroundAgentRunner` type alias**

In `src/marim_harness/deps.py` (around lines 38-44), add the trailing `str` (the `stream_id`) and update the comment:

```python
# (type, task, mcp_names, max_output_chars, model, isolation, stream_id) -> the
# sub-agent's final report. Like SubAgentRunner; when stream_id is set (the spawn's
# tool_call_id) the detached run also streams its events to the UI (Phase 2).
BackgroundAgentRunner = Callable[
    [str, str, list[str] | None, int | None, str | None, str | None, str],
    Awaitable[str],
]
```

- [ ] **Step 7: Pass the spawn's `tool_call_id` from the `spawn_agent` tool**

In `src/marim_harness/tools/provider.py`, in the background/auto-detach branch of `spawn_agent` (around lines 412-417), pass `ctx.tool_call_id or ""` as the trailing argument so the job's run is wired to the card the tool call already created:

```python
        job_id = ctx.deps.jobs.register(
            "agent", label,
            ctx.deps.services.run_background_agent(
                type, task, mcp_names, budget, model, isolation,
                ctx.tool_call_id or "",
            ),
        )
```

- [ ] **Step 8: Run the new tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_agent_subagents.py -v`
Expected: PASS — both new tests pass and every existing `run_background` / `handler` test in the file still passes (the existing positional callers `run_background("explore", "scan the repo")` and `run_background("explore", "go", None, 200)` are unaffected because `stream_id` is a trailing default).

- [ ] **Step 9: Lint + type-check, then commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/deps.py src/marim_harness/subagents.py src/marim_harness/tools/provider.py tests/test_agent_subagents.py
git commit -m "feat(subagents): stream background spawns to the UI via threaded stream_id"
```

---

### Task 2: `bg` marker + real tool tally for streamed background agents (widgets)

A background agent now streams its steps, so its row/card show the **real** tool tally (not "—" / "ran in background"), plus a quiet `bg` marker. The `detached` flag's meaning narrows from "no transcript" to "ran as a background job → mark it".

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent.py:191-201` (`_paint_header`), `:207-235` (`_paint_activity` — remove the `detached` branch)
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent_stats.py:19-32` (`row_cells`)
- Test: `tests/test_subagent_stats.py`, `tests/test_subagent_card.py`

**Interfaces:**
- Consumes: `SubAgentWidget.detached: bool` (set by Task 3's `note_detached_spawn`). It now means "ran as a background job"; it no longer suppresses the tool tally.
- Produces:
  - `row_cells(agent)` → cell `[1]` is `f"bg · {type} — {title}"` when `agent.detached` (else `f"{type} — {title}"`); cell `[2]` is always `str(agent.tool_count)`.
  - `SubAgentWidget._paint_header` renders a dim `bg` tag after the glyph when `self.detached`.
  - `SubAgentWidget._paint_activity` shows the real `N toolcall(s) · duration` for a finished background agent (no special detached line).

- [ ] **Step 1: Write/replace the failing tests**

In `tests/test_subagent_stats.py`, **replace** `test_row_cells_detached_has_no_tool_count` (lines 46-49) with:

```python
def test_row_cells_detached_shows_bg_tag_and_real_tally():
    # Phase 2: a background agent streams its steps, so it shows its real tool
    # tally; a "bg · " tag marks it as an off-turn (background) run.
    a = FakeAgent(status="pending", detached=True, tool_count=4)
    cells = row_cells(a)
    assert cells[1] == "bg · research — map the codebase"
    assert cells[2] == "4"
```

In `tests/test_subagent_card.py`, add:

```python
def test_detached_card_shows_bg_marker_in_header():
    w = SubAgentWidget("research", "Map it", "sonnet")
    w.detached = True
    w._paint_header()
    assert "bg" in str(w._header.render())


def test_finished_detached_card_shows_real_tool_tally():
    # Phase 2: a background agent streams its steps, so a finished card shows the
    # real tally rather than "ran in background".
    w = SubAgentWidget("research", "Map it", "sonnet")
    w.detached = True
    w.tool_count = 5
    w.finish("ok", status="done")
    line = str(w._activity.render())
    assert "5 toolcall" in line
    assert "ran in background" not in line
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_subagent_stats.py::test_row_cells_detached_shows_bg_tag_and_real_tally tests/test_subagent_card.py::test_detached_card_shows_bg_marker_in_header tests/test_subagent_card.py::test_finished_detached_card_shows_real_tool_tally -v`
Expected: FAIL — `row_cells` still emits `"—"` and no `"bg · "`; the header has no `bg`; the finished detached card still renders `"ran in background"`.

- [ ] **Step 3: Update `row_cells`**

In `src/marim_harness/interfaces/tui/widgets/subagent_stats.py`, replace `row_cells` (lines 19-32) with:

```python
def row_cells(agent) -> list[str]:
    """The six `DataTable` cells for one agent row: glyph, "{type} — {title}",
    tool count, tokens, cost, duration. A background (detached) agent carries a
    quiet "bg · " tag on its label so off-turn agents are tellable at a glance; it
    still shows its real streamed tool tally (Phase 2 streams its steps)."""
    label = f"{agent.agent_type} — {agent.display_title()}"
    if agent.detached:
        label = f"bg · {label}"
    tokens = human_tokens(agent.tokens) if agent.tokens else ""
    return [
        status_glyph(agent.status),
        label,
        str(agent.tool_count),
        tokens,
        agent.cost_text or "",
        agent._duration(),
    ]
```

- [ ] **Step 4: Render the `bg` tag in the card header**

In `src/marim_harness/interfaces/tui/widgets/subagent.py`, replace `_paint_header` (lines 191-201) with:

```python
    def _paint_header(self) -> None:
        # A derived title (not the raw prompt); CSS clips it with an ellipsis to the
        # card width. Content.assemble keeps the (untrusted) title a literal — never
        # markup-parsed — while tinting a failure glyph red so it reads at a glance.
        # A background (detached) spawn carries a dim ``bg`` tag so an off-turn agent
        # is tellable from one running inside the current turn.
        glyph_style = "red" if self.status in ("denied", "failed") else ""
        parts: list = [(f"{self._glyph()} ", glyph_style)]
        if self.detached:
            parts.append(("bg ", "dim"))
        parts.append(f"{self.agent_type} Task — {self.display_title()}")
        self._header.update(Content.assemble(*parts))
```

- [ ] **Step 5: Drop the detached branch from `_paint_activity`**

In `src/marim_harness/interfaces/tui/widgets/subagent.py`, in `_paint_activity` (lines 207-235), **remove** the `elif self.detached:` branch (lines 226-229) so a finished background agent falls through to the real run-summary line. The method becomes:

```python
    def _paint_activity(self) -> None:
        if self.status == "pending":
            # Show the current tool while running; "working…" before the first call.
            self._activity.update(Content(f"↳ {self.activity or 'working…'}"))
        elif self.status in ("failed", "denied"):
            # Surface why it failed (literal + red). The line is clipped to one row
            # by default; if the reason was clipped, a ▸/▾ marks it click-to-expand
            # (the full body also lives in the viewer transcript).
            expandable = self._full_reason != self._fail_reason
            if self._expanded and expandable:
                reason = self._full_reason
            else:
                reason = self._fail_reason or (
                    "denied" if self.status == "denied" else "failed"
                )
            marker = ("  ▾" if self._expanded else "  ▸") if expandable else ""
            # Let the line grow + wrap only while expanded; otherwise it stays one row.
            self._activity.set_class(self._expanded and expandable, "-expanded")
            self._activity.update(Content.assemble((f"↳ {reason}", "red"), (marker, "dim")))
        else:
            # Done: collapse to the run summary (tool tally + frozen duration). A
            # background agent streams its steps too, so its tally is real.
            plural = "" if self.tool_count == 1 else "s"
            self._activity.update(
                Content(f"↳ {self.tool_count} toolcall{plural} · {self._duration()}")
            )
```

- [ ] **Step 6: Update the `detached` field comment**

In `src/marim_harness/interfaces/tui/widgets/subagent.py`, update the comment on the `self.detached` field (lines 116-120) so it reflects the narrowed meaning:

```python
        # True when this spawn ran as a background job (Phase 2). It streams its
        # steps into this card like a foreground spawn, so the tally is real; the
        # flag only drives the quiet ``bg`` marker on the card header and list row.
        # Set by the renderer when the card is mapped to a background job
        # (note_detached_spawn).
        self.detached = False
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_subagent_stats.py tests/test_subagent_card.py -v`
Expected: PASS — the three new/updated tests pass and the rest of both files still passes.

- [ ] **Step 8: Lint + type-check, then commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/widgets/subagent.py src/marim_harness/interfaces/tui/widgets/subagent_stats.py tests/test_subagent_stats.py tests/test_subagent_card.py
git commit -m "feat(tui): bg marker + real tool tally for streamed background sub-agents"
```

---

### Task 3: Stream the detached card live — no placeholder, quiet settle (TUI integration)

Make `note_detached_spawn` mark the card as a background run (so Task 2's `bg` marker shows) and keep it pending for settle, **without** showing the placeholder — the card streams live via the `stream_id` threaded in Task 1. Settle is unchanged (`_fill_detached_card` → `finish()`), so it stays quiet.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py:349-366` (`note_detached_spawn`)
- Test: `tests/test_subagents_screen.py`

**Interfaces:**
- Consumes: `SubAgentWidget.detached` (Task 2's `bg` marker), `row_cells` (Task 2), the Task 1 streaming wiring, and the existing `_fill_detached_card` / `fill_finished_detached_cards` settle path (unchanged).
- Produces: `note_detached_spawn(content, widget, jobs) -> bool` — returns `True` for a detached-spawn handoff after marking `widget.detached = True` and mapping `job_id → widget`; the card shows **no** placeholder.

- [ ] **Step 1: Write/replace the failing tests**

In `tests/test_subagents_screen.py`, **replace** `test_detached_spawn_shows_pane_placeholder` (lines 141-163) with:

```python
@pytest.mark.anyio
async def test_detached_spawn_streams_live_with_bg_marker(tmp_path):
    """Phase 2: a detached spawn is marked as a background run (bg marker + detached
    flag) and kept pending for settle, but streams live into its pane — no
    'no live transcript' placeholder."""
    from marim_harness.interfaces.tui.widgets.subagent_stats import row_cells

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "bg work"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()

        kept = r.note_detached_spawn(
            "Started detached sub-agent job-1, watching…", w, app.harness.deps.jobs
        )
        await pilot.pause()
        assert kept is True
        assert w.detached is True                     # marked as a background run
        assert w.pane._placeholder.display is False    # no placeholder — it streams
        assert "bg" in str(w._header.render())         # bg marker on the card
        assert row_cells(w)[1].startswith("bg · ")     # bg marker on the list row


@pytest.mark.anyio
async def test_stream_event_after_clear_is_a_noop(tmp_path):
    """A background job that streams after /clear (its card cleared from the log)
    must not crash — on_subagent_event no-ops when the parent card is absent."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        r.reset()  # simulate /clear: cards + tool_widgets cleared
        await r.on_subagent_event("ghost", object(), None)  # must not raise
        await pilot.pause()
        assert r.tool_widgets.get("ghost") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_subagents_screen.py::test_detached_spawn_streams_live_with_bg_marker tests/test_subagents_screen.py::test_stream_event_after_clear_is_a_noop -v`
Expected: `test_detached_spawn_streams_live_with_bg_marker` FAILS — `note_detached_spawn` still calls `widget.pane.placeholder()`, so `w.pane._placeholder.display` is `True`. (`test_stream_event_after_clear_is_a_noop` may already pass — the existing guard handles it; that's fine, it pins the contract.)

- [ ] **Step 3: Rewrite `note_detached_spawn`**

In `src/marim_harness/interfaces/tui/stream_render.py`, replace `note_detached_spawn` (lines 349-366) with:

```python
    def note_detached_spawn(self, content: str, widget: "SubAgentWidget", jobs) -> bool:
        """If ``content`` is a detached-spawn handoff, mark the card as a background
        run and map its job_id → card so it settles when the job finishes; return
        True so the caller does NOT finish the card on the handoff text (which is a
        job-id handoff, not the report). Returns False for a normal report, so
        foreground spawns and wait_for_job cards finish as usual.

        Phase 2: a background sub-agent streams its transcript into this card's pane
        live (its run is wired with this spawn's stream_id), so the card shows real
        activity — no 'no live transcript' placeholder. ``widget.detached`` here
        means 'ran as a background job' and drives only the quiet ``bg`` marker on
        the card and list row. Fills at once if the job already settled (a fast job
        can finish before its handoff renders)."""
        job_id = _detached_job_id(content)
        if job_id is None:
            return False
        widget.detached = True  # bg marker; the live stream fills the tally + pane
        self._detached_cards[job_id] = widget
        self._fill_detached_card(job_id, jobs)
        return True
```

(The removed lines are the `widget.activity = "running in background…"` assignment and the `if widget.pane is not None: widget.pane.placeholder()` block — the streamed tool calls now fill the activity line, and the pane streams the transcript.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_subagents_screen.py -v`
Expected: PASS — both new tests pass and the rest of the file still passes.

- [ ] **Step 5: Lint + type-check, then commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/stream_render.py tests/test_subagents_screen.py
git commit -m "feat(tui): stream detached sub-agent cards live, drop the placeholder"
```

---

### Task 4: Full-suite green + manual live verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full suite in CI order**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: ruff clean, pyright clean, all tests pass, coverage ≥90.

- [ ] **Step 2: Manual live-terminal pass (the streaming caveat)**

Pilot tests drive widgets directly and do not prove real-terminal event delivery. Verify live in a real terminal (the Phase 1 testing caveat — Kitty-protocol delivery isn't proven by Pilot):

- Launch the TUI against a real provider (e.g. `MARIM_PROVIDER=openrouter MARIM_MODEL=openrouter/openrouter/owl-alpha uv run marim` — owl-alpha is free), or use the project's usual dev invocation.
- Trigger a background sub-agent (an explicit `background=True` spawn, or a wide fan-out that auto-detaches when `detach_fanout` is on).
- Confirm, in the inline log card and after pressing `ctrl+x`: the detached agent's row/card carry the `bg` marker; its transcript streams into the pane live (not a "no live transcript" placeholder); tokens/tools/cost tick on the row while it runs; on completion the glyph flips to ✓/✕ and the stats freeze, with no extra interruption.

- [ ] **Step 3: No commit**

Verification only — nothing to commit unless the manual pass surfaces a defect (in which case fix it under the relevant task with a regression test).

---

## Out of scope

- **Bash** background jobs (OS processes with an output buffer) — not sub-agents; their behavior is unchanged.
- Any **cross-process** mechanism — the spike showed none is needed; agent jobs are in-process `asyncio` tasks.
- Changes to the **model-facing** job contract (digest, `job_output`, `wait_for_job`, wake scheduler, spill files).
