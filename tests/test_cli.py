import io
from pathlib import Path

import pytest

from marim_harness.interfaces.cli import default_cmd, router
from marim_harness.permissions import Mode


def _cli_harness(tmp_path: Path, output_text: str = "ok"):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create("cli")
    model = TestModel(call_tools=[], custom_output_text=output_text)
    return Harness(
        model, BuiltinToolProvider(), deps,
        instructions="test", store=store, manager=manager,
    )


def test_is_headless_logic():
    from marim_harness.interfaces.cli.default_cmd import _is_headless

    assert _is_headless("hi", stdin_isatty=True) is True   # -p with text
    assert _is_headless(True, stdin_isatty=True) is True    # -p flag alone
    assert _is_headless(None, stdin_isatty=False) is True   # piped stdin
    assert _is_headless(None, stdin_isatty=True) is False   # tty, no -p -> TUI


def test_parser_defaults_and_flags():
    p = default_cmd._build_parser()

    args = p.parse_args([])
    assert args.workspace is None and args.prompt is None
    assert args.output_format == "text" and args.mode is None and args.resume is False

    args = p.parse_args(
        ["-p", "do it", "--output-format", "json", "--mode", "plan", "--resume"]
    )
    assert args.prompt == "do it" and args.output_format == "json"
    assert args.mode == "plan" and args.resume is True

    assert p.parse_args(["-p"]).prompt is True  # bare -p reads stdin


def test_parser_rejects_ask_mode():
    with pytest.raises(SystemExit):
        default_cmd._build_parser().parse_args(["--mode", "ask"])


def test_router_dispatches_management(monkeypatch):
    seen = {}
    monkeypatch.setattr(router, "load_environment", lambda: None)
    monkeypatch.setattr(
        router, "_MANAGEMENT",
        {"sessions": lambda argv: seen.update(argv=argv) or 7},
    )
    monkeypatch.setattr("sys.argv", ["marim", "sessions", "list", "--json"])
    with pytest.raises(SystemExit) as ei:
        router.main()
    assert ei.value.code == 7
    assert seen["argv"] == ["list", "--json"]


def test_router_falls_through_to_default(monkeypatch):
    monkeypatch.setattr(router, "load_environment", lambda: None)
    monkeypatch.setattr(
        router, "run_default", lambda argv: 0 if argv == ["-p", "hi"] else 99
    )
    monkeypatch.setattr("sys.argv", ["marim", "-p", "hi"])
    with pytest.raises(SystemExit) as ei:
        router.main()
    assert ei.value.code == 0


def test_run_default_headless_uses_auto_mode(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_build(workspace, *, mode, resume):
        captured.update(mode=mode, workspace=workspace, resume=resume)
        return _cli_harness(tmp_path, "auto-ran")

    monkeypatch.setattr(default_cmd, "build_harness", fake_build)
    out = io.StringIO()
    stdin = io.StringIO()
    stdin.isatty = lambda: True  # not piped; -p forces headless
    code = default_cmd.run_default(["-p", "hello"], stdin=stdin, out=out)
    assert code == 0
    assert captured["mode"] is Mode.auto
    assert out.getvalue().strip() == "auto-ran"


def test_run_default_respects_mode_override(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_build(workspace, *, mode, resume):
        captured["mode"] = mode
        return _cli_harness(tmp_path)

    monkeypatch.setattr(default_cmd, "build_harness", fake_build)
    stdin = io.StringIO()
    stdin.isatty = lambda: True
    default_cmd.run_default(["-p", "hi", "--mode", "plan"], stdin=stdin, out=io.StringIO())
    assert captured["mode"] is Mode.plan


def test_piped_stdin_triggers_headless(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        default_cmd, "build_harness",
        lambda workspace, *, mode, resume: _cli_harness(tmp_path, "piped-ok"),
    )
    out = io.StringIO()
    stdin = io.StringIO("read the file")
    stdin.isatty = lambda: False  # piped
    code = default_cmd.run_default([], stdin=stdin, out=out)
    assert code == 0
    assert out.getvalue().strip() == "piped-ok"


def test_run_default_tui_uses_ask_mode(monkeypatch, tmp_path: Path):
    captured = {}

    class FakeApp:
        def __init__(self, harness, history=None):
            captured["harness"] = harness
            captured["history"] = history

        def run(self):
            captured["ran"] = True

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))  # keep off the real file
    monkeypatch.setattr(
        default_cmd, "build_harness",
        lambda workspace, *, mode, resume: captured.update(mode=mode) or object(),
    )
    monkeypatch.setattr(default_cmd, "HarnessApp", FakeApp)
    stdin = io.StringIO()
    stdin.isatty = lambda: True  # interactive
    code = default_cmd.run_default([], stdin=stdin)
    assert code == 0
    assert captured["mode"] is Mode.ask
    assert captured["ran"] is True
    # The TUI is given a persistent prompt history.
    from marim_harness.history import PromptHistory

    assert isinstance(captured["history"], PromptHistory)


def test_empty_prompt_returns_error():
    out, err = io.StringIO(), io.StringIO()
    stdin = io.StringIO("")  # piped but empty
    stdin.isatty = lambda: False
    code = default_cmd.run_default([], stdin=stdin, out=out, err=err)
    assert code == 2
    assert "no prompt" in err.getvalue().lower()


def test_management_stubs_return_nonzero():
    from marim_harness.interfaces.cli import config, models, sessions

    for mod in (sessions, config, models):
        assert mod.main([]) == 2
