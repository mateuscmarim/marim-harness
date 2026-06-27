import json as _json_capture
import stat as _stat_capture
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.harness import Harness
from marim_harness.tools.provider import BuiltinToolProvider


def _capture_script(tmp_path, name: str, outfile) -> str:
    """A hook script that appends its stdin (one JSON payload) + a newline to
    *outfile*, so a test can read back every payload the event fired with."""
    p = tmp_path / name
    p.write_text(
        f'#!/usr/bin/env bash\ncat >> "{outfile}"\nprintf "\\n" >> "{outfile}"\n',
        encoding="utf-8",
    )
    p.chmod(p.stat().st_mode | _stat_capture.S_IEXEC | _stat_capture.S_IRWXU)
    return str(p)


def _read_hits(outfile) -> list:
    """Parse the payloads a _capture_script recorded (one JSON object per line)."""
    text = Path(outfile).read_text(encoding="utf-8") if Path(outfile).exists() else ""
    return [_json_capture.loads(ln) for ln in text.splitlines() if ln.strip()]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    """Point global config discovery at an empty per-test dir so the developer's
    real ~/.config/marim/ never leaks into the suite.

    ``config_dir()`` reads ``$XDG_CONFIG_HOME`` at call time, and the harness
    discovers user-global agents (``$XDG_CONFIG_HOME/marim/agents/*.md``) plus the
    global ``AGENTS.md`` and ``.env`` from there. A developer who configures global
    agents would otherwise get those folded into the *main* agent's instructions —
    e.g. the fan-out tests' fake models gate on ``"sub-agent" in instructions`` to
    tell the sub-agent context apart, and a global delegation policy mentioning
    "sub-agents" silently broke that heuristic. Isolating the dir makes every test
    hermetic. Tests that exercise global config set their own ``XDG_CONFIG_HOME``
    inside the test body, which runs after this fixture and overrides it."""
    cfg = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


def _edit_then_done_model() -> FunctionModel:
    """First model turn: call edit_file. After the tool result: reply 'done'.
    Supports both non-streamed and streamed requests."""
    import json as _json

    from pydantic_ai.models.function import DeltaToolCall

    state = {"n": 0}
    stream_state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="edit_file",
                        args={
                            "path": "a.txt",
                            "edits": [{"old_string": "foo", "new_string": "bar"}],
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    async def stream_fn(messages, info):
        stream_state["n"] += 1
        if stream_state["n"] == 1:
            yield {
                0: DeltaToolCall(
                    name="edit_file",
                    json_args=_json.dumps(
                        {
                            "path": "a.txt",
                            "edits": [{"old_string": "foo", "new_string": "bar"}],
                        }
                    ),
                    tool_call_id="tc-edit-1",
                )
            }
        else:
            yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


def _make_harness(model, deps, **config_kwargs) -> Harness:
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.", **config_kwargs)


def _text_model() -> FunctionModel:
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])
    return FunctionModel(fn)


def _last_instructions(messages) -> str:
    """The instructions attached to the current (most recent) request."""
    result = ""
    for message in messages:
        instr = getattr(message, "instructions", None)
        if instr:
            result = instr
    return result


def _make_subagent_def(ws: Path, name: str = "helper") -> None:
    d = ws / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: A helper.\ntools: [read_file]\n---\n\nHelp out.\n",
        encoding="utf-8",
    )
