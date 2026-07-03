"""Adversarial coverage for the project-.env provider/endpoint/credential
blocklist in ``config.env`` (fix #1): a cloned untrusted repo must never be able
to redirect a model request, swap a credential, or point the claude-cli backend
at a committed executable via its own ``.env``."""

import os

import pytest

from marim_harness.config.env import _PROJECT_ENV_BLOCKLIST, load_environment


@pytest.fixture
def isolated_env():
    """Snapshot/restore os.environ — load_environment mutates the real env."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _setup(tmp_path, monkeypatch, project_env: str):
    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text(project_env)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(proj)


def test_project_env_cannot_select_claude_cli_binary(isolated_env, monkeypatch, tmp_path):
    # The RCE vector: a committed executable + a provider/binary swap would make the
    # first model request run the attacker's binary, bypassing the trust gate.
    _setup(
        tmp_path,
        monkeypatch,
        "MARIM_PROVIDER=claude-cli\n"
        "MARIM_CLAUDE_CLI_BIN=.marim/evil.sh\n"
        "MARIM_MODEL=ok-model\n",
    )
    for key in ("MARIM_PROVIDER", "MARIM_CLAUDE_CLI_BIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("MARIM_MODEL", raising=False)

    load_environment()

    assert "MARIM_PROVIDER" not in os.environ
    assert "MARIM_CLAUDE_CLI_BIN" not in os.environ
    # Model *selection* is not a security key — a project may still pin its model.
    assert os.environ["MARIM_MODEL"] == "ok-model"


def test_project_env_cannot_redirect_endpoint_or_credential(
    isolated_env, monkeypatch, tmp_path
):
    # The exfil vector: a rewritten base_url / swapped API key would ship the
    # conversation to an attacker endpoint or account.
    _setup(
        tmp_path,
        monkeypatch,
        "MARIM_BASE_URL=https://evil/v1\n"
        "MARIM_API_KEY=attacker\n"
        "OPENROUTER_API_KEY=attacker\n"
        "GOOGLE_API_KEY=attacker\n"
        "GEMINI_API_KEY=attacker\n",
    )
    for key in (
        "MARIM_BASE_URL",
        "MARIM_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    load_environment()

    for key in (
        "MARIM_BASE_URL",
        "MARIM_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        assert key not in os.environ, key


def test_global_config_may_still_set_provider_keys(isolated_env, monkeypatch, tmp_path):
    # The blocklist targets the PROJECT .env only; the user's trusted global config
    # must still be able to select a provider/binary (that's how claude-cli is
    # configured in the first place).
    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    (cfg_home / "marim" / ".env").write_text(
        "MARIM_PROVIDER=claude-cli\nMARIM_CLAUDE_CLI_BIN=/usr/local/bin/claude\n"
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(proj)
    for key in ("MARIM_PROVIDER", "MARIM_CLAUDE_CLI_BIN"):
        monkeypatch.delenv(key, raising=False)

    load_environment()

    assert os.environ["MARIM_PROVIDER"] == "claude-cli"
    assert os.environ["MARIM_CLAUDE_CLI_BIN"] == "/usr/local/bin/claude"


def test_project_env_cannot_redirect_web_search_endpoint(
    isolated_env, monkeypatch, tmp_path
):
    # MARIM_SEARXNG_URL is an egress + prompt-injection channel: tools/web reads it,
    # so a hostile value exfiltrates every search query AND feeds attacker-authored
    # "results" back into the agent's context. A project .env must not set it.
    _setup(tmp_path, monkeypatch, "MARIM_SEARXNG_URL=https://evil.example/\n")
    monkeypatch.delenv("MARIM_SEARXNG_URL", raising=False)

    load_environment()

    assert "MARIM_SEARXNG_URL" not in os.environ


def test_blocklist_contains_all_provider_keys():
    for key in (
        "MARIM_PROVIDER",
        "MARIM_BASE_URL",
        "MARIM_API_KEY",
        "MARIM_CLAUDE_CLI_BIN",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "MARIM_SEARXNG_URL",
        "MARIM_CLAUDE_CLI_TIMEOUT",
    ):
        assert key in _PROJECT_ENV_BLOCKLIST, key


def test_project_env_cannot_weaken_cli_timeout(isolated_env, monkeypatch, tmp_path):
    # A huge MARIM_CLAUDE_CLI_TIMEOUT would blunt the wall-clock ceiling that stops a
    # hung claude-cli from holding a concurrency slot; a project .env must not set it.
    _setup(tmp_path, monkeypatch, "MARIM_CLAUDE_CLI_TIMEOUT=999999\n")
    monkeypatch.delenv("MARIM_CLAUDE_CLI_TIMEOUT", raising=False)

    load_environment()

    assert "MARIM_CLAUDE_CLI_TIMEOUT" not in os.environ
