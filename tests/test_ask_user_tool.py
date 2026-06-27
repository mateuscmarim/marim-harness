# tests/test_ask_user_tool.py
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.deps import Deps
from marim_harness.tools.provider import BuiltinToolProvider

_QUESTIONS = [
    {
        "question": "Which database?",
        "header": "DB",
        "options": [{"label": "Postgres"}, {"label": "SQLite"}],
    }
]


def _agent() -> Agent:
    agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[])), deps_type=Deps)
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


def test_ask_user_headless_returns_note(tmp_path):
    deps = Deps(workspace_root=tmp_path)  # ask_user is None
    agent = _agent()
    model, captured = _call_tool("ask_user", {"questions": _QUESTIONS})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert "no interactive UI" in captured["ret"]


def test_ask_user_cancelled_returns_note(tmp_path):
    async def cancel(questions):
        return None

    deps = Deps(workspace_root=tmp_path)
    deps.ask_user = cancel
    agent = _agent()
    model, captured = _call_tool("ask_user", {"questions": _QUESTIONS})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert "dismissed" in captured["ret"]


def test_ask_user_returns_header_keyed_json(tmp_path):
    import json

    async def answer(questions):
        return {"DB": "Postgres"}

    deps = Deps(workspace_root=tmp_path)
    deps.ask_user = answer
    agent = _agent()
    model, captured = _call_tool("ask_user", {"questions": _QUESTIONS})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert json.loads(captured["ret"]) == {"DB": "Postgres"}


def test_ask_user_empty_questions_returns_error(tmp_path):
    async def answer(questions):
        raise AssertionError("callback must not run for empty input")

    deps = Deps(workspace_root=tmp_path)
    deps.ask_user = answer
    agent = _agent()
    # a question whose only option has a blank label normalizes away to nothing
    model, captured = _call_tool(
        "ask_user",
        {"questions": [{"question": "x", "header": "x", "options": [{"label": " "}]}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert "at least one question" in captured["ret"]


def test_ask_user_fires_notification(tmp_path):
    from marim_harness.runtime.deps import Deps

    class _Spy:
        def __init__(self):
            self.calls = []

        async def notification(self, notification_type, title, message):
            self.calls.append((notification_type, title, message))

    spy = _Spy()

    async def _answer(questions):
        return {questions[0].header or "q": "yes"}

    deps = Deps(workspace_root=tmp_path, ask_user=_answer)
    deps.services.turn_hooks = spy
    agent = _agent()
    model, _ = _call_tool(
        "ask_user",
        {"questions": [{"question": "Proceed?", "header": "go",
                        "options": [{"label": "yes"}, {"label": "no"}]}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert spy.calls and spy.calls[0][0] == "ask_user"
    assert "Proceed?" in spy.calls[0][2]


def test_ask_user_registered_on_main_not_subagent(tmp_path):
    from marim_harness.tools.provider import _SUBAGENT_FNS

    agent = _agent()
    names = {t.name for t in agent._function_toolset.tools.values()}
    assert "ask_user" in names
    assert "ask_user" not in _SUBAGENT_FNS
