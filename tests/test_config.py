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
    assert load_config().subagent.concurrency == 3


def test_workflows_env_gate(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_WORKFLOWS", "0")
    cfg = load_config()
    assert cfg.workflows_enabled is False


def test_workflow_timeout_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_WORKFLOW_TIMEOUT", "3600")
    cfg = load_config()
    assert cfg.workflow_timeout_secs == 3600.0


def test_workflow_timeout_defaults_and_rejects_garbage(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_WORKFLOW_TIMEOUT", raising=False)
    assert load_config().workflow_timeout_secs == 1800.0
    for bad in ("banana", "0", "-5"):
        monkeypatch.setenv("MARIM_WORKFLOW_TIMEOUT", bad)
        assert load_config().workflow_timeout_secs == 1800.0


def test_subagent_concurrency_defaults_to_a_cap(monkeypatch):
    """Unset means the default cap, not unlimited: a runaway fan-out (a live
    workflow once iterated a JSON string and queued one spawn per CHARACTER)
    must be contained by default. 0 stays the explicit no-cap escape hatch."""
    from marim_harness.config.model import DEFAULT_SUBAGENT_CONCURRENCY

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_SUBAGENT_CONCURRENCY", raising=False)
    assert load_config().subagent.concurrency == DEFAULT_SUBAGENT_CONCURRENCY
    monkeypatch.setenv("MARIM_SUBAGENT_CONCURRENCY", "0")
    assert load_config().subagent.concurrency is None


def test_negative_subagent_concurrency_maps_to_none(monkeypatch):
    monkeypatch.setenv("MARIM_SUBAGENT_CONCURRENCY", "-5")
    assert load_config().subagent.concurrency is None


def test_parse_concurrency_helper():
    """The pure resolver, tested directly: unset/blank ⇒ the cap; a parseable
    non-positive int ⇒ None (unbounded opt-out); a positive int ⇒ itself;
    unparseable garbage ⇒ the safe cap, never silently unbounded."""
    from marim_harness.config.model import _parse_concurrency

    assert _parse_concurrency(None, 8) == 8
    assert _parse_concurrency("", 8) == 8
    assert _parse_concurrency("0", 8) is None
    assert _parse_concurrency("-3", 8) is None
    assert _parse_concurrency("4", 8) == 4
    assert _parse_concurrency("banana", 8) == 8  # garbage → cap, not unbounded


def test_load_config_reads_subagent_request_limit(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_SUBAGENT_REQUEST_LIMIT", "120")
    assert load_config().subagent.request_limit == 120


def test_subagent_request_limit_defaults_to_50(monkeypatch):
    monkeypatch.delenv("MARIM_SUBAGENT_REQUEST_LIMIT", raising=False)
    assert load_config().subagent.request_limit == 50
    # A non-positive value is rejected (per _int_env) and falls back to the default.
    monkeypatch.setenv("MARIM_SUBAGENT_REQUEST_LIMIT", "0")
    assert load_config().subagent.request_limit == 50


def test_mask_observations_defaults_on(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_MASK_OBSERVATIONS", raising=False)
    assert load_config().mask_observations is True


def test_mask_observations_opt_out(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_MASK_OBSERVATIONS", "0")
    assert load_config().mask_observations is False


def test_mask_thresholds_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_MASK_KEEP_RECENT", raising=False)
    monkeypatch.delenv("MARIM_MASK_MIN_CHARS", raising=False)
    cfg = load_config()
    assert cfg.mask_keep_recent == 4
    assert cfg.mask_min_chars == 200


def test_mask_thresholds_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_MASK_KEEP_RECENT", "2")
    monkeypatch.setenv("MARIM_MASK_MIN_CHARS", "500")
    cfg = load_config()
    assert cfg.mask_keep_recent == 2
    assert cfg.mask_min_chars == 500
    # Non-positive values are rejected (per _int_env) and fall back to defaults.
    monkeypatch.setenv("MARIM_MASK_KEEP_RECENT", "0")
    assert load_config().mask_keep_recent == 4


def test_detach_fanout_defaults_on(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_DETACH_FANOUT", raising=False)
    assert load_config().subagent.detach_fanout is True


def test_detach_fanout_opt_out(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_DETACH_FANOUT", "0")
    assert load_config().subagent.detach_fanout is False


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
    fake.assert_awaited_once_with("http://localhost:1234/v1", "lmstudio", strict=False)


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
    assert load_config().subagent.autonomous_wake is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no"])
def test_autonomous_wake_falsy_disables(monkeypatch, raw):
    monkeypatch.setenv("MARIM_AUTONOMOUS_WAKE", raw)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().subagent.autonomous_wake is False


def test_wake_depth_cap_defaults_to_eight(monkeypatch):
    monkeypatch.delenv("MARIM_WAKE_DEPTH_CAP", raising=False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().subagent.wake_depth_cap == 8


def test_wake_depth_cap_reads_env(monkeypatch):
    monkeypatch.setenv("MARIM_WAKE_DEPTH_CAP", "5")
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().subagent.wake_depth_cap == 5


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


# MARIM_MAX_CONTEXT_TOKENS is deliberately NOT in these parametrize lists: it is
# the deprecated alias for MARIM_CONTEXT_BUDGET, where 0 is meaningful
# ("unbudgeted"), so the sanitizer must leave it alone.
@pytest.mark.parametrize("key", ["MARIM_SUBAGENT_TRANSCRIPT_CAP", "MARIM_WAKE_DEPTH_CAP"])
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


@pytest.mark.parametrize("key", ["MARIM_SUBAGENT_TRANSCRIPT_CAP", "MARIM_WAKE_DEPTH_CAP"])
def test_load_environment_keeps_valid_positive_int(isolated_env, monkeypatch, tmp_path, key):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(key, "42")
    load_environment()
    assert os.environ[key] == "42"


def test_load_environment_invalid_int_falls_back_to_default(isolated_env, monkeypatch, tmp_path):
    """End-to-end: a garbage MARIM_MAX_CONTEXT_TOKENS yields the built-in default
    in the resolved config (not the garbage value). Garbage is now handled by
    _context_budget_env's direct parse (the var is no longer sanitizer-stripped,
    since 0 is meaningful for it)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "not-an-int")
    load_environment()
    assert load_config().max_context_tokens == 100_000


def test_load_environment_deprecated_zero_budget_survives(isolated_env, monkeypatch, tmp_path):
    """End-to-end: MARIM_MAX_CONTEXT_TOKENS=0 must survive load_environment's
    positive-int sanitizer (0 means "unbudgeted" for the budget alias, not an
    invalid value) and resolve to max_context_tokens == 0."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "0")
    load_environment()
    assert load_config().max_context_tokens == 0


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
    from marim_harness.config import model as _m
    from marim_harness.config.model import detect_active_providers
    for k in ("MARIM_PROVIDER", "OPENROUTER_API_KEY", "GOOGLE_API_KEY",
              "GEMINI_API_KEY", "MARIM_BASE_URL", "MARIM_API_KEY", "MARIM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
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


def test_multi_source_is_local_always_true():
    from marim_harness.config.model import ModelConfig, ModelSource, MultiModelSource
    orc = ModelSource(ModelConfig(provider="openrouter", model="x"))
    assert MultiModelSource({"openrouter": orc}, "openrouter").is_local is True


def test_parse_qualified_empty_remainder():
    from marim_harness.config.model import parse_qualified
    assert parse_qualified("local:", {"local", "openrouter"}, "openrouter") == ("local", "")


def test_multi_source_label_qualifies():
    from marim_harness.config.model import ModelConfig, ModelSource, MultiModelSource
    orc = ModelSource(ModelConfig(provider="openrouter", model="x"))
    multi = MultiModelSource({"openrouter": orc}, "openrouter")
    assert multi.label("openrouter:anthropic/c") == "openrouter:anthropic/c"
    assert multi.label("anthropic/c") == "openrouter:anthropic/c"  # bare gains default prefix


def test_default_mode_defaults_to_ask(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_DEFAULT_MODE", raising=False)
    assert load_config().default_mode == "ask"


def test_default_mode_reads_valid_env_case_insensitive(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_DEFAULT_MODE", "Auto")
    assert load_config().default_mode == "auto"


def test_default_mode_invalid_falls_back_to_ask(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_DEFAULT_MODE", "yolo")
    assert load_config().default_mode == "ask"


def test_load_environment_project_env_cannot_set_default_mode(
    isolated_env, monkeypatch, tmp_path
):
    # A cloned/untrusted project must NOT weaken the approval posture by shipping
    # MARIM_DEFAULT_MODE=auto in its .env — that comes only from the shell env or
    # the trusted global config.
    cfg_home = tmp_path / "xdg"
    (cfg_home / "marim").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text("MARIM_DEFAULT_MODE=auto\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("MARIM_DEFAULT_MODE", raising=False)

    load_environment()

    assert "MARIM_DEFAULT_MODE" not in os.environ
    assert load_config().default_mode == "ask"


def test_tool_search_defaults_to_auto(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_TOOL_SEARCH", raising=False)
    cfg = load_config()
    assert cfg.tool_search == "auto"
    assert cfg.tool_search_threshold == 15


def test_tool_search_reads_valid_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_TOOL_SEARCH", "On")
    monkeypatch.setenv("MARIM_TOOL_SEARCH_THRESHOLD", "30")
    cfg = load_config()
    assert cfg.tool_search == "on"
    assert cfg.tool_search_threshold == 30


def test_tool_search_invalid_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_TOOL_SEARCH", "sometimes")
    assert load_config().tool_search == "auto"


def test_tool_search_threshold_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_TOOL_SEARCH_THRESHOLD", "-3")
    # _POSITIVE_INT_KEYS sanitization only runs in load_environment(); _int_env
    # itself returns the default for a non-positive/garbage value at read time.
    assert load_config().tool_search_threshold == 15


# ---------------------------------------------------------------------------
# claude-cli provider
# ---------------------------------------------------------------------------

from marim_harness.config import model as model_mod  # noqa: E402


def test_claude_cli_is_a_known_provider():
    assert "claude-cli" in model_mod.KNOWN_PROVIDERS


def test_provider_config_claude_cli(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "claude-cli")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    cfg = model_mod.load_config()
    assert cfg.provider == "claude-cli"
    assert cfg.model is None or isinstance(cfg.model, str)
    assert cfg.api_key is None
    assert cfg.base_url is None


def test_provider_config_claude_cli_model_override(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "claude-cli")
    monkeypatch.setenv("MARIM_MODEL", "opus")
    assert model_mod.load_config().model == "opus"


def test_context_budget_env_resolution(monkeypatch, caplog):
    monkeypatch.setenv("MARIM_CONTEXT_BUDGET", "60000")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "111111")  # ignored when new var set
    with caplog.at_level("WARNING"):
        cfg = load_config()
    assert cfg.max_context_tokens == 60000
    # The new var wins silently: no deprecation nag while it is set.
    assert not any("deprecated" in r.message for r in caplog.records)


def test_deprecated_max_context_tokens_still_honored(monkeypatch, caplog):
    monkeypatch.delenv("MARIM_CONTEXT_BUDGET", raising=False)
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "70000")
    # Reset the once-only guard so this test is order-independent (an earlier
    # test may already have tripped the warning in this process).
    monkeypatch.setattr(model_mod, "_budget_deprecation_warned", False, raising=False)
    with caplog.at_level("WARNING"):
        cfg = load_config()
        load_config()  # a second load must NOT re-warn — the nag is one-time
    assert cfg.max_context_tokens == 70000
    deprecations = [
        r for r in caplog.records
        if "MARIM_MAX_CONTEXT_TOKENS" in r.message and "deprecated" in r.message
    ]
    assert len(deprecations) == 1


def test_context_budget_zero_means_unbudgeted(monkeypatch):
    monkeypatch.setenv("MARIM_CONTEXT_BUDGET", "0")
    cfg = load_config()
    assert cfg.max_context_tokens == 0


@pytest.mark.parametrize("var", ["MARIM_CONTEXT_BUDGET", "MARIM_MAX_CONTEXT_TOKENS"])
def test_context_budget_negative_is_garbage_not_uncapped(monkeypatch, var):
    """A typo'd negative budget must fail CLOSED (the 100k default), not open:
    treating it as 0 would mean unbudgeted — the failure direction is MORE
    spend. Only an explicit 0 uncaps."""
    for name in ("MARIM_CONTEXT_BUDGET", "MARIM_MAX_CONTEXT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(var, "-5")
    assert load_config().max_context_tokens == 100_000
    monkeypatch.setenv(var, "0")
    assert load_config().max_context_tokens == 0


def test_context_window_and_budgets_env(monkeypatch):
    monkeypatch.setenv("MARIM_CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("MARIM_CONTEXT_BUDGETS", "anthropic/claude-opus*=60000")
    cfg = load_config()
    assert cfg.context_window == 32768
    assert cfg.context_budgets == "anthropic/claude-opus*=60000"


def test_context_defaults(monkeypatch):
    for var in (
        "MARIM_CONTEXT_BUDGET",
        "MARIM_MAX_CONTEXT_TOKENS",
        "MARIM_CONTEXT_WINDOW",
        "MARIM_CONTEXT_BUDGETS",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.max_context_tokens == 100_000
    assert cfg.context_window is None
    assert cfg.context_budgets == ""


def test_has_creds_follows_binary(monkeypatch):
    monkeypatch.setattr(model_mod, "_claude_cli_available", lambda: True)
    assert model_mod._provider_has_creds("claude-cli") is True
    monkeypatch.setattr(model_mod, "_claude_cli_available", lambda: False)
    assert model_mod._provider_has_creds("claude-cli") is False


def test_build_model_claude_cli(monkeypatch):
    from dataclasses import replace

    cfg = replace(model_mod.load_config(), provider="claude-cli", model="sonnet")
    m = model_mod.build_model(cfg)
    from marim_harness.config.claude_cli_model import ClaudeCliModel

    assert isinstance(m, ClaudeCliModel)
    assert m.model_name == "sonnet"


def test_scratchpad_env_defaults_on(monkeypatch):
    monkeypatch.delenv("MARIM_SCRATCHPAD", raising=False)
    from marim_harness.config import load_config

    assert load_config().scratchpad_enabled is True


def test_scratchpad_env_off(monkeypatch):
    monkeypatch.setenv("MARIM_SCRATCHPAD", "0")
    from marim_harness.config import load_config

    assert load_config().scratchpad_enabled is False


def _clear_provider_env(monkeypatch):
    for k in ("MARIM_PROVIDER", "OPENROUTER_API_KEY", "GOOGLE_API_KEY",
              "GEMINI_API_KEY", "MARIM_BASE_URL", "MARIM_API_KEY", "MARIM_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_multi_source_refresh_picks_up_and_drops_providers(monkeypatch):
    """refresh_from_env mutates sources IN PLACE: a provider appears once its
    creds land in the env, and drops out once they're removed — while the
    default provider is always kept (startup must have a home)."""
    from marim_harness.config import model as _m
    from marim_harness.config.model import MultiModelSource

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    multi = MultiModelSource.from_env()
    held_sources = multi.sources  # the dict object closures/tests may hold
    assert set(multi.sources) == {"openrouter"}

    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    multi.refresh_from_env()
    assert set(multi.sources) == {"openrouter", "google"}
    assert multi.sources is held_sources  # same dict object — mutated, not replaced

    monkeypatch.delenv("GOOGLE_API_KEY")
    multi.refresh_from_env()
    assert set(multi.sources) == {"openrouter"}  # dropped; default kept


def test_multi_source_refresh_switches_default(monkeypatch):
    from marim_harness.config import model as _m
    from marim_harness.config.model import MultiModelSource

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    multi = MultiModelSource.from_env()
    assert multi.default == "openrouter"

    monkeypatch.setenv("MARIM_PROVIDER", "google")
    multi.refresh_from_env()
    assert multi.default == "google"
    assert "google" in multi.sources


def test_subagent_tiers_parsed_from_env(monkeypatch):
    from marim_harness.config.model import load_config

    monkeypatch.setenv("MARIM_SUBAGENT_TIER_CHEAP", "local:ornith-1.0-9b")
    monkeypatch.setenv("MARIM_SUBAGENT_TIER_HIGH", "openrouter:anthropic/claude-opus-4")
    cfg = load_config()
    assert cfg.subagent.tiers.cheap == "local:ornith-1.0-9b"
    assert cfg.subagent.tiers.med is None
    assert cfg.subagent.tiers.high == "openrouter:anthropic/claude-opus-4"


def test_subagent_tiers_default_empty(monkeypatch):
    from marim_harness.config.model import load_config

    monkeypatch.delenv("MARIM_SUBAGENT_TIER_CHEAP", raising=False)
    monkeypatch.delenv("MARIM_SUBAGENT_TIER_MED", raising=False)
    monkeypatch.delenv("MARIM_SUBAGENT_TIER_HIGH", raising=False)
    cfg = load_config()
    assert cfg.subagent.tiers.allowlist() == frozenset()


def test_subagent_tiering_enabled_by_default(monkeypatch):
    from marim_harness.config.model import load_config

    monkeypatch.delenv("MARIM_SUBAGENT_TIERING", raising=False)
    cfg = load_config()
    assert cfg.subagent.tiers.enabled is True


def test_subagent_tiering_disabled_via_env_keeps_slugs(monkeypatch):
    # MARIM_SUBAGENT_TIERING=false flips the routing off but leaves the curated
    # per-tier slugs intact, so re-enabling doesn't require re-entering them.
    from marim_harness.config.model import load_config

    monkeypatch.setenv("MARIM_SUBAGENT_TIER_CHEAP", "local:ornith-1.0-9b")
    monkeypatch.setenv("MARIM_SUBAGENT_TIERING", "false")
    cfg = load_config()
    assert cfg.subagent.tiers.enabled is False
    assert cfg.subagent.tiers.cheap == "local:ornith-1.0-9b"
    assert cfg.subagent.tiers.model_for("cheap") is None  # bypassed while disabled
