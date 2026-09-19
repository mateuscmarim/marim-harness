"""``marim --session <id>`` and the launch's attach branch (phase 4a).

The claim step is where a launch learns the session is owned elsewhere. For
an interactive launch whose owner is a reachable daemon, that refusal becomes
an attach: no Harness is built, no claim is taken, and the TUI is started
against a ``RemoteTarget``. Headless never attaches, a non-daemon holder is
the usual refusal, and a daemon that fails a probe is refused with the
probe's reason printed first.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from marim_harness.server.attach import AttachDecision, RemoteTarget

SID = "20260101-000000-aaaaaa"


class _TtyStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


def _write_session(workspace: Path, session_id: str) -> Path:
    from marim_harness.session import SessionManager

    manager = SessionManager(workspace)
    manager.dir.mkdir(parents=True, exist_ok=True)
    path = manager.dir / f"{session_id}.json"
    path.write_text(json.dumps({"id": session_id, "updated": "2026-01-01T00:00:00"}))
    return path


def _target(workspace: Path) -> RemoteTarget:
    return RemoteTarget(
        endpoint="http://127.0.0.1:8642",
        token="tok",
        workspace_id="ws0",
        session_id=SID,
        workspace_root=workspace,
    )


@pytest.fixture()
def launch(tmp_path, monkeypatch):
    """A launch harness: build/TUI/headless seams stubbed and recorded, the
    session owned by a "daemon" claim held in this process, and ``discover``
    replaced by a scripted decision."""
    from marim_harness.interfaces.cli import default_cmd, router
    from marim_harness.interfaces.cli import headless as headless_mod
    from marim_harness.runtime import bootstrap
    from marim_harness.server import attach as attach_mod
    from marim_harness.session.claim import try_acquire

    calls: list = []
    decisions: dict = {"decision": AttachDecision(target=_target(tmp_path))}

    def fake_discover(workspace, session_id, session_path, *, state_dir, fetch=None):
        calls.append(("discover", session_id, workspace, state_dir))
        return decisions["decision"]

    monkeypatch.setattr(attach_mod, "discover", fake_discover)
    monkeypatch.setattr(bootstrap, "build_harness", lambda *a, **kw: calls.append(("build", kw)))
    monkeypatch.setattr(default_cmd, "_tui_available", lambda: True)
    monkeypatch.setattr(router, "route_logging_to_file", lambda *a, **kw: None)
    monkeypatch.setattr(default_cmd, "_launch_tui", lambda h, **kw: calls.append(("tui", h)) or 0)
    monkeypatch.setattr(
        default_cmd, "_launch_remote_tui", lambda t: calls.append(("remote", t)) or 0
    )
    monkeypatch.setattr(headless_mod, "run_headless", lambda *a, **kw: calls.append("headless"))
    session_path = _write_session(tmp_path, SID)
    owner = try_acquire(session_path, kind="daemon", endpoint="http://127.0.0.1:8642")
    assert owner is not None
    err = io.StringIO()

    def run(argv):
        from marim_harness.interfaces.cli.default_cmd import run_default

        return run_default([str(tmp_path), *argv], stdin=_TtyStdin(), out=io.StringIO(), err=err)

    try:
        yield run, calls, decisions, err
    finally:
        owner.release()


def test_session_flag_attaches_to_the_owning_daemon(launch, tmp_path):
    run, calls, _decisions, err = launch
    assert run(["--session", SID]) == 0
    kinds = [c[0] for c in calls]
    assert kinds == ["discover", "remote"]  # no build, no local TUI
    assert calls[1][1] == _target(tmp_path)
    assert err.getvalue() == ""
    # discover got the session's real path and the daemon's state dir.
    _, sid, workspace, state_dir = calls[0]
    assert sid == SID and workspace == tmp_path
    assert state_dir.name == "server" and state_dir.parent.name == "marim-harness"


def test_resume_attaches_too_when_the_latest_is_daemon_owned(launch):
    run, calls, _decisions, _err = launch
    assert run(["--resume"]) == 0
    assert [c[0] for c in calls] == ["discover", "remote"]


def test_probe_failure_prints_the_reason_then_refuses(launch):
    run, calls, decisions, err = launch
    decisions["decision"] = AttachDecision(
        reason="the daemon at http://127.0.0.1:8642 is not answering"
    )
    assert run(["--session", SID]) == 2
    assert [c[0] for c in calls] == ["discover"]
    text = err.getvalue()
    assert text.startswith("not attaching: the daemon at http://127.0.0.1:8642 is not answering.\n")
    assert f"session {SID} is already open in daemon" in text
    assert "drop --resume/--session" in text


def test_non_daemon_decision_is_the_plain_refusal(launch):
    run, calls, decisions, err = launch
    decisions["decision"] = AttachDecision()
    assert run(["--session", SID]) == 2
    assert [c[0] for c in calls] == ["discover"]
    assert "not attaching" not in err.getvalue()
    assert "already open in daemon" in err.getvalue()


def test_headless_never_attaches(launch):
    run, calls, _decisions, err = launch
    assert run(["--session", SID, "-p", "hi"]) == 2
    assert calls == []  # discover is not even consulted
    assert "already open in daemon" in err.getvalue()


def test_session_flag_opens_an_unowned_session_locally(tmp_path, monkeypatch):
    from marim_harness.interfaces.cli import default_cmd, router
    from marim_harness.runtime import bootstrap
    from marim_harness.server import attach as attach_mod

    seen: dict = {}
    monkeypatch.setattr(
        attach_mod, "discover", lambda *a, **kw: pytest.fail("discover on an unowned session")
    )
    monkeypatch.setattr(default_cmd, "_tui_available", lambda: True)
    monkeypatch.setattr(router, "route_logging_to_file", lambda *a, **kw: None)
    _write_session(tmp_path, SID)
    _write_session(tmp_path, "20260102-000000-bbbbbb")  # a newer one --resume would pick

    class _Harness:
        def adopt_claim(self, claim, *, kind):
            seen["adopted"] = (claim, kind)

        def release_claim(self):
            seen["released"] = True

    def fake_build(workspace, *, launch, session_id, resume):
        seen["session_id"] = session_id
        return _Harness()

    monkeypatch.setattr(bootstrap, "build_harness", fake_build)
    monkeypatch.setattr(default_cmd, "_launch_tui", lambda h, **kw: 0)
    from marim_harness.interfaces.cli.default_cmd import run_default

    err = io.StringIO()
    code = run_default(
        [str(tmp_path), "--session", SID], stdin=_TtyStdin(), out=io.StringIO(), err=err
    )
    assert code == 0 and err.getvalue() == ""
    assert seen["session_id"] == SID  # --session pins the id; --resume's latest() is not used
    assert seen["adopted"][1] == "tui" and seen["released"] is True


def test_session_flag_with_an_unknown_id_is_refused(tmp_path, monkeypatch):
    from marim_harness.interfaces.cli import default_cmd, router
    from marim_harness.runtime import bootstrap

    monkeypatch.setattr(bootstrap, "build_harness", lambda *a, **kw: pytest.fail("built"))
    monkeypatch.setattr(default_cmd, "_tui_available", lambda: True)
    monkeypatch.setattr(router, "route_logging_to_file", lambda *a, **kw: None)
    from marim_harness.interfaces.cli.default_cmd import run_default

    err = io.StringIO()
    code = run_default(
        [str(tmp_path), "--session", "nope"], stdin=_TtyStdin(), out=io.StringIO(), err=err
    )
    assert code == 2
    assert "session nope no longer exists" in err.getvalue()


@pytest.mark.parametrize("bad", ["../elsewhere/x", "sub/x", "..", ".", "", "a\\b"])
def test_session_flag_rejects_a_path_like_id(tmp_path, monkeypatch, bad):
    """``--session`` becomes a file name under the sessions dir: a value
    with a separator would claim and load a session outside this workspace."""
    from marim_harness.interfaces.cli import default_cmd, router
    from marim_harness.runtime import bootstrap
    from marim_harness.session.claim import try_acquire as real_acquire

    monkeypatch.setattr(bootstrap, "build_harness", lambda *a, **kw: pytest.fail("built"))
    monkeypatch.setattr(default_cmd, "_tui_available", lambda: True)
    monkeypatch.setattr(router, "route_logging_to_file", lambda *a, **kw: None)
    claimed: list = []
    monkeypatch.setattr(
        "marim_harness.session.claim.try_acquire",
        lambda path, **kw: claimed.append(path) or real_acquire(path, **kw),
    )
    from marim_harness.interfaces.cli.default_cmd import run_default

    err = io.StringIO()
    code = run_default(
        [str(tmp_path), "--session", bad], stdin=_TtyStdin(), out=io.StringIO(), err=err
    )
    assert code == 2
    assert "is not a session id" in err.getvalue()
    assert claimed == []  # refused before any sidecar is written anywhere


def test_launch_target_precedence():
    from types import SimpleNamespace

    from marim_harness.interfaces.cli import default_cmd

    ws = Path("/ws")
    assert default_cmd._launch_target(SimpleNamespace(session="abc", resume=True), ws) == "abc"
    assert default_cmd._launch_target(SimpleNamespace(session=None, resume=False), ws) is None


def test_launch_remote_tui_builds_an_attached_app(tmp_path, monkeypatch):
    from marim_harness.interfaces.cli import default_cmd
    from marim_harness.interfaces.tui import app as app_mod

    seen: dict = {}

    class _App:
        def __init__(self, harness, history=None, *, remote=None):
            seen["harness"] = harness
            seen["remote"] = remote
            seen["history"] = history

        def run(self):
            seen["ran"] = True

    monkeypatch.setattr(app_mod, "HarnessApp", _App)
    target = _target(tmp_path)
    assert default_cmd._launch_remote_tui(target) == 0
    assert seen["harness"] is None and seen["remote"] is target and seen["ran"]
    assert seen["history"] is not None  # the persistent prompt history, as locally
