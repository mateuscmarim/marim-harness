import json as _json_capture
import stat as _stat_capture
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
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


_UI_HOOK_FIELDS = {
    "request_approval", "ask_user", "on_present_plan", "on_subagent_event",
    "on_subagent_notice", "on_subagent_model", "on_subagent_usage",
    "detach_fanout", "interactive", "notifier",
}


def _make_deps(root: Path, mode: Mode = Mode.auto, **kw) -> Deps:
    """Shorthand for Deps construction in tests.

    UI-hook kwargs (request_approval, ask_user, etc.) are automatically routed
    into a ``UIHooks`` sub-object; everything else goes to ``Deps`` directly.
    """
    ui_kw = {k: kw.pop(k) for k in list(kw) if k in _UI_HOOK_FIELDS}
    return Deps(
        workspace=WorkspaceConfig(root=root, mode=mode),
        ui=UIHooks(**ui_kw) if ui_kw else UIHooks(),
        **kw,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_lsp_bin_cache():
    """The LSP checker-binary lookups (``checks._ruff_bin``/``_pyright_bin``) are
    process-cached ``lru_cache``s (PATH is stable mid-session). Clear them around
    every test, suite-wide, so a monkeypatched ``shutil.which`` is honored and a
    stubbed PATH never leaks a fake binary path across test files."""
    from marim_harness.lsp import checks

    checks._ruff_bin.cache_clear()
    checks._pyright_bin.cache_clear()
    yield
    checks._ruff_bin.cache_clear()
    checks._pyright_bin.cache_clear()


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


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path_factory, monkeypatch):
    """Point the session store root at an empty per-test dir so the developer's
    real ~/.local/share/marim-harness/sessions never accrues leaked sessions.

    ``SessionStore``/``SessionManager`` resolve their base from ``$XDG_DATA_HOME``
    at construction time when no explicit ``base_dir`` is passed. A test that
    calls ``SessionManager(workspace)`` without ``base_dir=`` — easy to forget —
    would otherwise write a ``{workspace.name}-{digest}/`` dir straight into the
    real store. Isolating the env var makes that mistake harmless suite-wide.
    Tests that pass their own ``base_dir`` ignore this; tests that exercise the
    real XDG path set their own ``XDG_DATA_HOME`` in the body, which overrides."""
    data = tmp_path_factory.mktemp("xdg-data")
    monkeypatch.setenv("XDG_DATA_HOME", str(data))


# Suites whose tests exercise project-local ``.marim/skills`` / ``.marim/agents``.
# Those roots now load only in a TRUSTED workspace (gated behind
# MARIM_TRUST_PROJECT_HOOKS, matching the hooks/MCP gate — a cloned untrusted repo's
# skills/agents are not injected into the agent). These suites predate the gate and
# assume a user working in their own project, so they run trusted. The gate itself
# and the untrusted default are covered explicitly in test_skills.py / test_agents.py;
# test_skills_tool.py carries its own local trust fixture.
_TRUST_PROJECT_SUITES = frozenset({
    "test_agent_backend_field.py",
    "test_agent_hooks.py",
    "test_agent_instructions.py",
    "test_commands.py",
    "test_plugin_skills.py",
    "test_subagent_cli_spawn.py",
    "test_subagent_isolation.py",
    "test_subagent_resume.py",
    "test_subagent_safety.py",
    "test_subagent_transcript_capture.py",
})


@pytest.fixture(autouse=True)
def _trust_project_local_suites(request, monkeypatch):
    """Mark the workspace trusted for the suites in ``_TRUST_PROJECT_SUITES`` so their
    project-local skills/agents load. Scoped by filename rather than applied suite-wide
    because other suites (test_hooks_config.py, test_mcp.py, test_mcp_cli.py) assert the
    UNtrusted default and must keep seeing the env unset."""
    if Path(str(request.node.fspath)).name in _TRUST_PROJECT_SUITES:
        monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")


def _edit_then_done_model() -> FunctionModel:
    """Read a.txt, then edit it, then reply 'done'. The read step satisfies the
    read-before-edit guard (edit_file refuses to modify a file the agent hasn't
    read), mirroring how a real agent works. read_file isn't an approval-gated
    tool, so it runs without a callback — only the edit reaches the approval path.
    Supports both non-streamed and streamed requests."""
    import json as _json

    from pydantic_ai.models.function import DeltaToolCall

    state = {"n": 0}
    stream_state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"})]
            )
        if state["n"] == 2:
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
                    name="read_file",
                    json_args=_json.dumps({"path": "a.txt"}),
                    tool_call_id="tc-read-1",
                )
            }
        elif stream_state["n"] == 2:
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


def _make_harness(model, deps, provider=None, **config_kwargs) -> Harness:
    return Harness(model=model, provider=provider or BuiltinToolProvider(), deps=deps,
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
