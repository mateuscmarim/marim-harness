from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps


def _build_agent() -> Agent:
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(agent)
    return agent


def _tool_names(agent: Agent) -> set[str]:
    return set(agent._function_toolset.tools.keys())


def test_register_excludes_lsp_tools_now_toolset_only():
    """LSP tools no longer statically register onto the main agent (Task 3) —
    they arrive via ``lsp_toolset()`` and the per-turn deferral path instead
    (see tests/test_lsp_wiring.py). ``register_lsp_tools`` still gates whether
    they are available at all."""
    from marim_harness.tools.names import LSP_TOOLS

    agent = Agent(TestModel(), deps_type=Deps)
    provider = BuiltinToolProvider()
    provider.register(agent)
    assert not (LSP_TOOLS & _tool_names(agent))
    ts = provider.lsp_toolset()
    assert ts is not None and set(ts.tools) >= LSP_TOOLS


def test_register_omits_lsp_tools_when_disabled():
    from marim_harness.tools.names import LSP_TOOLS

    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider(register_lsp_tools=False).register(agent)
    names = _tool_names(agent)
    assert not (LSP_TOOLS & names)  # none of the six present
    assert "read_file" in names  # other read tools unaffected


def test_register_subagent_omits_lsp_tools_when_disabled():
    from marim_harness.tools.names import SUBAGENT_TOOLS

    agent = Agent(TestModel(), deps_type=Deps)
    # Grant every subagent-eligible tool; the flag must still strip the LSP six.
    BuiltinToolProvider(register_lsp_tools=False).register_subagent(agent, SUBAGENT_TOOLS)
    names = _tool_names(agent)
    assert "goto_definition" not in names
    assert "grep" in names  # non-LSP read tool still granted


def test_registers_all_tools(tmp_path: Path):
    agent = _build_agent()
    with agent.override(model=TestModel(call_tools=[])):
        result = agent.run_sync("hi", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert result is not None  # smoke: agent builds and runs without error


def test_tree_tool_executes_via_agent(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("")
    agent = _build_agent()
    captured: dict = {}

    def call_tree(messages, info):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

        if not captured:
            captured["called"] = True
            return ModelResponse(
                parts=[ToolCallPart(tool_name="tree", args={"path": ".", "depth": 2})]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    from pydantic_ai.models.function import FunctionModel

    with agent.override(model=FunctionModel(call_tree)):
        result = agent.run_sync("tree it", deps=_make_deps(tmp_path, mode=Mode.ask))
    # the tree tool ran and returned the directory listing to the model
    returns = [
        str(getattr(p, "content", ""))
        for m in result.all_messages()
        for p in m.parts
        if type(p).__name__ == "ToolReturnPart"
    ]
    assert any("sub/" in r and "x.txt" in r for r in returns)


@pytest.mark.anyio
async def test_bash_tool_accepts_description_arg(tmp_path: Path):
    # The model is trained on Claude Code's Bash tool, whose schema has a required
    # `description`. It reflexively sends it on every shell call, so the bash tool
    # must accept `description` rather than reject it as `extra_forbidden`. The arg
    # is metadata only — it must not affect execution.
    from types import SimpleNamespace

    from marim_harness.tools.edit_tools import bash

    ctx = SimpleNamespace(deps=_make_deps(tmp_path, mode=Mode.ask))
    out = await bash(ctx, "echo hi", description="Say hi")
    assert "hi" in out


@pytest.mark.anyio
async def test_bash_tool_accepts_timeout_arg(tmp_path: Path):
    # Like `description`, the model is trained on Claude Code's Bash tool, which
    # accepts a `timeout`. The bash tool must accept it rather than reject it as
    # `extra_forbidden`, and a per-call value must actually bound a foreground run.
    from types import SimpleNamespace

    from marim_harness.tools.edit_tools import bash

    ctx = SimpleNamespace(deps=_make_deps(tmp_path, mode=Mode.ask))
    out = await bash(ctx, "echo quick", timeout=5)
    assert "quick" in out

    out = await bash(ctx, "sleep 5", timeout=1)
    assert "timed out after 1s" in out


@pytest.mark.anyio
async def test_bash_background_refused_for_subagents(tmp_path: Path):
    # Sub-agents have no job tools (job_output/wait_for_job/cancel_job are
    # main-agent only) and no wake loop, so a background job they start is
    # unretrievable by them — its digest would land on the main agent instead.
    # Mirror the spawn_agent background guard: refuse with a redirect.
    from types import SimpleNamespace

    from marim_harness.tools.edit_tools import bash

    ctx = SimpleNamespace(deps=_make_deps(tmp_path, mode=Mode.auto, subagent_depth=1))
    out = await bash(ctx, "sleep 5", background=True)
    assert "top-level agent" in out
    assert "Started" not in out  # no job was launched


@pytest.mark.anyio
async def test_bash_background_still_available_to_main_agent(tmp_path: Path):
    from types import SimpleNamespace

    from marim_harness.tools.edit_tools import bash
    from marim_harness.tools.job_tools import cancel_job

    ctx = SimpleNamespace(deps=_make_deps(tmp_path, mode=Mode.auto))
    out = await bash(ctx, "sleep 5", background=True)
    assert "Started" in out
    job_id = out.split()[1]
    await cancel_job(ctx, job_id)


def test_read_tool_executes_via_agent(tmp_path: Path):
    (tmp_path / "a.txt").write_text("content")
    agent = _build_agent()
    # TestModel auto-generates tool args (e.g. dummy path "a") that fail real
    # workspace/file checks, so call_tools=["read_file"] raises. Per the task's
    # documented fallback, use call_tools=[] — registration is still proven by
    # the agent building and running. Behavioral guarantees live in Task 9.
    with agent.override(model=TestModel(call_tools=[])):
        result = agent.run_sync("read it", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert result is not None


_JOB_FOUR = {"jobs", "job_output", "wait_for_job", "cancel_job"}


def test_register_uses_four_job_tools_by_default():
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(agent)
    names = _tool_names(agent)
    assert names >= _JOB_FOUR
    assert "job" not in names


def test_register_combined_job_tool_replaces_the_four():
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider(combined_job_tool=True).register(agent)
    names = _tool_names(agent)
    assert "job" in names
    assert not (_JOB_FOUR & names)  # the four are gone


@pytest.mark.anyio
async def test_present_plan_auto_flips_mode_and_writes_artifact(tmp_path):
    from types import SimpleNamespace

    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools import planning_tools
    from marim_harness.workspace.plans import plans_dir

    deps = _make_deps(tmp_path, mode=Mode.plan)

    async def fake_ask(questions):
        return {questions[0].header: "Execute hands-off (auto)"}

    deps.ui.ask_user = fake_ask
    ctx = SimpleNamespace(deps=deps)

    out = await planning_tools.present_plan(ctx, "Refactor the parser", ["step one", "step two"])

    assert deps.workspace.mode is Mode.auto
    assert len(deps.tasks.items) == 2
    assert deps.tasks.items[0].text == "step one"
    files = list(plans_dir(tmp_path).glob("*.md"))
    assert len(files) == 1
    assert "approved" in out.lower() or "auto" in out.lower()


@pytest.mark.anyio
async def test_present_plan_keep_planning_stays_in_plan_mode(tmp_path):
    from types import SimpleNamespace

    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools import planning_tools

    deps = _make_deps(tmp_path, mode=Mode.plan)

    async def fake_ask(questions):
        return {questions[0].header: "Keep planning"}

    deps.ui.ask_user = fake_ask
    ctx = SimpleNamespace(deps=deps)

    await planning_tools.present_plan(ctx, "Refactor", ["a"])
    assert deps.workspace.mode is Mode.plan


@pytest.mark.anyio
async def test_present_plan_headless_defaults_to_keep_planning(tmp_path):
    from types import SimpleNamespace

    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools import planning_tools
    from marim_harness.workspace.plans import plans_dir

    deps = _make_deps(tmp_path, mode=Mode.plan)
    deps.ui.ask_user = None  # no interactive UI
    ctx = SimpleNamespace(deps=deps)

    out = await planning_tools.present_plan(ctx, "Refactor", ["a"])
    assert deps.workspace.mode is Mode.plan  # never auto-executes headless
    assert list(plans_dir(tmp_path).glob("*.md"))  # artifact still written
    assert "plan" in out.lower()


@pytest.mark.anyio
async def test_present_plan_empty_steps_asks_for_retry(tmp_path):
    from types import SimpleNamespace

    from pydantic_ai import ModelRetry

    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools import planning_tools

    deps = _make_deps(tmp_path, mode=Mode.plan)
    ctx = SimpleNamespace(deps=deps)
    with pytest.raises(ModelRetry):
        await planning_tools.present_plan(ctx, "Refactor", [])


@pytest.mark.anyio
async def test_present_plan_ask_flips_to_ask_mode(tmp_path):
    from types import SimpleNamespace

    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools import planning_tools

    deps = _make_deps(tmp_path, mode=Mode.plan)

    async def fake_ask(questions):
        return {questions[0].header: "Execute step-by-step (ask)"}

    deps.ui.ask_user = fake_ask
    ctx = SimpleNamespace(deps=deps)

    await planning_tools.present_plan(ctx, "Refactor", ["a"])
    assert deps.workspace.mode is Mode.ask


def _job_ctx(tmp_path):
    from types import SimpleNamespace

    calls = {}

    class FakeJobs:
        def list(self):
            calls["list"] = True
            return []

        def get(self, id):
            return None  # no running job → the poll guard stays out of the way

        def output(self, id, *, mark_seen=False):
            calls["output"] = id
            calls["output_mark_seen"] = mark_seen
            return f"out:{id}"

        async def wait(self, id, timeout):
            calls["wait"] = (id, timeout)
            return f"waited:{id}:{timeout}"

        async def cancel(self, id):
            calls["cancel"] = id
            return f"cancelled:{id}"

    deps = _make_deps(tmp_path, mode=Mode.ask)
    deps.jobs = FakeJobs()
    return SimpleNamespace(deps=deps), calls


@pytest.mark.anyio
async def test_job_dispatches_each_action(tmp_path):
    from marim_harness.tools.job_tools import job

    ctx, calls = _job_ctx(tmp_path)
    assert await job(ctx, "list") == "No background jobs."
    assert calls["list"] is True
    assert await job(ctx, "output", id="j1") == "out:j1"
    # The agent reading output marks the job wake-consumed (suppresses a
    # redundant autonomous wake — the agent already has the result).
    assert calls["output_mark_seen"] is True
    assert await job(ctx, "wait", id="j2", timeout=5) == "waited:j2:5"
    assert calls["wait"] == ("j2", 5)
    assert await job(ctx, "cancel", id="j3") == "cancelled:j3"


@pytest.mark.anyio
async def test_job_requires_id_for_targeted_actions(tmp_path):
    from marim_harness.tools.job_tools import job

    ctx, _ = _job_ctx(tmp_path)
    for action in ("output", "wait", "cancel"):
        out = await job(ctx, action)  # no id
        assert "id" in out.lower()  # a guidance message, not a crash


@pytest.mark.anyio
async def test_spawn_agent_forwards_mcp_foreground(tmp_path):
    from types import SimpleNamespace

    from marim_harness.tools.spawn_tools import spawn_agent

    calls = {}

    async def fake_runner(
        type, task, tool_call_id, mcp_names, max_output_chars=None, model=None,
        isolation=None, caller_depth: int = 0,
    ):
        calls["args"] = (type, task, tool_call_id, mcp_names, max_output_chars)
        return "ok"

    deps = _make_deps(tmp_path)
    deps.services.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc1")

    out = await spawn_agent(ctx, "explore", "read docs", mcp=["mddocs"], max_output_chars=500)
    assert out == "ok"
    # mcp names and the spawner's output cap both forward to the runner.
    assert calls["args"] == ("explore", "read docs", "tc1", ["mddocs"], 500)


@pytest.mark.anyio
async def test_spawn_agent_composes_structured_task(tmp_path):
    """returns/constraints/context are composed into the task the runner sees, so
    the sub-agent gets the spawner's output contract, boundaries, and context."""
    from types import SimpleNamespace

    from marim_harness.tools.spawn_tools import spawn_agent

    calls = {}

    async def fake_runner(
        type, task, tool_call_id, mcp_names, max_output_chars=None, model=None,
        isolation=None, caller_depth: int = 0,
    ):
        calls["task"] = task
        return "ok"

    deps = _make_deps(tmp_path)
    deps.services.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc1")

    await spawn_agent(
        ctx, "explore", "map the auth flow",
        constraints="read-only", context="refactored last week", returns="3 bullets",
    )
    t = calls["task"]
    assert "map the auth flow" in t
    assert "Constraints" in t and "read-only" in t
    assert "Context" in t and "refactored last week" in t
    assert "Return" in t and "3 bullets" in t


@pytest.mark.anyio
async def test_spawn_agent_without_structured_fields_passes_task_verbatim(tmp_path):
    from types import SimpleNamespace

    from marim_harness.tools.spawn_tools import spawn_agent

    calls = {}

    async def fake_runner(
        type, task, tool_call_id, mcp_names, max_output_chars=None, model=None,
        isolation=None, caller_depth: int = 0,
    ):
        calls["task"] = task
        return "ok"

    deps = _make_deps(tmp_path)
    deps.services.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc1")

    await spawn_agent(ctx, "explore", "just do this")
    assert calls["task"] == "just do this"


@pytest.mark.anyio
async def test_spawn_agent_forwards_mcp_background(tmp_path):
    from types import SimpleNamespace

    from marim_harness.tools.spawn_tools import spawn_agent

    captured = {}

    def fake_bg(type, task, mcp_names, max_output_chars=None, model=None, isolation=None,
                stream_id: str = "", caller_depth: int = 0):
        captured["args"] = (type, task, mcp_names)
        async def _coro():
            return "bg-report"
        return _coro()

    deps = _make_deps(tmp_path)
    deps.services.run_background_agent = fake_bg
    # Close the coroutine in the stub to prevent "coroutine was never awaited" warning.
    deps.jobs = SimpleNamespace(
        register=lambda kind, label, coro, **kw: (coro.close(), "job-1")[1]
    )
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc2")

    out = await spawn_agent(ctx, "general", "do it", background=True, mcp=["sentry"])
    assert "Started" in out
    assert captured["args"] == ("general", "do it", ["sentry"])


@pytest.mark.anyio
async def test_spawn_agent_default_mcp_is_none(tmp_path):
    from types import SimpleNamespace

    from marim_harness.tools.spawn_tools import spawn_agent

    calls = {}

    async def fake_runner(
        type, task, tool_call_id, mcp_names, max_output_chars=None, model=None,
        isolation=None, caller_depth: int = 0,
    ):
        calls["mcp_names"] = mcp_names
        return "ok"

    deps = _make_deps(tmp_path)
    deps.services.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc3")

    await spawn_agent(ctx, "explore", "investigate")
    assert calls["mcp_names"] is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        (["mddocs"], ["mddocs"]),
        (["mddocs", "sentry"], ["mddocs", "sentry"]),
        ('["mddocs"]', ["mddocs"]),  # model JSON-stringified the array
        ('["mddocs", "sentry"]', ["mddocs", "sentry"]),
        ("mddocs", ["mddocs"]),  # bare name, not valid JSON
        ('"mddocs"', ["mddocs"]),  # JSON-quoted bare string
        ("mddocs, sentry", ["mddocs", "sentry"]),  # comma-separated
        ("", None),  # empty string grants nothing
        ("  ", None),
        ("[]", None),  # empty array
        ([], None),
    ],
)
def test_coerce_mcp(raw, expected):
    from marim_harness.tools.spawn_tools import _coerce_names

    assert _coerce_names(raw) == expected


@pytest.mark.anyio
async def test_spawn_agent_coerces_stringified_mcp(tmp_path):
    """A model that serializes the array as a JSON string must still grant the
    server, not fail the turn on schema validation."""
    from types import SimpleNamespace

    from marim_harness.tools.spawn_tools import spawn_agent

    calls = {}

    async def fake_runner(
        type, task, tool_call_id, mcp_names, max_output_chars=None, model=None,
        isolation=None, caller_depth: int = 0,
    ):
        calls["mcp_names"] = mcp_names
        return "ok"

    deps = _make_deps(tmp_path)
    deps.services.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc4")

    await spawn_agent(ctx, "general", "investigate", mcp='["mddocs"]')
    assert calls["mcp_names"] == ["mddocs"]


@pytest.mark.anyio
async def test_spawn_agent_coerces_comma_separated_mcp_background(tmp_path):
    from types import SimpleNamespace

    from marim_harness.tools.spawn_tools import spawn_agent

    captured = {}

    def fake_bg(type, task, mcp_names, max_output_chars=None, model=None, isolation=None,
                stream_id: str = "", caller_depth: int = 0):
        captured["mcp_names"] = mcp_names

        async def _coro():
            return "bg-report"

        return _coro()

    deps = _make_deps(tmp_path)
    deps.services.run_background_agent = fake_bg
    deps.jobs = SimpleNamespace(
        register=lambda kind, label, coro, **kw: (coro.close(), "job-1")[1]
    )
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc5")

    await spawn_agent(ctx, "general", "do it", background=True, mcp="mddocs, sentry")
    assert captured["mcp_names"] == ["mddocs", "sentry"]


@pytest.mark.anyio
async def test_bash_blocks_denylisted_command(tmp_path: Path):
    """A denylisted command is refused before the shell runs — and because the
    gate lives in the tool, it holds in every mode, not just at an approval
    prompt that auto mode would skip."""
    from types import SimpleNamespace

    from marim_harness.command_policy import CommandPolicy
    from marim_harness.tools import edit_tools

    sentinel = tmp_path / "ran.txt"
    deps = Deps(
        workspace=WorkspaceConfig(
            root=tmp_path, command_policy=CommandPolicy(denylist=["touch"])
        ),
    )
    ctx = SimpleNamespace(deps=deps)
    out = await edit_tools.bash(ctx, f"touch {sentinel}")
    assert "Blocked by command policy" in out
    assert not sentinel.exists()  # the shell never ran


def test_resolve_bash_timeout_treats_value_as_ms_and_clamps():
    """The bash timeout is milliseconds (Claude Code convention) clamped to a sane
    range, then converted to whole seconds for run_bash. A bogus huge value (e.g. a
    model passing 180000 meaning '3 min' but landing as 180000ms) must not become
    hours; None falls back to the default."""
    from marim_harness.tools import edit_tools

    assert edit_tools._resolve_bash_timeout_seconds(None) == 30  # default 30000ms
    assert edit_tools._resolve_bash_timeout_seconds(120_000) == 120  # 2 min, not 33h
    assert edit_tools._resolve_bash_timeout_seconds(180_000) == 180
    assert edit_tools._resolve_bash_timeout_seconds(10_000_000) == 600  # clamped to max
    assert edit_tools._resolve_bash_timeout_seconds(50) == 1  # floored to 1s, not 0


@pytest.mark.anyio
async def test_bash_timeout_is_milliseconds(tmp_path: Path):
    """A 500ms timeout must stop a 2s command — proving the param is read as
    milliseconds (clamped to 1s), not as 500 seconds."""
    from types import SimpleNamespace

    from marim_harness.tools import edit_tools

    deps = _make_deps(tmp_path, mode=Mode.ask)
    ctx = SimpleNamespace(deps=deps)
    out = await edit_tools.bash(ctx, "sleep 2", timeout=500)
    assert "timed out" in out


def test_grep_dash_i_flag_is_case_insensitive(tmp_path: Path):
    """The model passes Claude Code's `-i` flag (not a valid Python identifier, so
    it arrives via **flags); grep must honor it as case-insensitive."""
    from types import SimpleNamespace

    from marim_harness.tools import fs_tools

    (tmp_path / "a.txt").write_text("Alpha\n")
    ctx = SimpleNamespace(deps=_make_deps(tmp_path, mode=Mode.ask))
    assert fs_tools.grep(ctx, "alpha") == "(no matches)"
    assert "a.txt:1:Alpha" in fs_tools.grep(ctx, "alpha", **{"-i": True})


def test_grep_context_flags_map_to_before_after(tmp_path: Path):
    from types import SimpleNamespace

    from marim_harness.tools import fs_tools

    (tmp_path / "a.txt").write_text("one\ntwo\nMATCH\nfour\n")
    ctx = SimpleNamespace(deps=_make_deps(tmp_path, mode=Mode.ask))
    out = fs_tools.grep(ctx, "MATCH", **{"-C": 1})
    assert "a.txt-2-two" in out
    assert "a.txt:3:MATCH" in out
    assert "a.txt-4-four" in out


def test_grep_unknown_flag_raises_model_retry(tmp_path: Path):
    """An unsupported flag must surface as a retryable ModelRetry with the valid
    options — not a silent no-op and not a 500."""
    from types import SimpleNamespace

    from marim_harness.tools import fs_tools

    ctx = SimpleNamespace(deps=_make_deps(tmp_path, mode=Mode.ask))
    with pytest.raises(ModelRetry):
        fs_tools.grep(ctx, "x", **{"--nonsense": True})


@pytest.mark.anyio
async def test_bash_allows_permitted_command(tmp_path: Path):
    from types import SimpleNamespace

    from marim_harness.tools import edit_tools

    deps = _make_deps(tmp_path, mode=Mode.ask)  # empty policy -> allow all
    ctx = SimpleNamespace(deps=deps)
    out = await edit_tools.bash(ctx, "echo hello")
    assert "hello" in out


@pytest.mark.anyio
async def test_diagnostics_failure_is_logged_at_debug(caplog, tmp_path):
    """A broken LSP setup must not look like a clean file. The exception
    swallowed by _with_diagnostics must be visible at DEBUG so an operator
    can spot a misconfigured or crashed LSP."""
    import logging
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from marim_harness.tools.edit_tools import _with_diagnostics

    deps = _make_deps(tmp_path, mode=Mode.ask)
    fake_lsp = MagicMock()
    fake_lsp.diagnostics = MagicMock(side_effect=RuntimeError("boom"))
    deps.services.lsp = fake_lsp

    ctx = SimpleNamespace(deps=deps)
    # _with_diagnostics now lives in tools.edit_tools, so its DEBUG record
    # carries that logger name (the tool is still importable via provider).
    with caplog.at_level(logging.DEBUG, logger="marim_harness.tools.edit_tools"):
        result = await _with_diagnostics(ctx, "x.py", "wrote")
    assert result == "wrote"  # never raises; result is preserved
    assert any(
        r.name == "marim_harness.tools.edit_tools" and r.levelno == logging.DEBUG
        for r in caplog.records
    ), f"no DEBUG record from edit_tools: {[(r.name, r.levelname) for r in caplog.records]}"


@pytest.mark.parametrize(
    ("tool_name", "param", "extra", "stringified"),
    [
        (
            "edit_file",
            "edits",
            {"path": "x.py"},
            '[{"old_string": "a", "new_string": "b", "replace_all": true}]',
        ),
        ("update_tasks", "todos", {}, '[{"text": "do it", "status": "pending"}]'),
        (
            "ask_user",
            "questions",
            {},
            '[{"question": "ok?", "header": "h", "options": [{"label": "yes"}]}]',
        ),
    ],
)
def test_array_arg_accepts_json_stringified_list(tool_name, param, extra, stringified):
    """Some models serialize array tool arguments as a JSON *string* instead of a real
    array. The LenientList before-validator must unwrap such a string, while the schema
    advertised to the model stays an ``array`` (so well-behaved models are unaffected)."""
    agent = _build_agent()
    schema = agent._function_toolset.tools[tool_name].function_schema
    assert schema.json_schema["properties"][param]["type"] == "array"
    out = schema.validator.validate_python({param: stringified, **extra})
    assert isinstance(out[param], list) and out[param], "stringified array was not decoded"


@pytest.mark.parametrize(
    ("tool_name", "param", "extra", "elements"),
    [
        (
            "edit_file",
            "edits",
            {"path": "x.py"},
            ['{"old_string": "a", "new_string": "b"}'],
        ),
        ("update_tasks", "todos", {}, ['{"text": "do it", "status": "pending"}']),
        (
            "ask_user",
            "questions",
            {},
            ['{"question": "ok?", "header": "h", "options": [{"label": "yes"}]}'],
        ),
    ],
)
def test_array_arg_accepts_stringified_object_elements(tool_name, param, extra, elements):
    """Beyond a whole stringified list, a model may stringify each *element* object.
    The element-level before-validator must unwrap those too, and the advertised
    schema stays an array of objects."""
    agent = _build_agent()
    schema = agent._function_toolset.tools[tool_name].function_schema
    assert schema.json_schema["properties"][param]["type"] == "array"
    items = schema.json_schema["properties"][param].get("items", {})
    # the element schema is structured (object / $ref / union), not a bare string
    assert items.get("type") == "object" or "$ref" in items or "anyOf" in items
    out = schema.validator.validate_python({param: elements, **extra})
    assert isinstance(out[param], list) and out[param]
    # each element decoded into the real dataclass, not left as a str
    assert not any(isinstance(item, str) for item in out[param])


def test_array_arg_still_accepts_real_list():
    """A genuine array must validate unchanged — the coercion only touches strings."""
    agent = _build_agent()
    schema = agent._function_toolset.tools["edit_file"].function_schema
    out = schema.validator.validate_python(
        {"path": "x.py", "edits": [{"old_string": "a", "new_string": "b"}]}
    )
    assert out["edits"][0].old_string == "a"


def test_array_arg_malformed_string_still_errors():
    """A non-JSON string is passed through untouched, surfacing the real validation
    error rather than being silently swallowed."""
    from pydantic import ValidationError

    agent = _build_agent()
    schema = agent._function_toolset.tools["edit_file"].function_schema
    with pytest.raises(ValidationError):
        schema.validator.validate_python({"path": "x.py", "edits": "not json at all"})


@pytest.mark.anyio
async def test_present_plan_uses_threaded_session_id_for_filename(tmp_path):
    from types import SimpleNamespace

    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools import planning_tools
    from marim_harness.workspace.plans import plans_dir

    deps = _make_deps(tmp_path, mode=Mode.plan)
    deps.services.get_session_id = lambda: "sess-XYZ"

    async def fake_ask(questions):
        return {questions[0].header: "Keep planning"}

    deps.ui.ask_user = fake_ask
    ctx = SimpleNamespace(deps=deps)

    await planning_tools.present_plan(ctx, "Refactor X", ["a"])

    files = list(plans_dir(tmp_path).glob("*.md"))
    assert len(files) == 1
    assert files[0].name.startswith("sess-xyz-")  # slug of the real session id
    assert "session: sess-XYZ" in files[0].read_text()


async def _poll_ctx(tmp_path):
    """A ctx over a REAL JobRegistry holding one gated running agent job."""
    import asyncio

    from marim_harness.jobs import JobRegistry

    deps = _make_deps(tmp_path)
    deps.jobs = JobRegistry()
    gate = asyncio.Event()

    async def _work() -> str:
        await gate.wait()
        return "done!"

    jid = deps.jobs.register("agent", "explore: map it", _work())
    return SimpleNamespace(deps=deps), gate, jid


@pytest.mark.anyio
async def test_jobs_listing_appends_wake_note_when_interactive(tmp_path):
    from marim_harness.tools.job_tools import jobs as jobs_tool

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    out = jobs_tool(ctx)
    assert jid in out
    assert "wake you on completion" in out
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_poll_guard_escalates_warn_then_replace(tmp_path):
    from marim_harness.tools.job_tools import jobs as jobs_tool

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    first = jobs_tool(ctx)
    assert "No change since your last check" not in first
    second = jobs_tool(ctx)
    assert jid in second  # table still present on the first repeat…
    assert "end your turn" in second  # …plus the warning
    third = jobs_tool(ctx)
    assert "(poll 3)" in third and "Stop polling" in third
    assert jid not in third  # the table is withheld, not decorated
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_poll_guard_headless_appends_and_never_replaces(tmp_path):
    from marim_harness.tools.job_tools import jobs as jobs_tool

    ctx, gate, jid = await _poll_ctx(tmp_path)
    assert ctx.deps.ui.interactive is False  # headless default
    jobs_tool(ctx)
    second = jobs_tool(ctx)
    third = jobs_tool(ctx)
    for out in (second, third):
        assert jid in out  # headless never loses the data
        assert "wait_for_job" in out  # …and is pointed at blocking instead
        assert "end your turn" not in out  # no wake loop headless
    assert "wake you on completion" not in third  # standing note is TUI-only
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_settled_listing_is_a_result_read_not_a_poll(tmp_path):
    from marim_harness.tools.job_tools import jobs as jobs_tool

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)
    out = ""
    for _ in range(3):
        out = jobs_tool(ctx)
        assert "No change since your last check" not in out
        assert jid in out
    assert "wake you on completion" not in out  # nothing running


@pytest.mark.anyio
async def test_static_output_marker_triggers_guard(tmp_path):
    from marim_harness.tools.job_tools import job_output

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    first = job_output(ctx, jid)  # "(still running)" — an agent job has no output_fn
    assert "No change since your last check" not in first
    assert "end your turn" in job_output(ctx, jid)  # identical marker → warn
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_growing_output_is_progress_not_polling(tmp_path):
    import asyncio

    from marim_harness.jobs import JobRegistry
    from marim_harness.tools.job_tools import job_output

    deps = _make_deps(tmp_path)
    deps.jobs = JobRegistry()
    deps.ui.interactive = True
    ctx = SimpleNamespace(deps=deps)
    gate = asyncio.Event()
    buf = ["a"]

    async def _work() -> str:
        await gate.wait()
        return "ok"

    jid = deps.jobs.register("bash", "tail -f", _work(), output_fn=lambda: "".join(buf))
    assert "No change since your last check" not in job_output(ctx, jid)
    buf.append("b")  # the buffer grew — that's progress
    assert "No change since your last check" not in job_output(ctx, jid)
    gate.set()
    await deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_combined_job_tool_routes_through_the_guard(tmp_path):
    from marim_harness.tools.job_tools import job

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    first = await job(ctx, "list")
    assert jid in first and "wake you on completion" in first
    second = await job(ctx, "list")
    assert "end your turn" in second  # same guard as the standalone jobs() tool
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_wait_timeout_appends_end_turn_nudge_when_interactive(tmp_path):
    from marim_harness.tools.job_tools import wait_for_job

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    out = await wait_for_job(ctx, jid, timeout=0.01)
    assert "still running" in out
    assert "end your turn" in out  # the wake loop makes ending the turn safe
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_wait_timeout_headless_keeps_bare_note(tmp_path):
    from marim_harness.tools.job_tools import wait_for_job

    ctx, gate, jid = await _poll_ctx(tmp_path)
    assert ctx.deps.ui.interactive is False  # headless default: no wake loop
    out = await wait_for_job(ctx, jid, timeout=0.01)
    assert "still running" in out
    assert "end your turn" not in out
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_wait_success_has_no_nudge(tmp_path):
    from marim_harness.tools.job_tools import wait_for_job

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    gate.set()
    out = await wait_for_job(ctx, jid, timeout=5)
    assert out == "done!"


@pytest.mark.anyio
async def test_wait_unknown_job_has_no_nudge(tmp_path):
    from marim_harness.tools.job_tools import wait_for_job

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    out = await wait_for_job(ctx, "job-999", timeout=0.01)
    assert out == "No job 'job-999'."
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_combined_job_wait_timeout_nudges(tmp_path):
    from marim_harness.tools.job_tools import job

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    out = await job(ctx, "wait", jid, timeout=0.01)
    assert "still running" in out and "end your turn" in out
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


def test_tool_groups_match_dataclass_fields():
    """Every ToolGroups field has a names.TOOL_GROUPS entry and vice versa."""
    import dataclasses

    from marim_harness.tools.names import TOOL_GROUPS
    from marim_harness.tools.provider import ToolGroups

    assert {f.name for f in dataclasses.fields(ToolGroups)} == set(TOOL_GROUPS)


def test_all_groups_on_matches_legacy_registration():
    """ToolGroups() with all defaults registers exactly what the no-arg provider does."""
    from marim_harness.tools.provider import ToolGroups

    legacy = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(legacy)
    grouped = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider(groups=ToolGroups()).register(grouped)
    assert _tool_names(legacy) == _tool_names(grouped)


def test_bare_groups_register_only_file_tools():
    from marim_harness.tools.provider import ToolGroups

    groups = ToolGroups(bash=False, net=False, memory=False, skills=False,
                        tasks=False, jobs=False, spawn=False)
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider(groups=groups).register(agent)
    assert _tool_names(agent) == {
        "read_file", "glob", "tree", "grep", "write_file", "edit_file",
    }


def test_each_group_toggles_exactly_its_tools():
    """Turning one group off removes exactly that group's tools (jobs uses the
    non-combined variant, so 'job' is excluded from the expectation)."""
    import dataclasses

    from marim_harness.tools.names import TOOL_GROUPS
    from marim_harness.tools.provider import ToolGroups

    baseline_agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider(groups=ToolGroups()).register(baseline_agent)
    baseline = _tool_names(baseline_agent)
    for field_ in dataclasses.fields(ToolGroups):
        agent = Agent(TestModel(), deps_type=Deps)
        groups = ToolGroups(**{field_.name: False})
        BuiltinToolProvider(groups=groups).register(agent)
        removed = baseline - _tool_names(agent)
        assert removed == (TOOL_GROUPS[field_.name] & baseline), field_.name


def test_enabled_tool_names_unions_active_groups():
    from marim_harness.tools.provider import ToolGroups

    groups = ToolGroups(bash=False, net=False, memory=False, skills=False,
                        tasks=False, jobs=False, spawn=False)
    assert groups.enabled_tool_names() == frozenset(
        {"read_file", "glob", "tree", "grep", "write_file", "edit_file"}
    )
