import sys

from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import HarnessServices, UIHooks
from marim_harness.runtime.harness import Harness
from tests.conftest import _make_deps, _make_harness


def test_services_default_run_workflow_none():
    assert HarnessServices().run_workflow is None


def test_ui_hooks_default_workflow_callbacks_none():
    ui = UIHooks()
    assert ui.on_workflow_spawn is None
    assert ui.on_workflow_log is None


def test_harness_wires_run_workflow_when_monty_available(tmp_path):
    """The dev environment has pydantic-monty installed (the [workflows]
    extra), so the default-enabled engine should build and services.run_workflow
    should be populated."""
    h: Harness = _make_harness(TestModel(), _make_deps(tmp_path))
    assert h.deps.services.run_workflow is not None


def test_harness_respects_workflows_disabled(tmp_path):
    h: Harness = _make_harness(TestModel(), _make_deps(tmp_path), workflows_enabled=False)
    assert h.deps.services.run_workflow is None


def test_harness_degrades_when_pydantic_monty_unavailable(tmp_path, monkeypatch):
    """When pydantic-monty is unavailable, workflow engine fails to import
    but harness builds successfully with run_workflow set to None."""
    # Block the pydantic_monty import
    monkeypatch.setitem(sys.modules, "pydantic_monty", None)
    # Remove the engine module if already imported so it re-executes the try/except
    monkeypatch.delitem(sys.modules, "marim_harness.workflows.engine", raising=False)

    # Build harness with workflows enabled (config-wise) but package missing
    h: Harness = _make_harness(TestModel(), _make_deps(tmp_path), workflows_enabled=True)

    # Should gracefully degrade: service is None even though enabled
    assert h.deps.services.run_workflow is None
