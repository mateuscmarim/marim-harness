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
    assert echoed == "second request"


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


@pytest.mark.anyio
async def test_clear_pending_shell_results_empties_queue(tmp_path):
    """/clear and session switches drop queued ! results — the next turn of a
    NEW conversation must not inject output from the dead one."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.add_shell_result("git status", "exit 0\nclean")
    tc.clear_pending_shell_results()
    prompt = await tc._assemble_prompt("hi")
    assert "<user-shell-commands>" not in prompt
