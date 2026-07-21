import asyncio
import subprocess
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
from marim_harness.mcp.manager import McpStatus
from marim_harness.session import SessionInfo


async def _default_maybe_compact(**kwargs) -> bool:
    return False


class _FakeApp:
    """Minimal stand-in for HarnessApp: records posts and spawned turns."""

    def __init__(self, workspace_root: Path | None = None):
        self.posted: list[str] = []
        self.turn_prompts: list[str] = []
        self._turn_worker = None
        self.turn_busy = False
        self._worker_tasks: list[asyncio.Future] = []
        self.stream = SimpleNamespace(current_assistant="sentinel")
        self.harness = SimpleNamespace(
            deps=SimpleNamespace(
                workspace=SimpleNamespace(root=workspace_root, skill_dirs=None)
            ),
            checkpoints=SimpleNamespace(list=lambda: []),
            session=SimpleNamespace(maybe_compact=_default_maybe_compact),
        )

        self.undone = False
        self.rewound: list[int] = []

    async def post_system(self, msg: str) -> None:
        self.posted.append(msg)

    async def undo_rewind(self) -> None:
        self.undone = True

    async def rewind_to_checkpoint(self, index: int) -> None:
        self.rewound.append(index)

    def _run_turn(self, text: str):
        self.turn_prompts.append(text)
        return ("coro", text)  # stand-in; not awaited by the fake

    def run_worker(self, coro, exclusive=False, group="default", exit_on_error=True):
        # Real coroutines (e.g. /compact's `run()`) are scheduled as tasks so a
        # test can `await drain_workers()` to observe their effects; the
        # `_run_turn` stand-in above returns a plain tuple, not a coroutine, so
        # it falls through to the old no-op behavior other command tests rely on.
        if asyncio.iscoroutine(coro):
            task = asyncio.ensure_future(coro)
            self._worker_tasks.append(task)
            return task
        return ("worker", coro)

    async def drain_workers(self) -> None:
        tasks, self._worker_tasks = self._worker_tasks, []
        if tasks:
            await asyncio.gather(*tasks)

    def start_system_turn(self, prompt: str) -> None:
        self.stream.current_assistant = None
        self._turn_worker = self.run_worker(self._run_turn(prompt), exclusive=True)


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


@pytest.mark.anyio
async def test_rewind_undo_routes_to_undo_rewind():
    app = _FakeApp()
    await dispatch(app, "/rewind undo")
    assert app.undone is True
    assert app.rewound == []  # 'undo' is not treated as a checkpoint number


@pytest.mark.anyio
async def test_rewind_number_still_routes_to_checkpoint():
    app = _FakeApp()
    await dispatch(app, "/rewind 2")
    assert app.rewound == [2]
    assert app.undone is False


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
    monkeypatch.setattr(
        "marim_harness.workspace.skills.builtin_root",
        lambda: tmp_path / "no-builtin",
    )
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
            mcp_status=McpStatus(connected=["files"], failed=[("web", "boom")]),
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
            mcp_servers=[], mcp_status=McpStatus()
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
            mcp_status=McpStatus(connected=list(names), failed=[]),
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


@pytest.mark.anyio
async def test_settings_command_opens_screen():
    app = _FakeApp()
    opened = []
    app.open_settings = lambda: opened.append(True)  # type: ignore[attr-defined]
    await dispatch(app, "/settings")
    assert opened == [True]


@pytest.mark.anyio
async def test_config_alias_opens_settings():
    app = _FakeApp()
    opened = []
    app.open_settings = lambda: opened.append(True)  # type: ignore[attr-defined]
    await dispatch(app, "/config")
    assert opened == [True]


def test_settings_command_registered():
    assert "settings" in COMMANDS_BY_NAME
    assert "config" in COMMANDS_BY_NAME  # alias
    assert COMMANDS_BY_NAME["config"].name == "settings"


def test_worktree_registered():
    assert "worktree" in COMMANDS_BY_NAME


def test_worktree_non_git_dir_posts_error(tmp_path):
    import asyncio
    app = _FakeApp(workspace_root=tmp_path)
    asyncio.run(dispatch(app, "/worktree list"))
    assert any("Not a git repository" in m for m in app.posted)


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_worktree_create_posts_launch_hint(tmp_path):
    import asyncio
    repo = _git_repo(tmp_path)
    app = _FakeApp(workspace_root=repo)
    asyncio.run(dispatch(app, "/worktree create feat/x"))
    joined = "\n".join(app.posted)
    assert "marim --worktree feat/x" in joined
    assert (repo / ".worktrees" / "feat/x").exists()


def test_worktree_create_requires_branch(tmp_path):
    import asyncio
    repo = _git_repo(tmp_path)
    app = _FakeApp(workspace_root=repo)
    asyncio.run(dispatch(app, "/worktree create"))
    assert any("Usage:" in m for m in app.posted)


def test_worktree_list_shows_branches(tmp_path):
    import asyncio
    repo = _git_repo(tmp_path)
    app = _FakeApp(workspace_root=repo)
    asyncio.run(dispatch(app, "/worktree create feat/x"))
    app.posted.clear()
    asyncio.run(dispatch(app, "/worktree list"))
    joined = "\n".join(app.posted)
    assert "main" in joined
    assert "feat/x" in joined


@pytest.mark.anyio
async def test_jobs_command_registered():
    assert "jobs" in COMMANDS_BY_NAME


@pytest.mark.anyio
async def test_jobs_lists_running_jobs():
    import asyncio

    from marim_harness.jobs import JobRegistry

    async def slow():
        await asyncio.sleep(5)
        return "x"

    reg = JobRegistry()
    reg.register("agent", "explore: map", slow())
    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=reg))
    await dispatch(app, "/jobs")
    out = app.posted[-1]
    # An agent row surfaces the type as its column + the concise title.
    assert "job-1" in out and "explore" in out and "map" in out
    await reg.cancel_all()


@pytest.mark.anyio
async def test_jobs_empty_reports_none():
    from marim_harness.jobs import JobRegistry

    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=JobRegistry()))
    await dispatch(app, "/jobs")
    assert "No background jobs" in app.posted[-1]


@pytest.mark.anyio
async def test_jobs_output_prints_result():
    from marim_harness.jobs import JobRegistry

    async def quick():
        return "the report body"

    reg = JobRegistry()
    job_id = reg.register("agent", "a", quick())
    await reg.wait(job_id)
    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=reg))
    await dispatch(app, "/jobs output job-1")
    assert "the report body" in app.posted[-1]


@pytest.mark.anyio
async def test_jobs_cancel_cancels_job():
    import asyncio

    from marim_harness.jobs import JobRegistry

    async def slow():
        await asyncio.sleep(5)
        return "x"

    reg = JobRegistry()
    job_id = reg.register("bash", "sleep", slow())
    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=reg))
    await dispatch(app, "/jobs cancel job-1")
    assert reg.get(job_id).status == "cancelled"
    assert "cancel" in app.posted[-1].lower()


@pytest.mark.anyio
async def test_jobs_wake_toggles_app_flag():
    app = _FakeApp()
    app.autonomous_wake = True
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=None))
    await dispatch(app, "/jobs wake off")
    assert app.autonomous_wake is False
    assert "off" in app.posted[-1].lower()
    await dispatch(app, "/jobs wake on")
    assert app.autonomous_wake is True
    assert "on" in app.posted[-1].lower()


@pytest.mark.anyio
async def test_jobs_unknown_subcommand_shows_usage():
    from marim_harness.jobs import JobRegistry

    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=JobRegistry()))
    await dispatch(app, "/jobs frobnicate")
    assert "usage" in app.posted[-1].lower()


@pytest.mark.anyio
async def test_compact_refuses_while_turn_busy():
    app = _FakeApp()
    app.turn_busy = True
    await dispatch(app, "/compact")
    assert any("turn is running" in m for m in app.posted)


@pytest.mark.anyio
async def test_compact_passes_manual_trigger_and_instructions():
    app = _FakeApp()
    calls = {}

    async def fake_maybe_compact(*, force=False, trigger="auto", instructions=None):
        calls.update(trigger=trigger, instructions=instructions)
        return True

    app.harness.session.maybe_compact = fake_maybe_compact
    await dispatch(app, "/compact focus on the auth bug")
    await app.drain_workers()
    assert calls == {"trigger": "manual", "instructions": "focus on the auth bug"}


@pytest.mark.anyio
async def test_compact_reports_nothing_to_do():
    app = _FakeApp()

    async def fake_maybe_compact(**kw):
        return False

    app.harness.session.maybe_compact = fake_maybe_compact
    await dispatch(app, "/compact")
    await app.drain_workers()
    assert any("Nothing to compact" in m for m in app.posted)
