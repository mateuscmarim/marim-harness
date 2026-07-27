import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.controller import TurnController
from marim_harness.runtime.harness import Harness, HarnessConfig, build_collaborators
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps


def test_turn_controller_accepts_collaborators(tmp_path):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    model = FunctionModel(fn)
    deps = _make_deps(tmp_path)
    collabs = build_collaborators(
        model,
        BuiltinToolProvider(),
        deps,
        "You are a coding agent.",
        HarnessConfig(),
        get_model=lambda: model,
    )

    tc = TurnController(
        agent=collabs.agent,
        session=collabs.session,
        checkpoints=collabs.checkpoints,
        hooks=collabs.hooks,
        mcp=collabs.mcp,
        deps=deps,
        get_model=lambda: model,
    )
    assert hasattr(tc, "run_turn")
    assert callable(tc.run_turn)


def _make_tc(model, tmp_path):
    """Build a minimal TurnController backed by real collaborators."""
    deps = _make_deps(tmp_path)
    collabs = build_collaborators(
        model,
        BuiltinToolProvider(),
        deps,
        "You are a coding agent.",
        HarnessConfig(),
        get_model=lambda: model,
    )
    tc = TurnController(
        agent=collabs.agent,
        session=collabs.session,
        checkpoints=collabs.checkpoints,
        hooks=collabs.hooks,
        mcp=collabs.mcp,
        deps=deps,
        get_model=lambda: model,
    )
    return tc


@pytest.mark.anyio
async def test_failed_turn_preserves_user_prompt(tmp_path):
    """When run_turn raises, the user's prompt survives in session history."""

    def raising_fn(messages, info):
        raise RuntimeError("turn boom")

    tc = _make_tc(FunctionModel(raising_fn), tmp_path)
    with pytest.raises(RuntimeError):
        await tc.run_turn("please remember this")
    user_texts = [
        p.content
        for m in tc.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert any("please remember this" in str(t) for t in user_texts)


@pytest.mark.anyio
async def test_actionable_failure_is_surfaced_to_model_next_turn(tmp_path):
    """After an actionable failure, the next turn's prompt carries a short note."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            raise UnexpectedModelBehavior("Exceeded max retries")
        latest = ""
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    latest = str(p.content)
        return ModelResponse(parts=[TextPart(content=latest)])

    tc = _make_tc(FunctionModel(fn), tmp_path)
    with pytest.raises(UnexpectedModelBehavior):
        await tc.run_turn("first request")
    echoed = await tc.run_turn("second request")
    assert "did not complete" in echoed
    assert "second request" in echoed
    # One-shot: a third clean turn carries no stale note.
    again = await tc.run_turn("third request")
    assert "did not complete" not in again


def _minimal_harness(tmp_path):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    return Harness(
        FunctionModel(fn), BuiltinToolProvider(),
        _make_deps(tmp_path),
        instructions="test",
    )


def test_harness_has_no_turn_state_fields(tmp_path):
    """Harness no longer owns mutable turn-state — it lives on TurnController."""
    h = _minimal_harness(tmp_path)
    assert not hasattr(h, "_pending_error_note")
    assert not hasattr(h, "_pending_hook_context")
    assert not hasattr(h, "_pending_jobs_digest")
    assert not hasattr(h, "_active_run_ctx")
    assert not hasattr(h, "_steer_buffer")
    assert hasattr(h, "turn_controller")
    assert h.turn_controller._pending_error_note is None


@pytest.mark.anyio
async def test_non_actionable_failure_leaves_no_note(tmp_path):
    """A plain runtime failure must not pollute the next prompt."""
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("render boom")
        latest = ""
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    latest = str(p.content)
        return ModelResponse(parts=[TextPart(content=latest)])

    tc = _make_tc(FunctionModel(fn), tmp_path)
    with pytest.raises(RuntimeError):
        await tc.run_turn("first request")
    echoed = await tc.run_turn("second request")
    assert "did not complete" not in echoed
    # The date envelope wraps every turn now; the important thing is that no
    # error note from the failed first turn leaked into the second prompt.
    assert echoed.endswith("second request")


# --- new encapsulation methods ---

def test_apply_session_start_context_sets_field(tmp_path):
    """apply_session_start_context writes the pending hook context."""
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    tc = _make_tc(FunctionModel(fn), tmp_path)
    assert tc._pending_hook_context is None
    tc.apply_session_start_context("startup output")
    assert tc._pending_hook_context == "startup output"


def test_clear_pending_jobs_digest_clears_field(tmp_path):
    """clear_pending_jobs_digest sets _pending_jobs_digest to None."""
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    tc = _make_tc(FunctionModel(fn), tmp_path)
    tc._pending_jobs_digest = "some digest"
    tc.clear_pending_jobs_digest()
    assert tc._pending_jobs_digest is None


@pytest.mark.anyio
async def test_assemble_prompt_injects_plan_preamble_in_plan_mode(tmp_path):
    from marim_harness.runtime.permissions import Mode

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    tc = _make_tc(FunctionModel(fn), tmp_path)
    tc.deps.workspace.mode = Mode.plan
    prompt = await tc._assemble_prompt("refactor the parser")
    assert "PLAN MODE" in prompt
    assert prompt.endswith("refactor the parser")


@pytest.mark.anyio
async def test_assemble_prompt_no_preamble_outside_plan_mode(tmp_path):
    from marim_harness.runtime.permissions import Mode

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    tc = _make_tc(FunctionModel(fn), tmp_path)
    tc.deps.workspace.mode = Mode.ask
    prompt = await tc._assemble_prompt("refactor the parser")
    assert "PLAN MODE" not in prompt


def test_session_id_getter_reads_live_store(tmp_path):
    """build_services threads a getter that reads the session controller's store
    live, so a session switch is reflected without rewiring."""
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    tc = _make_tc(FunctionModel(fn), tmp_path)
    getter = tc.deps.services.get_session_id
    assert getter is not None

    class _Store:
        session_id = "sess-live-1"

    tc.session.store = _Store()
    assert getter() == "sess-live-1"
    tc.session.store = None
    assert getter() is None


@pytest.mark.anyio
async def test_run_turn_defers_mcp_when_policy_on(tmp_path, monkeypatch):
    from pydantic_ai import DeferredLoadingToolset
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    captured = {}

    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="ok")]))
    tc = _make_tc(model, tmp_path)
    tc.deps.workspace.tool_search = "on"

    # Stand in two fake MCP servers so deferral has something to wrap.
    class _Srv:
        def __init__(self, name):
            self.id = name

        async def list_tools(self):
            return [1, 2, 3]

        def prefixed(self, prefix):
            # Compose prefixes each live server with its name (see
            # runtime/toolsets.py); mirror AbstractToolset.prefixed.
            from pydantic_ai.toolsets.prefixed import PrefixedToolset

            return PrefixedToolset(self, prefix)

    tc.mcp._live_servers = [_Srv("a"), _Srv("b")]
    tc.mcp.disabled = set()

    async def spy(prompt, deferred_results, toolsets, event_stream_handler, resumable):
        captured["toolsets"] = toolsets
        return "ok"

    monkeypatch.setattr(tc, "_run_with_approval", spy)
    await tc.run_turn("hi")
    assert len(captured["toolsets"]) == 1
    assert isinstance(captured["toolsets"][0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_run_turn_passes_plain_toolsets_when_off(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    captured = {}
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="ok")]))
    tc = _make_tc(model, tmp_path)
    tc.deps.workspace.tool_search = "off"

    class _Srv:
        id = "a"

        async def list_tools(self):
            return [1, 2, 3]

        def prefixed(self, prefix):
            from pydantic_ai.toolsets.prefixed import PrefixedToolset

            return PrefixedToolset(self, prefix)

    servers = [_Srv()]
    tc.mcp._live_servers = servers
    tc.mcp.disabled = set()

    async def spy(prompt, deferred_results, toolsets, event_stream_handler, resumable):
        captured["toolsets"] = toolsets
        return "ok"

    monkeypatch.setattr(tc, "_run_with_approval", spy)
    await tc.run_turn("hi")
    # Inline (not behind DeferredLoadingToolset), each prefixed with its name.
    assert [t.wrapped for t in captured["toolsets"]] == servers


def _ok_model():
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn)


def test_turn_model_is_raw_without_ui_listener(tmp_path):
    """Headless (no bind_ui): no on_ttft callback, so the round runs on the
    raw model object with no wrapper in between."""
    tc = _make_tc(_ok_model(), tmp_path)
    assert tc._turn_model() is tc.get_model()


def test_turn_model_wraps_for_ttft_when_ui_listens(tmp_path):
    """With bind_ui's on_ttft wired, the round's model is wrapped for TTFT
    reporting — but the raw model object is never replaced (its identity
    matters to /model switching and the claude-cli wiring)."""
    from marim_harness.runtime.ttft import TtftTrackingModel

    tc = _make_tc(_ok_model(), tmp_path)
    tc.deps.ui.on_ttft = lambda seconds: None
    round_model = tc._turn_model()
    assert isinstance(round_model, TtftTrackingModel)
    assert round_model.wrapped is tc.get_model()  # raw model untouched underneath


@pytest.mark.anyio
async def test_assemble_prompt_injects_pending_shell_results(tmp_path):
    """A queued `!` result rides the next turn's injected prefix, is consumed
    by that drain, and never re-injects on the following turn."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.add_shell_result("git status", "exit 0\nclean")
    prompt = await tc._assemble_prompt("what changed?")
    assert "<user-shell-commands>" in prompt
    assert "$ git status" in prompt
    assert "exit 0\nclean" in prompt
    assert "what changed?" in prompt
    prompt2 = await tc._assemble_prompt("and now?")
    assert "<user-shell-commands>" not in prompt2


@pytest.mark.anyio
async def test_shell_results_budget_drops_oldest_with_marker(tmp_path):
    """The pending queue is bounded: oldest entries fall off past the character
    budget and the block says how many were elided."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.add_shell_result("first-command", "x" * 15_000)
    tc.add_shell_result("second-command", "y" * 15_000)
    prompt = await tc._assemble_prompt("hi")
    assert "$ first-command" not in prompt
    assert "$ second-command" in prompt
    assert "1 earlier command(s) elided" in prompt


@pytest.mark.anyio
async def test_shell_results_keep_newest_even_if_oversized(tmp_path):
    """A single oversized entry is never dropped to zero — run_bash already caps
    individual outputs, so the newest result is always kept."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.add_shell_result("big", "z" * 50_000)
    prompt = await tc._assemble_prompt("hi")
    assert "$ big" in prompt


def test_harness_add_shell_result_delegates(tmp_path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="t"
    )
    harness.add_shell_result("echo hi", "exit 0\nhi")
    assert harness.turn_controller._pending_shell_results == [("echo hi", "exit 0\nhi")]


def test_clear_job_context_drops_pending_shell_results(tmp_path):
    """/clear, /new, and session switch all route through Harness._clear_job_context
    (verified by reading harness.py: reset(), new_session(), and switch_session()
    each call it), so exercising it directly covers all three call sites."""
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="t"
    )
    harness.add_shell_result("echo hi", "exit 0\nhi")
    harness._clear_job_context()
    assert harness.turn_controller._pending_shell_results == []


def test_clear_job_context_drops_pending_error_note_and_hook_context(tmp_path):
    """A conversation-context change (/clear, /new, /switch) must also drop the
    prior turn's one-shot error note and any unconsumed SessionStart hook
    context — both prepend onto the NEXT prompt, so leaving them set would splice
    a dead conversation's note into a fresh or switched-in one."""
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="t"
    )
    tc = harness.turn_controller
    tc._pending_error_note = "Note: your previous turn did not complete."
    tc.apply_session_start_context("stale startup context")
    harness._clear_job_context()
    assert tc._pending_error_note is None
    assert tc._pending_hook_context is None


def test_clear_pending_context_method(tmp_path):
    """clear_pending_context nulls both the error note and the hook context."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc._pending_error_note = "boom"
    tc._pending_hook_context = "ctx"
    tc.clear_pending_context()
    assert tc._pending_error_note is None
    assert tc._pending_hook_context is None


@pytest.mark.anyio
async def test_clear_pending_shell_results_empties_queue(tmp_path):
    """/clear and session switches drop queued ! results — the next turn of a
    NEW conversation must not inject output from the dead one."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.add_shell_result("git status", "exit 0\nclean")
    tc.clear_pending_shell_results()
    prompt = await tc._assemble_prompt("hi")
    assert "<user-shell-commands>" not in prompt


# ---------------------------------------------------------------------------
# Stage-aware checkpoint invalidation (I2) + the manual /compact entry point (C1)
# ---------------------------------------------------------------------------


def _spy_invalidate(tc) -> list[bool]:
    """Record calls to the checkpoint invalidation without touching real git.

    Repoint the ``on_history_restructured`` seam at the spy too: the controller
    captured the real bound method into the seam at construction time, so a spy
    installed only on ``checkpoints`` would never be reached via the seam."""
    calls: list[bool] = []
    tc.checkpoints.invalidate_after_compaction = lambda: calls.append(True)  # type: ignore[method-assign]
    tc.session.on_history_restructured = tc.checkpoints.invalidate_after_compaction
    return calls


def _fake_compact(tc, *, new_len: int | None):
    """Stub ``session.maybe_compact`` to report success and optionally rewrite
    ``session.history`` to ``new_len`` messages (simulating a summary stage).
    ``new_len=None`` leaves the length untouched — the micro-only case.
    Returns a dict recording the trigger/instructions it was called with.

    Real ``maybe_compact`` fires ``on_history_restructured`` (which the
    controller wires to checkpoint invalidation) precisely when the message
    count changes; the stub replicates that so these tests exercise the wired
    seam rather than the removed post-return invalidate."""
    seen: dict = {}

    async def fake(*, force=False, trigger="auto", instructions=None):
        seen.update(force=force, trigger=trigger, instructions=instructions)
        before = len(tc.session.history)
        if new_len is not None:
            tc.session.history = [object() for _ in range(new_len)]
        if len(tc.session.history) != before and tc.session.on_history_restructured is not None:
            tc.session.on_history_restructured()
        return True

    tc.session.maybe_compact = fake  # type: ignore[method-assign]
    return seen


@pytest.mark.anyio
async def test_micro_only_compaction_keeps_checkpoints(tmp_path):
    """A stage-1-only ('micro') compaction rewrites tool-return content in place
    and moves no message boundary, so the message count is unchanged and the
    checkpoints stay rewindable — they must NOT be invalidated."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.session.history = [object(), object(), object()]
    invalidated = _spy_invalidate(tc)
    _fake_compact(tc, new_len=None)  # micro: same length
    assert await tc._maybe_compact() is True
    assert invalidated == []


@pytest.mark.anyio
async def test_summary_compaction_invalidates_checkpoints(tmp_path):
    """A summary compaction collapses a prefix into one summary message — the
    message count shrinks, so the stale absolute checkpoint indices must be
    invalidated."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.session.history = [object(), object(), object(), object()]
    invalidated = _spy_invalidate(tc)
    _fake_compact(tc, new_len=2)  # summary: collapsed to a shorter list
    assert await tc._maybe_compact() is True
    assert invalidated == [True]


@pytest.mark.anyio
async def test_manual_compact_passes_trigger_and_invalidates_when_restructured(tmp_path):
    """C1: /compact routed through ``manual_compact`` passes trigger='manual'
    and still invalidates stale checkpoints when the history is restructured —
    the wrapper is the single place invalidation happens for every trigger."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.session.history = [object(), object(), object()]
    invalidated = _spy_invalidate(tc)
    seen = _fake_compact(tc, new_len=1)  # collapsed
    assert await tc.manual_compact(instructions="focus on auth") is True
    assert seen["trigger"] == "manual"
    assert seen["instructions"] == "focus on auth"
    assert invalidated == [True]


@pytest.mark.anyio
async def test_manual_compact_keeps_checkpoints_when_micro_only(tmp_path):
    """A manual /compact that only micro-compacts (no restructuring) must also
    preserve the user's rewind history."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.session.history = [object(), object()]
    invalidated = _spy_invalidate(tc)
    _fake_compact(tc, new_len=None)
    assert await tc.manual_compact() is True
    assert invalidated == []


def test_controller_wires_restructure_seam_to_checkpoint_invalidation(tmp_path):
    """The controller must hand its CheckpointManager's invalidator to the
    session as ``on_history_restructured`` — that is what moves invalidation to
    BEFORE the compacted-history persist (crash-safety). If this wiring is
    dropped, a restructuring compaction persists first and invalidates never/late,
    so a crash between the writes leaves stale checkpoints."""
    tc = _make_tc(_ok_model(), tmp_path)
    assert tc.session.on_history_restructured == tc.checkpoints.invalidate_after_compaction


@pytest.mark.anyio
async def test_no_compaction_never_invalidates(tmp_path):
    """When ``maybe_compact`` reports no compaction, checkpoints are untouched
    even though the wrapper ran."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.session.history = [object(), object()]
    invalidated = _spy_invalidate(tc)

    async def fake(*, force=False, trigger="auto", instructions=None):
        return False

    tc.session.maybe_compact = fake  # type: ignore[method-assign]
    assert await tc._maybe_compact() is False
    assert invalidated == []


@pytest.mark.anyio
async def test_harness_manual_compact_delegates_to_controller(tmp_path):
    """``Harness.manual_compact`` is a thin delegate to the turn controller's
    single invalidating entry point."""
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="t"
    )
    seen: dict = {}

    async def fake(instructions=None):
        seen.update(instructions=instructions)
        return True

    harness.turn_controller.manual_compact = fake  # type: ignore[method-assign]
    assert await harness.manual_compact(instructions="hi") is True
    assert seen == {"instructions": "hi"}
