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


def test_serve_main_rejects_unknown_args(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    import pytest

    from marim_harness.interfaces.cli import serve

    with pytest.raises(SystemExit):
        serve.main(["--bogus"], out=io.StringIO(), err=io.StringIO())
