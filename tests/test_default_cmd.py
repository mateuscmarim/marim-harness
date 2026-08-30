import io
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
    """A stand-in exposing only what _acquire_session reads."""
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("{}")
    store = SimpleNamespace(path=session_path, session_id=session_path.stem)
    return SimpleNamespace(session=SimpleNamespace(store=store))


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
