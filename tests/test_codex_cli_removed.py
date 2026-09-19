"""Removed execution paths fail explicitly; saved history and native Codex survive."""

from importlib import import_module
from unittest.mock import Mock

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.usage import RunUsage

from marim_harness.config.model import (
    KNOWN_PROVIDERS,
    ModelConfig,
    ModelSource,
    MultiModelSource,
    build_model,
    detect_active_providers,
    load_config,
)
from marim_harness.interfaces.tui.providers import PROVIDER_SPECS
from marim_harness.session import SessionManager
from marim_harness.workspace.agents import AgentDef
from tests.conftest import _make_deps, _make_harness, _text_model


def test_removed_provider_is_not_selectable():
    assert "codex-cli" not in KNOWN_PROVIDERS
    assert "codex-cli" not in {s.name for s in PROVIDER_SPECS}
    assert "openai-codex" in KNOWN_PROVIDERS
    assert "openai-codex" in {s.name for s in PROVIDER_SPECS}


@pytest.mark.parametrize("load", [load_config, detect_active_providers])
def test_legacy_environment_fails_without_paid_fallback(monkeypatch, load):
    monkeypatch.setenv("MARIM_PROVIDER", "codex-cli")
    monkeypatch.setenv("OPENROUTER_API_KEY", "should-not-be-used")
    with pytest.raises(ValueError, match="codex-cli has been removed.*openai-codex"):
        load()


@pytest.mark.parametrize(
    "provider,model", [("codex-cli", "gpt-5.6-terra"), ("openrouter", "codex-cli:gpt-5.6-terra")]
)
def test_direct_model_build_rejects_legacy_selection(provider, model):
    with pytest.raises(ValueError, match="codex-cli has been removed"):
        build_model(ModelConfig(provider=provider, model=model))


def test_saved_selection_never_routes_to_default_provider():
    paid = ModelSource(ModelConfig(provider="openrouter", model="paid"))
    paid.build = Mock(side_effect=AssertionError("paid fallback"))
    sources = MultiModelSource({"openrouter": paid}, "openrouter")
    assert sources.label("codex-cli:old") == "codex-cli:old"
    with pytest.raises(ValueError, match="openai-codex"):
        sources.build("codex-cli:old")
    paid.build.assert_not_called()


@pytest.mark.parametrize(
    "module",
    [
        "marim_harness.codex.server",
        "marim_harness.config.codex_cli_model",
        "marim_harness.subagents.codex_spawn",
    ],
)
def test_execution_modules_are_removed(module):
    with pytest.raises(ModuleNotFoundError):
        import_module(module)


def test_legacy_transcript_and_thread_reference_remain_readable(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.model = "codex-cli:gpt-5.6-terra"
    store.cli_thread_id = "codex-cli:old-thread"
    store.save([ModelRequest(parts=[UserPromptPart("prior work")])], RunUsage())
    restored = manager.store(store.session_id)
    messages, *_ = restored.load()
    assert restored.model == store.model
    assert restored.cli_thread_id == store.cli_thread_id
    assert messages[0].parts[0].content == "prior work"


@pytest.mark.anyio
async def test_legacy_subagent_cannot_execute_or_build_native_by_accident(tmp_path, monkeypatch):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    runner = harness.subagents
    role = AgentDef("old", "Old", "Work", frozenset(), "test", backend="codex-cli")
    monkeypatch.setattr(runner, "_resolve_agent", lambda _: role)
    agent, error = runner.build("old")
    assert agent is None and "codex-cli has been removed" in error
    result = await runner.run("old", "work", stream_id="old-spawn")
    assert "codex-cli has been removed" in result
    await harness.aclose()


@pytest.mark.anyio
async def test_legacy_spawn_resume_preserves_transcript_and_refuses_execution(
    tmp_path, monkeypatch
):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    runner = harness.subagents
    monkeypatch.setattr(runner, "_resume_preconditions", lambda _: ({"backend": "codex-cli"}, None))
    read = Mock(side_effect=AssertionError("legacy transcript was replayed"))
    monkeypatch.setattr(runner._transcripts, "read", read)
    job, message = await runner.resume_spawn("old-spawn")
    assert job is None and "codex-cli has been removed" in message
    read.assert_not_called()
    await harness.aclose()
