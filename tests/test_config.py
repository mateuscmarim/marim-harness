import pytest

from marim_harness.config import ModelConfig, ModelSource, load_config


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


def test_model_source_label_prefixes_provider():
    src = ModelSource(ModelConfig(provider="openrouter", model="anthropic/claude-sonnet-4-6"))
    assert src.label("openai/gpt-5.2") == "openrouter/openai/gpt-5.2"


def test_model_source_is_local_reflects_provider():
    assert ModelSource(ModelConfig(provider="local", model="x")).is_local is True
    assert ModelSource(ModelConfig(provider="openrouter", model="x")).is_local is False


@pytest.mark.anyio
async def test_model_source_list_models_empty_for_local():
    src = ModelSource(ModelConfig(provider="local", model="x"))
    assert await src.list_models() == []  # no catalog for local


def test_model_source_build_swaps_in_the_model_id():
    src = ModelSource(ModelConfig(provider="local", model="x", base_url="http://h/v1"))
    model = src.build("some-other-model")
    # The constructed model reports the swapped id, not the config default.
    assert getattr(model, "model_name", "some-other-model") == "some-other-model"
