"""Builder + bootstrap thinking wiring: the builder front door and the
bootstrap env pass-through both land on Harness.thinking_level_id."""

from pydantic_ai.models.test import TestModel


def test_builder_with_thinking(tmp_path):
    from marim_harness.runtime.builder import HarnessBuilder

    h = (
        HarnessBuilder(workspace=tmp_path, model=TestModel(call_tools=[]))
        .with_thinking("high")
        .build()
    )
    assert h.thinking_level_id == "high"


def test_bootstrap_passes_thinking_env(monkeypatch, tmp_path):
    from marim_harness.runtime.bootstrap import build_harness

    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_THINKING", "medium")
    harness = build_harness(tmp_path)
    assert harness.thinking_level_id == "medium"
