import io
import json

from marim_harness.cli import config as config_cmd


def _clear_marim_env(monkeypatch):
    for name in (
        "MARIM_PROVIDER",
        "MARIM_MODEL",
        "MARIM_BASE_URL",
        "MARIM_API_KEY",
        "OPENROUTER_API_KEY",
        "MARIM_MAX_CONTEXT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_no_subcommand_returns_2():
    err = io.StringIO()
    assert config_cmd.main([], err=err) == 2
    assert err.getvalue().strip()  # some usage/help text


def test_show_text_no_key(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("MARIM_MODEL", "openai/gpt-5.2")
    out = io.StringIO()
    code = config_cmd.main(["show"], out=out)
    assert code == 0
    text = out.getvalue()
    assert "openrouter" in text
    assert "openai/gpt-5.2" in text
    assert "not set" in text  # api key not set
    assert str(tmp_path / "marim" / ".env") in text


def test_show_text_with_key_does_not_leak(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-supersecret")
    out = io.StringIO()
    assert config_cmd.main(["show"], out=out) == 0
    text = out.getvalue()
    assert "sk-supersecret" not in text
    assert "set" in text


def test_show_json(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    monkeypatch.setenv("MARIM_MODEL", "openai/gpt-5.2")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "12345")
    out = io.StringIO()
    assert config_cmd.main(["show", "--json"], out=out) == 0
    obj = json.loads(out.getvalue())
    assert obj["provider"] == "openrouter"
    assert obj["model"] == "openai/gpt-5.2"
    assert obj["max_context_tokens"] == 12345
    assert obj["api_key_set"] is True
    assert obj["global_config_path"] == str(tmp_path / "marim" / ".env")
    assert "api_key" not in obj
    assert "sk-secret" not in out.getvalue()


def test_set_creates_file_and_writes_line(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    out = io.StringIO()
    code = config_cmd.main(["set", "MARIM_MODEL", "openai/gpt-5.2"], out=out)
    assert code == 0
    env_file = tmp_path / "marim" / ".env"
    assert env_file.exists()
    assert "MARIM_MODEL=openai/gpt-5.2" in env_file.read_text()
    assert "openai/gpt-5.2" in out.getvalue()


def test_set_updates_existing_key_in_place(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    env_file = tmp_path / "marim" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("MARIM_MODEL=old-model\nMARIM_PROVIDER=local\n")
    assert config_cmd.main(["set", "MARIM_MODEL", "new-model"]) == 0
    content = env_file.read_text()
    assert "MARIM_MODEL=new-model" in content
    assert "old-model" not in content
    assert "MARIM_PROVIDER=local" in content  # other lines preserved
    # key not duplicated
    assert content.count("MARIM_MODEL=") == 1


def test_set_rejects_unknown_key(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    err = io.StringIO()
    code = config_cmd.main(["set", "BOGUS_KEY", "x"], err=err)
    assert code == 2
    assert err.getvalue().strip()
    assert not (tmp_path / "marim" / ".env").exists()


def test_set_masks_secret_value_in_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    out = io.StringIO()
    assert config_cmd.main(["set", "MARIM_API_KEY", "sk-supersecret"], out=out) == 0
    text = out.getvalue()
    assert "sk-supersecret" not in text
    assert "***" in text
    # but the real value is written to disk
    env_file = tmp_path / "marim" / ".env"
    assert "MARIM_API_KEY=sk-supersecret" in env_file.read_text()
