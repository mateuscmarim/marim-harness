from __future__ import annotations

import stat

import pytest

from marim_harness.codex import env as cenv


def _fake_bin(tmp_path, name="codex"):
    p = tmp_path / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_resolve_binary_prefers_env_override(tmp_path, monkeypatch):
    p = _fake_bin(tmp_path, "my-codex")
    monkeypatch.setenv(cenv.CODEX_BINARY_ENV, str(p))
    assert cenv.resolve_codex_binary() == str(p)


def test_resolve_binary_falls_back_to_path(tmp_path, monkeypatch):
    _fake_bin(tmp_path)
    monkeypatch.delenv(cenv.CODEX_BINARY_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cenv.resolve_codex_binary() == str(tmp_path / "codex")


def test_resolve_binary_missing(monkeypatch, tmp_path):
    monkeypatch.delenv(cenv.CODEX_BINARY_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cenv.resolve_codex_binary() is None


def test_codex_home_default_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert cenv.codex_home().name == ".codex"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cenv.codex_home() == tmp_path


def test_logged_in_requires_auth_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cenv.codex_logged_in() is False
    (tmp_path / "auth.json").write_text("{}")
    assert cenv.codex_logged_in() is True


def test_available_needs_binary_and_login(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(cenv.CODEX_BINARY_ENV, raising=False)
    assert cenv.codex_available() is False
    _fake_bin(tmp_path)
    assert cenv.codex_available() is False
    (tmp_path / "auth.json").write_text("{}")
    assert cenv.codex_available() is True


@pytest.mark.parametrize(
    "raw, expected",
    [("", 600.0), ("30", 30.0), ("1.5", 1.5), ("nope", 600.0), ("-1", 600.0)],
)
def test_timeout_parsing(monkeypatch, raw, expected):
    if raw:
        monkeypatch.setenv(cenv.CODEX_TIMEOUT_ENV, raw)
    else:
        monkeypatch.delenv(cenv.CODEX_TIMEOUT_ENV, raising=False)
    assert cenv.codex_timeout() == expected


@pytest.mark.parametrize(
    "ua, expected",
    [
        ("codex_cli_rs/0.152.1 (Arch Linux; x86_64)", (0, 152, 1)),
        ("codex_cli_rs/1.2 foo", (1, 2)),
        ("garbage", None),
    ],
)
def test_parse_version(ua, expected):
    assert cenv.parse_version(ua) == expected


def test_check_min_version_accepts_and_rejects():
    cenv.check_min_version("codex_cli_rs/0.152.1")
    cenv.check_min_version("codex_cli_rs/1.0.0")
    with pytest.raises(cenv.CodexUnavailable, match="0.152"):
        cenv.check_min_version("codex_cli_rs/0.140.0")
    with pytest.raises(cenv.CodexUnavailable, match="version"):
        cenv.check_min_version("weird")


def test_env_blocklist_hides_codex_knobs():
    from marim_harness.config.env import _PROJECT_ENV_BLOCKLIST

    assert "MARIM_CODEX_CLI_BIN" in _PROJECT_ENV_BLOCKLIST
    assert "MARIM_CODEX_CLI_TIMEOUT" in _PROJECT_ENV_BLOCKLIST
    assert "CODEX_HOME" in _PROJECT_ENV_BLOCKLIST
