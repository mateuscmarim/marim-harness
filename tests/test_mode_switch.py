"""Harness mode wiring: live switch is always applied, persistence is opt-in
(default False, so the TUI's cycle/toggle stays a live, per-launch setting)."""

from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager
from marim_harness.tools.provider import BuiltinToolProvider


def _harness(tmp_path, **kwargs) -> Harness:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, "Be helpful.", **kwargs
    )


def test_set_mode_default_does_not_persist(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    h.set_mode(Mode.plan)
    assert h.mode is Mode.plan
    assert store.mode is None


def test_set_mode_persist_true_writes_store(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    h.set_mode(Mode.plan, persist=True)
    assert h.mode is Mode.plan
    assert store.mode == "plan"


def test_cycle_mode_default_does_not_persist(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    result = h.cycle_mode()
    assert result is h.mode
    assert store.mode is None


def test_cycle_mode_persist_true_writes_store(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    result = h.cycle_mode(persist=True)
    assert store.mode == result.value
