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


def test_acquire_session_permits_an_anonymous_session(tmp_path):
    """No store means nothing persisted, so there is nothing to clobber."""
    from marim_harness.interfaces.cli.default_cmd import _acquire_session

    harness = SimpleNamespace(session=SimpleNamespace(store=None))
    claim, ok = _acquire_session(harness, kind="tui", err=io.StringIO())
    assert ok is True
    assert claim is None
