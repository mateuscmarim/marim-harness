"""``--unsafe-full-access``: the launch flag that stops the workspace root
being a boundary.

The feature is deliberately spread thin — a bool on ``WorkspaceConfig`` that
the path guard, the external-CLI policy table, the instructions block and the
status bar each read — so its tests live together here rather than scattered
across whichever file each layer happens to sit in. What they collectively pin
is one rule: the flag widens *reach*, it never grants *approval*.
"""

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness import BuilderError, HarnessBuilder
from marim_harness.runtime.deps import Deps, HarnessServices, WorkspaceConfig
from marim_harness.runtime.permissions import (
    PLAN_READ_ONLY,
    Decision,
    ExternalRequest,
    LaunchOptions,
    Mode,
    decide_external,
)
from marim_harness.tools import edit_tools, fs_tools
from marim_harness.tools.impl import fs
from marim_harness.workspace.fs import ReadLedger

# --------------------------------------------------------------- policy table


def _write(*paths: Path) -> ExternalRequest:
    return ExternalRequest(mutating=True, paths=tuple(paths))


def test_auto_escalates_a_stray_write_without_the_flag(tmp_path: Path):
    """The row the flag exists to retire, first shown intact: in auto mode an
    external CLI writing outside the workspace is escalated to a prompt. That
    escalation is the approval treadmill a spawn rooted elsewhere walks into,
    once per file it touches."""
    stray = tmp_path / "elsewhere" / "a.txt"
    d = decide_external(Mode.auto, _write(stray), tmp_path / "ws", None)
    assert d.ask and not d.accept and str(stray) in d.reason


def test_auto_accepts_a_stray_write_under_full_access(tmp_path: Path):
    stray = tmp_path / "elsewhere" / "a.txt"
    d = decide_external(Mode.auto, _write(stray), tmp_path / "ws", None, full_access=True)
    assert d == Decision(accept=True)


def test_full_access_does_not_turn_ask_mode_into_auto(tmp_path: Path):
    """Reach, not approval: ask still routes every mutation to the human, in
    the workspace and outside it alike."""
    ws = tmp_path / "ws"
    for target in (ws / "a.txt", tmp_path / "elsewhere" / "a.txt"):
        d = decide_external(Mode.ask, _write(target), ws, None, full_access=True)
        assert d.ask and not d.accept


def test_full_access_does_not_let_plan_mode_mutate(tmp_path: Path):
    ws = tmp_path / "ws"
    d = decide_external(Mode.plan, _write(ws / "a.txt"), ws, None, full_access=True)
    assert d == Decision(accept=False, reason=PLAN_READ_ONLY)
    assert not d.ask


def test_full_access_leaves_non_mutating_requests_alone(tmp_path: Path):
    """Reads were already accepted outside plan mode; the flag must not change
    the answer for them, nor give it a different reason."""
    read = ExternalRequest(mutating=False)
    for mode in (Mode.ask, Mode.auto):
        assert decide_external(mode, read, tmp_path, None, full_access=True) == Decision(
            accept=True
        )


# ----------------------------------------------------------------- path guard


def test_write_outside_the_workspace_is_refused_without_the_flag(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ModelRetry):
        fs.write_file(ws, str(tmp_path / "outside.txt"), "hi")


def test_full_access_write_reaches_any_absolute_path(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = tmp_path / "outside.txt"
    fs.write_file(ws, str(target), "hi", None, fs.PathScope(full_access=True))
    assert target.read_text() == "hi"


def test_full_access_read_and_edit_reach_outside_too(tmp_path: Path):
    """read_file has to honor the flag for edit_file to be usable at all: the
    read-before-edit ledger would otherwise refuse every out-of-workspace edit
    on the grounds that the file was never read."""
    ws = tmp_path / "ws"
    ws.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("hello world\n")
    wide = fs.PathScope(full_access=True)
    ledger = ReadLedger()

    out = fs.read_file(ws, str(target), scope=wide, ledger=ledger)
    assert isinstance(out, str) and "hello world" in out
    fs.edit_file(ws, str(target), [fs.Edit(old_string="hello", new_string="goodbye")], ledger, wide)
    assert target.read_text() == "goodbye world\n"


def test_relative_paths_still_land_in_the_workspace_under_full_access(tmp_path: Path):
    """The workspace stops being a boundary but stays the default: a bare
    relative path resolves inside it exactly as before, or the flag would
    silently divert ordinary project writes somewhere else."""
    ws = tmp_path / "ws"
    ws.mkdir()
    fs.write_file(ws, "note.txt", "hi", None, fs.PathScope(full_access=True))
    assert (ws / "note.txt").read_text() == "hi"


def test_relative_escape_is_anchored_at_the_workspace_not_the_filesystem_root(tmp_path: Path):
    """``../notes.md`` from ``<tmp>/ws`` means ``<tmp>/notes.md`` — the file the
    agent is plainly naming. Implementing full access by handing ``/`` to the
    existing guard as an extra root would instead land on ``/notes.md``:
    permitted, resolvable, and the wrong file, with nothing to say so."""
    ws = tmp_path / "ws"
    ws.mkdir()
    fs.write_file(ws, "../notes.md", "hi", None, fs.PathScope(full_access=True))
    assert (tmp_path / "notes.md").read_text() == "hi"


def test_full_access_is_the_last_fallback_after_the_named_roots(tmp_path: Path):
    """A symlink pointing out of an extra root escapes that root's guard, so it
    reaches the full-access fallback — and lands on the real file behind the
    link, the same path every other layer would have named. The flag only ever
    converts a refusal into a resolution; it never moves a write."""
    ws, scratch, outside = tmp_path / "ws", tmp_path / "scratch", tmp_path / "outside"
    for d in (ws, scratch, outside):
        d.mkdir()
    (scratch / "link").symlink_to(outside)
    escaping = str(scratch / "link" / "x.txt")

    with pytest.raises(ModelRetry):
        fs.write_file(ws, escaping, "hi", None, fs.PathScope(roots=(scratch,)))
    fs.write_file(ws, escaping, "hi", None, fs.PathScope(roots=(scratch,), full_access=True))
    assert (outside / "x.txt").read_text() == "hi"


# ------------------------------------------------------------------ tool layer


def _ctx(ws: Path, *, full_access: bool) -> SimpleNamespace:
    deps = Deps(workspace=WorkspaceConfig(root=ws, full_access=full_access))
    deps.services = HarnessServices()
    return SimpleNamespace(deps=deps)


@pytest.mark.anyio
async def test_write_tool_threads_the_session_flag(tmp_path: Path):
    """End of the wire: what the session was launched with decides whether the
    model-facing tool can reach out of the workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    target = tmp_path / "outside.txt"

    with pytest.raises(ModelRetry):
        await edit_tools.write_file(_ctx(ws, full_access=False), str(target), "hi")
    await edit_tools.write_file(_ctx(ws, full_access=True), str(target), "hi")
    assert target.read_text() == "hi"


def test_write_scope_carries_both_halves(tmp_path: Path):
    assert fs_tools.write_scope(_ctx(tmp_path, full_access=True)) == fs.PathScope(
        roots=(), full_access=True
    )
    assert not fs_tools.write_scope(_ctx(tmp_path, full_access=False)).full_access


# --------------------------------------------------------------- the builder


def _instructions(harness) -> str:
    captured = []

    def observe(messages, info):
        captured.append(info.instructions)
        return ModelResponse(parts=[TextPart("done")])

    with harness.agent.override(model=FunctionModel(observe)):
        harness.agent.run_sync("Inspect instructions", deps=harness.deps)
    return captured[0]


def test_builder_grants_full_access_and_tells_the_model(tmp_path: Path):
    """The prompt has to say the guard is off, or the tool docstrings ("relative
    to the workspace root") actively mislead and a model routes around them
    through bash — losing the read-before-edit ledger and the diff cards."""
    harness = HarnessBuilder(workspace=tmp_path, model=TestModel()).with_full_access().build()
    assert harness.deps.workspace.full_access is True
    text = _instructions(harness)
    assert "--unsafe-full-access" in text
    # Both halves: the guard is gone AND the workspace is still where work
    # belongs. Only the first reads as licence to write anywhere.
    assert "ANY absolute path" in text and "still resolve inside the workspace" in text


def test_builder_defaults_to_a_contained_workspace(tmp_path: Path):
    harness = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    assert harness.deps.workspace.full_access is False
    assert "--unsafe-full-access" not in _instructions(harness)


def test_with_full_access_and_with_deps_is_a_build_error(tmp_path: Path):
    """The silent no-op is the dangerous shape here: an embedder who asked for
    full access and didn't get it would read the refusals that follow as the
    agent's own judgement."""
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path))
    builder = (
        HarnessBuilder(workspace=tmp_path, model=TestModel()).with_deps(deps).with_full_access()
    )
    with pytest.raises(BuilderError) as exc:
        builder.build()
    assert any("with_full_access" in p for p in exc.value.problems)


# -------------------------------------------------------------------- the flag


class _TtyStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


def _stub_build(monkeypatch, captured: dict):
    """Capture the LaunchOptions build_harness is called with, and hand back a
    harness stub complete enough for the claim/run plumbing around it."""
    from marim_harness.runtime import bootstrap

    def fake_build(workspace, *, launch, **kw):
        captured["launch"] = launch
        return SimpleNamespace(
            session=SimpleNamespace(store=None, history=[]),
            adopt_claim=lambda *a, **k: None,
            release_claim=lambda: None,
        )

    monkeypatch.setattr(bootstrap, "build_harness", fake_build, raising=True)


def test_the_flag_defaults_off_and_parses():
    from marim_harness.interfaces.cli.default_cmd import _build_parser

    assert _build_parser().parse_args([]).full_access is False
    assert _build_parser().parse_args(["--unsafe-full-access"]).full_access is True


def test_headless_forwards_the_flag_and_announces_it(tmp_path: Path, monkeypatch):
    """A launch flag is worth having only because a human typed it, so the run
    says out loud what it was granted — on stderr, which survives the ``-p``
    output being piped somewhere."""
    from marim_harness.interfaces.cli import headless as headless_mod
    from marim_harness.interfaces.cli.default_cmd import run_default

    captured: dict = {}
    _stub_build(monkeypatch, captured)

    async def fake_headless(*a, **kw):
        return 0

    monkeypatch.setattr(headless_mod, "run_headless", fake_headless)
    err = io.StringIO()
    code = run_default(
        [str(tmp_path), "-p", "hi", "--unsafe-full-access"],
        stdin=io.StringIO(),
        out=io.StringIO(),
        err=err,
    )
    assert code == 0
    assert captured["launch"].full_access is True
    assert "--unsafe-full-access" in err.getvalue() and str(tmp_path) in err.getvalue()


def test_headless_without_the_flag_says_nothing(tmp_path: Path, monkeypatch):
    from marim_harness.interfaces.cli import headless as headless_mod
    from marim_harness.interfaces.cli.default_cmd import run_default

    captured: dict = {}
    _stub_build(monkeypatch, captured)

    async def fake_headless(*a, **kw):
        return 0

    monkeypatch.setattr(headless_mod, "run_headless", fake_headless)
    err = io.StringIO()
    run_default([str(tmp_path), "-p", "hi"], stdin=io.StringIO(), out=io.StringIO(), err=err)
    assert captured["launch"].full_access is False
    assert err.getvalue() == ""


def test_the_tui_carries_the_banner_into_the_transcript(tmp_path: Path, monkeypatch):
    """Textual paints over anything already on stderr, so the interactive path
    hands the same line to the app as a launch notice instead."""
    from marim_harness.interfaces.cli import default_cmd, router
    from marim_harness.interfaces.cli import headless as headless_mod
    from marim_harness.interfaces.cli.default_cmd import run_default

    captured: dict = {}
    _stub_build(monkeypatch, captured)
    launched: dict = {}
    monkeypatch.setattr(default_cmd, "_tui_available", lambda: True)
    monkeypatch.setattr(router, "route_logging_to_file", lambda *a, **kw: None)
    monkeypatch.setattr(default_cmd, "_launch_tui", lambda h, **kw: launched.update(kw) or 0)
    monkeypatch.setattr(headless_mod, "run_headless", None)

    code = run_default(
        [str(tmp_path), "--unsafe-full-access"],
        stdin=_TtyStdin(),
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert code == 0
    assert captured["launch"].full_access is True
    (notice,) = launched["notices"]
    assert "--unsafe-full-access" in notice and str(tmp_path) in notice


def test_launch_options_default_to_nothing_in_particular():
    """``build_harness``'s own default. A bare ``LaunchOptions()`` must never be
    the thing that turns the flag on — only an explicit argv can."""
    assert LaunchOptions() == LaunchOptions(mode=None, full_access=False)


def test_serve_never_hands_out_full_access(tmp_path: Path, monkeypatch):
    """A daemon reachable over the network must not be able to grant the whole
    filesystem, so the supervisor builds without the flag: no route, no payload
    field, no way in."""
    import asyncio

    from marim_harness.server import supervisor as supervisor_mod

    captured: dict = {}

    def fake_build(workspace, *, launch, **kw):
        captured["launch"] = launch

        class _H:
            session = SimpleNamespace(history=[])

            async def connect(self):
                pass

            async def session_start(self, kind):
                pass

        return _H()

    monkeypatch.setattr("marim_harness.runtime.bootstrap.build_harness", fake_build)
    asyncio.run(supervisor_mod.default_harness_factory(tmp_path, "s1", Mode.auto))
    assert captured["launch"].full_access is False
