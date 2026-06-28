import io
import subprocess
from pathlib import Path

import pytest

from marim_harness.interfaces.cli import default_cmd, router
from marim_harness.interfaces.cli.default_cmd import _build_parser, _enter_worktree
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_deps


def _cli_harness(tmp_path: Path, output_text: str = "ok"):
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness, HarnessConfig
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create("cli")
    model = TestModel(call_tools=[], custom_output_text=output_text)
    return Harness(
        model, BuiltinToolProvider(), deps,
        instructions="test",
        config=HarnessConfig(store=store, manager=manager),
    )


def test_is_headless_logic():
    from marim_harness.interfaces.cli.default_cmd import _is_headless

    assert _is_headless("hi", stdin_isatty=True) is True   # -p with text
    assert _is_headless(True, stdin_isatty=True) is True    # -p flag alone
    assert _is_headless(None, stdin_isatty=False) is True   # piped stdin
    assert _is_headless(None, stdin_isatty=True) is False   # tty, no -p -> TUI
    # `textual serve` drives the TUI over pipes (stdin is NOT a tty) and signals
    # the web driver via TEXTUAL_DRIVER. That still wants the full TUI, so the
    # piped-stdin heuristic must not pull it into headless.
    assert _is_headless(None, stdin_isatty=False, textual_driver=True) is False
    # An explicit prompt is still a headless one-shot, even under a driver.
    assert _is_headless("hi", stdin_isatty=False, textual_driver=True) is True
    # Default (no driver) keeps the original behavior.
    assert _is_headless(None, stdin_isatty=False, textual_driver=False) is True


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


def test_version_flag_prints_and_exits(capsys):
    """--version is argparse's version action: it prints `marim <version>` and
    exits 0 before any heavy import or workspace resolution."""
    with pytest.raises(SystemExit) as ei:
        default_cmd._build_parser().parse_args(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("marim ")
    assert default_cmd._version() in out


def test_router_dispatches_management(monkeypatch):
    # Dispatch now imports the matched subcommand module lazily and calls its
    # main(argv[1:]); patch the real sessions.main rather than a _MANAGEMENT dict.
    import marim_harness.interfaces.cli.sessions as sessions_cmd

    seen = {}
    monkeypatch.setattr(router, "load_environment", lambda: None)
    monkeypatch.setattr(sessions_cmd, "main", lambda argv: seen.update(argv=argv) or 7)
    monkeypatch.setattr("sys.argv", ["marim", "sessions", "list", "--json"])
    with pytest.raises(SystemExit) as ei:
        router.main()
    assert ei.value.code == 7
    assert seen["argv"] == ["list", "--json"]


def test_router_falls_through_to_default(monkeypatch):
    # run_default is now imported lazily inside main() from default_cmd; patch it
    # at its source module so the local `from .default_cmd import run_default` binds
    # the fake.
    monkeypatch.setattr(router, "load_environment", lambda: None)
    monkeypatch.setattr(
        default_cmd, "run_default", lambda argv: 0 if argv == ["-p", "hi"] else 99
    )
    monkeypatch.setattr("sys.argv", ["marim", "-p", "hi"])
    with pytest.raises(SystemExit) as ei:
        router.main()
    assert ei.value.code == 0


def test_run_default_headless_uses_auto_mode(monkeypatch, tmp_path: Path):
    import marim_harness.runtime.bootstrap as bootstrap

    captured = {}

    def fake_build(workspace, *, mode, resume):
        captured.update(mode=mode, workspace=workspace, resume=resume)
        return _cli_harness(tmp_path, "auto-ran")

    monkeypatch.setattr(bootstrap, "build_harness", fake_build)
    out = io.StringIO()
    stdin = io.StringIO()
    stdin.isatty = lambda: True  # not piped; -p forces headless
    code = default_cmd.run_default(["-p", "hello"], stdin=stdin, out=out)
    assert code == 0
    assert captured["mode"] is Mode.auto
    assert out.getvalue().strip() == "auto-ran"


def test_run_default_respects_mode_override(monkeypatch, tmp_path: Path):
    import marim_harness.runtime.bootstrap as bootstrap

    captured = {}

    def fake_build(workspace, *, mode, resume):
        captured["mode"] = mode
        return _cli_harness(tmp_path)

    monkeypatch.setattr(bootstrap, "build_harness", fake_build)
    stdin = io.StringIO()
    stdin.isatty = lambda: True
    default_cmd.run_default(["-p", "hi", "--mode", "plan"], stdin=stdin, out=io.StringIO())
    assert captured["mode"] is Mode.plan


def test_piped_stdin_triggers_headless(monkeypatch, tmp_path: Path):
    import marim_harness.runtime.bootstrap as bootstrap

    monkeypatch.setattr(
        bootstrap, "build_harness",
        lambda workspace, *, mode, resume: _cli_harness(tmp_path, "piped-ok"),
    )
    out = io.StringIO()
    stdin = io.StringIO("read the file")
    stdin.isatty = lambda: False  # piped
    code = default_cmd.run_default([], stdin=stdin, out=out)
    assert code == 0
    assert out.getvalue().strip() == "piped-ok"


def test_run_default_tui_omits_mode_for_configured_default(monkeypatch, tmp_path: Path):
    # The interactive TUI no longer hardcodes a mode: it omits it so build_harness
    # resolves the configured default (MARIM_DEFAULT_MODE, default "ask"). The
    # mock therefore receives mode=None and accepts it as optional.
    import marim_harness.interfaces.tui.app as tui_app
    import marim_harness.runtime.bootstrap as bootstrap

    captured = {}

    class FakeApp:
        def __init__(self, harness, history=None):
            captured["harness"] = harness
            captured["history"] = history

        def run(self):
            captured["ran"] = True

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))  # keep off the real file
    monkeypatch.setattr(
        bootstrap, "build_harness",
        lambda workspace, *, mode=None, resume: captured.update(mode=mode) or object(),
    )
    monkeypatch.setattr(tui_app, "HarnessApp", FakeApp)
    stdin = io.StringIO()
    stdin.isatty = lambda: True  # interactive
    code = default_cmd.run_default([], stdin=stdin)
    assert code == 0
    assert captured["mode"] is None  # delegated to build_harness's config default
    assert captured["ran"] is True
    # The TUI is given a persistent prompt history.
    from marim_harness.interfaces.history import PromptHistory

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


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_parser_accepts_worktree_flag():
    args = _build_parser().parse_args(["--worktree", "feat/x"])
    assert args.worktree == "feat/x"


def test_parser_worktree_defaults_none():
    args = _build_parser().parse_args([])
    assert args.worktree is None


def test_enter_worktree_resolves_path(tmp_path: Path):
    repo = _git_repo(tmp_path)
    err = io.StringIO()
    result = _enter_worktree(repo, "feat/x", err)
    assert result == repo / ".worktrees" / "feat/x"
    assert err.getvalue() == ""


def test_enter_worktree_non_git_dir_returns_none(tmp_path: Path):
    err = io.StringIO()
    result = _enter_worktree(tmp_path, "feat/x", err)
    assert result is None
    assert "not a git repository" in err.getvalue()


def test_enter_worktree_bad_branch_returns_none(tmp_path: Path):
    repo = _git_repo(tmp_path)
    err = io.StringIO()
    result = _enter_worktree(repo, "../escape", err)
    assert result is None
    assert "--worktree" in err.getvalue()
