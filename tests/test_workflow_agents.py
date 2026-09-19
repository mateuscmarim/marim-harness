"""Contracts at the upstream workflow / Marim runner boundary."""

import asyncio
import json
from dataclasses import replace

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness import DynamicWorkflow

from marim_harness.workflows.agents import RunnerWorkflowAgent
from marim_harness.workflows.catalog import WorkflowBinding, build_workflow_bindings
from marim_harness.workflows.errors import WorkflowResultError
from marim_harness.workflows.invocation import WorkflowInvocation, current_workflow
from marim_harness.workspace.agents import AgentDef
from tests.conftest import _make_deps, _make_harness
from tests.fakes import fake_claude_bin, read_claude_argvs


def _role(name, plugin=None, backend="native"):
    return AgentDef(name, "Worker description", "prompt", frozenset(), "test", plugin, backend)


def test_catalog_identifiers_are_unique_deterministic_and_preserve_roles():
    roles = [_role(name) for name in ("claude-deep", "claude_deep", "for", "123", "print")]
    roles.append(_role("deep-review", plugin="review-kit"))
    catalog = build_workflow_bindings(roles)
    assert catalog == build_workflow_bindings(list(reversed(roles)))
    assert len({b.name for b in catalog}) == len(roles)
    assert all(b.name.isidentifier() for b in catalog)
    assert {b.agent_type for b in catalog} == {r.qualified_name for r in roles}
    assert "worker_for" in {b.name for b in catalog}
    assert "review_kit_deep_review" in {b.name for b in catalog}


def test_typed_defaults_require_the_original_roles_and_explicit_names_are_preserved():
    assert build_workflow_bindings([]) == ()
    roles = [_role("researcher"), _role("explore"), _role("research_findings")]
    by_name = {b.name: b for b in build_workflow_bindings(roles)}
    assert by_name["research_findings"].agent_type == "researcher"
    assert by_name["verify_claim"].agent_type == "explore"
    assert len(by_name) == 5
    explicit = WorkflowBinding("custom", "explore", isolation="worktree")
    assert explicit in build_workflow_bindings(roles, [explicit])


@pytest.mark.parametrize("name", ["for", "print", "bad-name", "_private", ""])
def test_explicit_unsafe_names_are_rejected(name):
    with pytest.raises(ValueError, match="safe Python identifier"):
        WorkflowBinding(name, "explore")


def test_invalid_catalog_contracts_are_rejected_before_dispatch():
    with pytest.raises(ValueError, match="Unknown workflow agent"):
        build_workflow_bindings([], [WorkflowBinding("worker", "missing")])
    binding = WorkflowBinding("worker", "explore")
    with pytest.raises(ValueError, match="Duplicate workflow binding"):
        build_workflow_bindings([_role("explore")], [binding, binding])
    with pytest.raises(ValueError, match="object root"):
        WorkflowBinding("worker", "explore", {"type": "array"})
    with pytest.raises(ValueError, match="worktree isolation"):
        WorkflowBinding("worker", "explore", {"type": "object"}, "worktree")


def test_malformed_schema_is_rejected_when_constructing_bridge():
    async def spawn(*args):
        pytest.fail("A malformed schema dispatched a worker")

    schema = {"type": "object", "properties": {"ok": {"type": "unknown"}}}
    with pytest.raises(WorkflowResultError, match="not a valid JSON Schema"):
        RunnerWorkflowAgent(WorkflowBinding("worker", "explore", schema), "worker", spawn)


@pytest.mark.parametrize("name", ["research_findings", "verify_claim"])
def test_explicit_bindings_cannot_replace_shipped_skill_contracts(name):
    binding = WorkflowBinding(name, "explore")
    with pytest.raises(ValueError, match="reserved for a shipped contract"):
        build_workflow_bindings([_role("explore")], [binding])


async def _call(agents, deps, code):
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    toolset = (
        await DynamicWorkflow(agents=agents, forward_usage=False, inherit_model=False)
        .get_toolset()
        .for_run(ctx)
    )
    tool = (await toolset.get_tools(ctx))["run_workflow"]
    return await toolset.call_tool("run_workflow", {"code": code}, ctx, tool)


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["native", "claude-cli"])
async def test_upstream_dispatches_once_through_runner_with_original_role(tmp_path, backend):
    calls = []

    async def spawn(*args):
        calls.append(args)
        return "report"

    role = _role("deep-worker", plugin="plugin", backend=backend)
    binding = build_workflow_bindings([role])[0]
    bridge = RunnerWorkflowAgent(binding, role.description, spawn)
    deps = _make_deps(tmp_path)
    deps.subagent_depth = 1
    invocation = WorkflowInvocation("parent", deps)
    token = current_workflow.set(invocation)
    try:
        result = await _call([bridge], deps, f'await {binding.name}(task="task")')
    finally:
        current_workflow.reset(token)
    assert result == "report"
    assert calls == [
        ("plugin:deep-worker", "task", "parent::wf1", None, None, None, None, 1, None, None, None)
    ]


@pytest.mark.anyio
async def test_concurrent_children_inherit_parent_and_get_distinct_ids(tmp_path):
    active = 0
    peak = 0
    ids = []

    async def spawn(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        ids.append(args[2])
        await asyncio.sleep(0.01)
        active -= 1
        return args[1]

    deps = _make_deps(tmp_path)
    bridge = RunnerWorkflowAgent(WorkflowBinding("worker", "explore"), "worker", spawn)
    token = current_workflow.set(WorkflowInvocation("parent", deps))
    try:
        result = await _call(
            [bridge],
            deps,
            'import asyncio\nawait asyncio.gather(worker(task="a"), worker(task="b"))',
        )
    finally:
        current_workflow.reset(token)
    assert result == ["a", "b"]
    assert peak == 2
    assert ids == ["parent::wf1", "parent::wf2"]


@pytest.mark.anyio
async def test_structured_reports_are_uncapped_decoded_and_validated(tmp_path):
    schema = {
        "type": "object",
        "properties": {"payload": {"type": "string"}},
        "required": ["payload"],
    }
    payload = {"payload": "x" * 30_000}
    reports = iter([json.dumps(payload), '{"payload": 123}'])
    calls = []

    async def spawn(*args):
        calls.append(args)
        return next(reports)

    bridge = RunnerWorkflowAgent(WorkflowBinding("worker", "explore", schema), "worker", spawn)
    assert bridge.output_json_schema() == schema
    deps = _make_deps(tmp_path)
    token = current_workflow.set(WorkflowInvocation("parent", deps))
    try:
        assert await _call([bridge], deps, 'await worker(task="large")') == payload
        with pytest.raises(WorkflowResultError, match="does not match"):
            await bridge.run("bad")
    finally:
        current_workflow.reset(token)
    assert len(calls) == 2
    assert all(args[4] is None and args[9] == schema for args in calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "option",
    [
        {"usage": RunUsage()},
        {"model": TestModel()},
        {"usage_limits": UsageLimits(request_limit=1)},
        {"message_history": []},
    ],
)
async def test_forwarded_run_configuration_is_rejected(tmp_path, option):
    async def spawn(*args):
        pytest.fail("Unsupported settings dispatched a worker")

    bridge = RunnerWorkflowAgent(WorkflowBinding("worker", "explore"), "worker", spawn)
    token = current_workflow.set(WorkflowInvocation("parent", _make_deps(tmp_path)))
    try:
        with pytest.raises(ValueError, match="do not support run option"):
            await bridge.run("task", **option)
    finally:
        current_workflow.reset(token)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["success", "error", "cancel", "announcement"])
async def test_child_card_completes_on_every_exit_and_renderer_errors_are_harmless(
    tmp_path, failure
):
    from marim_harness.interfaces.tui.stream_render import subagent_failed

    completions = []
    dispatched = []
    deps = _make_deps(tmp_path)
    invocation = WorkflowInvocation("parent", deps)

    async def announce(*args):
        if failure == "announcement":
            invocation.aborted = True
        else:
            raise RuntimeError("renderer unavailable")

    def complete(stream, report):
        completions.append((stream, report))
        raise RuntimeError("renderer unavailable")

    async def spawn(*args):
        dispatched.append(args)
        if failure == "error":
            raise ValueError("worker failed")
        if failure == "cancel":
            raise asyncio.CancelledError()
        return "report"

    deps.ui.on_workflow_spawn = announce
    deps.ui.on_workflow_spawn_done = complete
    bridge = RunnerWorkflowAgent(WorkflowBinding("worker", "explore"), "worker", spawn)
    token = current_workflow.set(invocation)
    try:
        if failure == "success":
            assert (await bridge.run("task")).output == "report"
        elif failure == "error":
            with pytest.raises(ValueError):
                await bridge.run("task")
        else:
            with pytest.raises(asyncio.CancelledError):
                await bridge.run("task")
    finally:
        current_workflow.reset(token)
    assert len(completions) == 1
    assert completions[0][0] == "parent::wf1"
    assert subagent_failed(completions[0][1]) is (failure != "success")
    assert len(dispatched) == (0 if failure == "announcement" else 1)


@pytest.mark.anyio
async def test_plain_binding_forwards_worktree_isolation(tmp_path):
    seen = []

    async def spawn(*args):
        seen.append(args)
        return "Worktree report"

    binding = replace(WorkflowBinding("worker", "explore"), isolation="worktree")
    bridge = RunnerWorkflowAgent(binding, "worker", spawn)
    token = current_workflow.set(WorkflowInvocation("parent", _make_deps(tmp_path)))
    try:
        assert (await bridge.run("task")).output == "Worktree report"
    finally:
        current_workflow.reset(token)
    assert seen[0][6] == "worktree"


@pytest.mark.anyio
async def test_task_cancellation_during_announcement_completes_card_without_dispatch(tmp_path):
    entered = asyncio.Event()
    completed = []
    deps = _make_deps(tmp_path)

    async def announce(*args):
        entered.set()
        await asyncio.Event().wait()

    async def spawn(*args):
        pytest.fail("Cancelled announcement dispatched a worker")

    deps.ui.on_workflow_spawn = announce
    deps.ui.on_workflow_spawn_done = lambda *args: completed.append(args)
    bridge = RunnerWorkflowAgent(WorkflowBinding("worker", "explore"), "worker", spawn)
    token = current_workflow.set(WorkflowInvocation("parent", deps))
    try:
        task = asyncio.create_task(bridge.run("task"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        current_workflow.reset(token)
    assert completed == [("parent::wf1", "Sub-agent 'explore' failed: workflow cancelled")]


@pytest.mark.anyio
async def test_native_runner_enforces_schema_and_accounts_one_worker_run(tmp_path):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    deps = _make_deps(tmp_path)
    harness = _make_harness(
        TestModel(call_tools=[], custom_output_args={"ok": True}), deps, workflows_enabled=False
    )
    bridge = RunnerWorkflowAgent(
        WorkflowBinding("worker", "explore", schema), "worker", harness.subagents.run
    )
    token = current_workflow.set(WorkflowInvocation("parent", deps))
    try:
        assert await _call([bridge], deps, 'await worker(task="review")') == {"ok": True}
    finally:
        current_workflow.reset(token)
    assert harness.subagents.session.usage.requests == 1


@pytest.mark.anyio
@pytest.mark.parametrize("valid", [True, False])
async def test_cli_runner_report_is_validated_without_hidden_respawn(tmp_path, monkeypatch, valid):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    report = '{"ok": true}' if valid else '{"ok": "invalid"}'
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": report}]]})
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", binary)
    deps = _make_deps(tmp_path)
    harness = _make_harness(
        TestModel(),
        deps,
        workflows_enabled=False,
        extra_agents=(_role("cli-worker", backend="claude-cli"),),
    )
    bridge = RunnerWorkflowAgent(
        WorkflowBinding("worker", "cli-worker", schema), "worker", harness.subagents.run
    )
    token = current_workflow.set(WorkflowInvocation("parent", deps))
    try:
        if valid:
            assert await _call([bridge], deps, 'await worker(task="review")') == {"ok": True}
        else:
            with pytest.raises(WorkflowResultError, match="does not match"):
                await bridge.run("review")
    finally:
        current_workflow.reset(token)
    assert len(read_claude_argvs(tmp_path)) == 1
    assert harness.subagents.session.usage.output_tokens == 4


def test_available_catalog_omits_builtin_shadowed_by_programmatic_plugin_alias(tmp_path):
    plugin = _role("explore", plugin="specialist")
    h = _make_harness(TestModel(), _make_deps(tmp_path), extra_agents=(plugin,))
    roles = h.subagents.available_agents()
    assert plugin in roles
    assert "explore" not in {role.qualified_name for role in roles}
    bindings = build_workflow_bindings(roles)
    assert "verify_claim" not in {binding.name for binding in bindings}
    assert next(b for b in bindings if b.name == "specialist_explore").agent_type == (
        "specialist:explore"
    )
