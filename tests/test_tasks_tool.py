from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.deps import Deps
from marim_harness.tools.provider import BuiltinToolProvider


def _agent() -> Agent:
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(agent)
    return agent


def _call_tool(tool_name: str, args: dict):
    """A FunctionModel that calls ``tool_name`` once, then echoes its return."""
    state: dict = {}
    captured: dict = {}

    def model(messages, info):
        if not state:
            state["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart":
                    captured["ret"] = str(p.content)
        return ModelResponse(parts=[TextPart(content=captured.get("ret", ""))])

    return FunctionModel(model), captured


def test_update_tasks_mutates_deps(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    agent = _agent()
    model, captured = _call_tool(
        "update_tasks",
        {"todos": [
            {"text": "first", "status": "done"},
            {"text": "second", "status": "in_progress"},
            {"text": "third"},
        ]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert [t.text for t in deps.tasks.items] == ["first", "second", "third"]
    assert [t.status for t in deps.tasks.items] == ["done", "in_progress", "pending"]


def test_update_tasks_returns_summary(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    agent = _agent()
    model, captured = _call_tool(
        "update_tasks",
        {"todos": [{"text": "a", "status": "done"}, {"text": "b"}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert "2 tasks" in captured["ret"]
    assert "1 done" in captured["ret"]


def test_update_tasks_replaces_previous_list(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    deps.tasks.replace([{"text": "old", "status": "pending"}])
    agent = _agent()
    model, _ = _call_tool("update_tasks", {"todos": [{"text": "new"}]})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert [t.text for t in deps.tasks.items] == ["new"]


def test_update_tasks_is_not_approval_gated(tmp_path):
    """It only mutates in-memory session state, so it must run with no approval
    round (the run completes and echoes the summary)."""
    deps = Deps(workspace_root=tmp_path)
    agent = _agent()
    model, captured = _call_tool("update_tasks", {"todos": [{"text": "x"}]})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert "1 tasks" in captured["ret"]


class _TaskSpy:
    def __init__(self):
        self.subjects = []

    async def task_completed(self, task_subject, task_id=None, task_description=""):
        self.subjects.append(task_subject)


def test_update_tasks_fires_task_completed_for_newly_done(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    spy = _TaskSpy()
    deps.services.turn_hooks = spy
    agent = _agent()
    model, _ = _call_tool(
        "update_tasks",
        {"todos": [{"text": "a", "status": "done"},
                   {"text": "b", "status": "in_progress"},
                   {"text": "c"}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert spy.subjects == ["a"]


def test_update_tasks_does_not_refire_already_done(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    deps.tasks.replace([{"text": "a", "status": "done"}])
    spy = _TaskSpy()
    deps.services.turn_hooks = spy
    agent = _agent()
    model, _ = _call_tool(
        "update_tasks",
        {"todos": [{"text": "a", "status": "done"},
                   {"text": "b", "status": "done"}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert spy.subjects == ["b"]


def test_update_tasks_no_hooks_is_safe(tmp_path):
    deps = Deps(workspace_root=tmp_path)  # turn_hooks defaults None
    agent = _agent()
    model, _ = _call_tool("update_tasks", {"todos": [{"text": "x", "status": "done"}]})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert [t.text for t in deps.tasks.items] == ["x"]
