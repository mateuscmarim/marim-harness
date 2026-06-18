import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness
from marim_harness.deps import Deps
from marim_harness.lsp.manager import LspManager
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _edit_then_done_model, _make_harness


def _raising_model() -> FunctionModel:
    """A model that fails mid-turn (simulates an API outage, or — the reported
    case — a render error raised by the TUI's event_stream_handler)."""

    def fn(messages, info):
        raise RuntimeError("turn boom")

    return FunctionModel(fn)


def _fail_once_then_echo_model(exc: BaseException) -> FunctionModel:
    """Turn 1 raises ``exc``; every later turn echoes back the latest user
    prompt text it received, so a test can assert what was prepended."""
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            raise exc
        latest = ""
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    latest = str(p.content)
        return ModelResponse(parts=[TextPart(content=latest)])

    return FunctionModel(fn)


def test_actionable_error_note_surfaces_only_model_fixable_failures():
    """Only failures the model itself can act on get a next-turn note. Harness
    or render bugs, cancellations, and transient infra (rate limits, 5xx) get
    None — re-prompting the model wouldn't help and would only add noise."""
    from pydantic_ai.exceptions import (
        ModelHTTPError,
        UnexpectedModelBehavior,
        UsageLimitExceeded,
    )
    from textual.markup import MarkupError

    from marim_harness.agent import _actionable_error_note

    # Not the model's to fix.
    assert _actionable_error_note(MarkupError("bad markup")) is None
    assert _actionable_error_note(RuntimeError("a render bug")) is None
    assert _actionable_error_note(asyncio.CancelledError()) is None
    assert _actionable_error_note(
        ModelHTTPError(status_code=429, model_name="m")
    ) is None  # rate limit — transient
    assert _actionable_error_note(
        ModelHTTPError(status_code=503, model_name="m")
    ) is None  # server error — transient

    # The model can adjust and continue from these.
    assert _actionable_error_note(
        ModelHTTPError(status_code=400, model_name="m", body="too long")
    ) is not None
    assert _actionable_error_note(
        UnexpectedModelBehavior("Exceeded maximum retries")
    ) is not None
    assert _actionable_error_note(UsageLimitExceeded("limit reached")) is not None


@pytest.mark.anyio
async def test_actionable_failure_is_surfaced_to_model_next_turn(tmp_path: Path):
    """After an actionable failure, the next turn's prompt carries a short note
    so the model knows the prior turn did not complete and can adjust."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(
        _fail_once_then_echo_model(UnexpectedModelBehavior("Exceeded max retries")),
        deps,
    )
    with pytest.raises(UnexpectedModelBehavior):
        await harness.run_turn("first request")
    echoed = await harness.run_turn("second request")
    assert "did not complete" in echoed  # the note rode along
    assert "second request" in echoed  # ...prepended to the real prompt
    # And it is one-shot: a third, clean turn carries no stale note.
    again = await harness.run_turn("third request")
    assert "did not complete" not in again


@pytest.mark.anyio
async def test_non_actionable_failure_leaves_no_note(tmp_path: Path):
    """A plain harness/render failure must not pollute the next prompt — the
    model can't fix it, so surfacing it would only mislead."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_fail_once_then_echo_model(RuntimeError("render boom")), deps)
    with pytest.raises(RuntimeError):
        await harness.run_turn("first request")
    echoed = await harness.run_turn("second request")
    assert "did not complete" not in echoed
    assert echoed == "second request"


@pytest.mark.anyio
async def test_failed_turn_preserves_user_prompt_in_history(tmp_path: Path):
    """When a turn raises, the user's prompt must survive in history so the
    session can continue instead of forgetting the request entirely."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_raising_model(), deps)
    with pytest.raises(RuntimeError):
        await harness.run_turn("please remember this request")
    user_texts = [
        p.content
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert any("please remember this request" in str(t) for t in user_texts)


@pytest.mark.anyio
async def test_failed_turn_persists_so_a_new_harness_can_resume(tmp_path: Path):
    """A turn that fails must still be persisted to the store, so a resumed
    session sees the lost prompt rather than starting blank."""
    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    harness = Harness(
        model=_raising_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    with pytest.raises(RuntimeError):
        await harness.run_turn("a request that crashed the turn")

    resumed = Harness(
        model=_edit_then_done_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x", store=store,
    )
    resumed.resume()
    user_texts = [
        p.content
        for m in resumed.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert any("a request that crashed the turn" in str(t) for t in user_texts)


# ---------------------------------------------------------------------------
# LspManager lifecycle wiring
# ---------------------------------------------------------------------------


def _minimal_harness(tmp_path: Path):
    """Build a Harness with the simplest valid wiring for lifecycle tests."""
    from pydantic_ai.models.test import TestModel

    return Harness(
        TestModel(),
        BuiltinToolProvider(),
        Deps(workspace_root=tmp_path),
        instructions="test",
    )


def test_harness_wires_lsp_manager(tmp_path):
    h = _minimal_harness(tmp_path)
    assert isinstance(h.lsp, LspManager)
    assert h.deps.lsp is h.lsp


@pytest.mark.anyio
async def test_harness_aclose_shuts_down_lsp(tmp_path):
    h = _minimal_harness(tmp_path)
    closed = {"n": 0}

    async def fake_aclose():
        closed["n"] += 1

    h.lsp.aclose = fake_aclose  # type: ignore[method-assign]
    await h.aclose()
    assert closed["n"] == 1


# ---------------------------------------------------------------------------
# Model switching
# ---------------------------------------------------------------------------


def _named_model(model_id: str) -> FunctionModel:
    """A model whose every reply names the id it was built for, so a test can
    tell which model actually ran a turn."""
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=f"from {model_id}")])

    return FunctionModel(fn)


class _FakeSource:
    """Stand-in for config.ModelSource: builds id-tagged models, no network."""

    def __init__(self) -> None:
        self.built: list[str] = []

    def build(self, model_id: str) -> FunctionModel:
        self.built.append(model_id)
        return _named_model(model_id)

    def label(self, model_id: str) -> str:
        return f"fake/{model_id}"

    @property
    def is_local(self) -> bool:
        return False

    async def list_models(self):
        return []


def _switch_harness(tmp_path, *, source=None, summarizer=None, titler=None):
    from marim_harness.agent import HarnessConfig
    from marim_harness.session import SessionManager

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    return Harness(
        model=_named_model("startup"), provider=BuiltinToolProvider(), deps=deps,
        instructions="x",
        config=HarnessConfig(
            store=manager.create(), manager=manager,
            model_source=source, model_id="startup",
            summarizer=summarizer, titler=titler,
        ),
    )


async def _fake_titler(messages) -> str:
    return "Generated Title"


@pytest.mark.anyio
async def test_set_model_switches_model_and_label(tmp_path: Path):
    src = _FakeSource()
    h = _switch_harness(tmp_path, source=src)
    h.set_model("openai/gpt-5.2")
    assert h.model_id == "openai/gpt-5.2"
    assert h.model_label == "fake/openai/gpt-5.2"
    assert src.built == ["openai/gpt-5.2"]
    out = await h.run_turn("hello")
    assert out == "from openai/gpt-5.2"  # the new model actually ran the turn


@pytest.mark.anyio
async def test_set_model_rebuilds_configured_aux_agents(tmp_path: Path):
    async def summarizer(messages):
        return "s"

    h = _switch_harness(tmp_path, source=_FakeSource(),
                        summarizer=summarizer, titler=_fake_titler)
    old_summarizer, old_titler = h.session.summarizer, h.session.titler
    h.set_model("openai/gpt-5.2")
    assert h.session.summarizer is not old_summarizer  # repointed at the new model
    assert h.session.titler is not old_titler


@pytest.mark.anyio
async def test_set_model_leaves_unconfigured_aux_alone(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())  # no summarizer/titler
    h.set_model("openai/gpt-5.2")
    assert h.session.summarizer is None  # not fabricated
    assert h.session.titler is None


def test_set_model_without_source_is_noop(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(model=_named_model("startup"), provider=BuiltinToolProvider(),
                deps=deps, instructions="x", model_id="startup")
    h.set_model("openai/gpt-5.2")  # no source -> nothing changes
    assert h.model_id == "startup"


def test_set_model_persists_to_session(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())
    h.set_model("openai/gpt-5.2")
    assert h.session.store.model == "openai/gpt-5.2"
    assert h.session.manager.store(h.session.store.session_id).model == "openai/gpt-5.2"


@pytest.mark.anyio
async def test_switch_session_restores_its_model(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())
    h.set_model("openai/gpt-5.2")
    alpha_id = h.session.store.session_id

    # A fresh session reverts to the startup model...
    h.new_session("beta")
    h.set_model("anthropic/claude-sonnet-4-6")
    assert h.model_id == "anthropic/claude-sonnet-4-6"

    # ...and switching back restores alpha's saved model.
    h.switch_session(alpha_id)
    assert h.model_id == "openai/gpt-5.2"
    assert h.model_label == "fake/openai/gpt-5.2"
