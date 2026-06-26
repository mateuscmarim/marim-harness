import os
from pathlib import Path

import pytest

from marim_harness.config import (
    ModelConfig,
    ModelSource,
    config_dir,
    global_config_path,
    load_config,
    load_environment,
)


@pytest.fixture
def isolated_env():
    """Snapshot and restore os.environ so dotenv-set vars don't leak across
    tests (load_environment mutates the real environment)."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_load_config_reads_command_lists(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_COMMAND_DENYLIST", "rm -rf, sudo")
    monkeypatch.setenv("MARIM_COMMAND_ALLOWLIST", "^git , ^ls")
    cfg = load_config()
    assert cfg.command_denylist == ["rm -rf", "sudo"]
    # patterns are whitespace-stripped, like every other comma-separated list
    assert cfg.command_allowlist == ["^git", "^ls"]


def test_load_config_reads_subagent_concurrency(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_SUBAGENT_CONCURRENCY", "3")
    assert load_config().subagent_concurrency == 3


def test_subagent_concurrency_defaults_to_unbounded(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_SUBAGENT_CONCURRENCY", raising=False)
    # Unset (and the explicit 0 "off" sentinel) both mean no cap.
    assert load_config().subagent_concurrency is None
    monkeypatch.setenv("MARIM_SUBAGENT_CONCURRENCY", "0")
    assert load_config().subagent_concurrency is None


def test_negative_subagent_concurrency_maps_to_none(monkeypatch):
    monkeypatch.setenv("MARIM_SUBAGENT_CONCURRENCY", "-5")
    assert load_config().subagent_concurrency is None


def test_detach_fanout_defaults_on(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_DETACH_FANOUT", raising=False)
    assert load_config().detach_fanout is True


def test_detach_fanout_opt_out(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_DETACH_FANOUT", "0")
    assert load_config().detach_fanout is False


def test_load_config_command_lists_default_empty(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_COMMAND_DENYLIST", raising=False)
    monkeypatch.delenv("MARIM_COMMAND_ALLOWLIST", raising=False)
    cfg = load_config()
    assert cfg.command_denylist == []
    assert cfg.command_allowlist == []


def test_load_config_defaults_to_openrouter(monkeypatch):
    monkeypatch.delenv("MARIM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    cfg = load_config()
    assert isinstance(cfg, ModelConfig)
    assert cfg.provider == "openrouter"
    assert cfg.api_key == "sk-test"
    assert cfg.model  # a non-empty default model id


def test_load_config_local_reads_base_url(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "local")
    monkeypatch.setenv("MARIM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("MARIM_MODEL", "qwen2.5-coder")
    cfg = load_config()
    assert cfg.provider == "local"
    assert cfg.base_url == "http://localhost:11434/v1"
    assert cfg.model == "qwen2.5-coder"


def test_unknown_provider_warns_and_falls_back_to_openrouter(monkeypatch, caplog):
    monkeypatch.setenv("MARIM_PROVIDER", "azure")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    with caplog.at_level("WARNING", logger="marim_harness.config.model"):
        cfg = load_config()
    # Behavior preserved (still constructs an openrouter config) but no longer silent.
    assert cfg.provider == "openrouter"
    assert any("azure" in r.message for r in caplog.records)


def test_int_env_logs_when_value_unparseable(monkeypatch, caplog):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "abc")
    with caplog.at_level("DEBUG", logger="marim_harness.config.model"):
        cfg = load_config()
    assert cfg.max_context_tokens == 100_000  # reverts to default
    assert any("MARIM_MAX_CONTEXT_TOKENS" in r.message for r in caplog.records)


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "on", "yes", "Yes"])
def test_proactive_memory_truthy_values_enable(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_PROACTIVE_MEMORY", raw)
    assert load_config().proactive_memory is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", "", "garbage"])
def test_proactive_memory_non_truthy_values_disable(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_PROACTIVE_MEMORY", raw)
    assert load_config().proactive_memory is False


def test_proactive_memory_defaults_off(monkeypatch):
    monkeypatch.delenv("MARIM_PROACTIVE_MEMORY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert load_config().proactive_memory is False


def test_model_source_label_prefixes_provider():
    src = ModelSource(ModelConfig(provider="openrouter", model="anthropic/claude-sonnet-4-6"))
    assert src.label("openai/gpt-5.2") == "openrouter/openai/gpt-5.2"


def test_load_config_google_reads_api_key(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MARIM_API_KEY", raising=False)
    cfg = load_config()
    assert cfg.provider == "google"
    assert cfg.api_key == "AIza-test"
    assert cfg.model  # non-empty default


def test_load_config_google_falls_back_to_gemini_key(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "google")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.delenv("MARIM_API_KEY", raising=False)
    cfg = load_config()
    assert cfg.api_key == "gemini-key"


def test_load_config_google_model_override(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("MARIM_MODEL", "gemini-2.0-flash")
    cfg = load_config()
    assert cfg.model == "gemini-2.0-flash"


def test_model_source_is_local_reflects_provider():
    assert ModelSource(ModelConfig(provider="local", model="x")).is_local is True
    assert ModelSource(ModelConfig(provider="openrouter", model="x")).is_local is False
    assert ModelSource(ModelConfig(provider="google", model="x")).is_local is False


@pytest.mark.anyio
async def test_model_source_list_models_empty_for_local_without_base_url():
    src = ModelSource(ModelConfig(provider="local", model="x"))  # base_url is None
    assert await src.list_models() == []  # nothing to query without an endpoint


@pytest.mark.anyio
async def test_model_source_list_models_fetches_local_catalog(monkeypatch):
    """A local provider with a base_url lists models from that server's
    /v1/models endpoint (LM Studio / Ollama)."""
    from unittest.mock import AsyncMock

    from marim_harness.workspace import ModelEntry

    fake = AsyncMock(return_value=[ModelEntry(id="qwen2.5-coder", name="qwen2.5-coder")])
    monkeypatch.setattr("marim_harness.config.model.fetch_local_models", fake)
    src = ModelSource(ModelConfig(provider="local", model="x", base_url="http://localhost:1234/v1",
                                  api_key="lmstudio"))
    entries = await src.list_models()
    assert [e.id for e in entries] == ["qwen2.5-coder"]
    fake.assert_awaited_once_with("http://localhost:1234/v1", "lmstudio")


@pytest.mark.anyio
async def test_model_source_list_models_empty_for_google():
    src = ModelSource(ModelConfig(provider="google", model="gemini-2.5-flash"))
    assert await src.list_models() == []  # no catalog for Google


def test_model_source_build_swaps_in_the_model_id():
    src = ModelSource(ModelConfig(provider="local", model="x", base_url="http://h/v1"))
    model = src.build("some-other-model")
    # The constructed model reports the swapped id, not the config default ("x").
    # The default here is a sentinel that can never equal the expected value, so
    # a missing/renamed attribute fails the test instead of silently passing.
    assert getattr(model, "model_name", None) == "some-other-model"


def test_config_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_dir() == tmp_path / "marim"


def test_config_dir_defaults_to_dot_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert config_dir() == Path("/home/u/.config/marim")


def test_global_config_path_is_env_under_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert global_config_path() == tmp_path / "marim" / ".env"


def test_load_environment_project_overrides_global(isolated_env, monkeypatch, tmp_path):
    # Global config supplies the key; project .env overrides the model.
    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    (cfg_home / "marim" / ".env").write_text(
        "OPENROUTER_API_KEY=global-key\nMARIM_MODEL=global-model\n"
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text("MARIM_MODEL=project-model\n")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MARIM_MODEL", raising=False)

    load_environment()

    assert os.environ["OPENROUTER_API_KEY"] == "global-key"  # fallback from global
    assert os.environ["MARIM_MODEL"] == "project-model"  # project wins over global


def test_load_environment_real_env_wins(isolated_env, monkeypatch, tmp_path):
    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    (cfg_home / "marim" / ".env").write_text("OPENROUTER_API_KEY=global-key\n")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-key")

    load_environment()

    assert os.environ["OPENROUTER_API_KEY"] == "real-key"  # shell env beats files


def test_load_environment_project_env_cannot_grant_trust(isolated_env, monkeypatch, tmp_path):
    # A cloned/untrusted project shipping its own .env must NOT be able to flip
    # the hooks trust flag — that gate decides whether .marim/hooks.json (arbitrary
    # commands) runs. Trust may come only from the real shell env or global config.
    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text("MARIM_TRUST_PROJECT_HOOKS=1\n")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)

    load_environment()

    assert "MARIM_TRUST_PROJECT_HOOKS" not in os.environ
    assert load_config().trust_project_hooks is False


def test_load_environment_global_env_can_grant_trust(isolated_env, monkeypatch, tmp_path):
    # The user's own global config IS trusted, so it may enable project hooks.
    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    (cfg_home / "marim" / ".env").write_text("MARIM_TRUST_PROJECT_HOOKS=1\n")
    proj = tmp_path / "proj"
    proj.mkdir()

    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)

    load_environment()

    assert os.environ["MARIM_TRUST_PROJECT_HOOKS"] == "1"
    assert load_config().trust_project_hooks is True


def test_load_environment_project_env_cannot_set_command_policy(
    isolated_env, monkeypatch, tmp_path
):
    # The shell-command allow/deny policy is a security control set by the user,
    # not by a checked-out repo — the project .env can't define or weaken it.
    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text(
        "MARIM_COMMAND_DENYLIST=\nMARIM_COMMAND_ALLOWLIST=rm\nMARIM_MODEL=ok-model\n"
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("MARIM_COMMAND_DENYLIST", raising=False)
    monkeypatch.delenv("MARIM_COMMAND_ALLOWLIST", raising=False)
    monkeypatch.delenv("MARIM_MODEL", raising=False)

    load_environment()

    assert "MARIM_COMMAND_DENYLIST" not in os.environ
    assert "MARIM_COMMAND_ALLOWLIST" not in os.environ
    # A non-security key from the same project .env still loads normally.
    assert os.environ["MARIM_MODEL"] == "ok-model"


def test_lsp_defaults_on(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_LSP", raising=False)
    monkeypatch.delenv("MARIM_LSP_TOOLS", raising=False)
    cfg = load_config()
    assert cfg.lsp_enabled is True
    assert cfg.lsp_tools_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no"])
def test_lsp_master_switch_off(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_LSP", raw)
    assert load_config().lsp_enabled is False


@pytest.mark.parametrize("raw", ["0", "false", "off", "no"])
def test_lsp_tools_switch_off(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_LSP_TOOLS", raw)
    cfg = load_config()
    # tools off does not flip the master switch — diagnostics-on-edit survives.
    assert cfg.lsp_enabled is True
    assert cfg.lsp_tools_enabled is False


def test_job_tool_combined_defaults_off(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_JOB_TOOL_COMBINED", raising=False)
    assert load_config().job_tool_combined is False


@pytest.mark.parametrize("raw", ["1", "true", "on", "yes"])
def test_job_tool_combined_truthy_enables(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_JOB_TOOL_COMBINED", raw)
    assert load_config().job_tool_combined is True


def test_trust_project_hooks_defaults_false(monkeypatch):
    from marim_harness.config.model import load_config
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    assert load_config().trust_project_hooks is False


def test_trust_project_hooks_env_truthy(monkeypatch):
    from marim_harness.config.model import load_config
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    assert load_config().trust_project_hooks is True


def test_autonomous_wake_defaults_on(monkeypatch):
    monkeypatch.delenv("MARIM_AUTONOMOUS_WAKE", raising=False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().autonomous_wake is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no"])
def test_autonomous_wake_falsy_disables(monkeypatch, raw):
    monkeypatch.setenv("MARIM_AUTONOMOUS_WAKE", raw)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().autonomous_wake is False


def test_wake_depth_cap_defaults_to_eight(monkeypatch):
    monkeypatch.delenv("MARIM_WAKE_DEPTH_CAP", raising=False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().wake_depth_cap == 8


def test_wake_depth_cap_reads_env(monkeypatch):
    monkeypatch.setenv("MARIM_WAKE_DEPTH_CAP", "5")
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().wake_depth_cap == 5


def test_load_environment_survives_malformed_project_env(
    isolated_env, monkeypatch, tmp_path, caplog
):
    """A corrupt/hostile project .env (runs on cloned repos) must not crash
    startup: load_environment logs a warning and continues, and a good global
    fallback still applies."""
    from unittest.mock import patch

    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    (cfg_home / "marim" / ".env").write_text("MARIM_MODEL=global-model\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text("garbage not a dotenv\n")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("MARIM_MODEL", raising=False)

    # Force the project parse to raise to prove the guard catches *any* failure,
    # not just whatever dotenv happens to tolerate. env.py imports dotenv_values
    # locally from `dotenv`, so patch it at the source module.
    with (
        patch("dotenv.dotenv_values", side_effect=ValueError("corrupt .env")),
        caplog.at_level("WARNING", logger="marim_harness.config.env"),
    ):
        load_environment()  # must not raise

    assert any("project .env" in r.message for r in caplog.records)
    # global fallback still applied despite the broken project file
    assert os.environ["MARIM_MODEL"] == "global-model"


@pytest.mark.parametrize("key", ["MARIM_MAX_CONTEXT_TOKENS", "MARIM_WAKE_DEPTH_CAP"])
@pytest.mark.parametrize("bad", ["-5", "0", "abc", "1.5", "  "])
def test_load_environment_drops_invalid_positive_int(
    isolated_env, monkeypatch, tmp_path, key, bad
):
    """Negative/zero/non-integer numeric knobs are dropped so the downstream
    reader falls back to its default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(key, bad)
    load_environment()
    assert key not in os.environ


@pytest.mark.parametrize("key", ["MARIM_MAX_CONTEXT_TOKENS", "MARIM_WAKE_DEPTH_CAP"])
def test_load_environment_keeps_valid_positive_int(isolated_env, monkeypatch, tmp_path, key):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(key, "42")
    load_environment()
    assert os.environ[key] == "42"


def test_load_environment_invalid_int_falls_back_to_default(isolated_env, monkeypatch, tmp_path):
    """End-to-end: a bad MARIM_MAX_CONTEXT_TOKENS yields the built-in default in
    the resolved config (not the garbage value)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "-9")
    load_environment()
    assert load_config().max_context_tokens == 100_000


def test_parse_qualified_known_prefix_routes_to_provider():
    from marim_harness.config.model import parse_qualified
    active = {"openrouter", "local", "google"}
    assert parse_qualified("openrouter:anthropic/claude-sonnet-4-6", active, "openrouter") == (
        "openrouter", "anthropic/claude-sonnet-4-6")
    assert parse_qualified("local:qwen2.5-coder", active, "openrouter") == (
        "local", "qwen2.5-coder")


def test_parse_qualified_bare_id_uses_default():
    from marim_harness.config.model import parse_qualified
    active = {"openrouter", "local"}
    assert parse_qualified("anthropic/claude-sonnet-4-6", active, "openrouter") == (
        "openrouter", "anthropic/claude-sonnet-4-6")


def test_parse_qualified_unknown_prefix_is_treated_as_bare_id():
    from marim_harness.config.model import parse_qualified
    # 'google' is NOT active here, so 'google/gemma' is a bare OpenRouter id, not a provider.
    active = {"openrouter", "local"}
    assert parse_qualified("google/gemma-2-9b", active, "openrouter") == (
        "openrouter", "google/gemma-2-9b")


def test_detect_active_providers_includes_each_with_creds(monkeypatch):
    from marim_harness.config.model import detect_active_providers
    for k in ("MARIM_PROVIDER", "OPENROUTER_API_KEY", "GOOGLE_API_KEY",
              "GEMINI_API_KEY", "MARIM_BASE_URL", "MARIM_API_KEY", "MARIM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("MARIM_BASE_URL", "http://localhost:1234/v1")
    configs, default = detect_active_providers()
    assert set(configs) == {"openrouter", "local"}
    assert default == "openrouter"
    assert configs["local"].base_url == "http://localhost:1234/v1"


def test_detect_active_providers_always_includes_default(monkeypatch):
    from marim_harness.config.model import detect_active_providers
    for k in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
              "MARIM_BASE_URL", "MARIM_API_KEY", "MARIM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MARIM_PROVIDER", "google")  # default, but no key set
    configs, default = detect_active_providers()
    assert default == "google"
    assert "google" in configs


@pytest.mark.anyio
async def test_multi_source_list_models_merges_and_tags(monkeypatch):
    from unittest.mock import AsyncMock

    from marim_harness.config.model import ModelConfig, ModelSource, MultiModelSource
    from marim_harness.workspace import ModelEntry

    orc = ModelSource(ModelConfig(provider="openrouter", model="x"))
    loc = ModelSource(ModelConfig(provider="local", model="y", base_url="http://h/v1"))
    monkeypatch.setattr(orc, "list_models",
                        AsyncMock(return_value=[ModelEntry(id="anthropic/c", name="C")]))
    monkeypatch.setattr(loc, "list_models",
                        AsyncMock(return_value=[ModelEntry(id="qwen", name="Qwen")]))
    multi = MultiModelSource({"openrouter": orc, "local": loc}, "openrouter")
    entries = await multi.list_models()
    tagged = {e.qualified for e in entries}
    assert tagged == {"openrouter:anthropic/c", "local:qwen"}


@pytest.mark.anyio
async def test_multi_source_list_models_survives_a_failing_provider(monkeypatch):
    from unittest.mock import AsyncMock

    from marim_harness.config.model import ModelConfig, ModelSource, MultiModelSource
    from marim_harness.workspace import ModelEntry

    ok = ModelSource(ModelConfig(provider="local", model="y", base_url="http://h/v1"))
    bad = ModelSource(ModelConfig(provider="openrouter", model="x"))
    monkeypatch.setattr(ok, "list_models",
                        AsyncMock(return_value=[ModelEntry(id="qwen", name="Q")]))
    monkeypatch.setattr(bad, "list_models", AsyncMock(side_effect=RuntimeError("down")))
    multi = MultiModelSource({"local": ok, "openrouter": bad}, "openrouter")
    entries = await multi.list_models()
    assert [e.qualified for e in entries] == ["local:qwen"]


def test_multi_source_build_routes_by_prefix(monkeypatch):
    from marim_harness.config.model import ModelConfig, ModelSource, MultiModelSource
    calls = {}
    orc = ModelSource(ModelConfig(provider="openrouter", model="x"))
    loc = ModelSource(ModelConfig(provider="local", model="y", base_url="http://h/v1"))
    monkeypatch.setattr(orc, "build", lambda mid: calls.setdefault("or", mid))
    monkeypatch.setattr(loc, "build", lambda mid: calls.setdefault("loc", mid))
    multi = MultiModelSource({"openrouter": orc, "local": loc}, "openrouter")
    multi.build("local:qwen2.5-coder")
    multi.build("anthropic/claude-sonnet-4-6")  # bare -> default (openrouter)
    assert calls == {"loc": "qwen2.5-coder", "or": "anthropic/claude-sonnet-4-6"}


def test_multi_source_label_qualifies():
    from marim_harness.config.model import ModelConfig, ModelSource, MultiModelSource
    orc = ModelSource(ModelConfig(provider="openrouter", model="x"))
    multi = MultiModelSource({"openrouter": orc}, "openrouter")
    assert multi.label("openrouter:anthropic/c") == "openrouter:anthropic/c"
    assert multi.label("anthropic/c") == "openrouter:anthropic/c"  # bare gains default prefix
