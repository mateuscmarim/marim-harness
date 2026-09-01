"""The serve CLI entry: routing, arg parsing, and startup wiring. uvicorn is
always stubbed; bind_listener binds a real ephemeral socket where not stubbed."""

import io
import socket

import pytest


def _ipv6_loopback_available() -> bool:
    """Whether ``::1`` can actually be bound here — some CI/container
    environments have IPv6 disabled entirely, which makes binding it fail for
    reasons unrelated to what this test is checking."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


def test_router_reserves_serve_keyword():
    from marim_harness.interfaces.cli.router import _MANAGEMENT

    assert "serve" in _MANAGEMENT


def test_serve_main_builds_app_and_runs_uvicorn(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import uvicorn

    calls = {}

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    from marim_harness.interfaces.cli import serve

    # The advertised port (9999, asserted below) comes from args.port, not the
    # real bind — so binding an ephemeral port keeps this test hermetic
    # against a live process squatting 9999 for unrelated reasons.
    monkeypatch.setattr(
        serve, "bind_listener", lambda host, port, _real=serve.bind_listener: _real(host, 0)
    )
    out, err = io.StringIO(), io.StringIO()
    code = serve.main(["--port", "9999"], out=out, err=err)
    assert code == 0
    assert "fd" in calls["kwargs"] and isinstance(calls["kwargs"]["fd"], int)
    assert "host" not in calls["kwargs"]
    assert "port" not in calls["kwargs"]
    assert calls["app"].state.token  # token generated and wired
    token_file = tmp_path / "xdg-data" / "marim-harness" / "server" / "token"
    assert token_file.exists()
    assert "9999" in out.getvalue()
    assert str(token_file) in out.getvalue()


def test_serve_publishes_runtime_json_for_the_life_of_the_run(tmp_path, monkeypatch):
    """runtime.json must exist while uvicorn is serving and be gone after a
    clean exit — a client discovers the daemon by reading it, and the supervisor
    stamps that same endpoint into the claims it takes."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import uvicorn

    from marim_harness.server.runtime import read_runtime, runtime_path
    from marim_harness.server.supervisor import SessionSupervisor

    state_dir = tmp_path / "xdg-data" / "marim-harness" / "server"
    seen = {}

    real_init = SessionSupervisor.__init__

    def spy_init(self, *args, **kwargs):
        seen["endpoint"] = kwargs.get("endpoint")
        real_init(self, *args, **kwargs)

    def fake_run(app, **kwargs):
        # Mid-run: the record is on disk and points at this daemon.
        seen["during"] = read_runtime(state_dir)

    monkeypatch.setattr(SessionSupervisor, "__init__", spy_init)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    from marim_harness.interfaces.cli import serve

    # The advertised port (9998, asserted below) comes from args.port, not the
    # real bind — so binding an ephemeral port keeps this test hermetic
    # against a live process squatting 9998 for unrelated reasons.
    monkeypatch.setattr(
        serve, "bind_listener", lambda host, port, _real=serve.bind_listener: _real(host, 0)
    )
    assert serve.main(["--port", "9998"], out=io.StringIO(), err=io.StringIO()) == 0

    assert seen["endpoint"] == "http://127.0.0.1:9998"
    during = seen["during"]
    assert during is not None
    assert (during.host, during.port) == ("127.0.0.1", 9998)
    assert during.url == "http://127.0.0.1:9998"
    assert not runtime_path(state_dir).exists()  # cleared on the way out


class _Tty(io.StringIO):
    """stdout that claims to be a terminal — the one signal the banner gate reads."""

    def isatty(self) -> bool:
        return True


def _run_serve(argv, tmp_path, monkeypatch, *, out):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("MARIM_NO_BANNER", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    from marim_harness.interfaces.cli import serve

    # These tests care about the banner/args, not the bind: force an
    # ephemeral port so they don't collide with a live daemon squatting the
    # requested port (e.g. the default 8642 on a dev machine).
    monkeypatch.setattr(
        serve, "bind_listener", lambda host, port, _real=serve.bind_listener: _real(host, 0)
    )
    assert serve.main(argv, out=out, err=io.StringIO()) == 0
    return out.getvalue()


def test_serve_prints_the_wordmark_on_a_tty(tmp_path, monkeypatch):
    text = _run_serve([], tmp_path, monkeypatch, out=_Tty())
    assert "█" in text  # the wordmark
    assert "\033[" in text  # accented
    assert "workspaces" in text and "idle ttl" in text


def test_serve_stays_plain_when_stdout_is_not_a_tty(tmp_path, monkeypatch):
    text = _run_serve([], tmp_path, monkeypatch, out=io.StringIO())
    assert "█" not in text
    assert "\033[" not in text
    assert text.startswith("marim serve ")


def test_serve_no_banner_flag_and_env_suppress_the_wordmark(tmp_path, monkeypatch):
    flagged = _run_serve(["--no-banner"], tmp_path, monkeypatch, out=_Tty())
    assert "█" not in flagged

    monkeypatch.setenv("MARIM_NO_BANNER", "1")
    out = _Tty()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    from marim_harness.interfaces.cli import serve

    monkeypatch.setattr(
        serve, "bind_listener", lambda host, port, _real=serve.bind_listener: _real(host, 0)
    )
    assert serve.main([], out=out, err=io.StringIO()) == 0
    assert "█" not in out.getvalue()


def test_serve_banner_honors_no_color(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = _Tty()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    from marim_harness.interfaces.cli import serve

    monkeypatch.setattr(
        serve, "bind_listener", lambda host, port, _real=serve.bind_listener: _real(host, 0)
    )
    assert serve.main([], out=out, err=io.StringIO()) == 0
    text = out.getvalue()
    assert "█" in text and "\033[" not in text


def test_serve_startup_reports_the_workspaces_root_it_adopted(tmp_path, monkeypatch):
    root = tmp_path / "elsewhere"
    text = _run_serve(
        ["--workspaces-root", str(root), "--idle-ttl", "30"],
        tmp_path,
        monkeypatch,
        out=io.StringIO(),
    )
    assert str(root) in text
    assert "idle ttl: 30s" in text


def test_serve_main_rejects_unknown_args(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import pytest

    from marim_harness.interfaces.cli import serve

    with pytest.raises(SystemExit):
        serve.main(["--bogus"], out=io.StringIO(), err=io.StringIO())


def test_bind_listener_second_bind_fails():
    from marim_harness.interfaces.cli.serve import bind_listener

    first = bind_listener("127.0.0.1", 0)
    try:
        port = first.getsockname()[1]
        import pytest

        with pytest.raises(OSError):
            bind_listener("127.0.0.1", port)
    finally:
        first.close()


@pytest.mark.skipif(not _ipv6_loopback_available(), reason="IPv6 loopback unavailable")
def test_bind_listener_strips_ipv6_brackets():
    from marim_harness.interfaces.cli.serve import bind_listener

    sock = bind_listener("[::1]", 0)
    try:
        assert sock.family.name == "AF_INET6"
    finally:
        sock.close()


def test_serve_bind_failure_publishes_no_runtime_json(tmp_path, monkeypatch):
    import io

    from marim_harness.interfaces.cli import serve

    occupier = serve.bind_listener("127.0.0.1", 0)
    port = occupier.getsockname()[1]
    monkeypatch.setattr(serve, "_default_state_dir", lambda: tmp_path)
    ran = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: ran.append(True))
    err = io.StringIO()
    try:
        rc = serve.main(["--port", str(port)], out=io.StringIO(), err=err)
    finally:
        occupier.close()
    assert rc == 1
    assert ran == []
    assert not (tmp_path / "runtime.json").exists()
    assert "cannot bind" in err.getvalue()
