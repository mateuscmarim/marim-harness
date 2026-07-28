"""The serve CLI entry: routing, arg parsing, and startup wiring (uvicorn is
stubbed — we never bind a real port in tests)."""

import io


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

    out, err = io.StringIO(), io.StringIO()
    code = serve.main(["--port", "9999"], out=out, err=err)
    assert code == 0
    assert calls["kwargs"]["host"] == "127.0.0.1"
    assert calls["kwargs"]["port"] == 9999
    assert calls["app"].state.token  # token generated and wired
    token_file = tmp_path / "xdg-data" / "marim-harness" / "server" / "token"
    assert token_file.exists()
    assert "9999" in out.getvalue()
    assert str(token_file) in out.getvalue()


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

    assert serve.main([], out=out, err=io.StringIO()) == 0
    assert "█" not in out.getvalue()


def test_serve_banner_honors_no_color(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = _Tty()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    from marim_harness.interfaces.cli import serve

    assert serve.main([], out=out, err=io.StringIO()) == 0
    text = out.getvalue()
    assert "█" in text and "\033[" not in text


def test_serve_startup_reports_the_workspaces_root_it_adopted(tmp_path, monkeypatch):
    root = tmp_path / "elsewhere"
    text = _run_serve(
        ["--workspaces-root", str(root), "--idle-ttl", "30"],
        tmp_path, monkeypatch, out=io.StringIO(),
    )
    assert str(root) in text
    assert "idle ttl: 30s" in text


def test_serve_main_rejects_unknown_args(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import pytest

    from marim_harness.interfaces.cli import serve

    with pytest.raises(SystemExit):
        serve.main(["--bogus"], out=io.StringIO(), err=io.StringIO())
