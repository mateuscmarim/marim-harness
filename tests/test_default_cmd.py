import io
import json
from pathlib import Path
from types import SimpleNamespace

from marim_harness.interfaces.cli.default_cmd import _build_parser


def test_think_flag_sets_env():
    parser = _build_parser()
    args = parser.parse_args(["--think", "high"])
    assert args.think == "high"


def test_think_flag_choices_reject_unknown():
    import pytest as _pytest

    parser = _build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["--think", "ultra"])


def _harness_with_session(session_path):
    """A stand-in exposing what _acquire_session reads, plus the adopt/release
    claim protocol _run_claimed now drives (mirrors Harness.adopt_claim /
    Harness.release_claim: adopting a second claim releases the first, release
    is idempotent)."""
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("{}")
    store = SimpleNamespace(path=session_path, session_id=session_path.stem)
    harness = SimpleNamespace(session=SimpleNamespace(store=store), _claim=None)

    def adopt_claim(claim, *, kind):
        if harness._claim is not None and harness._claim is not claim:
            harness._claim.release()
        harness._claim = claim

    def release_claim():
        claim, harness._claim = harness._claim, None
        if claim is not None:
            claim.release()

    harness.adopt_claim = adopt_claim
    harness.release_claim = release_claim
    return harness


def test_acquire_session_claims_and_permits(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _acquire_session

    harness = _harness_with_session(tmp_path / "s.json")
    err = io.StringIO()
    claim, ok = _acquire_session(harness, kind="tui", err=err)
    try:
        assert ok is True
        assert claim is not None
        assert err.getvalue() == ""
    finally:
        claim.release()


def test_acquire_session_refuses_when_already_owned(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _acquire_session
    from marim_harness.session.claim import try_acquire

    session_path = tmp_path / "s.json"
    harness = _harness_with_session(session_path)
    outsider = try_acquire(session_path, kind="daemon", endpoint="http://127.0.0.1:8642")
    err = io.StringIO()
    try:
        claim, ok = _acquire_session(harness, kind="tui", err=err)
        assert ok is False
        assert claim is None
        assert "already open" in err.getvalue()
        assert "daemon" in err.getvalue()
        assert "8642" in err.getvalue()
    finally:
        outsider.release()


class _TtyStdin(io.StringIO):
    """stdin that claims to be a terminal — the signal that routes to the TUI."""

    def isatty(self) -> bool:
        return True


def _stub_launch_paths(monkeypatch, tmp_path, *, tui, headless):
    """Point run_default at a stand-in harness and stub both launch seams.

    Returns the session path the fake harness is bound to, so a test can
    contend for its claim with a real flock.
    """
    from marim_harness.interfaces.cli import default_cmd, router
    from marim_harness.interfaces.cli import headless as headless_mod
    from marim_harness.runtime import bootstrap

    session_path = tmp_path / "sessions" / "s.json"
    harness = _harness_with_session(session_path)
    monkeypatch.setattr(bootstrap, "build_harness", lambda *a, **kw: harness, raising=True)
    monkeypatch.setattr(default_cmd, "_tui_available", lambda: True)
    monkeypatch.setattr(router, "route_logging_to_file", lambda *a, **kw: None)
    monkeypatch.setattr(default_cmd, "_launch_tui", tui)
    monkeypatch.setattr(headless_mod, "run_headless", headless)
    return session_path


async def _must_not_be_awaited(*args, **kwargs):
    raise AssertionError("headless run started on the TUI path")


def test_run_default_refuses_a_claimed_session_without_launching(tmp_path, monkeypatch):
    """Both front-ends must exit 2 before the app or the headless turn starts —
    a launched-then-refused run would already have loaded the baseline it is
    forbidden to write back."""
    from marim_harness.interfaces.cli.default_cmd import run_default
    from marim_harness.session.claim import try_acquire

    def _must_not_run(*args, **kwargs):
        raise AssertionError("launched despite the session being claimed")

    session_path = _stub_launch_paths(
        monkeypatch, tmp_path, tui=_must_not_run, headless=_must_not_run
    )
    outsider = try_acquire(session_path, kind="daemon", endpoint="http://127.0.0.1:8642")
    assert outsider is not None
    try:
        err = io.StringIO()
        assert run_default([], stdin=_TtyStdin(), out=io.StringIO(), err=err) == 2
        assert "already open" in err.getvalue()

        err = io.StringIO()
        code = run_default(["-p", "hello"], stdin=_TtyStdin(), out=io.StringIO(), err=err)
        assert code == 2
        assert "already open" in err.getvalue()
    finally:
        outsider.release()


def test_run_default_releases_the_claim_after_a_normal_run(tmp_path, monkeypatch):
    """The claim lives exactly as long as the run: once run_default returns,
    another process can take the session."""
    from marim_harness.interfaces.cli.default_cmd import run_default
    from marim_harness.session.claim import try_acquire

    held = {}

    def fake_tui(harness):
        # Mid-run the session is ours: an outsider is refused.
        held["contended"] = try_acquire(harness.session.store.path, kind="daemon")
        return 0

    session_path = _stub_launch_paths(
        monkeypatch, tmp_path, tui=fake_tui, headless=_must_not_be_awaited
    )
    assert run_default([], stdin=_TtyStdin(), out=io.StringIO(), err=io.StringIO()) == 0
    assert held["contended"] is None

    after = try_acquire(session_path, kind="daemon")
    assert after is not None
    after.release()


def test_acquire_session_permits_an_anonymous_session(tmp_path):
    """No store means nothing persisted, so there is nothing to clobber."""
    from marim_harness.interfaces.cli.default_cmd import _acquire_session

    harness = SimpleNamespace(session=SimpleNamespace(store=None))
    claim, ok = _acquire_session(harness, kind="tui", err=io.StringIO())
    assert ok is True
    assert claim is None


def test_resolve_target_session_none_without_resume(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _resolve_target_session

    assert _resolve_target_session(tmp_path, resume=False) is None


def _write_session(workspace, session_id: str, *, updated: str = "") -> Path:
    """A session file where SessionManager(workspace) will actually look for it.

    SessionManager nests every workspace under a hashed
    ``{name}-{digest}/`` directory below XDG_DATA_HOME (store.py's
    ``workspace_slug``/``_workspace_dir``) — never the workspace path itself.
    Writing straight into ``workspace`` (as the brief's Step-1 tests do) is
    invisible to it; route fixtures through the manager's real ``.dir``
    instead, same as the rest of the suite's SessionManager tests.
    """
    from marim_harness.session import SessionManager

    manager = SessionManager(workspace)
    manager.dir.mkdir(parents=True, exist_ok=True)
    path = manager.dir / f"{session_id}.json"
    path.write_text(json.dumps({"id": session_id, "updated": updated}))
    return path


def test_resolve_target_session_picks_latest(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _resolve_target_session

    _write_session(tmp_path, "20260101-000000-aaaaaa", updated="2026-01-01T00:00:00")
    _write_session(tmp_path, "20260202-000000-bbbbbb", updated="2026-02-02T00:00:00")
    assert _resolve_target_session(tmp_path, resume=True) == "20260202-000000-bbbbbb"


def test_resolve_target_session_none_when_no_sessions(tmp_path):
    from marim_harness.interfaces.cli.default_cmd import _resolve_target_session

    assert _resolve_target_session(tmp_path, resume=True) is None


def test_claim_and_build_never_lets_build_harness_look_up_latest_unclaimed(tmp_path, monkeypatch):
    """Review-bot #470: build_harness(resume=True, session_id=None) performs
    its OWN latest() lookup with no claim held (bootstrap.py:133) — a session
    created between our resolve and its lookup would be resumed unclaimed.
    --resume must therefore pin the build via session_id (resolved target) or
    build fresh (resume=False); the resume flag never reaches build_harness."""
    import io

    from marim_harness.interfaces.cli import default_cmd
    from marim_harness.runtime import bootstrap

    seen: dict = {}

    def fake_build(workspace, *, mode, session_id, resume):
        seen["session_id"] = session_id
        seen["resume"] = resume
        return object()

    monkeypatch.setattr(bootstrap, "build_harness", fake_build)
    err = io.StringIO()

    # Resolved target: pinned via session_id, resume stays False.
    target = "20260101-000000-tttttt"
    _write_session(tmp_path, target)
    monkeypatch.setattr(default_cmd, "_resolve_target_session", lambda ws, r: target)
    harness, claim = default_cmd._claim_and_build(
        tmp_path, resume=True, mode=None, kind="headless", err=err
    )
    assert harness is not None and err.getvalue() == ""
    assert seen == {"session_id": target, "resume": False}
    assert claim is not None  # the pre-build claim on the target is returned
    claim.release()

    # No target (fresh or mid-race deleted): a fresh build, never a second
    # unclaimed lookup.
    monkeypatch.setattr(default_cmd, "_resolve_target_session", lambda ws, r: None)
    harness, claim = default_cmd._claim_and_build(
        tmp_path, resume=True, mode=None, kind="headless", err=err
    )
    assert harness is not None
    assert seen == {"session_id": None, "resume": False}
    assert claim is None


def test_claim_target_claims_resolved_session(tmp_path):
    import io

    from marim_harness.interfaces.cli.default_cmd import _claim_target

    _write_session(tmp_path, "20260101-000000-aaaaaa")
    claim, ok = _claim_target(
        tmp_path, "20260101-000000-aaaaaa", kind="headless", err=io.StringIO()
    )
    try:
        assert ok is True and claim is not None
    finally:
        if claim is not None:
            claim.release()


def test_claim_target_refuses_owned_session(tmp_path):
    import io

    from marim_harness.interfaces.cli.default_cmd import _claim_target
    from marim_harness.session.claim import try_acquire

    session_path = _write_session(tmp_path, "20260101-000000-aaaaaa")
    outsider = try_acquire(session_path, kind="daemon", endpoint="http://127.0.0.1:8643")
    err = io.StringIO()
    try:
        claim, ok = _claim_target(tmp_path, "20260101-000000-aaaaaa", kind="headless", err=err)
    finally:
        outsider.release()
    assert ok is False and claim is None
    assert "already open in daemon" in err.getvalue()
    assert "http://127.0.0.1:8643" in err.getvalue()


def test_claim_target_refuses_vanished_session(tmp_path):
    """A target resolved by latest() but deleted before the claim lands must be
    refused, not silently claimed and driven as an empty session (review-bot #466)."""
    import io

    from marim_harness.interfaces.cli.default_cmd import _claim_target

    err = io.StringIO()
    claim, ok = _claim_target(tmp_path, "20260101-000000-aaaaaa", kind="headless", err=err)
    assert ok is False and claim is None
    assert "no longer exists" in err.getvalue()


def test_run_default_refuses_before_building_a_claimed_session(tmp_path, monkeypatch):
    """The ordering proof: with --resume, refusal happens before build_harness
    is even called. Mirrors _stub_launch_paths (tests/test_default_cmd.py:69)
    but records the build call instead of stubbing it away."""
    from marim_harness.interfaces.cli import default_cmd
    from marim_harness.interfaces.cli import headless as headless_mod
    from marim_harness.interfaces.cli.default_cmd import run_default
    from marim_harness.runtime import bootstrap
    from marim_harness.session.claim import try_acquire

    session_path = _write_session(tmp_path, "20260101-000000-aaaaaa")
    outsider = try_acquire(session_path, kind="daemon", endpoint="http://127.0.0.1:8642")
    assert outsider is not None
    built = []
    monkeypatch.setattr(bootstrap, "build_harness", lambda *a, **kw: built.append("build"))
    monkeypatch.setattr(default_cmd, "_tui_available", lambda: True)
    monkeypatch.setattr(default_cmd, "_launch_tui", lambda h: built.append("tui"))
    monkeypatch.setattr(headless_mod, "run_headless", lambda *a, **kw: built.append("headless"))
    err = io.StringIO()
    try:
        code = run_default(
            [str(tmp_path), "--resume", "-p", "x"],
            stdin=_TtyStdin(),
            out=io.StringIO(),
            err=err,
        )
    finally:
        outsider.release()
    assert code == 2
    assert built == []
    assert "already open" in err.getvalue()
