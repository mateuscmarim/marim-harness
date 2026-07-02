import io
import json

from marim_harness.interfaces.cli import config as config_cmd


def _clear_marim_env(monkeypatch):
    for name in (
        "MARIM_PROVIDER",
        "MARIM_MODEL",
        "MARIM_BASE_URL",
        "MARIM_API_KEY",
        "OPENROUTER_API_KEY",
        "MARIM_MAX_CONTEXT_TOKENS",
        "MARIM_PROACTIVE_MEMORY",
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


def test_set_value_with_special_chars_round_trips(monkeypatch, tmp_path):
    """A value containing whitespace or '#' must survive write→reload. Written
    unquoted, dotenv strips everything from the '#' on (and trailing space), so
    the value read back would not match what was set."""
    from dotenv import dotenv_values

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    value = "http://proxy/v1 # staging"
    assert config_cmd.main(["set", "MARIM_BASE_URL", value]) == 0
    env_file = tmp_path / "marim" / ".env"
    assert dotenv_values(env_file)["MARIM_BASE_URL"] == value
    # a sibling plain value is still preserved on a later write
    assert config_cmd.main(["set", "MARIM_MODEL", "openai/gpt-5.2"]) == 0
    vals = dotenv_values(env_file)
    assert vals["MARIM_BASE_URL"] == value
    assert vals["MARIM_MODEL"] == "openai/gpt-5.2"


def test_set_accepts_proactive_memory_key(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    out = io.StringIO()
    code = config_cmd.main(["set", "MARIM_PROACTIVE_MEMORY", "true"], out=out)
    assert code == 0
    env_file = tmp_path / "marim" / ".env"
    assert "MARIM_PROACTIVE_MEMORY=true" in env_file.read_text()


def test_show_surfaces_proactive_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    monkeypatch.setenv("MARIM_PROACTIVE_MEMORY", "true")
    out = io.StringIO()
    assert config_cmd.main(["show"], out=out) == 0
    assert "proactive_memory" in out.getvalue()

    out_json = io.StringIO()
    assert config_cmd.main(["show", "--json"], out=out_json) == 0
    assert json.loads(out_json.getvalue())["proactive_memory"] is True


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


def test_set_accepts_context_budget_including_zero(monkeypatch, tmp_path):
    """MARIM_CONTEXT_BUDGET is settable and 0 (= unbudgeted) is a valid value."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    assert config_cmd.main(["set", "MARIM_CONTEXT_BUDGET", "0"], out=io.StringIO()) == 0
    assert "MARIM_CONTEXT_BUDGET=0" in (tmp_path / "marim" / ".env").read_text()


def test_set_rejects_negative_context_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    err = io.StringIO()
    assert config_cmd.main(["set", "MARIM_CONTEXT_BUDGET", "-5"], err=err) == 2
    assert err.getvalue().strip()


def test_set_accepts_context_window_and_budgets(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    assert config_cmd.main(["set", "MARIM_CONTEXT_WINDOW", "131072"], out=io.StringIO()) == 0
    assert config_cmd.main(
        ["set", "MARIM_CONTEXT_BUDGETS", "anthropic/claude-opus*=60000"], out=io.StringIO()
    ) == 0
    text = (tmp_path / "marim" / ".env").read_text()
    assert "MARIM_CONTEXT_WINDOW=131072" in text
    assert "MARIM_CONTEXT_BUDGETS=anthropic/claude-opus*=60000" in text


def test_set_rejects_non_positive_context_window(monkeypatch, tmp_path):
    """0 means unbudgeted for the BUDGET; a window of 0 is meaningless."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    err = io.StringIO()
    assert config_cmd.main(["set", "MARIM_CONTEXT_WINDOW", "0"], err=err) == 2
    assert "positive" in err.getvalue().lower()


def test_set_still_accepts_deprecated_max_context_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    code = config_cmd.main(["set", "MARIM_MAX_CONTEXT_TOKENS", "120000"], out=io.StringIO())
    assert code == 0
    assert "MARIM_MAX_CONTEXT_TOKENS=120000" in (tmp_path / "marim" / ".env").read_text()


def test_set_rejects_invalid_default_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    err = io.StringIO()
    code = config_cmd.main(["set", "MARIM_DEFAULT_MODE", "yolo"], err=err)
    assert code == 2
    assert "ask" in err.getvalue() and "auto" in err.getvalue()


def test_set_accepts_and_normalizes_default_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    out = io.StringIO()
    code = config_cmd.main(["set", "MARIM_DEFAULT_MODE", "Auto"], out=out)
    assert code == 0
    env_text = (tmp_path / "marim" / ".env").read_text()
    assert "MARIM_DEFAULT_MODE=auto" in env_text


def test_show_includes_default_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    monkeypatch.setenv("MARIM_DEFAULT_MODE", "plan")
    out = io.StringIO()
    assert config_cmd.main(["show"], out=out) == 0
    text = out.getvalue()
    assert "default_mode" in text and "plan" in text


def test_set_rejects_invalid_tool_search(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    err = io.StringIO()
    assert config_cmd.main(["set", "MARIM_TOOL_SEARCH", "maybe"], err=err) == 2
    assert "off" in err.getvalue() and "auto" in err.getvalue() and "on" in err.getvalue()


def test_set_accepts_tool_search(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_cmd.main(["set", "MARIM_TOOL_SEARCH", "On"], out=io.StringIO()) == 0
    assert "MARIM_TOOL_SEARCH=on" in (tmp_path / "marim" / ".env").read_text()


def test_set_rejects_non_positive_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    err = io.StringIO()
    assert config_cmd.main(["set", "MARIM_TOOL_SEARCH_THRESHOLD", "0"], err=err) == 2
    assert "positive" in err.getvalue().lower()


def test_set_accepts_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_cmd.main(["set", "MARIM_TOOL_SEARCH_THRESHOLD", "25"], out=io.StringIO()) == 0
    assert "MARIM_TOOL_SEARCH_THRESHOLD=25" in (tmp_path / "marim" / ".env").read_text()


def test_show_includes_tool_search(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    monkeypatch.setenv("MARIM_TOOL_SEARCH", "on")
    out = io.StringIO()
    assert config_cmd.main(["show"], out=out) == 0
    text = out.getvalue()
    assert "tool_search" in text and "on" in text
