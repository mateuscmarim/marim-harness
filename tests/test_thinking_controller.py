"""Harness thinking wiring + main-loop application: the config default seeds
the live level, the session level overrides it, the live setter switches and
persists, and TurnController folds the level into per-run model_settings."""

import pytest
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.harness import _DEFAULT_MODEL_SETTINGS, Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager
from marim_harness.tools.provider import BuiltinToolProvider


def _harness(tmp_path, **kwargs) -> Harness:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, "Be helpful.", **kwargs
    )


def test_config_default_seeds_the_live_level(tmp_path):
    h = _harness(tmp_path, thinking_level="high")
    assert h.thinking_level_id == "high"
    assert h.get_thinking() == "high"


def test_unconfigured_leaves_the_level_none(tmp_path):
    h = _harness(tmp_path)
    assert h.thinking_level_id is None


def test_session_level_overrides_config_default(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.thinking = "low"
    h = _harness(tmp_path, store=store, manager=manager, thinking_level="high")
    assert h.thinking_level_id == "low"


def test_session_off_overrides_config_default(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.thinking = "off"
    h = _harness(tmp_path, store=store, manager=manager, thinking_level="high")
    assert h.thinking_level_id == "off"


def test_set_thinking_level_switches_and_persists(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    h.set_thinking_level("medium")
    assert h.thinking_level_id == "medium"
    assert store.thinking == "medium"


def test_controller_folds_level_into_run_settings(tmp_path):
    h = _harness(tmp_path, thinking_level="high")
    settings = h.turn_controller._turn_model_settings()
    assert settings["thinking"] == "high"
    assert settings["parallel_tool_calls"] is True


def test_controller_off_leaves_settings_identical_to_default(tmp_path):
    h = _harness(tmp_path)  # unset → None
    assert h.turn_controller._turn_model_settings() == _DEFAULT_MODEL_SETTINGS
    h.set_thinking_level("off")
    assert h.turn_controller._turn_model_settings() == _DEFAULT_MODEL_SETTINGS
    assert "thinking" not in h.turn_controller._turn_model_settings()


@pytest.mark.anyio
async def test_run_applies_settings_and_does_not_mutate_default(tmp_path):
    h = _harness(tmp_path, thinking_level="high")
    await h.run_turn("hi")
    # _DEFAULT_MODEL_SETTINGS is the shared agent-level default; settings_for
    # must copy, never mutate it.
    assert "thinking" not in _DEFAULT_MODEL_SETTINGS
