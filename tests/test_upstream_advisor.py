"""Executable contracts for the upstream Advisor migration (offline models only)."""

from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness import Advisor

from marim_harness.runtime.builder import HarnessBuilder


def make_harness(tmp_path, executor, advisor=None, **kwargs):
    builder = HarnessBuilder(workspace=tmp_path, model=executor)
    if advisor is not None:
        builder.with_advisor("reviewer", **kwargs)
    harness = builder.build()
    if advisor is not None:
        harness._build_advisor_model = lambda model_id: advisor
    return harness


@pytest.mark.anyio
async def test_runtime_uses_upstream_advisor(tmp_path):
    seen = []

    def executor(messages, info):
        seen.extend(p for m in messages for p in m.parts if isinstance(p, ToolReturnPart))
        if seen:
            return ModelResponse(parts=[TextPart("done")])
        tool = next(t for t in info.function_tools if t.name == "advisor")
        assert tool.parameters_json_schema["required"] == ["prompt"]
        return ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "Review the migration"})])

    h = make_harness(
        tmp_path,
        FunctionModel(executor),
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("Check the rollback.")])),
    )
    await h.run_turn("Review")
    assert seen[-1].content == "Check the rollback."


def test_legacy_advisor_module_is_removed():
    with pytest.raises(ModuleNotFoundError, match=r"marim_harness\.capabilities"):
        import_module("marim_harness.capabilities.advisor")


def test_direct_upstream_advisor_defaults():
    defaults = Advisor("test")
    assert defaults.mode == "auto" and defaults.forward_history is False
    assert defaults.max_uses is defaults.max_tokens is defaults.caching is None
    for option in ("id", "description", "defer_loading"):
        with pytest.raises(TypeError):
            Advisor("test", **{option: True})


def returns(messages):
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def consulting_executor(count=1, parallel=False):
    def execute(messages, info):
        if len(returns(messages)) >= count:
            return ModelResponse(parts=[TextPart("done")])
        calls = count if parallel else 1
        return ModelResponse(
            parts=[
                ToolCallPart("advisor", {"prompt": "Review the migration"}, f"a{i}")
                for i in range(calls)
            ]
        )

    return FunctionModel(execute)


@pytest.mark.anyio
async def test_non_native_executor_uses_local_advisor(tmp_path):
    calls = []

    def advise(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart("local advice")])

    h = make_harness(tmp_path, consulting_executor(), FunctionModel(advise))
    assert (await h.run_turn("go")).result == "done"
    assert len(calls) == 1
    assert returns(h.session.history)[0].content == "local advice"


@pytest.mark.anyio
@pytest.mark.parametrize("model_id", ["reviewer", "local:reviewer"])
async def test_runtime_preserves_model_source(tmp_path, model_id):
    advisor = TestModel(custom_output_text="source advice")
    source = Mock()
    source.build.return_value = advisor
    h = (
        HarnessBuilder(workspace=tmp_path, model=consulting_executor())
        .with_config_overrides(model_source=source)
        .with_advisor(model_id)
        .build()
    )
    cap = h._build_turn_advisor(model_id)
    assert cap.model is advisor and cap.mode == "local"
    await h.run_turn("go")
    assert source.build.call_args.args == (model_id,)
    assert returns(h.session.history)[0].content == "source advice"


@pytest.mark.anyio
async def test_runtime_forwards_completed_history(tmp_path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    seen = []

    def advise(messages, info):
        seen.extend(messages)
        assert not info.function_tools
        return ModelResponse(parts=[TextPart("advice")])

    h = make_harness(tmp_path, consulting_executor(), FunctionModel(advise))
    h.session.history = [
        ModelRequest(parts=[UserPromptPart("prior question")]),
        ModelResponse(parts=[TextPart("prior answer")]),
    ]
    await h.run_turn("new question")
    parts = [p for m in seen for p in m.parts]
    assert any(getattr(p, "content", None) == "prior question" for p in parts)
    assert any(getattr(p, "content", None) == "prior answer" for p in parts)
    assert any(getattr(p, "content", None) == "Review the migration" for p in parts)
    assert not any(isinstance(p, ToolCallPart) for p in parts)


@pytest.mark.anyio
async def test_disabled_advisor_has_no_tool_or_guidance(tmp_path):
    from marim_harness.advisor import ADVISOR_GUIDANCE

    def execute(messages, info):
        assert "advisor" not in [t.name for t in info.function_tools]
        assert ADVISOR_GUIDANCE not in (info.instructions or "")
        return ModelResponse(parts=[TextPart("done")])

    await make_harness(tmp_path, FunctionModel(execute)).run_turn("go")


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["claude", "codex"])
async def test_cli_advisor_isolation(tmp_path, monkeypatch, backend):
    from marim_harness.config.claude_cli_model import ClaudeCliModel
    from marim_harness.config.codex_cli_model import CodexCliModel

    cls = ClaudeCliModel if backend == "claude" else CodexCliModel
    parent = cls("reviewer")
    parent.session_ref_getter = lambda: "live-thread"
    closed = []
    used = []

    async def request(self, messages, model_settings, model_request_parameters):
        used.append(self)
        assert self is not parent and self.ephemeral
        assert self.cwd == str(tmp_path)
        assert self.mode_getter() == "plan"
        assert self.session_ref_getter is None
        return ModelResponse(parts=[TextPart("cli advice")])

    async def close(self):
        closed.append(self)

    monkeypatch.setattr(cls, "request", request)
    monkeypatch.setattr(cls, "aclose", close)
    h = make_harness(tmp_path, consulting_executor(), parent)
    await h.run_turn("go")
    assert len(used) == 1 and closed == used and parent not in closed


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["claude", "codex"])
async def test_cli_executor_omits_runtime_advisor(tmp_path, monkeypatch, backend):
    from marim_harness.config.claude_cli_model import ClaudeCliModel
    from marim_harness.config.codex_cli_model import CodexCliModel

    cls = ClaudeCliModel if backend == "claude" else CodexCliModel

    async def request(self, messages, model_settings, model_request_parameters):
        assert "advisor" not in [t.name for t in model_request_parameters.function_tools]
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(cls, "request", request)
    h = make_harness(tmp_path, cls("executor"), TestModel())
    h._build_advisor_model = Mock(side_effect=AssertionError("must not resolve advisor"))
    await h.run_turn("go")
    h._build_advisor_model.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["local", "native", "fallback"])
async def test_explicit_sdk_advisor_composition(tmp_path, monkeypatch, kind):
    from pydantic_ai.native_tools import AdvisorTool
    from pydantic_ai.profiles import ModelProfile

    local = TestModel(custom_output_text="advice")
    if kind == "native":

        class NativeMock(TestModel):
            @property
            def system(self):
                return "anthropic"

            async def request(self, messages, model_settings, model_request_parameters):
                _, model_request_parameters = self.prepare_request(
                    model_settings, model_request_parameters
                )
                self.last_model_request_parameters = model_request_parameters
                return ModelResponse(parts=[TextPart("native done")])

        executor = NativeMock(
            call_tools=[], profile=ModelProfile(supported_native_tools=frozenset({AdvisorTool}))
        )
    else:
        executor = consulting_executor()
    if kind == "local":
        cap = Advisor(local)
    else:
        cap = Advisor("anthropic:reviewer")
        import pydantic_ai.models

        original = pydantic_ai.models.infer_model
        monkeypatch.setattr(
            pydantic_ai.models,
            "infer_model",
            lambda model, **kw: local if model == "anthropic:reviewer" else original(model, **kw),
        )
    h = HarnessBuilder(workspace=tmp_path, model=executor).with_capability(cap).build()
    await h.run_turn("go")
    if kind == "native":
        params = executor.last_model_request_parameters
        assert len([t for t in params.native_tools if isinstance(t, AdvisorTool)]) == 1
        assert not [t for t in params.function_tools if t.name == "advisor"]
    else:
        assert len(returns(h.session.history)) == 1
        assert returns(h.session.history)[0].content == "advice"


@pytest.mark.anyio
@pytest.mark.parametrize("activate_later", [False, True])
async def test_duplicate_advisor_rejected_before_request(tmp_path, activate_later):
    execute = Mock(side_effect=AssertionError("provider must not run"))
    builder = HarnessBuilder(workspace=tmp_path, model=FunctionModel(execute))
    builder.with_capability(Advisor(TestModel()))
    if not activate_later:
        builder.with_advisor("reviewer")
    h = builder.build()
    if activate_later:
        h.set_advisor_model("reviewer")
    with pytest.raises(ValueError, match="either"):
        await h.run_turn("go")
    execute.assert_not_called()


@pytest.mark.anyio
async def test_parallel_consultations_obey_request_cap(tmp_path):
    calls = []

    async def advise(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart("advice")])

    h = make_harness(
        tmp_path, consulting_executor(2, parallel=True), FunctionModel(advise), max_uses=1
    )
    await h.run_turn("go")
    assert len(calls) == 1
    results = [r.content for r in returns(h.session.history)]
    assert results.count("advice") == 1
    assert sum("limit reached" in r for r in results) == 1


@pytest.mark.anyio
async def test_cap_resets_each_model_request(tmp_path):
    calls = []

    def advise(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart("advice")])

    h = make_harness(tmp_path, consulting_executor(2), FunctionModel(advise), max_uses=1)
    await h.run_turn("go")
    assert calls == [1, 1]
    assert [r.content for r in returns(h.session.history)] == ["advice", "advice"]


@pytest.mark.parametrize("max_tokens", [0, 512, 1023, 1024, 2048])
def test_advisor_bounds(tmp_path, max_tokens):
    if max_tokens < 1024:
        with pytest.raises(ValueError, match="1024"):
            make_harness(tmp_path, TestModel(), TestModel(), max_tokens=max_tokens)
    else:
        make_harness(tmp_path, TestModel(), TestModel(), max_tokens=max_tokens)
    for uses in (-1, 0):
        with pytest.raises(ValueError, match="at least 1"):
            Advisor(TestModel(), max_uses=uses)
    for uses in (1, None):
        assert Advisor(TestModel(), max_uses=uses).max_uses == uses


@pytest.mark.parametrize("value,expected", [(None, None), ("0", None), ("2", 2)])
def test_environment_use_cap(monkeypatch, tmp_path, value, expected):
    from marim_harness.runtime.bootstrap import build_harness

    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("MARIM_ADVISOR_MAX_USES", raising=False)
    if value is not None:
        monkeypatch.setenv("MARIM_ADVISOR_MAX_USES", value)
    assert build_harness(tmp_path)._advisor_max_uses == expected


def test_base_dependency_contract():
    from importlib.metadata import requires

    requirements = requires("marim-harness")
    assert any("pydantic-ai-harness==0.31.0" in r and "extra" not in r for r in requirements)
    assert any("pydantic-ai-slim" in r and "<3,>=2.44" in r for r in requirements)


def test_no_legacy_advisor_engine():
    root = Path(__file__).parents[1] / "src/marim_harness"
    text = "\n".join(p.read_text() for p in root.rglob("*.py"))
    for retired in (
        "services.advise",
        "advisor_uses",
        "_CLIP_ATTEMPTS",
        "def consult(",
        "def make_advisor(",
        "def _advise_prompt(",
        "[advisor usage:",
    ):
        assert retired not in text
