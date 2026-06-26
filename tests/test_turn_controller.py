import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness, HarnessConfig, build_collaborators
from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.turn_controller import TurnController


def test_turn_controller_accepts_collaborators(tmp_path):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    model = FunctionModel(fn)
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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
        Deps(workspace_root=tmp_path, mode=Mode.auto),
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
