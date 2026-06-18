from pathlib import Path
from types import SimpleNamespace

import pytest

from marim_harness.interfaces.tui.commands import (
    COMMANDS,
    COMMANDS_BY_NAME,
    dispatch,
    resolve_ref,
)
from marim_harness.interfaces.tui.themes import THEME_NAMES
from marim_harness.session import SessionInfo


class _FakeApp:
    """Minimal stand-in for HarnessApp: records posts and spawned turns."""

    def __init__(self, workspace_root: Path | None = None):
        self.posted: list[str] = []
        self.turn_prompts: list[str] = []
        self._turn_worker = None
        self._current_assistant = "sentinel"
        self.harness = SimpleNamespace(
            deps=SimpleNamespace(workspace_root=workspace_root)
        )

    async def post_system(self, msg: str) -> None:
        self.posted.append(msg)

    def _run_turn(self, text: str):
        self.turn_prompts.append(text)
        return ("coro", text)  # stand-in; not awaited by the fake

    def run_worker(self, coro, exclusive=False):
        return ("worker", coro)


def _infos() -> list:
    return [
        SessionInfo(id="alpha", name="Alpha", updated="2026-06-01", message_count=2, tokens=10),
        SessionInfo(id="beta", name="Beta Work", updated="2026-05-01", message_count=4, tokens=20),
    ]


def test_resolve_ref_by_index():
    infos = _infos()
    assert resolve_ref(infos, "1") is infos[0]
    assert resolve_ref(infos, "2") is infos[1]


def test_resolve_ref_by_id_and_name():
    infos = _infos()
    assert resolve_ref(infos, "beta") is infos[1]
    assert resolve_ref(infos, "beta work") is infos[1]  # name, case-insensitive


def test_resolve_ref_misses():
    infos = _infos()
    assert resolve_ref(infos, "9") is None
    assert resolve_ref(infos, "0") is None
    assert resolve_ref(infos, "nope") is None
    assert resolve_ref(infos, "") is None


def test_every_command_has_summary_and_handler():
    for cmd in COMMANDS:
        assert cmd.summary
        assert callable(cmd.handler)


def test_aliases_resolve_to_their_command():
    assert COMMANDS_BY_NAME["quit"] is COMMANDS_BY_NAME["exit"]
    assert COMMANDS_BY_NAME["ls"] is COMMANDS_BY_NAME["sessions"]
    assert COMMANDS_BY_NAME["?"] is COMMANDS_BY_NAME["help"]


def test_new_is_its_own_command_not_a_clear_alias():
    assert COMMANDS_BY_NAME["new"] is not COMMANDS_BY_NAME["clear"]
    assert COMMANDS_BY_NAME["new"].name == "new"


def test_core_commands_present():
    names = ("help", "clear", "sessions", "new", "switch", "name", "mode", "model",
             "remember", "skill", "exit")
    for name in names:
        assert name in COMMANDS_BY_NAME


@pytest.mark.anyio
async def test_usage_command_reports_split_and_cost():
    from pydantic_ai.usage import RunUsage

    app = _FakeApp()
    app.harness = SimpleNamespace(
        session=SimpleNamespace(
            usage=RunUsage(
                input_tokens=56000, output_tokens=2000,
                cache_read_tokens=50000, cache_write_tokens=5000,
            ),
        ),
        model_id="claude-sonnet-4-6",
    )
    await dispatch(app, "/usage")
    msg = app.posted[-1].lower()
    assert "in" in msg and "cached" in msg and "out" in msg
    assert "$" in app.posted[-1]  # cost shown for a priced model


@pytest.mark.anyio
async def test_usage_command_alias_cost_resolves():
    assert COMMANDS_BY_NAME.get("cost") is COMMANDS_BY_NAME.get("usage")


@pytest.mark.anyio
async def test_usage_command_omits_cost_for_unpriced_model():
    from pydantic_ai.usage import RunUsage

    app = _FakeApp()
    app.harness = SimpleNamespace(
        session=SimpleNamespace(usage=RunUsage(input_tokens=1000, output_tokens=200)),
        model_id="some-local-unpriced-model",
    )
    await dispatch(app, "/usage")
    assert "$" not in app.posted[-1]


@pytest.mark.anyio
async def test_remember_empty_arg_shows_usage():
    app = _FakeApp()
    await dispatch(app, "/remember")
    assert app.turn_prompts == []  # no turn spawned
    assert app.posted and "usage" in app.posted[0].lower()


@pytest.mark.anyio
async def test_remember_spawns_turn_with_fact_and_tool_instruction():
    app = _FakeApp()
    await dispatch(app, "/remember the build uses uv")
    assert len(app.turn_prompts) == 1
    prompt = app.turn_prompts[0]
    assert "the build uses uv" in prompt
    assert "remember tool" in prompt
    assert app._turn_worker is not None


def _make_skill(root: Path, name: str, *, description="A skill.", manual=False) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    extra = "disable-model-invocation: true\n" if manual else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\nDo it.\n",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_skill_no_arg_lists_skills(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    _make_skill(ws / ".marim" / "skills", "code-review", description="Reviews diffs.")
    _make_skill(ws / ".marim" / "skills", "deploy", description="Ships it.", manual=True)
    app = _FakeApp(workspace_root=ws)
    await dispatch(app, "/skill")
    assert app.turn_prompts == []  # listing doesn't spawn a turn
    out = app.posted[0]
    assert "code-review" in out
    assert "Reviews diffs." in out
    assert "deploy" in out  # manual-only skills still listed...
    assert "manual-only" in out  # ...but tagged


@pytest.mark.anyio
async def test_skill_no_arg_no_skills(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    app = _FakeApp(workspace_root=tmp_path / "ws")
    await dispatch(app, "/skill")
    assert app.turn_prompts == []
    assert "No skills found" in app.posted[0]


@pytest.mark.anyio
async def test_sessions_marks_the_active_session():
    app = _FakeApp()
    infos = _infos()
    app.harness = SimpleNamespace(
        session=SimpleNamespace(sessions=lambda: infos, session_name="Beta Work")
    )
    await dispatch(app, "/sessions")
    lines = app.posted[-1].splitlines()
    beta_line = next(line for line in lines if "Beta Work" in line)
    alpha_line = next(line for line in lines if "Alpha" in line)
    assert "← active" in beta_line
    assert "← active" not in alpha_line


@pytest.mark.anyio
async def test_mcp_lists_server_status():
    app = _FakeApp()
    app.harness = SimpleNamespace(
        mcp=SimpleNamespace(
            mcp_servers=[
                SimpleNamespace(id="files"),
                SimpleNamespace(id="web"),
                SimpleNamespace(id="idle"),
            ],
            mcp_status={"connected": ["files"], "failed": [("web", "boom")]},
        )
    )
    await dispatch(app, "/mcp")
    out = app.posted[0]
    assert "files" in out and "connected" in out  # live
    assert "web" in out and "boom" in out  # failed, with its error
    assert "idle" in out and "not connected" in out  # configured but not up


@pytest.mark.anyio
async def test_mcp_none_configured():
    app = _FakeApp()
    app.harness = SimpleNamespace(
        mcp=SimpleNamespace(
            mcp_servers=[], mcp_status={"connected": [], "failed": []}
        )
    )
    await dispatch(app, "/mcp")
    assert "No MCP servers configured" in app.posted[0]


class _FakeMcpHarness:
    """A harness stand-in that records enable/disable calls for command tests."""

    def __init__(self, names, *, disabled=(), enable_error=None):
        self._names = list(names)
        # MCP state lives on a nested manager, mirroring the real harness.mcp.
        self.mcp = SimpleNamespace(
            disabled=set(disabled),
            mcp_servers=[SimpleNamespace(id=n) for n in names],
            mcp_status={"connected": list(names), "failed": []},
            configured_names=lambda: list(self._names),
        )
        self.enabled_calls: list[str] = []
        self.disabled_calls: list[str] = []
        self._enable_error = enable_error

    async def disable_server(self, name):
        self.disabled_calls.append(name)
        self.mcp.disabled.add(name)

    async def enable_server(self, name):
        self.enabled_calls.append(name)
        if self._enable_error:
            return self._enable_error
        self.mcp.disabled.discard(name)
        return None


@pytest.mark.anyio
async def test_mcp_lists_disabled_state():
    app = _FakeApp()
    app.harness = _FakeMcpHarness(["files", "web"], disabled={"web"})
    await dispatch(app, "/mcp")
    out = app.posted[0]
    assert "files" in out and "connected" in out
    assert "web" in out and "disabled" in out  # disabled shown distinctly


@pytest.mark.anyio
async def test_mcp_disable_one_server():
    app = _FakeApp()
    app.harness = _FakeMcpHarness(["files", "web"])
    await dispatch(app, "/mcp disable files")
    assert app.harness.disabled_calls == ["files"]
    assert "files" in app.posted[0] and "disabled" in app.posted[0].lower()


@pytest.mark.anyio
async def test_mcp_enable_one_server():
    app = _FakeApp()
    app.harness = _FakeMcpHarness(["files", "web"], disabled={"files"})
    await dispatch(app, "/mcp enable files")
    assert app.harness.enabled_calls == ["files"]
    assert "files" in app.posted[0] and "enabled" in app.posted[0].lower()


@pytest.mark.anyio
async def test_mcp_disable_all_servers():
    app = _FakeApp()
    app.harness = _FakeMcpHarness(["files", "web"])
    await dispatch(app, "/mcp disable all")
    assert app.harness.disabled_calls == ["files", "web"]  # every configured one


@pytest.mark.anyio
async def test_mcp_enable_all_servers():
    app = _FakeApp()
    app.harness = _FakeMcpHarness(["files", "web"], disabled={"files", "web"})
    await dispatch(app, "/mcp enable all")
    assert app.harness.enabled_calls == ["files", "web"]


@pytest.mark.anyio
async def test_mcp_enable_reports_connection_failure():
    app = _FakeApp()
    app.harness = _FakeMcpHarness(["web"], disabled={"web"}, enable_error="boom")
    await dispatch(app, "/mcp enable web")
    assert "boom" in app.posted[0]  # failure surfaced, not swallowed


@pytest.mark.anyio
async def test_mcp_unknown_server_name():
    app = _FakeApp()
    app.harness = _FakeMcpHarness(["files"])
    await dispatch(app, "/mcp disable nope")
    assert app.harness.disabled_calls == []  # nothing toggled
    assert "nope" in app.posted[0]


@pytest.mark.anyio
async def test_mcp_unknown_subcommand_shows_usage():
    app = _FakeApp()
    app.harness = _FakeMcpHarness(["files"])
    await dispatch(app, "/mcp frobnicate")
    assert "usage" in app.posted[0].lower()


def _theme_app() -> "_FakeApp":
    app = _FakeApp()
    app.theme = "marim-teal"
    return app


@pytest.mark.anyio
async def test_theme_no_arg_lists_themes_and_current():
    app = _theme_app()
    await dispatch(app, "/theme")
    out = "\n".join(app.posted)
    for name in THEME_NAMES:
        assert name in out
    assert "marim-teal" in out  # current is shown


@pytest.mark.anyio
async def test_theme_sets_a_valid_theme():
    app = _theme_app()
    await dispatch(app, "/theme marim-amber")
    assert app.theme == "marim-amber"


@pytest.mark.anyio
async def test_theme_rejects_unknown_name():
    app = _theme_app()
    await dispatch(app, "/theme bogus")
    assert app.theme == "marim-teal"  # unchanged
    assert any("bogus" in m for m in app.posted)


def test_theme_is_a_registered_command():
    assert "theme" in COMMANDS_BY_NAME


@pytest.mark.anyio
async def test_theme_setting_already_active_is_silent_noop():
    app = _theme_app()
    await dispatch(app, "/theme marim-teal")
    assert app.theme == "marim-teal"
    assert not app.posted


@pytest.mark.anyio
async def test_skill_with_name_spawns_activation_turn():
    app = _FakeApp()
    await dispatch(app, "/skill code-review only the parser")
    assert len(app.turn_prompts) == 1
    prompt = app.turn_prompts[0]
    assert "code-review" in prompt
    assert "activate_skill" in prompt
    assert "only the parser" in prompt  # extra context threaded through
    assert app._turn_worker is not None
